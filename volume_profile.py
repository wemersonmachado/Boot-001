"""
Volume Profile — Point of Control (POC), Value Area High/Low.

Calcula histograma de volume por nível de preço usando klines já disponíveis.
Zero APIs extras — usa dados do klines_cache.

POC  — nível de preço com maior volume negociado (ímã de preço)
VAH  — Value Area High: 70% do volume está abaixo deste nível
VAL  — Value Area Low:  70% do volume está acima deste nível
"""
import numpy as np
import pandas as pd
from typing import Optional


def compute(df: pd.DataFrame, bins: int = 50) -> dict:
    """
    Calcula Volume Profile a partir de um DataFrame OHLCV.

    Args:
        df  : DataFrame com colunas high, low, close, volume
        bins: número de níveis de preço (resolução do histograma)

    Returns:
        {poc, vah, val, profile: [{price, volume}], total_volume}
    """
    if df is None or len(df) < 10:
        return _empty()

    try:
        highs   = df["high"].values.astype(float)
        lows    = df["low"].values.astype(float)
        volumes = df["volume"].values.astype(float)

        price_min = float(np.min(lows))
        price_max = float(np.max(highs))
        if price_max <= price_min:
            return _empty()

        edges    = np.linspace(price_min, price_max, bins + 1)
        midpoints = (edges[:-1] + edges[1:]) / 2
        hist     = np.zeros(bins)

        for h, l, v in zip(highs, lows, volumes):
            candle_range = h - l
            if candle_range <= 0:
                idx = np.searchsorted(edges, (h + l) / 2, side="right") - 1
                idx = max(0, min(bins - 1, idx))
                hist[idx] += v
            else:
                lo_idx = max(0, np.searchsorted(edges, l, side="left") - 1)
                hi_idx = min(bins - 1, np.searchsorted(edges, h, side="right") - 1)
                if hi_idx <= lo_idx:
                    hist[lo_idx] += v
                else:
                    n = hi_idx - lo_idx + 1
                    per_bin = v / n
                    hist[lo_idx:hi_idx + 1] += per_bin

        total_vol = float(hist.sum())
        if total_vol == 0:
            return _empty()

        poc_idx = int(np.argmax(hist))
        poc     = float(midpoints[poc_idx])

        # Value Area: 70% do volume total
        target   = total_vol * 0.70
        vah_idx  = poc_idx
        val_idx  = poc_idx
        accum    = hist[poc_idx]

        while accum < target:
            up_vol   = hist[vah_idx + 1] if vah_idx + 1 < bins else 0
            down_vol = hist[val_idx - 1] if val_idx - 1 >= 0  else 0
            if up_vol >= down_vol and vah_idx + 1 < bins:
                vah_idx += 1
                accum   += hist[vah_idx]
            elif val_idx - 1 >= 0:
                val_idx -= 1
                accum   += hist[val_idx]
            else:
                break

        vah = float(midpoints[vah_idx])
        val = float(midpoints[val_idx])

        profile = [
            {"price": round(float(midpoints[i]), 6), "volume": round(float(hist[i]), 2)}
            for i in range(bins)
        ]

        return {
            "poc":          round(poc, 6),
            "vah":          round(vah, 6),
            "val":          round(val, 6),
            "total_volume": round(total_vol, 2),
            "profile":      profile,
            "bins":         bins,
            "price_min":    round(price_min, 6),
            "price_max":    round(price_max, 6),
        }

    except Exception as e:
        print(f"[VOL_PROFILE] Erro: {e}")
        return _empty()


def score_confluence(vp: dict, price: float, direction: str, atr: float) -> float:
    """
    Retorna bonus/penalidade de score baseado na posição do preço no Volume Profile.

    LONG:
      Preço próximo ao VAL (suporte de volume) → +8pts  (comprar no chão do VA)
      Preço acima do VAH (breakout do VA)      → +5pts  (momentum confirmado)
      Preço entre VAL e VAH (meio do range)    → -3pts  (sem edge claro)

    SHORT:
      Preço próximo ao VAH (resistência)       → +8pts
      Preço abaixo do VAL (breakdown)          → +5pts
      Preço entre VAL e VAH                    → -3pts

    POC magnet: se preço está entre 0.5× ATR do POC → neutro (alvo, não entrada)
    """
    poc = vp.get("poc", 0)
    vah = vp.get("vah", 0)
    val = vp.get("val", 0)

    if not poc or not price or not atr:
        return 0.0

    tol = atr * 0.5   # tolerância = metade do ATR

    # Preço muito próximo do POC → neutro, o preço tende a rejeitar ou passar
    if abs(price - poc) < tol:
        return 0.0

    if direction == "LONG":
        if abs(price - val) < tol:    return +8.0   # suporte de volume
        if price > vah:               return +5.0   # breakout acima do VA
        if val < price < vah:         return -3.0   # meio do range
    else:  # SHORT
        if abs(price - vah) < tol:   return +8.0   # resistência de volume
        if price < val:               return +5.0   # breakdown abaixo do VA
        if val < price < vah:         return -3.0

    return 0.0


def nearest_levels(vp: dict, price: float) -> dict:
    """Retorna os níveis de Volume Profile mais próximos acima e abaixo do preço."""
    poc = vp.get("poc", price)
    vah = vp.get("vah", price)
    val = vp.get("val", price)

    levels_above = [l for l in [poc, vah, val] if l > price]
    levels_below = [l for l in [poc, vah, val] if l < price]

    return {
        "nearest_above": min(levels_above) if levels_above else None,
        "nearest_below": max(levels_below) if levels_below else None,
        "poc":           poc,
        "vah":           vah,
        "val":           val,
    }


def _empty() -> dict:
    return {"poc": 0.0, "vah": 0.0, "val": 0.0, "total_volume": 0.0,
            "profile": [], "bins": 0, "price_min": 0.0, "price_max": 0.0}
