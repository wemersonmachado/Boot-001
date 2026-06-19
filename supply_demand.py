"""
Supply & Demand Zone Engine
Builds major S/R zones from swing highs/lows + volume clusters.
Results are cached per symbol (5-min TTL).
"""
import time
import numpy as np
import pandas as pd
from typing import Optional

from klines_cache import get_klines_cached as get_klines

_cache: dict = {}   # symbol → {ts, zones}
_TTL = 300          # 5 minutes


# ── Zone detection ────────────────────────────────────────────────────────────

def _detect_zones(df: pd.DataFrame) -> dict:
    """
    Returns supply/demand zones from 1h candles.
    Each zone has: level, type (SUPPLY/DEMAND), strength (touches), vol_weight.
    """
    if df is None or len(df) < 50:
        return {"support_zones": [], "resistance_zones": [], "structure": "RANGING"}

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]
    price  = close.iloc[-1]

    # ── Swing highs / lows ────────────────────────────────────────────────
    swing_highs: list[float] = []
    swing_lows:  list[float] = []
    for i in range(2, len(df) - 2):
        h = high.iloc[i]
        l = low.iloc[i]
        if h > high.iloc[i-1] and h > high.iloc[i-2] and h > high.iloc[i+1] and h > high.iloc[i+2]:
            swing_highs.append(h)
        if l < low.iloc[i-1] and l < low.iloc[i-2] and l < low.iloc[i+1] and l < low.iloc[i+2]:
            swing_lows.append(l)

    # ── Volume-weighted cluster detection ────────────────────────────────
    # Divide price range into buckets, weight by volume
    price_min = low.min()
    price_max = high.max()
    n_buckets = 40
    bucket_size = (price_max - price_min) / n_buckets if price_max > price_min else 1

    vol_profile: dict[int, float] = {}
    for i in range(len(df)):
        mid   = (high.iloc[i] + low.iloc[i]) / 2
        bucket = int((mid - price_min) / bucket_size)
        vol_profile[bucket] = vol_profile.get(bucket, 0) + volume.iloc[i]

    # Top 5 volume buckets → high-volume nodes (HVN)
    sorted_buckets = sorted(vol_profile.items(), key=lambda x: x[1], reverse=True)[:5]
    hvn_levels = [price_min + (b + 0.5) * bucket_size for b, _ in sorted_buckets]

    # ── Build zones with strength count ──────────────────────────────────
    tolerance = price * 0.008  # 0.8% tolerance to cluster nearby levels

    def cluster(levels: list[float]) -> list[dict]:
        if not levels:
            return []
        levels_sorted = sorted(set(levels))
        clusters: list[dict] = []
        current = [levels_sorted[0]]
        for lv in levels_sorted[1:]:
            if lv - current[-1] < tolerance:
                current.append(lv)
            else:
                clusters.append({"level": round(np.mean(current), 6), "touches": len(current)})
                current = [lv]
        clusters.append({"level": round(np.mean(current), 6), "touches": len(current)})
        return clusters

    resist_clusters = cluster(swing_highs)
    support_clusters = cluster(swing_lows)

    # Add HVN weight to nearest cluster
    for hvn in hvn_levels:
        min_r = min(resist_clusters, key=lambda x: abs(x["level"] - hvn), default=None)
        min_s = min(support_clusters, key=lambda x: abs(x["level"] - hvn), default=None)
        if min_r and abs(min_r["level"] - hvn) < tolerance * 3:
            min_r["vol_weight"] = min_r.get("vol_weight", 1) + 1
        elif min_s and abs(min_s["level"] - hvn) < tolerance * 3:
            min_s["vol_weight"] = min_s.get("vol_weight", 1) + 1

    # Fill missing vol_weight
    for z in resist_clusters + support_clusters:
        z.setdefault("vol_weight", 1)
        z["strength"] = z["touches"] + z["vol_weight"]

    # ── Structure ─────────────────────────────────────────────────────────
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1] > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1] < swing_lows[-2]
        structure = "UPTREND" if (hh and hl) else ("DOWNTREND" if (lh and ll) else "RANGING")
    else:
        structure = "RANGING"

    # ── Nearest zones to current price ────────────────────────────────────
    above = [z for z in resist_clusters if z["level"] > price]
    below = [z for z in support_clusters if z["level"] < price]

    nearest_resist = min(above, key=lambda x: x["level"] - price) if above else None
    nearest_support = max(below, key=lambda x: price - x["level"]) if below else None

    # ── Breakout probability ───────────────────────────────────────────────
    # Higher if price is within 1% of resistance and volume is elevated
    breakout_prob = 0
    if nearest_resist:
        dist_pct = (nearest_resist["level"] - price) / price * 100
        avg_vol  = volume.rolling(20).mean().iloc[-1]
        cur_vol  = volume.iloc[-1]
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
        if dist_pct < 1.0:
            breakout_prob = min(90, int(40 + vol_ratio * 15 + (1 - dist_pct) * 20))
        elif dist_pct < 2.5:
            breakout_prob = min(60, int(20 + vol_ratio * 10))

    return {
        "structure":        structure,
        "resistance_zones": sorted(resist_clusters, key=lambda x: -x["strength"])[:5],
        "support_zones":    sorted(support_clusters, key=lambda x: -x["strength"])[:5],
        "nearest_resistance": nearest_resist,
        "nearest_support":    nearest_support,
        "breakout_prob":      breakout_prob,
        "current_price":      round(price, 6),
    }


# ── Public API ────────────────────────────────────────────────────────────────

async def get_zones(symbol: str, timeframe: str = "1h") -> dict:
    now = time.time()
    cached = _cache.get(symbol)
    if cached and (now - cached["ts"]) < _TTL:
        return cached["zones"]

    try:
        df = await get_klines(symbol, timeframe, limit=200)
        zones = _detect_zones(df)
    except Exception:
        zones = {"support_zones": [], "resistance_zones": [], "structure": "RANGING",
                 "nearest_resistance": None, "nearest_support": None,
                 "breakout_prob": 0, "current_price": 0}

    _cache[symbol] = {"ts": now, "zones": zones}
    return zones


# ── Smart Money Concepts: Liquidity Sweeps ────────────────────────────────────

def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 30) -> dict:
    """
    SMC Liquidity Sweep Detector.
    Checks if the last candle wicks swept a local swing level and rejected.
    """
    if df is None or len(df) < lookback + 5:
        return {"sweep": False, "type": "", "level": 0.0}

    try:
        highs = df["high"].values
        lows  = df["low"].values
        closes = df["close"].values
        volumes = df["volume"].values

        # Encontra extremos das últimas lookback velas (excluindo os últimos 3 candles)
        ref_highs = highs[-lookback:-3]
        ref_lows  = lows[-lookback:-3]
        swing_high = float(np.max(ref_highs))
        swing_low  = float(np.min(ref_lows))

        # Analisa os últimos 2 candles para ver se houve varredura
        for idx in [-1, -2]:
            cur_low = float(lows[idx])
            cur_high = float(highs[idx])
            cur_close = float(closes[idx])
            
            # Spike de volume na vela de varredura
            vol_avg = float(np.mean(volumes[-20:]))
            vol_ratio = volumes[idx] / vol_avg if vol_avg > 0 else 1.0

            # Bullish Sweep: vela fura a mínima (swing_low), mas fecha acima dela
            if cur_low < swing_low and cur_close > swing_low and vol_ratio >= 1.2:
                return {"sweep": True, "type": "BULLISH_SWEEP", "level": swing_low}

            # Bearish Sweep: vela fura a máxima (swing_high), mas fecha abaixo dela
            if cur_high > swing_high and cur_close < swing_high and vol_ratio >= 1.2:
                return {"sweep": True, "type": "BEARISH_SWEEP", "level": swing_high}

    except Exception:
        pass

    return {"sweep": False, "type": "", "level": 0.0}
