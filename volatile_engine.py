"""
Volatile Altcoin Engine — momentum-only strategy.

Regras V2 (Momentum Confluence):
  1. Volume OBRIGATÓRIO ≥ 4x média 20 períodos (5x no 3m)
  2. Breakout CONFIRMADO: close cruzou max/min das últimas 40 velas (60 no 3m)
  3. OBV/CVD slope: fluxo comprador líquido a favor nas últimas 10 velas (15 no 3m)
  4. Regime BTC: bloqueia LONGs se BTC 1h < EMA55
  5. Tendência EMA alinhada: 9 > 21 > 55 (LONG) ou 9 < 21 < 55 (SHORT)
  6. RSI: faixa 50-78 LONG / 22-50 SHORT no 3m; <80 / >20 nos demais
  7. Stop: 1x ATR | TP parcial 50% em 1.5x ATR → stop p/ breakeven
  8. Trailing 1x ATR no restante; EMA cross só encerra após o parcial
  9. vol_drop 1.5x apenas antes do TP parcial

Timeframes válidos: 3m / 5m / 15m / 30m / 1h
"""
import asyncio
import numpy as np
import pandas as pd
from typing import Optional

from models import TradeSignal, SignalScore, Direction
from klines_cache import get_klines_cached


# ── Indicadores internos ──────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(period).mean()


def _cvd(df: pd.DataFrame) -> pd.Series:
    """Cumulative Volume Delta via taker buy volume (coluna tbav se existir)."""
    if "tbav" in df.columns:
        delta = df["tbav"].astype(float) - (df["volume"] - df["tbav"].astype(float))
        return delta.cumsum()
    # fallback OBV: sinal do close x volume
    sign = np.sign(df["close"].diff()).fillna(0)
    return (sign * df["volume"]).cumsum()


# ── Regime BTC (cache 15 min) ─────────────────────────────────────────────────

_btc_regime_cache: dict = {"ts": 0.0, "up": True}

async def _btc_regime_up() -> bool:
    """True se BTC 1h > EMA55 (LONGs em micro-cap permitidos)."""
    import time as _time
    now = _time.time()
    if now - _btc_regime_cache["ts"] < 900:
        return _btc_regime_cache["up"]
    try:
        btc = await get_klines_cached("BTCUSDT", "1h", limit=120)
        if btc is not None and len(btc) >= 60:
            ema55 = _ema(btc["close"], 55).iloc[-1]
            _btc_regime_cache["up"] = bool(btc["close"].iloc[-1] > ema55)
            _btc_regime_cache["ts"] = now
    except Exception:
        pass
    return _btc_regime_cache["up"]


# ── Gates de entrada ──────────────────────────────────────────────────────────

def _gate_volume(df: pd.DataFrame, min_mult: float = 4.0) -> bool:
    """Volume do último candle ≥ min_mult × média 20 períodos."""
    vol = df["volume"]
    avg = vol.rolling(20).mean().iloc[-1]
    return avg > 0 and vol.iloc[-1] >= avg * min_mult


def _gate_cvd_slope(df: pd.DataFrame, direction: Direction, n: int = 10) -> bool:
    """Fluxo líquido (CVD/OBV) subindo nas últimas n velas (LONG) ou caindo (SHORT)."""
    if len(df) < n + 2:
        return False
    cvd = _cvd(df)
    if direction == Direction.LONG:
        return cvd.iloc[-1] > cvd.iloc[-1 - n]
    return cvd.iloc[-1] < cvd.iloc[-1 - n]


def _gate_breakout(df: pd.DataFrame, direction: Direction, lookback: int = 40) -> bool:
    """
    Breakout confirmado: close cruzou o high/low das últimas `lookback` velas.
    Exige o candle anterior também confirmando para evitar falso breakout de 1 candle.
    """
    close = df["close"]
    if direction == Direction.LONG:
        recent_high = df["high"].iloc[-lookback - 1 : -1].max()
        return close.iloc[-1] > recent_high and close.iloc[-2] > recent_high * 0.997
    else:
        recent_low = df["low"].iloc[-lookback - 1 : -1].min()
        return close.iloc[-1] < recent_low and close.iloc[-2] < recent_low * 1.003


def _gate_trend(df: pd.DataFrame, direction: Direction) -> bool:
    """EMA 9 > 21 > 55 para LONG | EMA 9 < 21 < 55 para SHORT."""
    close = df["close"]
    e9  = _ema(close, 9).iloc[-1]
    e21 = _ema(close, 21).iloc[-1]
    e55 = _ema(close, 55).iloc[-1]
    if direction == Direction.LONG:
        return e9 > e21 and e21 > e55
    return e9 < e21 and e21 < e55


def _gate_rsi(df: pd.DataFrame, direction: Direction, strict: bool = False) -> bool:
    """Rejeita entrada num pump/dump já exausto. strict=True (3m): faixa fechada."""
    rsi_val = _rsi(df["close"]).iloc[-1]
    if strict:
        if direction == Direction.LONG:
            return 50 <= rsi_val <= 78
        return 22 <= rsi_val <= 50
    if direction == Direction.LONG:
        return rsi_val < 80
    return rsi_val > 20


# ── Checagem de continuação (para manutenção de posição) ─────────────────────

def check_continuation(
    df: pd.DataFrame, direction: Direction, partial_done: bool = False
) -> bool:
    """
    Retorna False para sair antecipadamente.
    V2:
    - ANTES do TP parcial: só vol_drop (volume < 1.5x média) encerra
    - APÓS o TP parcial: só EMA 9 cruzando contra encerra
      (o trailing stop de 1x ATR fica por conta do executor)
    """
    if not partial_done:
        vol = df["volume"]
        avg = vol.rolling(20).mean().iloc[-1]
        return not (avg > 0 and vol.iloc[-1] < avg * 1.5)
    close = df["close"]
    e9  = _ema(close, 9).iloc[-1]
    e21 = _ema(close, 21).iloc[-1]
    if direction == Direction.LONG  and e9 < e21:
        return False
    if direction == Direction.SHORT and e9 > e21:
        return False
    return True


# ── Score de qualidade ────────────────────────────────────────────────────────

def _calc_score(df: pd.DataFrame, direction: Direction) -> float:
    """
    Score 0-100 ponderado para ativos voláteis.
    Volume 40% | Tendência 30% | Momentum RSI 30%
    """
    close = df["close"]
    vol   = df["volume"]
    price = float(close.iloc[-1])

    # Volume score — sem base fixa; 5x avg = 80pts, 8x = 100pts
    avg_vol   = float(vol.rolling(20).mean().iloc[-1]) or 1.0
    vol_ratio = float(vol.iloc[-1]) / avg_vol
    vol_score = min(100.0, vol_ratio / 8.0 * 100)

    # Trend score — usa separação relativa entre EMAs (magnitude, não binário)
    e9  = _ema(close, 9)
    e21 = _ema(close, 21)
    e55 = _ema(close, 55)
    ref = price * 0.005  # 0.5% do preço como referência de separação

    ts = 0.0
    if direction == Direction.LONG:
        ts += 40 * min(1.0, max(0.0, (e9.iloc[-1]  - e21.iloc[-1]) / ref))
        ts += 35 * min(1.0, max(0.0, (e21.iloc[-1] - e55.iloc[-1]) / ref))
        slope_pct = (e9.iloc[-1] - e9.iloc[-3]) / (price * 0.001)
        ts += 25 * min(1.0, max(0.0, slope_pct))
    else:
        ts += 40 * min(1.0, max(0.0, (e21.iloc[-1] - e9.iloc[-1])  / ref))
        ts += 35 * min(1.0, max(0.0, (e55.iloc[-1] - e21.iloc[-1]) / ref))
        slope_pct = (e9.iloc[-3] - e9.iloc[-1]) / (price * 0.001)
        ts += 25 * min(1.0, max(0.0, slope_pct))

    # Momentum score via RSI — recompensa zona ideal, penaliza extremos
    rsi_val = float(_rsi(close).iloc[-1])
    if direction == Direction.LONG:
        if   50 < rsi_val <= 62: ms = 100   # zona ideal
        elif 62 < rsi_val <= 70: ms = 80
        elif 70 < rsi_val <= 78: ms = 50    # próximo da exaustão
        elif 45 <= rsi_val <= 50: ms = 65
        elif rsi_val > 78:        ms = 20
        else:                     ms = 10
    else:
        if   38 <= rsi_val < 50: ms = 100
        elif 30 <= rsi_val < 38: ms = 80
        elif 22 <= rsi_val < 30: ms = 50
        elif 50 <= rsi_val < 55: ms = 65
        elif rsi_val < 22:        ms = 20
        else:                     ms = 10

    return vol_score * 0.40 + ts * 0.30 + ms * 0.30


# ── Avaliação principal ───────────────────────────────────────────────────────

def _evaluate(
    symbol: str,
    df: pd.DataFrame,
    direction: Direction,
    timeframe: str,
) -> Optional[TradeSignal]:
    # Parâmetros por TF: 3m exige confirmações extras
    is_3m    = timeframe == "3m"
    vol_mult = 5.0 if is_3m else 4.0
    lookback = 60  if is_3m else 40
    cvd_n    = 15  if is_3m else 10

    # Gates obrigatórios (ordem: mais barato → mais caro)
    if not _gate_volume(df, vol_mult):              return None
    if not _gate_cvd_slope(df, direction, cvd_n):   return None
    if not _gate_breakout(df, direction, lookback): return None
    if not _gate_trend(df, direction):              return None
    if not _gate_rsi(df, direction, strict=is_3m):  return None

    total = _calc_score(df, direction)
    if total < 60:
        return None

    close  = df["close"]
    atr_v  = _atr(df).iloc[-1]
    price  = close.iloc[-1]
    vol    = df["volume"]
    avg_v  = vol.rolling(20).mean().iloc[-1]
    vr     = vol.iloc[-1] / avg_v if avg_v > 0 else 1.0
    rsi_v  = _rsi(close).iloc[-1]

    if atr_v <= 0:
        return None

    # Levels V2: stop 1x ATR | TP único (2026-06-23) — captura o movimento completo
    if direction == Direction.LONG:
        stop = price - atr_v
        tp1  = price + atr_v * 2.5
        tp2  = tp1
    else:
        stop = price + atr_v
        tp1  = price - atr_v * 2.5
        tp2  = tp1

    risk   = abs(price - stop)
    reward = abs(tp1 - price)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    if rr < 1.4:
        return None

    score_obj = SignalScore(
        trend=_calc_score(df, direction) * 0.30,  # proxy
        volume=min(100, vr / 3.0 * 100),
        momentum=60,
        market_structure=0,
        funding_oi=50,
        news_context=50,
        total_override=round(total, 1),
    )

    trade_type = "SCALP" if timeframe in {"1m", "3m", "5m", "15m"} else "DAY_TRADE"

    return TradeSignal(
        asset=symbol,
        direction=direction,
        entry=round(price, 6),
        stop_loss=round(stop, 6),
        tp1=round(tp1, 6),
        tp2=round(tp2, 6),
        tp3=round(tp2, 6),
        rr=rr,
        confidence=round(total, 1),
        reason=f"VOLATILE V2 | Vol {vr:.1f}x | RSI {rsi_v:.0f} | Breakout {timeframe} | TP único 2.5ATR",
        score=score_obj,
        timeframe=timeframe,
        trade_type=trade_type,
        anomaly=f"Breakout confirmado + Vol {vr:.1f}x",
    )


# ── API pública ───────────────────────────────────────────────────────────────

async def analyze_volatile(
    symbol: str,
    timeframe: str,
    direction: Optional[Direction] = None,
    df: Optional[pd.DataFrame] = None,
) -> Optional[TradeSignal]:
    """
    Analisa um ativo com estratégia momentum-only.
    Retorna None se as condições obrigatórias não forem atendidas.
    """
    if timeframe not in {"1m", "3m", "5m", "15m"}:
        return None
    try:
        if df is None:
            df = await get_klines_cached(symbol, timeframe, limit=300)
        if df is None or len(df) < 80:
            return None
        dirs  = [direction] if direction else [Direction.LONG, Direction.SHORT]
        # Regime BTC: sem LONG em micro-cap com BTC 1h abaixo da EMA55
        # Ignora no backtest (quando df é fornecido)
        if df is None and Direction.LONG in dirs and not await _btc_regime_up():
            dirs = [d for d in dirs if d != Direction.LONG]
            if not dirs:
                return None
        best: Optional[TradeSignal] = None
        for d in dirs:
            sig = _evaluate(symbol, df, d, timeframe)
            if sig and (best is None or sig.confidence > best.confidence):
                best = sig
        return best
    except Exception:
        return None


async def scan_volatile(
    symbols: list[str],
    timeframes: list[str] | None = None,
) -> list[TradeSignal]:
    """Scan rápido dos ativos voláteis com regras momentum-only."""
    if timeframes is None:
        timeframes = ["1m", "3m", "5m", "15m"]
    tasks   = [analyze_volatile(sym, tf) for sym in symbols for tf in timeframes]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    signals = [r for r in results if isinstance(r, TradeSignal)]
    signals.sort(key=lambda s: s.confidence, reverse=True)
    return signals
