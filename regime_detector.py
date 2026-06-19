"""
Regime Detector — detecta se o mercado está em tendência ou lateralização.
Chaveamento automático de parâmetros para maximizar win rate em cada regime.

Regimes:
  TRENDING  — ADX > 25 + EMA alinhada: usa trend-following (EMA cross, momentum)
  RANGING   — ADX < 20: usa mean-reversion (RSI extremos, BB bands, VWAP)
  VOLATILE  — ATR% > 2× média histórica: reduz tamanho, aumenta SL

Score adjustments por regime:
  TRENDING: +8pts para sinais na direção da tendência, -8pts contra
  RANGING:  +8pts para reversões (RSI < 30 long, > 70 short), +4pts VWAP
  VOLATILE: score cap em 75 (evita overconfidence), SL multiplicado por 1.3
"""
import numpy as np
import pandas as pd
from typing import Literal

Regime = Literal["TRENDING", "RANGING", "VOLATILE", "NEUTRAL"]


# ── ADX ───────────────────────────────────────────────────────────────────────

def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    plus_dm  = high.diff()
    minus_dm = low.diff().abs()
    plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr_s    = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period, adjust=False).mean()  / atr_s.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx       = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.ewm(span=period, adjust=False).mean()


def _atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr_val = tr.ewm(span=period, adjust=False).mean().iloc[-1]
    price   = df["close"].iloc[-1]
    return float(atr_val / price * 100) if price > 0 else 0.0


def _ema_aligned(df: pd.DataFrame, direction: str) -> bool:
    """Retorna True se EMA 9 > 21 > 50 (LONG) ou inverso (SHORT)."""
    close = df["close"]
    e9  = close.ewm(span=9,  adjust=False).mean().iloc[-1]
    e21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    if direction == "LONG":
        return e9 > e21 > e50
    return e9 < e21 < e50


# ── Detecção principal ─────────────────────────────────────────────────────────

def detect(df: pd.DataFrame, direction: str = "LONG") -> dict:
    """
    Detecta regime de mercado.
    Retorna dict com regime, adx, atr_pct, ema_aligned, score_adj, sl_mult_adj.
    """
    if len(df) < 30:
        return _neutral()

    adx_series  = _adx(df)
    adx_val     = float(adx_series.iloc[-1]) if not np.isnan(adx_series.iloc[-1]) else 20.0
    atr_pct_val = _atr_pct(df)

    # Volatilidade histórica: compara ATR% atual com média de 50 candles
    atr_hist    = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr_ma50    = float(atr_hist.rolling(50).mean().iloc[-1]) if len(df) >= 50 else atr_pct_val
    price       = float(df["close"].iloc[-1])
    atr_ma50_pct = atr_ma50 / price * 100 if price > 0 else 1.0
    is_volatile  = atr_pct_val > atr_ma50_pct * 2.0

    ema_ok = _ema_aligned(df, direction)

    # Classifica regime
    if is_volatile:
        regime = "VOLATILE"
    elif adx_val >= 25 and ema_ok:
        regime = "TRENDING"
    elif adx_val < 20:
        regime = "RANGING"
    else:
        regime = "NEUTRAL"

    score_adj, sl_mult_adj, score_cap = _adjustments(regime, direction, df, adx_val)

    return {
        "regime":       regime,
        "adx":          round(adx_val, 1),
        "atr_pct":      round(atr_pct_val, 3),
        "ema_aligned":  ema_ok,
        "score_adj":    score_adj,
        "sl_mult_adj":  sl_mult_adj,
        "score_cap":    score_cap,   # None = sem cap
    }


def _adjustments(regime: str, direction: str, df: pd.DataFrame, adx: float) -> tuple:
    """Retorna (score_adj, sl_mult_adj, score_cap)."""
    close = df["close"]
    rsi_series = _rsi(close)
    rsi_val    = float(rsi_series.iloc[-1]) if len(rsi_series) > 0 else 50.0

    if regime == "TRENDING":
        # Reforça sinais na direção da tendência
        score_adj = +8.0
        sl_mult   = 1.0
        cap       = None

    elif regime == "RANGING":
        # Favorece reversões nos extremos
        if direction == "LONG" and rsi_val < 35:
            score_adj = +8.0
        elif direction == "SHORT" and rsi_val > 65:
            score_adj = +8.0
        elif direction == "LONG" and rsi_val > 55:
            score_adj = -6.0    # long em topo de range = ruim
        elif direction == "SHORT" and rsi_val < 45:
            score_adj = -6.0
        else:
            score_adj = +2.0
        sl_mult = 0.8   # SL menor em ranging (menos espaço)
        cap     = None

    elif regime == "VOLATILE":
        score_adj = 0.0
        sl_mult   = 1.3   # SL maior em mercado volátil
        cap       = 75.0  # limita confiança para evitar overtrading

    else:  # NEUTRAL
        score_adj = 0.0
        sl_mult   = 1.0
        cap       = None

    return score_adj, sl_mult, cap


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _neutral() -> dict:
    return {
        "regime": "NEUTRAL", "adx": 20.0, "atr_pct": 0.0,
        "ema_aligned": False, "score_adj": 0.0,
        "sl_mult_adj": 1.0, "score_cap": None,
    }


# ── Cache global de regime por ativo (lido pelos endpoints) ───────────────────
_regime_cache: dict[str, dict] = {}   # asset → último regime detectado


def update_cache(symbol: str, regime_data: dict):
    _regime_cache[symbol.upper()] = {**regime_data, "updated_at": __import__("time").time()}


def get_cache(symbol: str) -> dict:
    return _regime_cache.get(symbol.upper(), _neutral())


def get_all_regimes() -> dict:
    return {k: v for k, v in _regime_cache.items()}
