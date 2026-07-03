import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # acha modulos do bot na pasta-mae
"""
Backtest Micro Cap — 10 dias, 3m e 5m
5 ativos: SNDKUSDT, BEATUSDT, CLUSDT, SOXLUSDT, WLDUSDT
Dados: data.binance.vision (sem IP ban)
Estratégia: V5.3 adaptada (EMA + RSI + Volume + MACD + anti pump/dump)
"""
import warnings
warnings.filterwarnings("ignore")

import io, zipfile, datetime as _dt, urllib.request
import numpy as np
import pandas as pd

SYMBOLS    = ["SNDKUSDT", "BEATUSDT", "CLUSDT", "SOXLUSDT", "WLDUSDT"]
TIMEFRAMES = ["3m", "5m"]
DAYS       = 10
CAPITAL    = 1000.0
FEE        = 0.0004    # 0.04% por lado
SCORE_TH   = 4         # UPGRADE: mínimo 4/5 componentes (era 3)
VOL_MIN    = 2.0       # UPGRADE: volume mínimo 2x média (era 1.5x)
SL_ATR     = 1.5
TP_ATR     = 3.0


def fetch_ohlcv(symbol: str, tf: str) -> pd.DataFrame:
    base = f"https://data.binance.vision/data/futures/um/daily/klines/{symbol}/{tf}/"
    end_d   = _dt.datetime.now(_dt.timezone.utc).date()
    start_d = (end_d - _dt.timedelta(days=DAYS))

    rows = []
    d = start_d
    while d <= end_d:
        url = f"{base}{symbol}-{tf}-{d.strftime('%Y-%m-%d')}.zip"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                raw_bytes = r.read()
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
                with z.open(z.namelist()[0]) as f:
                    chunk = pd.read_csv(f, header=None)
                    if not str(chunk.iloc[0, 0]).lstrip("-").isdigit():
                        chunk = chunk.iloc[1:].reset_index(drop=True)
                    chunk = chunk.iloc[:, :6].copy()
                    chunk.columns = ["ts","open","high","low","close","volume"]
                    rows.append(chunk)
        except Exception:
            pass
        d += _dt.timedelta(days=1)

    if not rows:
        return None
    df = pd.concat(rows, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop(columns=["ts"]).set_index("timestamp")
    return df[~df.index.duplicated()].sort_index().astype(float)


def detect_structure(highs, lows, window=20) -> np.ndarray:
    """
    Detecta estrutura candle a candle: UPTREND / DOWNTREND / RANGING.
    Replica identify_structure() do signal_engine usando janela deslizante.
    Retorna array de strings com mesmo índice do df.
    """
    n = len(highs)
    result = np.full(n, "RANGING", dtype=object)

    for i in range(window + 2, n):
        h = highs[i - window: i]
        l = lows[i  - window: i]

        sh = [h[j] for j in range(1, len(h)-1) if h[j] > h[j-1] and h[j] > h[j+1]]
        sl = [l[j] for j in range(1, len(l)-1) if l[j] < l[j-1] and l[j] < l[j+1]]

        if len(sh) >= 2 and len(sl) >= 2:
            hh = sh[-1] > sh[-2]
            hl = sl[-1] > sl[-2]
            lh = sh[-1] < sh[-2]
            ll = sl[-1] < sl[-2]
            if hh and hl:
                result[i] = "UPTREND"
            elif lh and ll:
                result[i] = "DOWNTREND"

    return result


def compute_signals(df: pd.DataFrame):
    c = df["close"]; h = df["high"]; l = df["low"]; v = df["volume"]

    e9   = c.ewm(span=9,  adjust=False).mean()
    e21  = c.ewm(span=21, adjust=False).mean()
    e50  = c.ewm(span=50, adjust=False).mean()

    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    hl_tr = h - l
    hc    = (h - c.shift()).abs()
    lc    = (l - c.shift()).abs()
    atr   = pd.concat([hl_tr, hc, lc], axis=1).max(axis=1).rolling(14).mean()

    vol_avg   = v.rolling(20).mean()
    vol_ratio = v / vol_avg.replace(0, np.nan)

    ml   = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig  = ml.ewm(span=9, adjust=False).mean()
    hist = ml - sig
    macd_up = hist > hist.shift(1)

    # UPGRADE: estrutura de mercado (janela deslizante)
    structure = detect_structure(h.values, l.values)
    is_uptrend   = pd.Series(structure == "UPTREND",   index=c.index)
    is_downtrend = pd.Series(structure == "DOWNTREND", index=c.index)

    # Score LONG — 5 componentes + filtro estrutura
    s1 = (e9 > e21) & (e21 > e50)
    s2 = (rsi >= 45) & (rsi <= 70)
    s3 = vol_ratio >= VOL_MIN          # UPGRADE: 2x (era 1.5x)
    s4 = macd_up
    s5 = c > e21
    score_l = s1.astype(int) + s2.astype(int) + s3.astype(int) + s4.astype(int) + s5.astype(int)

    # Score SHORT — 5 componentes + filtro estrutura
    s1s = (e9 < e21) & (e21 < e50)
    s2s = (rsi >= 30) & (rsi <= 55)
    s3s = vol_ratio >= VOL_MIN
    s4s = ~macd_up
    s5s = c < e21
    score_s = s1s.astype(int) + s2s.astype(int) + s3s.astype(int) + s4s.astype(int) + s5s.astype(int)

    # UPGRADE: pump/dump veto
    anti_pd = (rsi > 76) | (vol_ratio > 6.5)
    score_l = score_l.where(~anti_pd, 0)
    score_s = score_s.where(~anti_pd, 0)

    # UPGRADE: estrutura obrigatória — zera score na direção errada
    score_l = score_l.where(is_uptrend,   0)   # LONG só em UPTREND
    score_s = score_s.where(is_downtrend, 0)   # SHORT só em DOWNTREND

    # UPGRADE: RR adaptativo — tendência forte abre alvos maiores
    slope = (e21 - e21.shift(5)) / e21.shift(5) * 100
    strong_trend = slope.abs() > 0.3

    sl_frac = (atr * SL_ATR) / c
    tp_frac = np.where(strong_trend, (atr * 4.0) / c, (atr * TP_ATR) / c)
    tp_frac = pd.Series(tp_frac, index=c.index)

    return score_l, score_s, sl_frac, tp_frac, atr, rsi, vol_ratio, structure


def simulate(score_long: pd.Series, score_short: pd.Series, close: pd.Series,
             sl_frac: pd.Series, tp_frac: pd.Series) -> dict:
    """
    Simulação candle-a-candle com direção decidida pela estratégia.
    Entra LONG se score_long > score_short e score_long >= SCORE_TH.
    Entra SHORT se score_short > score_long e score_short >= SCORE_TH.
    Nunca abre os dois lados ao mesmo tempo — igual ao bot real.
    """
    trades = []
    in_trade = False
    entry_price = sl = tp = 0.0
    entry_idx   = 0
    direction   = None  # "LONG" ou "SHORT"

    prices  = close.values
    sl_f    = sl_frac.values
    tp_f    = tp_frac.values
    sc_l    = score_long.values
    sc_s    = score_short.values
    n       = len(prices)

    for i in range(1, n):
        if not in_trade:
            sl_ok = not np.isnan(sl_f[i-1]) and not np.isnan(tp_f[i-1])
            go_long  = sc_l[i-1] >= SCORE_TH and sc_l[i-1] > sc_s[i-1] and sl_ok
            go_short = sc_s[i-1] >= SCORE_TH and sc_s[i-1] > sc_l[i-1] and sl_ok

            if go_long:
                entry_price = prices[i]
                sl          = entry_price * (1 - sl_f[i-1])
                tp          = entry_price * (1 + tp_f[i-1])
                direction   = "LONG"
                in_trade    = True
                entry_idx   = i
            elif go_short:
                entry_price = prices[i]
                sl          = entry_price * (1 + sl_f[i-1])
                tp          = entry_price * (1 - tp_f[i-1])
                direction   = "SHORT"
                in_trade    = True
                entry_idx   = i
        else:
            p      = prices[i]
            is_short = direction == "SHORT"
            hit_sl = (p <= sl) if not is_short else (p >= sl)
            hit_tp = (p >= tp) if not is_short else (p <= tp)

            if hit_tp or hit_sl or (i - entry_idx) > 200:
                exit_price = tp if hit_tp else (sl if hit_sl else p)
                if is_short:
                    pnl_pct = (entry_price - exit_price) / entry_price * 100
                else:
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                pnl_pct -= FEE * 200
                trades.append({
                    "pnl_pct":   pnl_pct,
                    "direction": direction,
                    "exit":      "TP" if hit_tp else ("SL" if hit_sl else "TM"),
                })
                in_trade = False

    if not trades:
        return {"trades": 0, "win_rate": 0, "profit_factor": 0,
                "total_return": 0, "max_dd": 0, "tp_hits": 0, "sl_hits": 0,
                "long_trades": 0, "short_trades": 0, "final_equity": CAPITAL}

    wins   = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0]
    wr     = len(wins) / len(trades) * 100
    gp     = sum(wins) if wins else 0
    gl     = abs(sum(losses)) if losses else 0.001
    pf     = round(gp / gl, 3)

    equity = CAPITAL
    peak   = CAPITAL
    max_dd = 0.0
    for t in trades:
        equity *= (1 + t["pnl_pct"] / 100)
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd: max_dd = dd

    total_return = (equity - CAPITAL) / CAPITAL * 100

    return {
        "trades":        len(trades),
        "win_rate":      round(wr, 1),
        "profit_factor": pf,
        "total_return":  round(total_return, 2),
        "max_dd":        round(max_dd, 1),
        "tp_hits":       sum(1 for t in trades if t["exit"] == "TP"),
        "sl_hits":       sum(1 for t in trades if t["exit"] == "SL"),
        "long_trades":   sum(1 for t in trades if t["direction"] == "LONG"),
        "short_trades":  sum(1 for t in trades if t["direction"] == "SHORT"),
        "final_equity":  round(equity, 2),
    }


def grade(pf):
    if pf >= 1.5: return "[BOM]"
    if pf >= 1.0: return "[OK] "
    return "[RUIM]"


# ── MAIN ──────────────────────────────────────────────────────────────────────
all_results = {}

print(f"\n{'#'*60}")
print(f"  BACKTEST MICRO CAP — {DAYS} dias | Cap: ${CAPITAL}")
print(f"  Ativos: {', '.join(SYMBOLS)}")
print(f"  TFs: {TIMEFRAMES}  |  Score: {SCORE_TH}/5  |  SL:{SL_ATR}x ATR  TP:{TP_ATR}x ATR")
print(f"{'#'*60}")

for tf in TIMEFRAMES:
    print(f"\n{'='*60}")
    print(f"  TIMEFRAME: {tf}")
    print(f"{'='*60}")
    print(f"  {'Ativo':<14} {'Trades':>5} {'WR':>7} {'PF':>7} {'Ret%':>8} {'MaxDD':>7} {'Exits':<11} {'Direcoes':<16} {'Estrutura':<28} {'Grade'}")
    print(f"  {'-'*115}")

    tf_results = []
    for sym in SYMBOLS:
        df = fetch_ohlcv(sym, tf)
        if df is None or len(df) < 100:
            print(f"  {sym:<14} SEM DADOS")
            continue

        sc_l, sc_s, sl_f, tp_f, atr_s, rsi_s, vol_s, struct = compute_signals(df)
        uptrend_pct   = round((struct == "UPTREND").mean()   * 100, 0)
        downtrend_pct = round((struct == "DOWNTREND").mean() * 100, 0)
        ranging_pct   = round((struct == "RANGING").mean()   * 100, 0)
        r = simulate(sc_l, sc_s, df["close"], sl_f, tp_f)

        pf  = r["profit_factor"]
        ret = r["total_return"]
        mix = f"L:{r['long_trades']} S:{r['short_trades']}"
        struct_info = f"UP:{uptrend_pct:.0f}% DW:{downtrend_pct:.0f}% RG:{ranging_pct:.0f}%"
        print(
            f"  {sym:<14} {r['trades']:>5} {r['win_rate']:>6.1f}% "
            f"{pf:>7.3f} {ret:>+8.2f}% {r['max_dd']:>6.1f}% "
            f"{r['tp_hits']:>4}TP {r['sl_hits']:>4}SL  {mix:<14} {struct_info:<28} {grade(pf)}"
        )
        tf_results.append({"sym": sym, "tf": tf, **r})

    all_results[tf] = tf_results

# ── RESUMO GLOBAL ─────────────────────────────────────────────────────────────
print(f"\n\n{'#'*60}")
print(f"  RESUMO GLOBAL")
print(f"{'#'*60}")

for tf, results in all_results.items():
    if not results:
        continue
    pf_vals  = [r["profit_factor"] for r in results if r["trades"] > 0]
    ret_vals = [r["total_return"] for r in results if r["trades"] > 0]
    wr_vals  = [r["win_rate"] for r in results if r["trades"] > 0]
    if not pf_vals:
        continue
    pf_avg  = sum(pf_vals) / len(pf_vals)
    ret_avg = sum(ret_vals) / len(ret_vals)
    wr_avg  = sum(wr_vals) / len(wr_vals)
    positivos = sum(1 for r in results if r["total_return"] > 0)
    print(f"\n  [{tf}]  PF médio: {pf_avg:.3f}  |  WR médio: {wr_avg:.1f}%  |  "
          f"Ret médio: {ret_avg:+.2f}%  |  Combos lucrativos: {positivos}/{len(results)}")

# Melhor e pior combo
all_flat = [r for rs in all_results.values() for r in rs if r["trades"] > 0]
if all_flat:
    best  = max(all_flat, key=lambda x: x["profit_factor"])
    worst = min(all_flat, key=lambda x: x["profit_factor"])
    print(f"\n  MELHOR:  {best['sym']} {best['tf']}  PF={best['profit_factor']:.3f}  Ret={best['total_return']:+.2f}%  L:{best['long_trades']} S:{best['short_trades']}")
    print(f"  PIOR:    {worst['sym']} {worst['tf']}  PF={worst['profit_factor']:.3f}  Ret={worst['total_return']:+.2f}%  L:{worst['long_trades']} S:{worst['short_trades']}")

print(f"\n{'#'*60}\n")
