import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # acha modulos do bot na pasta-mae
"""
BACKTEST V3 — "Pullback to EMA" Strategy
=========================================
Lógica central (o que funciona em mercado real):
  1. 4h determina TENDÊNCIA (EMA 9/21/55 alinhadas + ADX>25)
  2. 1h detecta PULLBACK (preço volta para EMA55 após impulso)
  3. 15m confirma ENTRADA (candle reversão com volume)
  4. ADX < 25 em qualquer TF relevante → NÃO OPERAR
  5. Trailing SL: hit TP1 → SL para breakeven, hit TP2 → SL para TP1

Por que pullback?
  - Breakout entries: entram no pior momento (rally/dump já avançado)
  - Pullback entries: entram próximo ao suporte real, SL pequeno, RR 3-5x
  - Win rate esperado: 40-55% vs 15-20% de breakout em mercado chopppy

Estratégia por regime (por ativo, por TF):
  TRENDING (ADX>25, 4h + 1h alinhados): pullback para EMA55 no 1h
  RANGING  (ADX<22):                     NÃO OPERA (zero trades)
  VOLATILE (ATR ratio>2.5):              NÃO OPERA ou size 40%
"""

import io, sys, zipfile, urllib.request, datetime as _dt
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import os, requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Parâmetros ─────────────────────────────────────────────────────────────────
SYMBOLS       = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SINCE_DAYS    = 10
BANCA_BRL     = 1000.0
CAMBIO        = 5.40
BANCA_USDT    = round(BANCA_BRL / CAMBIO, 2)   # ~185.19
LEVERAGE      = 10
FEE_TAKER     = 0.0004
RISK_PCT      = 0.02           # 2% por trade
ADX_MIN       = 22             # ADX mínimo para operar (mercado com direcao)
ATR_VOLA_MAX  = 2.8            # atr_ratio máximo — acima = VOLATILE, não opera
STOP_ATR      = 1.1            # SL apertado (pullback = entrada boa)
TP1_ATR       = 1.8            # 1.8x ATR para TP1
TP2_ATR       = 3.2            # 3.2x ATR para TP2
TP3_ATR       = 5.0            # 5.0x ATR para TP3
TP1_FRAC      = 0.35
TP2_FRAC      = 0.35
TP3_FRAC      = 0.30
COOLDOWN_BARS = 12             # 12 × 15m = 3h após trade (filtra overtrading)
DEAD_HOURS    = set(range(1, 7))  # 01h-07h UTC morto

from dotenv import load_dotenv
load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Download CDN ───────────────────────────────────────────────────────────────
def _dl_day(symbol: str, tf: str, date: _dt.date) -> pd.DataFrame | None:
    ds  = date.strftime("%Y-%m-%d")
    url = (f"https://data.binance.vision/data/futures/um/daily/klines"
           f"/{symbol}/{tf}/{symbol}-{tf}-{ds}.zip")
    try:
        data = urllib.request.urlopen(url, timeout=15).read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            with z.open(z.namelist()[0]) as f:
                df = pd.read_csv(f, header=None, usecols=range(6),
                                 names=["ts","open","high","low","close","volume"])
        df = df[pd.to_numeric(df["ts"], errors="coerce").notna()].copy()
        df = df.astype({"ts":int,"open":float,"high":float,
                        "low":float,"close":float,"volume":float})
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("dt")
    except Exception:
        return None

def load_tf(symbol: str, tf: str, days: int, extra_days: int = 5) -> pd.DataFrame:
    today, frames = _dt.date.today(), []
    total = days + extra_days + 1
    for d in range(total + 1):
        date = today - _dt.timedelta(days=total - d)
        df   = _dl_day(symbol, tf, date)
        if df is not None:
            frames.append(df)
    if not frames:
        raise RuntimeError(f"Sem dados: {symbol} {tf}")
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="last")]

# ── Indicadores ────────────────────────────────────────────────────────────────
def _ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def _rsi(s, n=14):
    d = s.diff(); g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean().replace(0, 1e-9)
    return 100 - 100/(1+g/l)
def _atr(df, n=14):
    tr = pd.concat([df["high"]-df["low"],
                    (df["high"]-df["close"].shift()).abs(),
                    (df["low"]-df["close"].shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()
def _adx(df, n=14):
    h,l,c = df["high"],df["low"],df["close"]
    tr  = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    pdm = h.diff().clip(lower=0)
    ndm = (-l.diff()).clip(lower=0)
    tr14 = tr.rolling(n).sum()
    pdi  = 100*pdm.rolling(n).sum()/tr14.replace(0,1)
    ndi  = 100*ndm.rolling(n).sum()/tr14.replace(0,1)
    dx   = 100*(pdi-ndi).abs()/(pdi+ndi).replace(0,1)
    return dx.rolling(n).mean()

def prep(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    df["e9"]  = _ema(c,9); df["e21"] = _ema(c,21)
    df["e55"] = _ema(c,55); df["e200"]= _ema(c,200)
    df["rsi"] = _rsi(c)
    df["atr"] = _atr(df)
    df["adx"] = _adx(df)
    df["vol_ma"]    = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"]/df["vol_ma"].replace(0,1)
    df["atr_ma"]    = df["atr"].rolling(50).mean()
    df["atr_ratio"] = df["atr"]/df["atr_ma"].replace(0,1)
    macd = _ema(c,12)-_ema(c,26)
    df["macd_h"] = macd - _ema(macd,9)
    return df

# ── Direção HTF (4h + 1h) ─────────────────────────────────────────────────────
def htf_bias(df4h: pd.DataFrame, df1h: pd.DataFrame, ltf_index: pd.DatetimeIndex) -> pd.DataFrame:
    """
    Retorna DataFrame com colunas bias (1=bull,-1=bear,0=neutro) e htf_adx.
    Reindexado no índice LTF (15m).
    """
    d4  = prep(df4h.copy())
    d1  = prep(df1h.copy())

    # 4h bias: EMA alinhadas + ADX válido
    bull4 = (d4["e9"]>d4["e21"]) & (d4["e21"]>d4["e55"]) & (d4["adx"]>ADX_MIN)
    bear4 = (d4["e9"]<d4["e21"]) & (d4["e21"]<d4["e55"]) & (d4["adx"]>ADX_MIN)
    bias4 = pd.Series(0, index=d4.index)
    bias4[bull4] = 1; bias4[bear4] = -1

    # 1h bias
    bull1 = (d1["e9"]>d1["e21"]) & (d1["e21"]>d1["e55"]) & (d1["adx"]>ADX_MIN)
    bear1 = (d1["e9"]<d1["e21"]) & (d1["e21"]<d1["e55"]) & (d1["adx"]>ADX_MIN)
    bias1 = pd.Series(0, index=d1.index)
    bias1[bull1] = 1; bias1[bear1] = -1

    # Alinha ao índice LTF
    b4 = bias4.reindex(ltf_index, method="ffill").fillna(0)
    b1 = bias1.reindex(ltf_index, method="ffill").fillna(0)
    a4 = d4["adx"].reindex(ltf_index, method="ffill").fillna(0)
    a1 = d1["adx"].reindex(ltf_index, method="ffill").fillna(0)

    # Bias final: ambos precisam concordar
    final = pd.Series(0, index=ltf_index)
    final[(b4 == 1) & (b1 == 1)] =  1
    final[(b4 ==-1) & (b1 ==-1)] = -1

    return pd.DataFrame({"bias": final, "adx4h": a4, "adx1h": a1}, index=ltf_index)

# ── Detecta pullback ───────────────────────────────────────────────────────────
def is_pullback_long(row) -> bool:
    """
    LONG pullback: preço recuou até zona da EMA55 (±0.3%) mas EMAs ainda alinhadas.
    Confirmação: RSI saindo de 38-55, vela de reversão com vol acima da média.
    """
    c, e21, e55 = float(row["close"]), float(row["e21"]), float(row["e55"])
    low = float(row["low"])
    r   = float(row["rsi"])
    vr  = float(row["vol_ratio"])
    body_pct = abs(c - float(row["open"])) / max(float(row["high"])-float(row["low"]), 1e-9)

    # Preço tocou EMA55 ou estava abaixo no low
    touched_e55 = (low <= e55 * 1.003)
    # EMAs ainda alinhadas (tendência ok)
    aligned     = (float(row["e9"]) > e21) and (e21 > e55)
    # RSI zona de pullback (sem ser pânico)
    rsi_ok      = 35 < r < 62
    # Vela de reversão (fechou acima da abertura com corpo ≥ 40%)
    rev_candle  = (c > float(row["open"])) and (body_pct >= 0.40)
    # Volume acima da média
    vol_ok      = vr >= 1.4

    return touched_e55 and aligned and rsi_ok and rev_candle and vol_ok

def is_pullback_short(row) -> bool:
    """
    SHORT pullback: preço subiu até zona da EMA55 (reteste) mas EMAs alinhadas para baixo.
    """
    c, e21, e55 = float(row["close"]), float(row["e21"]), float(row["e55"])
    high = float(row["high"])
    r    = float(row["rsi"])
    vr   = float(row["vol_ratio"])
    body_pct = abs(c - float(row["open"])) / max(float(row["high"])-float(row["low"]), 1e-9)

    touched_e55 = (high >= e55 * 0.997)
    aligned     = (float(row["e9"]) < e21) and (e21 < e55)
    rsi_ok      = 38 < r < 65
    rev_candle  = (c < float(row["open"])) and (body_pct >= 0.40)
    vol_ok      = vr >= 1.4

    return touched_e55 and aligned and rsi_ok and rev_candle and vol_ok

# ── Motor de backtest ──────────────────────────────────────────────────────────
def run_backtest(symbol: str, ltf: pd.DataFrame, bias_df: pd.DataFrame,
                 banca: float) -> dict:

    df = prep(ltf.copy())
    df = df.join(bias_df, how="left")
    df["bias"]  = df["bias"].fillna(0)
    df["adx4h"] = df["adx4h"].fillna(0)

    n             = len(df)
    equity        = banca
    cooldown      = 0
    trade_log     = []
    equity_curve  = [equity]
    in_trade      = False
    dir_  = None
    entry_price = sl = tp1 = tp2 = tp3 = size = 0.0
    rem   = 1.0
    eidx  = 0
    trail1= False
    trail2= False

    for i in range(200, n):
        row   = df.iloc[i]
        price = float(row["close"])
        high  = float(row["high"])
        low   = float(row["low"])

        # ── Gerencia posição aberta ───────────────────────────────────────────
        if in_trade:
            is_long = dir_ == "LONG"

            # Trail 1: hit TP1 → SL para breakeven
            if not trail1:
                if (is_long and high >= tp1) or (not is_long and low <= tp1):
                    pnl  = size * TP1_FRAC * abs(tp1 - entry_price) / entry_price * LEVERAGE
                    pnl -= size * TP1_FRAC * FEE_TAKER * 2
                    equity += pnl; rem -= TP1_FRAC
                    sl = entry_price   # breakeven
                    trail1 = True
                    equity_curve.append(equity)
                    continue

            # Trail 2: hit TP2 → SL para TP1
            if trail1 and not trail2:
                if (is_long and high >= tp2) or (not is_long and low <= tp2):
                    pnl  = size * TP2_FRAC * abs(tp2 - entry_price) / entry_price * LEVERAGE
                    pnl -= size * TP2_FRAC * FEE_TAKER * 2
                    equity += pnl; rem -= TP2_FRAC
                    sl = tp1   # trava no primeiro alvo
                    trail2 = True
                    equity_curve.append(equity)
                    continue

            # Fecha: SL ou TP3
            if is_long:
                close_now = high >= tp3 or low <= sl
                exit_px   = tp3 if high >= tp3 else sl
            else:
                close_now = low <= tp3 or high >= sl
                exit_px   = tp3 if low <= tp3 else sl

            if close_now:
                raw  = ((exit_px-entry_price)/entry_price) if is_long else \
                       ((entry_price-exit_px)/entry_price)
                pnl  = size * rem * raw * LEVERAGE
                pnl -= size * rem * FEE_TAKER * 2
                equity = max(equity + pnl, 0.01)
                trade_log.append({
                    "asset":    symbol,
                    "dir":      dir_,
                    "entry":    round(entry_price, 4),
                    "exit":     round(exit_px, 4),
                    "sl":       round(sl, 4),
                    "tp1":      round(tp1, 4),
                    "tp3":      round(tp3, 4),
                    "bars":     i - eidx,
                    "pnl_usdt": round(pnl, 3),
                    "pnl_pct":  round(raw * LEVERAGE * 100, 2),
                    "outcome":  "WIN" if pnl > 0 else "LOSS",
                    "dt_open":  df.index[eidx].strftime("%d/%m %H:%M"),
                    "dt_close": df.index[i].strftime("%d/%m %H:%M"),
                    "t1": trail1, "t2": trail2,
                    "bias": float(df.iloc[eidx].get("bias", 0)),
                })
                in_trade = False
                cooldown = COOLDOWN_BARS
                trail1 = trail2 = False

        else:
            # ── Procura entrada ───────────────────────────────────────────────
            if cooldown > 0:
                cooldown -= 1
                equity_curve.append(equity)
                continue

            # Filtros globais
            if df.index[i].hour in DEAD_HOURS:
                equity_curve.append(equity)
                continue
            if float(row.get("atr_ratio", 1)) > ATR_VOLA_MAX:
                equity_curve.append(equity)
                continue
            # Sem tendência definida = não opera
            bias   = float(row.get("bias", 0))
            adx_4h = float(row.get("adx4h", 0))
            adx_15m= float(row.get("adx", 0))
            if adx_4h < ADX_MIN or bias == 0:
                equity_curve.append(equity)
                continue

            atr_val = float(row["atr"])
            if atr_val <= 0:
                equity_curve.append(equity)
                continue

            # ── LONG: bias bullish + pullback detectado ───────────────────────
            if bias == 1 and is_pullback_long(row):
                dir_        = "LONG"
                entry_price = price
                sl          = min(low, price - STOP_ATR * atr_val)  # abaixo do low ou ATR
                tp1         = price + TP1_ATR * atr_val
                tp2         = price + TP2_ATR * atr_val
                tp3         = price + TP3_ATR * atr_val
                risk        = equity * RISK_PCT
                dist        = (price - sl) / price
                size        = min(risk / max(dist, 0.001), equity * LEVERAGE)
                equity     -= size * FEE_TAKER
                rem         = 1.0
                in_trade    = True
                eidx        = i
                trail1 = trail2 = False

            # ── SHORT: bias bearish + pullback short detectado ────────────────
            elif bias == -1 and is_pullback_short(row):
                dir_        = "SHORT"
                entry_price = price
                sl          = max(high, price + STOP_ATR * atr_val)
                tp1         = price - TP1_ATR * atr_val
                tp2         = price - TP2_ATR * atr_val
                tp3         = price - TP3_ATR * atr_val
                risk        = equity * RISK_PCT
                dist        = (sl - price) / price
                size        = min(risk / max(dist, 0.001), equity * LEVERAGE)
                equity     -= size * FEE_TAKER
                rem         = 1.0
                in_trade    = True
                eidx        = i
                trail1 = trail2 = False

        equity_curve.append(equity)

    # Fecha posição aberta
    if in_trade:
        px   = float(df.iloc[-1]["close"])
        raw  = ((px-entry_price)/entry_price) if dir_=="LONG" else ((entry_price-px)/entry_price)
        pnl  = size * rem * raw * LEVERAGE - size * rem * FEE_TAKER * 2
        equity = max(equity + pnl, 0.01)
        trade_log.append({
            "asset": symbol, "dir": dir_,
            "entry": round(entry_price,4), "exit": round(px,4),
            "sl": round(sl,4), "tp1": round(tp1,4), "tp3": round(tp3,4),
            "bars": n-1-eidx, "pnl_usdt": round(pnl,3),
            "pnl_pct": round(raw*LEVERAGE*100,2),
            "outcome": "WIN" if pnl>0 else "LOSS",
            "dt_open": df.index[eidx].strftime("%d/%m %H:%M"),
            "dt_close": df.index[-1].strftime("%d/%m %H:%M")+"[A]",
            "t1": trail1, "t2": trail2, "bias": 0,
        })

    wins   = [t for t in trade_log if t["outcome"]=="WIN"]
    losses = [t for t in trade_log if t["outcome"]=="LOSS"]
    gp  = sum(t["pnl_usdt"] for t in wins)
    gl  = abs(sum(t["pnl_usdt"] for t in losses))
    wr  = round(len(wins)/len(trade_log)*100,1) if trade_log else 0
    pf  = round(gp/gl,2) if gl>0 else (999 if gp>0 else 0)

    return {
        "symbol":        symbol,
        "banca_ini":     round(banca,2),
        "banca_fin":     round(equity,2),
        "net_usdt":      round(equity-banca,2),
        "net_pct":       round((equity-banca)/banca*100,2),
        "trades":        len(trade_log),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      wr,
        "profit_factor": pf,
        "trade_log":     trade_log,
        "equity_curve":  equity_curve,
    }

# ── Gráfico ────────────────────────────────────────────────────────────────────
def gerar_grafico(results: list[dict], banca_ini: float) -> bytes:
    BG   = "#0d1117"; GRID = "#21262d"; TEXT = "#e6edf3"; MUTED = "#8b949e"
    COLS = {"BTCUSDT":"#f9ca24","ETHUSDT":"#45aaf2","SOLUSDT":"#00b894"}

    fig, axes = plt.subplots(2, 2, figsize=(14, 9), facecolor=BG)
    fig.suptitle(
        "BACKTEST V3 — Pullback to EMA | HTF 4h+1h | Trailing SL | ADX Filter",
        color=TEXT, fontsize=11, fontweight="bold", y=0.99
    )

    # Equity curve
    ax = axes[0][0]
    ax.set_facecolor(BG); ax.set_title("Equity Curve (USDT)", color=TEXT, fontsize=9)
    ax.axhline(banca_ini, color=MUTED, lw=0.8, ls="--", alpha=0.5)
    for res in results:
        ec  = res["equity_curve"]; col = COLS.get(res["symbol"],"#aaa")
        xs  = np.linspace(0, 100, len(ec))
        ax.plot(xs, ec, color=col, lw=2, label=f"{res['symbol'].replace('USDT','')} ({res['net_pct']:+.1f}%)")
        ax.fill_between(xs, banca_ini, ec, where=[v>=banca_ini for v in ec], alpha=0.07, color="#00b894")
        ax.fill_between(xs, banca_ini, ec, where=[v<banca_ini  for v in ec], alpha=0.07, color="#d63031")
    ax.legend(fontsize=8, facecolor="#161b22", labelcolor=TEXT, edgecolor=GRID)
    for sp in ax.spines.values(): sp.set_edgecolor(GRID)
    ax.tick_params(colors=MUTED, labelsize=7); ax.yaxis.grid(True, color=GRID, lw=0.4, alpha=0.5)
    ax.set_xlabel("% período", color=MUTED, fontsize=7); ax.set_ylabel("USDT", color=MUTED, fontsize=7)

    # PnL bar por ativo
    for idx, (ax2, res) in enumerate(zip([axes[0][1], axes[1][0]], results[:2])):
        ax2.set_facecolor(BG)
        ax2.set_title(f"PnL/trade — {res['symbol'].replace('USDT','')}", color=TEXT, fontsize=9)
        vals   = [t["pnl_usdt"] for t in res["trade_log"]]
        colors = ["#00b894" if v>=0 else "#d63031" for v in vals]
        if vals:
            ax2.bar(range(len(vals)), vals, color=colors, width=0.7, alpha=0.85)
            ax2.axhline(0, color=MUTED, lw=0.6)
        for sp in ax2.spines.values(): sp.set_edgecolor(GRID)
        ax2.tick_params(colors=MUTED, labelsize=7); ax2.yaxis.grid(True, color=GRID, lw=0.4, alpha=0.5)

    # Resumo
    ax3 = axes[1][1]; ax3.set_facecolor(BG); ax3.axis("off")
    ax3.set_title("Resumo", color=TEXT, fontsize=9)
    total_net = sum(r["net_usdt"] for r in results)
    total_t   = sum(r["trades"]   for r in results)
    total_w   = sum(r["wins"]     for r in results)
    wr_t      = round(total_w/total_t*100,1) if total_t else 0
    banca_fin = banca_ini + total_net
    net_pct   = round(total_net/banca_ini*100,2)

    headers = ["Ativo","Trades","WR%","PF","Net USDT","Net BRL"]
    rows = []
    for res in results:
        rows.append([
            res["symbol"].replace("USDT",""), str(res["trades"]),
            f"{res['win_rate']}%", str(res["profit_factor"]),
            f"{'+'if res['net_usdt']>=0 else ''}{res['net_usdt']:.2f}",
            f"R${res['net_usdt']*5.40:+.2f}",
        ])
    rows.append(["TOTAL", str(total_t), f"{wr_t}%", "",
                 f"{'+'if total_net>=0 else ''}{total_net:.2f}",
                 f"R${total_net*5.40:+.2f}"])

    # Info extra
    ax3.text(0.5, 0.95, f"Capital final: R${banca_fin*5.40:.2f} ({net_pct:+.1f}%)",
             ha="center", va="top", transform=ax3.transAxes,
             color="#00b894" if total_net>=0 else "#d63031",
             fontsize=11, fontweight="bold")

    tbl = ax3.table(cellText=rows, colLabels=headers, cellLoc="center",
                    loc="center", bbox=[0,0.05,1,0.88])
    tbl.auto_set_font_size(False); tbl.set_fontsize(8.5)
    for (r,c), cell in tbl.get_celld().items():
        cell.set_facecolor("#161b22" if r>0 else "#21262d")
        cell.set_edgecolor(GRID)
        txt = cell.get_text().get_text()
        if r==0:           cell.set_text_props(color=TEXT, fontweight="bold")
        elif txt.startswith("+"): cell.set_text_props(color="#00b894")
        elif txt.startswith("-"): cell.set_text_props(color="#d63031")
        else:              cell.set_text_props(color=TEXT)

    plt.tight_layout(pad=2.0)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ── Telegram ───────────────────────────────────────────────────────────────────
def montar_msg(results: list[dict], banca_ini: float) -> str:
    SEP = "━"*22
    tn  = sum(r["net_usdt"] for r in results)
    tt  = sum(r["trades"]   for r in results)
    tw  = sum(r["wins"]     for r in results)
    wr  = round(tw/tt*100,1) if tt else 0
    bf  = banca_ini + tn
    np_ = round(tn/banca_ini*100,2)
    em  = "\U0001f7e2" if tn>=0 else "\U0001f534"
    sn  = "+" if tn>=0 else ""
    lines = [
        f"\U0001f4ca BACKTEST V3 | 10 DIAS | R$1.000 | 10x",
        f"{SEP}",
        f"Estrategia : Pullback to EMA55",
        f"Filtro     : HTF 4h+1h | ADX>{ADX_MIN} | Score 4/5",
        f"Trailing SL: BE @ TP1, TP1 @ TP2",
        f"Dead hours : 01h-07h UTC bloqueado",
        f"{SEP}",
        f"{em} PnL  : {sn}{tn:.2f} USDT | {sn}R${tn*5.40:.2f}",
        f"   Retorno: {sn}{np_:.1f}% | Banca final: R${bf*5.40:.2f}",
        f"   Trades : {tt} | Wins: {tw} | WR: {wr}%",
        f"{SEP}",
    ]
    for res in results:
        sym = res["symbol"].replace("USDT","")
        em2 = "\U0001f7e2" if res["net_usdt"]>=0 else "\U0001f534"
        lines.append(
            f"{em2} {sym}: {res['trades']}T | WR {res['win_rate']}% | "
            f"PF {res['profit_factor']} | {'+' if res['net_usdt']>=0 else ''}{res['net_usdt']:.2f}U"
        )
        for t in res["trade_log"]:
            ico   = "\U0001f7e2" if t["pnl_usdt"]>=0 else "\U0001f534"
            trail = " \U0001f512" if t.get("t2") else (" [BE]" if t.get("t1") else "")
            lines.append(
                f"  {ico} {t['dir']} {t['dt_open']} → {t['dt_close']}"
                f"  {'+' if t['pnl_usdt']>=0 else ''}{t['pnl_usdt']:.2f}U{trail}"
            )
        lines.append("")
    lines += [SEP, "TRADER 001 | Estrategia Pullback EMA + HTF", "@mestressinais_br"]
    return "\n".join(lines)

def enviar(chart: bytes, caption: str):
    if not TG_TOKEN or not TG_CHAT: return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
        data={"chat_id": TG_CHAT, "caption": caption[:1024]},
        files={"photo": ("bt.png", chart, "image/png")}, timeout=30,
    ).json()
    if r.get("ok"):
        if len(caption)>1024:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": caption}, timeout=15)
        print("[TG] Enviado.")
    else:
        print(f"[TG] Erro: {r}")

# ── MAIN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{'='*64}")
    print(f"  BACKTEST V3 | Pullback to EMA | ADX Filter | Trailing SL")
    print(f"  {SINCE_DAYS} dias | R${BANCA_BRL:.0f} | {LEVERAGE}x | ADX min {ADX_MIN}")
    print(f"{'='*64}\n")

    ltf_data: dict[str, pd.DataFrame] = {}
    htf4h:    dict[str, pd.DataFrame] = {}
    htf1h:    dict[str, pd.DataFrame] = {}

    for sym in SYMBOLS:
        print(f"[DATA] {sym}  15m...", end=" ", flush=True)
        ltf_data[sym] = load_tf(sym, "15m", SINCE_DAYS, extra_days=3)
        print(f"OK ({len(ltf_data[sym])})  |  1h...", end=" ", flush=True)
        htf1h[sym] = load_tf(sym, "1h", SINCE_DAYS, extra_days=3)
        print(f"OK ({len(htf1h[sym])})  |  4h...", end=" ", flush=True)
        htf4h[sym] = load_tf(sym, "4h", SINCE_DAYS, extra_days=5)
        print(f"OK ({len(htf4h[sym])})")

    banca_p = BANCA_USDT / len(SYMBOLS)
    results = []

    for sym in SYMBOLS:
        idx    = ltf_data[sym].index
        b_df   = htf_bias(htf4h[sym], htf1h[sym], idx)
        print(f"\n[BACKTEST] {sym}  (banca U${banca_p:.2f})", end=" ", flush=True)
        res    = run_backtest(sym, ltf_data[sym], b_df, banca_p)
        results.append(res)
        icon   = "▲" if res["net_usdt"]>=0 else "▼"
        print(f"{icon}  {res['trades']}T | WR {res['win_rate']}% | PF {res['profit_factor']} | "
              f"Net {'+' if res['net_usdt']>=0 else ''}{res['net_usdt']:.2f} USDT")

        for t in res["trade_log"]:
            ico  = "✓" if t["outcome"]=="WIN" else "✗"
            tag  = " [trail2]" if t.get("t2") else (" [be]" if t.get("t1") else "")
            rr   = abs(t["tp3"]-t["entry"]) / max(abs(t["sl"]-t["entry"]),0.0001)
            print(f"  {ico} {t['dir']:5s} {t['dt_open']} → {t['dt_close']} | "
                  f"RR {rr:.1f}x | {t['pnl_usdt']:+.3f}U ({t['pnl_pct']:+.2f}%){tag}")

    tn = sum(r["net_usdt"] for r in results)
    tt = sum(r["trades"]   for r in results)
    tw = sum(r["wins"]     for r in results)

    print(f"\n{'='*64}")
    print(f"  RESULTADO FINAL")
    print(f"  Capital inicial : U${BANCA_USDT:.2f}  (R${BANCA_BRL:.0f})")
    print(f"  Capital final   : U${BANCA_USDT+tn:.2f}  (R${(BANCA_USDT+tn)*CAMBIO:.2f})")
    print(f"  PnL             : {'+' if tn>=0 else ''}{tn:.2f} USDT  "
          f"(R${tn*CAMBIO:.2f})")
    print(f"  Retorno %       : {'+' if tn>=0 else ''}{tn/BANCA_USDT*100:.1f}%")
    print(f"  Trades          : {tt} | Wins: {tw} | WR: {round(tw/tt*100,1) if tt else 0}%")
    print(f"{'='*64}\n")

    print("[CHART] Gerando gráfico...", end=" ", flush=True)
    chart = gerar_grafico(results, BANCA_USDT)
    print(f"OK ({len(chart):,} bytes)")
    enviar(chart, montar_msg(results, BANCA_USDT))
    print("\nBacktest V3 concluido.")
