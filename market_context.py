"""
Market Context — Fase 1: novas fontes de dados (Volume Profile, regime de
volatilidade, dominância BTC, sessão de mercado).

Tudo calculado localmente a partir de klines já buscados (sem API extra),
exceto BTC dominance que usa CoinGecko (cache 10min, sem chave).
"""
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from data_fetcher import get_session

_BTC_DOM_CACHE: dict = {"value": None, "ts": 0.0}
_BTC_DOM_TTL_S = 600  # 10min


# ── Volume Profile (POC / VAH / VAL) ──────────────────────────────────────────
def volume_profile(df: pd.DataFrame, n_bins: int = 24) -> dict:
    """Calcula o perfil de volume sobre o range de preço do df.
    POC = preço com mais volume negociado. VAH/VAL = limites da Value Area (70% do volume)."""
    if df is None or len(df) < 10:
        return {"poc": None, "vah": None, "val": None}

    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        return {"poc": None, "vah": None, "val": None}

    bins = np.linspace(lo, hi, n_bins + 1)
    vol_per_bin = np.zeros(n_bins)
    for _, row in df.iterrows():
        # distribui o volume da vela uniformemente entre os bins que ela cobre
        b_lo = np.searchsorted(bins, row["low"], side="right") - 1
        b_hi = np.searchsorted(bins, row["high"], side="right") - 1
        b_lo, b_hi = max(0, min(b_lo, n_bins - 1)), max(0, min(b_hi, n_bins - 1))
        span = b_hi - b_lo + 1
        vol_per_bin[b_lo:b_hi + 1] += row["volume"] / span

    poc_idx = int(np.argmax(vol_per_bin))
    poc = (bins[poc_idx] + bins[poc_idx + 1]) / 2

    total_vol = vol_per_bin.sum()
    target = total_vol * 0.70
    order = np.argsort(vol_per_bin)[::-1]
    acc = 0.0
    included = set()
    for idx in order:
        acc += vol_per_bin[idx]
        included.add(idx)
        if acc >= target:
            break
    val_idx, vah_idx = min(included), max(included)
    val = bins[val_idx]
    vah = bins[vah_idx + 1]

    return {"poc": round(float(poc), 6), "vah": round(float(vah), 6), "val": round(float(val), 6)}


def score_volume_profile(df: pd.DataFrame, direction, price: float) -> float:
    """Bônus/penalidade conforme posição do preço atual vs POC/VAH/VAL.
    LONG perto/abaixo do VAL (desconto, espaço até POC) = bônus.
    LONG colado no VAH/acima (caro, sem espaço) = penalidade. Espelha para SHORT."""
    vp = volume_profile(df)
    if vp["poc"] is None:
        return 0.0
    poc, vah, val = vp["poc"], vp["vah"], vp["val"]
    is_long = getattr(direction, "value", direction) in ("LONG", "long")

    if is_long:
        if price <= val:
            return 6.0   # comprando na zona de desconto
        if price >= vah:
            return -6.0  # comprando caro, sem espaço até POC
        return 2.0 if price < poc else 0.0
    else:
        if price >= vah:
            return 6.0
        if price <= val:
            return -6.0
        return 2.0 if price > poc else 0.0


# ── Regime de volatilidade ────────────────────────────────────────────────────
def volatility_regime(df: pd.DataFrame, atr_period: int = 14, lookback: int = 60) -> dict:
    """Classifica o regime atual de volatilidade comparando ATR% atual com a
    distribuição do ATR% nas últimas `lookback` velas."""
    if df is None or len(df) < atr_period + 5:
        return {"regime": "UNKNOWN", "atr_pct": None, "percentile": None}

    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr_series = tr.rolling(atr_period).mean()
    atr_pct_series = (atr_series / df["close"] * 100).dropna()

    if len(atr_pct_series) < 10:
        return {"regime": "UNKNOWN", "atr_pct": None, "percentile": None}

    window = atr_pct_series.iloc[-lookback:] if len(atr_pct_series) > lookback else atr_pct_series
    current = float(atr_pct_series.iloc[-1])
    percentile = float((window < current).mean() * 100)

    if percentile <= 20:
        regime = "BAIXA_VOL"      # candidato a expansão (squeeze)
    elif percentile >= 80:
        regime = "ALTA_VOL"       # risco elevado, mover stops/tamanho
    else:
        regime = "NORMAL"

    return {"regime": regime, "atr_pct": round(current, 3), "percentile": round(percentile, 1)}


def score_volatility_regime(df: pd.DataFrame) -> tuple[float, str]:
    """ALTA_VOL: penalidade leve (mais risco de stop). BAIXA_VOL: pequeno bônus
    (pré-expansão, squeeze) — mas só some o bônus, nunca substitui o squeeze_bonus existente."""
    vr = volatility_regime(df)
    if vr["regime"] == "ALTA_VOL":
        return -4.0, "ALTA-VOL"
    if vr["regime"] == "BAIXA_VOL":
        return 2.0, "BAIXA-VOL"
    return 0.0, ""


# ── Dominância BTC ────────────────────────────────────────────────────────────
async def get_btc_dominance() -> Optional[float]:
    now = time.time()
    if _BTC_DOM_CACHE["value"] is not None and (now - _BTC_DOM_CACHE["ts"]) < _BTC_DOM_TTL_S:
        return _BTC_DOM_CACHE["value"]
    try:
        session = await get_session()
        async with session.get("https://api.coingecko.com/api/v3/global", timeout=8) as resp:
            if resp.status != 200:
                return _BTC_DOM_CACHE["value"]
            data = await resp.json()
            dom = float(data["data"]["market_cap_percentage"]["btc"])
            _BTC_DOM_CACHE["value"] = dom
            _BTC_DOM_CACHE["ts"] = now
            return dom
    except Exception:
        return _BTC_DOM_CACHE["value"]


# ── Sessão de mercado ─────────────────────────────────────────────────────────
def market_session(ts: Optional[float] = None) -> str:
    """Sessão aproximada por horário UTC: Ásia 00-08h, Europa 07-16h, EUA 13-22h
    (overlaps contam como ambas — retorna a combinação mais específica)."""
    hour = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).hour
    asia = 0 <= hour < 8
    europe = 7 <= hour < 16
    us = 13 <= hour < 22
    if europe and us:
        return "EUROPA_EUA"
    if asia and europe:
        return "ASIA_EUROPA"
    if asia:
        return "ASIA"
    if europe:
        return "EUROPA"
    if us:
        return "EUA"
    return "OFF_HOURS"
