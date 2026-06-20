"""
TRADER 001 — Módulo de Backtesting
Usado pelo endpoint GET /backtest/{symbol} do dashboard.
Baixa dados do CDN data.binance.vision (sem API key, sem IP ban).
"""
import asyncio
import io
import zipfile
import urllib.request
import datetime as _dt
from typing import Optional

import numpy as np
import pandas as pd

# ── Estratégias disponíveis ───────────────────────────────────────────────────
STRATEGIES = {
    "EMA_CROSS_MOMENTUM": {
        "name": "EMA Cross + Momentum",
        "description": "EMA 9/21/50 + RSI 14 + Volume 1.5x — estratégia principal do bot",
    },
    "BREAKOUT_VOLUME": {
        "name": "Breakout Volume",
        "description": "Breakout acima de resistência de 20 velas + volume 2x",
    },
    "PULLBACK_RETRACE": {
        "name": "Pullback Retrace",
        "description": "Retração 38-62% de Fibonacci em tendência confirmada por EMA50",
    },
    "TREND_FOLLOW": {
        "name": "Trend Follow Simples",
        "description": "EMA 20 cruza EMA 50 com RSI 40-70 como filtro de tendência",
    },
}

# ── Download CDN ─────────────────────────────────────────────────────────────
def _download_ohlcv(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Baixa OHLCV do CDN público Binance. Sem API, sem rate limit."""
    base   = f"https://data.binance.vision/data/futures/um/daily/klines/{symbol}/{timeframe}/"
    end_d  = _dt.datetime.utcnow().date()
    start_d = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).date()
    rows = []
    d = start_d
    while d <= end_d:
        url = f"{base}{symbol}-{timeframe}-{d.strftime('%Y-%m-%d')}.zip"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = r.read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                with z.open(z.namelist()[0]) as f:
                    raw = pd.read_csv(f, header=None)
                    if not str(raw.iloc[0, 0]).lstrip("-").isdigit():
                        raw = raw.iloc[1:].reset_index(drop=True)
                    chunk = raw.iloc[:, :6].copy()
                    chunk.columns = ["ts", "open", "high", "low", "close", "volume"]
                    rows.append(chunk)
        except Exception:
            pass
        d += _dt.timedelta(days=1)

    if not rows:
        return pd.DataFrame()
    df = pd.concat(rows, ignore_index=True)
    df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
    # FIX: a Binance mudou o open_time de alguns datasets para MICROSSEGUNDOS (16 dígitos).
    # Detecta a unidade pela magnitude para não estourar o pd.to_datetime ('overflows').
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts"])
    _ts0 = float(df["ts"].iloc[0]) if len(df) else 0.0
    _unit = "us" if _ts0 >= 1e14 else ("ms" if _ts0 >= 1e11 else "s")
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit=_unit, utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


# ── Indicadores ───────────────────────────────────────────────────────────────
def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def _vol_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    avg = volume.rolling(period).mean()
    return volume / avg.replace(0, np.nan)


# ── Motor de simulação ────────────────────────────────────────────────────────
def _simulate(df: pd.DataFrame, signals: pd.Series, direction: str,
              leverage: int, fee: float = 0.0004, stop_atr: float = 1.5,
              tp1_atr: float = 2.0, tp2_atr: float = 3.5) -> dict:
    """
    Simula trades a partir de sinais booleanos.
    direction: 'LONG' | 'SHORT' | 'BOTH' (alternates based on signal column)
    """
    close  = df["close"].values
    high   = df["high"].values
    low    = df["low"].values
    atr_v  = _atr(df["high"], df["low"], df["close"]).values
    dates  = df["ts"].dt.strftime("%Y-%m-%d").values
    sig    = signals.values

    trades      = []
    in_trade    = False
    entry_i     = 0
    entry_price = 0.0
    sl_price    = 0.0
    tp1_price   = 0.0
    tp2_price   = 0.0
    is_long     = True
    cooldown    = 0

    for i in range(50, len(df)):
        if cooldown > 0:
            cooldown -= 1
            continue
        if not in_trade and sig[i]:
            atr_i = atr_v[i]
            if np.isnan(atr_i) or atr_i <= 0:
                continue
            is_long = True if direction == "LONG" else (False if direction == "SHORT" else (len(trades) % 2 == 0))
            entry_price = close[i]
            if is_long:
                sl_price  = entry_price - stop_atr * atr_i
                tp1_price = entry_price + tp1_atr * atr_i
                tp2_price = entry_price + tp2_atr * atr_i
            else:
                sl_price  = entry_price + stop_atr * atr_i
                tp1_price = entry_price - tp1_atr * atr_i
                tp2_price = entry_price - tp2_atr * atr_i
            in_trade = True
            entry_i  = i
            continue

        if in_trade:
            hit_sl  = (low[i]  <= sl_price)  if is_long else (high[i] >= sl_price)
            hit_tp1 = (high[i] >= tp1_price) if is_long else (low[i]  <= tp1_price)
            hit_tp2 = (high[i] >= tp2_price) if is_long else (low[i]  <= tp2_price)

            exit_price = None
            result     = None
            if hit_tp2:
                exit_price = tp2_price
                result     = "WIN"
            elif hit_tp1:
                exit_price = tp1_price
                result     = "WIN"
            elif hit_sl:
                exit_price = sl_price
                result     = "LOSS"
            elif i - entry_i >= 48:  # timeout: 48 velas
                exit_price = close[i]
                result     = "WIN" if (close[i] > entry_price if is_long else close[i] < entry_price) else "LOSS"

            if exit_price is not None:
                raw_pnl_pct = ((exit_price - entry_price) / entry_price) * leverage
                if not is_long:
                    raw_pnl_pct = -raw_pnl_pct
                net_pnl_pct = raw_pnl_pct - 2 * fee * leverage
                rr = abs(exit_price - entry_price) / (abs(entry_price - sl_price) or 1)
                trades.append({
                    "date":      dates[entry_i],
                    "direction": "LONG" if is_long else "SHORT",
                    "entry":     round(entry_price, 6),
                    "exit":      round(exit_price, 6),
                    "pnl_pct":   round(net_pnl_pct * 100, 3),
                    "rr":        round(rr, 2),
                    "result":    result,
                })
                in_trade = False
                cooldown = 5

    if not trades:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "profit_factor": 0.0, "total_pnl_pct": 0.0, "max_drawdown_pct": 0.0,
                "avg_rr": 0.0, "trades": []}

    wins    = [t for t in trades if t["result"] == "WIN"]
    losses  = [t for t in trades if t["result"] == "LOSS"]
    win_r   = len(wins) / len(trades) * 100
    gross_p = sum(t["pnl_pct"] for t in wins)
    gross_l = abs(sum(t["pnl_pct"] for t in losses)) or 1
    pf      = round(gross_p / gross_l, 3)

    # Drawdown
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t["pnl_pct"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    avg_rr = round(np.mean([t["rr"] for t in trades]), 2) if trades else 0.0

    return {
        "symbol":         df.attrs.get("symbol", "?"),
        "strategy":       df.attrs.get("strategy", "?"),
        "timeframe":      df.attrs.get("timeframe", "?"),
        "days":           df.attrs.get("days", 0),
        "total_trades":   len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(win_r, 2),
        "profit_factor":  pf,
        "total_pnl_pct":  round(sum(t["pnl_pct"] for t in trades), 3),
        "max_drawdown_pct": round(max_dd, 3),
        "avg_rr":         avg_rr,
        "trades":         trades,
    }


# ── Estratégias ───────────────────────────────────────────────────────────────
def _signals_ema_cross(df: pd.DataFrame) -> pd.Series:
    ema9  = _ema(df["close"], 9)
    ema21 = _ema(df["close"], 21)
    ema50 = _ema(df["close"], 50)
    rsi   = _rsi(df["close"])
    volr  = _vol_ratio(df["volume"])
    trend_ok   = (ema9 > ema21) & (ema21 > ema50)
    rsi_ok     = (rsi >= 45) & (rsi <= 72)
    vol_ok     = volr >= 1.5
    prev_ema9  = ema9.shift(1)
    prev_ema21 = ema21.shift(1)
    cross      = (prev_ema9 <= prev_ema21) & (ema9 > ema21)
    return (trend_ok & rsi_ok & vol_ok & cross).fillna(False)


def _signals_breakout(df: pd.DataFrame) -> pd.Series:
    resist = df["high"].rolling(20).max().shift(1)
    volr   = _vol_ratio(df["volume"])
    rsi    = _rsi(df["close"])
    return ((df["close"] > resist) & (volr >= 2.0) & (rsi < 75)).fillna(False)


def _signals_pullback(df: pd.DataFrame) -> pd.Series:
    ema50 = _ema(df["close"], 50)
    high20 = df["high"].rolling(20).max()
    low20  = df["low"].rolling(20).min()
    fib38  = high20 - (high20 - low20) * 0.382
    fib62  = high20 - (high20 - low20) * 0.618
    in_zone = (df["close"] >= fib62) & (df["close"] <= fib38)
    uptrend = df["close"] > ema50
    rsi     = _rsi(df["close"])
    rsi_ok  = (rsi >= 40) & (rsi <= 65)
    return (in_zone & uptrend & rsi_ok).fillna(False)


def _signals_trend_follow(df: pd.DataFrame) -> pd.Series:
    ema20 = _ema(df["close"], 20)
    ema50 = _ema(df["close"], 50)
    rsi   = _rsi(df["close"])
    cross = (ema20.shift(1) <= ema50.shift(1)) & (ema20 > ema50)
    rsi_ok = (rsi >= 40) & (rsi <= 70)
    return (cross & rsi_ok).fillna(False)


_SIGNAL_FN = {
    "EMA_CROSS_MOMENTUM": _signals_ema_cross,
    "BREAKOUT_VOLUME":    _signals_breakout,
    "PULLBACK_RETRACE":   _signals_pullback,
    "TREND_FOLLOW":       _signals_trend_follow,
}


# ── Ponto de entrada público ──────────────────────────────────────────────────
def _run_backtest_sync(symbol: str, strategy: str, timeframe: str,
                       direction: str, days: int, leverage: int) -> dict:
    df = _download_ohlcv(symbol, timeframe, days)
    if df.empty:
        return {"error": f"Sem dados para {symbol}/{timeframe}. Verifique o símbolo e tente novamente."}

    df.attrs["symbol"]   = symbol
    df.attrs["strategy"] = strategy
    df.attrs["timeframe"] = timeframe
    df.attrs["days"]     = days

    signal_fn = _SIGNAL_FN.get(strategy, _signals_ema_cross)
    signals   = signal_fn(df)
    return _simulate(df, signals, direction, leverage)


async def run_backtest(symbol: str, strategy: str = "EMA_CROSS_MOMENTUM",
                       timeframe: str = "1h", direction: str = "BOTH",
                       days: int = 120, leverage: int = 10) -> dict:
    """Async wrapper — roda o backtest em thread separada para não bloquear o event loop."""
    try:
        result = await asyncio.to_thread(_run_backtest_sync, symbol, strategy, timeframe, direction, days, leverage)
        return result
    except Exception as e:
        return {"error": str(e)}


async def run_full_benchmark():
    """Roda benchmark em todos os pares principais e salva JSON."""
    import json
    from pathlib import Path
    pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
    results = []
    for sym in pairs:
        for strat in STRATEGIES:
            r = await run_backtest(sym, strat, "1h", "BOTH", 90, 10)
            if "error" not in r:
                results.append(r)
    out = Path(__file__).parent / "backtest_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    print(f"[BACKTEST] Benchmark completo: {len(results)} combinações salvas em {out}")
    return results
