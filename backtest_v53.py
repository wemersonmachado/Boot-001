"""
Backtest V5.3 — vectorbt + Binance Futures (ccxt)
Replica a lógica central do bot:
  - EMA9 > EMA21 > EMA50 (trend)
  - RSI 14 entre 45-70 (long) / 30-55 (short)
  - Volume ratio >= 1.5x média 20 períodos
  - Score composto >= threshold (configurável)
  - Stop: 1.5x ATR14  |  TP: 3.0x ATR14  (RR ~2.0)
  - Anti pump/dump: RSI > 76 ou vol_ratio > 6.5 → skip
"""
import warnings, sys, io
warnings.filterwarnings("ignore")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
import ccxt
import vectorbt as vbt
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOLS      = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
                "BNB/USDT:USDT", "XRP/USDT:USDT"]
TIMEFRAME    = "5m"
SINCE_DAYS   = 90          # janela de backtest (dias)
INITIAL_CAP  = 1000.0      # USDT
FEE          = 0.0004      # 0.04% por lado (taker Binance futures)
SCORE_THRESH = 3           # componentes mínimos alinhados para entrada
STOP_ATR_M   = 1.5         # multiplicador ATR para stop
TP_ATR_M     = 3.0         # multiplicador ATR para take profit

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
def fetch_ohlcv(symbol: str) -> pd.DataFrame:
    """
    Baixa dados do CDN público data.binance.vision (futures UM).
    Não usa a API REST — não afetado por IP ban.
    """
    import io, zipfile, urllib.request, datetime as _dt

    sym = symbol.replace("/", "").replace(":USDT", "").replace("USDT", "") + "USDT"
    base = f"https://data.binance.vision/data/futures/um/daily/klines/{sym}/{TIMEFRAME}/"

    end_d   = datetime.now(timezone.utc).date()
    start_d = (datetime.now(timezone.utc) - pd.Timedelta(days=SINCE_DAYS)).date()

    rows = []
    d = start_d
    while d <= end_d:
        url = f"{base}{sym}-{TIMEFRAME}-{d.strftime('%Y-%m-%d')}.zip"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = r.read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                with z.open(z.namelist()[0]) as f:
                    raw = pd.read_csv(f, header=None)
                    # data.binance.vision: pode ter header textual na linha 0
                    if not str(raw.iloc[0, 0]).lstrip("-").isdigit():
                        raw = raw.iloc[1:].reset_index(drop=True)
                    chunk = raw.iloc[:, :6].copy()
                    chunk.columns = ["ts","open","high","low","close","volume"]
                    rows.append(chunk)
        except Exception:
            pass
        d += _dt.timedelta(days=1)

    if not rows:
        raise ValueError(f"Sem dados para {sym}")

    df = pd.concat(rows, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop(columns=["ts"]).set_index("timestamp")
    return df[~df.index.duplicated()].sort_index().astype(float)

# ── INDICADORES ───────────────────────────────────────────────────────────────
def compute_signals(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # EMAs
    e9   = c.ewm(span=9,   adjust=False).mean()
    e21  = c.ewm(span=21,  adjust=False).mean()
    e50  = c.ewm(span=50,  adjust=False).mean()

    # RSI 14
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - 100 / (1 + rs)

    # ATR 14
    hl  = h - l
    hc  = (h - c.shift()).abs()
    lc  = (l - c.shift()).abs()
    atr = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean()

    # Volume ratio (vs média 20)
    vol_avg   = v.rolling(20).mean()
    vol_ratio = v / vol_avg.replace(0, np.nan)

    # MACD hist direction
    macd_line = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    signal    = macd_line.ewm(span=9, adjust=False).mean()
    hist      = macd_line - signal
    macd_up   = hist > hist.shift(1)

    # Score LONG (máximo 5 componentes)
    s_ema   = (e9 > e21) & (e21 > e50)           # 1 - trend
    s_rsi   = (rsi >= 45) & (rsi <= 70)           # 2 - RSI zona neutra/bullish
    s_vol   = vol_ratio >= 1.5                     # 3 - volume acima da média
    s_macd  = macd_up                              # 4 - MACD acelerando
    s_above = c > e21                              # 5 - preço acima EMA21

    score_long = s_ema.astype(int) + s_rsi.astype(int) + s_vol.astype(int) \
               + s_macd.astype(int) + s_above.astype(int)

    # Score SHORT (inverso)
    s_ema_s  = (e9 < e21) & (e21 < e50)
    s_rsi_s  = (rsi >= 30) & (rsi <= 55)
    s_vol_s  = vol_ratio >= 1.5
    s_macd_s = ~macd_up
    s_below  = c < e21

    score_short = s_ema_s.astype(int) + s_rsi_s.astype(int) + s_vol_s.astype(int) \
                + s_macd_s.astype(int) + s_below.astype(int)

    # Anti pump/dump filter
    anti_pd = (rsi > 76) | (vol_ratio > 6.5)

    # Entradas
    long_entry  = (score_long  >= SCORE_THRESH) & ~anti_pd
    short_entry = (score_short >= SCORE_THRESH) & ~anti_pd

    # Exits via ATR (SL/TP fixo por posição)
    # vectorbt usa sl_stop e tp_stop como fração do preço de entrada
    sl_frac = (atr * STOP_ATR_M) / c   # SL = 1.5 ATR
    tp_frac = (atr * TP_ATR_M)  / c   # TP = 3.0 ATR

    return long_entry, short_entry, sl_frac, tp_frac, atr, rsi, vol_ratio

# ── BACKTEST POR SÍMBOLO ──────────────────────────────────────────────────────
def run_backtest(symbol: str) -> dict:
    print(f"\n{'='*55}")
    print(f"  {symbol}  |  {TIMEFRAME}  |  {SINCE_DAYS}d")
    print(f"{'='*55}")

    try:
        df = fetch_ohlcv(symbol)
        print(f"  Candles: {len(df):,}  ({df.index[0].date()} -> {df.index[-1].date()})")
    except Exception as e:
        print(f"  ERRO ao baixar dados: {e}")
        return {}

    long_entry, short_entry, sl_frac, tp_frac, atr, rsi, vol_ratio = compute_signals(df)

    c = df["close"]

    # ── LONG ──
    pf_long = vbt.Portfolio.from_signals(
        close        = c,
        entries      = long_entry,
        exits        = pd.Series(False, index=c.index),
        sl_stop      = sl_frac,
        tp_stop      = tp_frac,
        init_cash    = INITIAL_CAP / 2,
        fees         = FEE,
        freq         = TIMEFRAME,
        short_entries= False,
    )

    # ── SHORT ──
    pf_short = vbt.Portfolio.from_signals(
        close        = c,
        entries      = short_entry,
        exits        = pd.Series(False, index=c.index),
        sl_stop      = sl_frac,
        tp_stop      = tp_frac,
        init_cash    = INITIAL_CAP / 2,
        fees         = FEE,
        freq         = TIMEFRAME,
        short_entries= True,
    )

    def stats_dict(pf, label):
        try:
            s       = pf.stats()
            trades  = pf.trades.records_readable
            n       = len(trades)
            wins    = (trades["PnL"] > 0).sum() if n > 0 else 0
            wr      = wins / n * 100 if n > 0 else 0
            avg_win = trades[trades["PnL"] > 0]["PnL"].mean() if wins > 0 else 0
            avg_los = trades[trades["PnL"] <= 0]["PnL"].mean() if (n - wins) > 0 else 0
            pf_rat  = abs(avg_win / avg_los) if avg_los != 0 else 0
            return {
                "label":       label,
                "trades":      n,
                "win_rate":    round(wr, 1),
                "profit_factor": round(pf_rat, 2),
                "total_return":  round(float(s.get("Total Return [%]", 0)), 2),
                "max_dd":        round(float(s.get("Max Drawdown [%]", 0)), 2),
                "sharpe":        round(float(s.get("Sharpe Ratio", 0)), 2),
                "avg_win_usdt":  round(float(avg_win), 2),
                "avg_loss_usdt": round(float(avg_los), 2),
            }
        except Exception as e:
            return {"label": label, "error": str(e)}

    r_long  = stats_dict(pf_long,  "LONG")
    r_short = stats_dict(pf_short, "SHORT")

    print(f"\n  {'Métrica':<22} {'LONG':>10} {'SHORT':>10}")
    print(f"  {'-'*44}")
    for key in ["trades", "win_rate", "profit_factor", "total_return", "max_dd", "sharpe"]:
        l = r_long.get(key, "-")
        s = r_short.get(key, "-")
        unit = "%" if key in ("win_rate", "total_return", "max_dd") else ""
        print(f"  {key:<22} {str(l)+unit:>10} {str(s)+unit:>10}")

    # Sinal de qualidade
    pf_avg = (r_long.get("profit_factor", 0) + r_short.get("profit_factor", 0)) / 2
    wr_avg = (r_long.get("win_rate", 0) + r_short.get("win_rate", 0)) / 2
    if pf_avg >= 1.5 and wr_avg >= 50:
        grade = "✅ BOM"
    elif pf_avg >= 1.2:
        grade = "⚠️  ACEITÁVEL"
    else:
        grade = "❌ RUIM"
    print(f"\n  Grade: {grade}  (PF médio={pf_avg:.2f}, WR médio={wr_avg:.1f}%)")

    return {"symbol": symbol, "long": r_long, "short": r_short}


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{'#'*55}")
    print(f"  BACKTEST V5.3 — {TIMEFRAME} — últimos {SINCE_DAYS} dias")
    print(f"  Score threshold: {SCORE_THRESH}/5  |  SL: {STOP_ATR_M}x ATR  |  TP: {TP_ATR_M}x ATR")
    print(f"  Fee: {FEE*100:.3f}% por lado  |  Capital: ${INITIAL_CAP}")
    print(f"{'#'*55}")

    all_results = []
    for sym in SYMBOLS:
        r = run_backtest(sym)
        if r:
            all_results.append(r)

    # Resumo consolidado
    print(f"\n\n{'#'*55}")
    print(f"  RESUMO CONSOLIDADO — {len(all_results)} pares")
    print(f"{'#'*55}")
    print(f"  {'Par':<18} {'WR L':>6} {'WR S':>6} {'PF L':>6} {'PF S':>6} {'DD L':>7}")
    print(f"  {'-'*53}")
    for r in all_results:
        sym  = r["symbol"].replace("/USDT:USDT","")
        wrl  = r["long"].get("win_rate", 0)
        wrs  = r["short"].get("win_rate", 0)
        pfl  = r["long"].get("profit_factor", 0)
        pfs  = r["short"].get("profit_factor", 0)
        ddl  = r["long"].get("max_dd", 0)
        print(f"  {sym:<18} {wrl:>5.1f}% {wrs:>5.1f}% {pfl:>6.2f} {pfs:>6.2f} {ddl:>6.1f}%")

    pf_all = np.mean([r["long"].get("profit_factor",0) for r in all_results] +
                     [r["short"].get("profit_factor",0) for r in all_results])
    print(f"\n  Profit Factor médio global: {pf_all:.2f}")
    print(f"  (Estratégia V5.3 backtest reportou PF 1.808)")
