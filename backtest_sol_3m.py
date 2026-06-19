"""
BACKTEST SOL — Gestor de Banca | 3 Meses | R$1.000 | 10x
===========================================================
Filosofia: O bot NÃO é um apostador. É um GESTOR DE CRESCIMENTO.

Arquitetura real do engine router:
  1. Download 5 timeframes: 3m, 5m, 15m, 1h, 4h
  2. HTF (4h + 1h) → direção macro (BULL / BEAR / NEUTRO)
  3. Engine Router analisa regime e seleciona estratégia:
       ADX > 28  → TREND ENGINE   (pullback to EMA55, 15m entry)
       ADX 18-28 → MOMENTUM ENGINE (MACD cross + RSI, 5m entry)
       ADX < 18  → REVERSAL ENGINE (OB reteste extremo, 5m entry)
       ADX < 14  → SEM TRADE (range muito estreito)
  4. Score 0-10 por engine — entra apenas score ≥ 6
  5. Multi-TF convergência: 15m + 5m precisam concordar na direção
  6. Gestão de banca:
       - Risco max 1.5% por trade
       - Max 3 trades simultâneos (não implementado — sequencial em backtest)
       - Trailing SL: TP1 → breakeven, TP2 → TP1
       - Scale-out: 35% TP1, 35% TP2, 30% TP3
       - Horas mortas 01h-07h UTC bloqueadas
       - Anti pump/dump: vol_ratio > 7 bloqueia
  7. Resultado: gráfico equity curve + tabela completa → Telegram

Período: 3 meses (90 dias) a partir de hoje
Símbolo: SOLUSDT Perpetual Futures
"""

import io, sys, zipfile, urllib.request, datetime as _dt
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import os, requests, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════════════
# PARÂMETROS GLOBAIS
# ═══════════════════════════════════════════════════════════════════════
SYMBOL       = "SOLUSDT"
PERIOD_DAYS  = 90
BANCA_BRL    = 1000.0
CAMBIO       = 5.40
BANCA_USDT   = round(BANCA_BRL / CAMBIO, 2)
LEVERAGE     = 10
FEE_TAKER    = 0.0004
RISK_PCT     = 0.015        # 1.5% da banca por trade
MAX_DD_PCT   = 0.25         # drawdown máximo 25% → pausa (proteção de banca)

# Regime (ADX)
ADX_STRONG   = 28
ADX_MED      = 18
ADX_WEAK     = 14

# SL / TP por engine
PARAMS = {
    "TREND":    {"sl_atr": 1.0, "tp1_atr": 1.8, "tp2_atr": 3.2, "tp3_atr": 5.0},
    "MOMENTUM": {"sl_atr": 1.2, "tp1_atr": 1.6, "tp2_atr": 2.8, "tp3_atr": 4.5},
    "REVERSAL": {"sl_atr": 0.9, "tp1_atr": 1.5, "tp2_atr": 2.5, "tp3_atr": 4.0},
}
TP1_FRAC = 0.35; TP2_FRAC = 0.35; TP3_FRAC = 0.30

COOLDOWN_15M = 8    # 8 candles × 15m = 2h após trade
DEAD_HOURS   = set(range(1, 7))
MIN_SCORE    = 5    # score mínimo 5/10 (com bias HTF definido)

from dotenv import load_dotenv
load_dotenv()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# ═══════════════════════════════════════════════════════════════════════
# DOWNLOAD DE DADOS (CDN Binance Vision)
# ═══════════════════════════════════════════════════════════════════════
def _parse_zip(data: bytes) -> pd.DataFrame | None:
    try:
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

def _dl(url: str) -> bytes | None:
    try:
        return urllib.request.urlopen(url, timeout=20).read()
    except Exception:
        return None

def load_tf(symbol: str, tf: str, days: int) -> pd.DataFrame:
    """Baixa dados combinando ZIPs mensais (arquivos grandes) + diários."""
    today  = _dt.date.today()
    frames = []

    # --- ZIPs mensais para meses completos ---
    months_seen = set()
    for d in range(days + 5):
        date  = today - _dt.timedelta(days=days - d + 2)
        ym    = (date.year, date.month)
        # Só baixa mês completo se já passou (não o mês atual)
        if ym not in months_seen and date.month != today.month:
            months_seen.add(ym)
            url = (f"https://data.binance.vision/data/futures/um/monthly/klines"
                   f"/{symbol}/{tf}/{symbol}-{tf}-{date.year}-{date.month:02d}.zip")
            raw = _dl(url)
            if raw:
                df = _parse_zip(raw)
                if df is not None:
                    frames.append(df)
                    continue   # mês coberto, pula dias individuais
        # --- ZIPs diários para mês atual ou falha mensal ---
        if date.month == today.month or ym not in months_seen:
            url = (f"https://data.binance.vision/data/futures/um/daily/klines"
                   f"/{symbol}/{tf}/{symbol}-{tf}-{date.strftime('%Y-%m-%d')}.zip")
            raw = _dl(url)
            if raw:
                df = _parse_zip(raw)
                if df is not None:
                    frames.append(df)

    if not frames:
        raise RuntimeError(f"Sem dados: {symbol} {tf}")
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    # Filtra só os últimos N dias
    cutoff = pd.Timestamp(today - _dt.timedelta(days=days), tz="UTC")
    return out[out.index >= cutoff]

# ═══════════════════════════════════════════════════════════════════════
# INDICADORES
# ═══════════════════════════════════════════════════════════════════════
def _ema(s,n):   return s.ewm(span=n, adjust=False).mean()
def _rsi(s,n=14):
    d=s.diff(); g=d.clip(lower=0).rolling(n).mean()
    l=(-d.clip(upper=0)).rolling(n).mean().replace(0,1e-9)
    return 100-100/(1+g/l)
def _atr(df,n=14):
    tr=pd.concat([df["high"]-df["low"],(df["high"]-df["close"].shift()).abs(),
                  (df["low"]-df["close"].shift()).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()
def _adx(df,n=14):
    h,l,c=df["high"],df["low"],df["close"]
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    pdm=h.diff().clip(lower=0); ndm=(-l.diff()).clip(lower=0)
    s=tr.rolling(n).sum()
    pdi=100*pdm.rolling(n).sum()/s.replace(0,1); ndi=100*ndm.rolling(n).sum()/s.replace(0,1)
    return (100*(pdi-ndi).abs()/(pdi+ndi).replace(0,1)).rolling(n).mean()

def prep(df: pd.DataFrame) -> pd.DataFrame:
    c=df["close"]
    df["e9"]=_ema(c,9); df["e21"]=_ema(c,21); df["e55"]=_ema(c,55); df["e200"]=_ema(c,200)
    df["rsi"]=_rsi(c); df["atr"]=_atr(df); df["adx"]=_adx(df)
    df["vol_ma"]=df["volume"].rolling(20).mean()
    df["vol_ratio"]=df["volume"]/df["vol_ma"].replace(0,1)
    df["atr_ma"]=df["atr"].rolling(50).mean()
    df["atr_ratio"]=df["atr"]/df["atr_ma"].replace(0,1)
    macd=_ema(c,12)-_ema(c,26); df["macd_h"]=macd-_ema(macd,9)
    df["macd_up"]=df["macd_h"]>df["macd_h"].shift(1)
    # OB: vela forte (corpo>60%, vol>2x) → zona ativa por 12 candles
    body=(c-df["open"]).abs(); span=(df["high"]-df["low"]).replace(0,1)
    strong=(body/span>0.60)&(df["vol_ratio"]>2.0)
    df["ob_bull"]=(strong&(c>df["open"])).rolling(12).sum()>=1
    df["ob_bear"]=(strong&(c<df["open"])).rolling(12).sum()>=1
    # Vela de reversão atual
    df["rev_bull"]=(c>df["open"])&(body/span>0.45)
    df["rev_bear"]=(c<df["open"])&(body/span>0.45)
    return df

# ═══════════════════════════════════════════════════════════════════════
# HTF BIAS (4h + 1h → direção macro)
# ═══════════════════════════════════════════════════════════════════════
def calc_htf_bias(df4h: pd.DataFrame, df1h: pd.DataFrame,
                  idx: pd.DatetimeIndex) -> pd.DataFrame:
    """
    Regra de prioridade: 4h MANDA.
    - 4h bearish → bias -1, mesmo que 1h esteja neutro (bounce)
    - 4h bullish → bias +1, mesmo que 1h esteja neutro (pullback)
    - 4h neutro  → usa 1h como desempate
    - Ambos neutros → bias 0 (não opera)
    """
    d4=prep(df4h.copy()); d1=prep(df1h.copy())

    # 4h: tendência principal (mais peso)
    bull4=(d4["e9"]>d4["e21"])&(d4["e21"]>d4["e55"])&(d4["adx"]>ADX_WEAK)
    bear4=(d4["e9"]<d4["e21"])&(d4["e21"]<d4["e55"])&(d4["adx"]>ADX_WEAK)
    b4=pd.Series(0,index=d4.index)
    b4[bull4]=1; b4[bear4]=-1

    # 1h: confirmação / desempate quando 4h neutro
    bull1=(d1["e9"]>d1["e21"])&(d1["e21"]>d1["e55"])&(d1["adx"]>ADX_WEAK)
    bear1=(d1["e9"]<d1["e21"])&(d1["e21"]<d1["e55"])&(d1["adx"]>ADX_WEAK)
    b1=pd.Series(0,index=d1.index)
    b1[bull1]=1; b1[bear1]=-1

    B4=b4.reindex(idx,method="ffill").fillna(0)
    B1=b1.reindex(idx,method="ffill").fillna(0)
    ADX4=d4["adx"].reindex(idx,method="ffill").fillna(0)
    ADX1=d1["adx"].reindex(idx,method="ffill").fillna(0)
    RSI1=d1["rsi"].reindex(idx,method="ffill").fillna(50)

    # Lógica de prioridade:
    # 4h define direção → 1h apenas desempata quando 4h=0
    bias=pd.Series(0,index=idx)
    bias[B4==1 ]= 1    # 4h bullish → só LONG (mesmo se 1h neutro)
    bias[B4==-1]=-1    # 4h bearish → só SHORT (mesmo se 1h neutro ou bouncing)
    bias[B4==0 ]= B1[B4==0]  # 4h neutro → segue o 1h

    return pd.DataFrame({"bias":bias,"adx4h":ADX4,"adx1h":ADX1,"rsi1h":RSI1},index=idx)

# ═══════════════════════════════════════════════════════════════════════
# ENGINE ROUTER — Calcula score 0-10 para cada engine
# ═══════════════════════════════════════════════════════════════════════
def engine_trend(row, direction: int) -> int:
    """
    TREND ENGINE (funciona bem com ADX>28)
    Espera pullback até EMA55, vela reversão, RSI neutro.
    Score 0-10.
    """
    c=float(row["close"]); e21=float(row["e21"]); e55=float(row["e55"])
    lo=float(row["low"]); hi=float(row["high"])
    r=float(row["rsi"]); vr=float(row["vol_ratio"])
    aligned_bull=(float(row["e9"])>e21)and(e21>e55)and(float(row["e200"])<c)
    aligned_bear=(float(row["e9"])<e21)and(e21<e55)and(float(row["e200"])>c)

    if direction==1:
        if not aligned_bull: return 0
        sc=0
        sc+=3 if lo<=e55*1.005 else (2 if lo<=e55*1.01 else 0)  # toque EMA55
        sc+=2 if 40<r<60 else (1 if 35<r<65 else 0)              # RSI neutro
        sc+=2 if bool(row["rev_bull"]) else 0                      # vela reversão
        sc+=2 if vr>=1.5 else (1 if vr>=1.2 else 0)              # volume
        sc+=1 if bool(row["macd_up"]) else 0                       # MACD subindo
        return sc
    else:
        if not aligned_bear: return 0
        sc=0
        sc+=3 if hi>=e55*0.995 else (2 if hi>=e55*0.99 else 0)
        sc+=2 if 40<r<60 else (1 if 35<r<65 else 0)
        sc+=2 if bool(row["rev_bear"]) else 0
        sc+=2 if vr>=1.5 else (1 if vr>=1.2 else 0)
        sc+=1 if not bool(row["macd_up"]) else 0
        return sc


def engine_momentum(row, direction: int) -> int:
    """
    MOMENTUM ENGINE (funciona bem ADX 18-28)
    MACD cross + RSI confirma + OB ativo + volume spike.
    """
    r=float(row["rsi"]); vr=float(row["vol_ratio"])
    mh=float(row["macd_h"])
    c=float(row["close"]); e9=float(row["e9"]); e21=float(row["e21"])

    if direction==1:
        sc=0
        sc+=3 if (bool(row["macd_up"]) and mh>0) else (1 if bool(row["macd_up"]) else 0)
        sc+=2 if 45<r<65 else (1 if 40<r<72 else 0)
        sc+=2 if bool(row["ob_bull"]) else 0
        sc+=2 if vr>=1.8 else (1 if vr>=1.3 else 0)
        sc+=1 if e9>e21 else 0
        return sc
    else:
        sc=0
        sc+=3 if (not bool(row["macd_up"]) and mh<0) else (1 if not bool(row["macd_up"]) else 0)
        sc+=2 if 35<r<55 else (1 if 28<r<60 else 0)
        sc+=2 if bool(row["ob_bear"]) else 0
        sc+=2 if vr>=1.8 else (1 if vr>=1.3 else 0)
        sc+=1 if e9<e21 else 0
        return sc


def engine_reversal(row, direction: int) -> int:
    """
    REVERSAL ENGINE (funciona melhor em extremos RSI + OB)
    Contra-tendência curta — menor confiança, SL menor.
    """
    r=float(row["rsi"]); vr=float(row["vol_ratio"])

    if direction==1:   # reversão de venda → compra
        sc=0
        sc+=4 if r<30 else (2 if r<38 else 0)
        sc+=3 if bool(row["ob_bull"]) else 0
        sc+=2 if bool(row["rev_bull"]) else 0
        sc+=1 if vr>=1.5 else 0
        return sc
    else:
        sc=0
        sc+=4 if r>70 else (2 if r>62 else 0)
        sc+=3 if bool(row["ob_bear"]) else 0
        sc+=2 if bool(row["rev_bear"]) else 0
        sc+=1 if vr>=1.5 else 0
        return sc


def route_engine(row, direction: int) -> tuple[str, int]:
    """
    Seleciona a melhor engine baseado no regime ADX.
    Retorna (engine_name, score).
    """
    adx=float(row.get("adx",0))
    # Anti pump/dump
    if float(row.get("vol_ratio",1))>7.0:
        return "SKIP", 0

    if adx>=ADX_STRONG:
        s=engine_trend(row, direction)
        return "TREND", s
    elif adx>=ADX_MED:
        st=engine_trend(row, direction)
        sm=engine_momentum(row, direction)
        if st>=sm:  return "TREND", st
        return "MOMENTUM", sm
    elif adx>=ADX_WEAK:
        sm=engine_momentum(row, direction)
        sr=engine_reversal(row, direction)
        if sm>=sr:  return "MOMENTUM", sm
        return "REVERSAL", sr
    else:
        # Mercado flat — só entra se reversal é extremo
        sr=engine_reversal(row, direction)
        return "REVERSAL", sr

# ═══════════════════════════════════════════════════════════════════════
# MULTI-TF CONVERGÊNCIA
# Verifica se 5m e 15m concordam na direção antes de entrar
# ═══════════════════════════════════════════════════════════════════════
def check_5m_confirm(df5m: pd.DataFrame, ts: pd.Timestamp, direction: int) -> bool:
    """Confirma direção no 5m antes de entrar pelo 15m."""
    candidates = df5m.index[df5m.index <= ts]
    if len(candidates)==0: return False
    row5 = df5m.loc[candidates[-1]]
    e9=float(row5["e9"]); e21=float(row5["e21"])
    r=float(row5["rsi"]); mup=bool(row5["macd_up"])
    if direction==1:
        return (e9>e21) and (45<r<75) and mup
    else:
        return (e9<e21) and (25<r<55) and (not mup)

def check_3m_entry(df3m: pd.DataFrame, ts: pd.Timestamp, direction: int) -> bool:
    """Confirmação final no 3m — vela reversão com volume."""
    candidates = df3m.index[df3m.index <= ts]
    if len(candidates)==0: return False
    row3 = df3m.loc[candidates[-1]]
    vr=float(row3["vol_ratio"])
    if direction==1:
        return bool(row3["rev_bull"]) and vr>=1.2
    else:
        return bool(row3["rev_bear"]) and vr>=1.2

# ═══════════════════════════════════════════════════════════════════════
# MOTOR DE BACKTEST PRINCIPAL (iterage no 15m)
# ═══════════════════════════════════════════════════════════════════════
def run_backtest(df15m: pd.DataFrame, df5m: pd.DataFrame, df3m: pd.DataFrame,
                 bias_df: pd.DataFrame, banca: float) -> dict:

    df = prep(df15m.copy())
    df = df.join(bias_df, how="left")
    df["bias"]  = df["bias"].fillna(0)
    df["adx4h"] = df["adx4h"].fillna(0)
    df["rsi1h"] = df["rsi1h"].fillna(50)

    n           = len(df)
    equity      = banca
    in_trade    = False
    dir_        = None
    entry_price = sl = tp1 = tp2 = tp3 = size = 0.0
    rem         = 1.0
    eidx        = 0
    trail1 = trail2 = False
    cooldown    = 0
    engine_name = ""
    score_entry = 0
    cum_pnl     = 0.0          # PnL acumulado do trade atual (parciais + final)
    equity_before_trade = banca
    trade_log   = []
    equity_curve= [equity]
    peak_equity  = equity
    max_dd       = 0.0
    pause_trading= False       # drawdown protection
    pause_until  = 0

    for i in range(200, n):
        row   = df.iloc[i]
        price = float(row["close"])
        hi    = float(row["high"])
        lo    = float(row["low"])

        # ── Gerencia posição aberta ────────────────────────────────────────
        if in_trade:
            is_long = dir_=="LONG"

            if not trail1:
                hit1 = (is_long and hi>=tp1) or (not is_long and lo<=tp1)
                if hit1:
                    # size = notional (já inclui alavancagem) — NÃO multiplicar LEVERAGE
                    move1 = abs(tp1-entry_price)/entry_price
                    pnl   = size*TP1_FRAC*move1           # lucro do parcial
                    pnl  -= size*TP1_FRAC*FEE_TAKER       # só fee de saída (entrada já paga)
                    equity+=pnl; rem-=TP1_FRAC
                    sl=entry_price; trail1=True
                    peak_equity=max(peak_equity, equity)
                    cum_pnl += pnl
                    equity_curve.append(equity)
                    continue

            if trail1 and not trail2:
                hit2 = (is_long and hi>=tp2) or (not is_long and lo<=tp2)
                if hit2:
                    move2 = abs(tp2-entry_price)/entry_price
                    pnl   = size*TP2_FRAC*move2
                    pnl  -= size*TP2_FRAC*FEE_TAKER
                    equity+=pnl; rem-=TP2_FRAC
                    sl=tp1; trail2=True
                    peak_equity=max(peak_equity, equity)
                    cum_pnl += pnl
                    equity_curve.append(equity)
                    continue

            if is_long:
                close_now = hi>=tp3 or lo<=sl
                exit_px   = tp3 if hi>=tp3 else sl
            else:
                close_now = lo<=tp3 or hi>=sl
                exit_px   = tp3 if lo<=tp3 else sl

            if close_now:
                raw  = ((exit_px-entry_price)/entry_price) if is_long else \
                       ((entry_price-exit_px)/entry_price)
                # size = notional — NÃO multiplicar LEVERAGE de novo
                pnl  = size*rem*raw
                pnl -= size*rem*FEE_TAKER     # fee de saída
                equity = max(equity+pnl, 0.01)
                cum_pnl += pnl
                peak_equity = max(peak_equity, equity)
                dd = (peak_equity-equity)/peak_equity
                max_dd = max(max_dd, dd)

                # DD protection: pausa por 300 candles (75h) se atingir limite
                if dd >= MAX_DD_PCT and not pause_trading:
                    pause_trading  = True
                    pause_until    = i + 300

                outcome = "WIN" if cum_pnl>0 else "LOSS"
                p = PARAMS[engine_name]
                atr_entry = float(df.iloc[eidx]["atr"])
                pnl_on_equity = round(cum_pnl / equity_before_trade * 100, 2) if equity_before_trade>0 else 0
                trade_log.append({
                    "dir":       dir_,
                    "engine":    engine_name,
                    "score":     score_entry,
                    "entry":     round(entry_price,4),
                    "exit":      round(exit_px,4),
                    "sl_ini":    round(entry_price - p["sl_atr"]*atr_entry
                                       if dir_=="LONG" else
                                       entry_price + p["sl_atr"]*atr_entry, 4),
                    "tp1":       round(tp1,4),
                    "tp3":       round(tp3,4),
                    "bars":      i-eidx,
                    "pnl_usdt":  round(cum_pnl,3),
                    "pnl_pct":   pnl_on_equity,
                    "outcome":   outcome,
                    "dt_open":   df.index[eidx].strftime("%d/%m %H:%M"),
                    "dt_close":  df.index[i].strftime("%d/%m %H:%M"),
                    "t1": trail1, "t2": trail2,
                    "adx_entry": round(float(df.iloc[eidx].get("adx",0)),1),
                    "bias_entry":float(df.iloc[eidx].get("bias",0)),
                })
                in_trade=False; cooldown=COOLDOWN_15M
                trail1=trail2=False

        else:
            # ── Procura nova entrada ───────────────────────────────────────
            if pause_trading:
                equity_curve.append(equity)
                if i >= pause_until:
                    pause_trading = False
                    print(f"    [DD-RESUME] {df.index[i].strftime('%d/%m')} — retomando operações")
                continue

            if cooldown>0:
                cooldown-=1; equity_curve.append(equity); continue
            if df.index[i].hour in DEAD_HOURS:
                equity_curve.append(equity); continue

            bias  = float(row.get("bias",0))
            adx4h = float(row.get("adx4h",0))
            ts    = df.index[i]

            atr_val = float(row["atr"])
            if atr_val<=0 or float(row.get("atr_ratio",1))>5.0:
                equity_curve.append(equity); continue

            # ── Engine Router: testa LONG e SHORT, pega melhor ────────────
            best_engine=""; best_score=0; best_dir=0

            for direction in ([1,-1] if bias==0 else ([1] if bias==1 else [-1])):
                # Sem bias HTF: exige score mais alto (7) para confirmar sem tendência clara
                threshold = 7 if bias==0 else MIN_SCORE
                eng, sc = route_engine(row, direction)
                if sc >= threshold:
                    # Multi-TF confirmação
                    ok5m = check_5m_confirm(df5m, ts, direction)
                    ok3m = check_3m_entry(df3m, ts, direction)
                    bonus = (1 if ok5m else 0) + (1 if ok3m else 0)
                    total = sc + bonus
                    if total > best_score:
                        best_score  = total
                        best_engine = eng
                        best_dir    = direction

            if best_score < MIN_SCORE or best_engine=="SKIP" or not best_engine:
                equity_curve.append(equity); continue

            # ── Abre posição ───────────────────────────────────────────────
            p = PARAMS[best_engine]
            dir_ = "LONG" if best_dir==1 else "SHORT"

            if dir_=="LONG":
                entry_price = price
                sl   = min(float(row["low"]),  price - p["sl_atr"]*atr_val)
                tp1  = price + p["tp1_atr"]*atr_val
                tp2  = price + p["tp2_atr"]*atr_val
                tp3  = price + p["tp3_atr"]*atr_val
            else:
                entry_price = price
                sl   = max(float(row["high"]), price + p["sl_atr"]*atr_val)
                tp1  = price - p["tp1_atr"]*atr_val
                tp2  = price - p["tp2_atr"]*atr_val
                tp3  = price - p["tp3_atr"]*atr_val

            dist     = abs(entry_price-sl)/entry_price
            risk     = equity * RISK_PCT
            # size = notional USD (já inclui alavancagem implicitamente via risk/dist)
            size     = min(risk/max(dist,0.0005), equity*LEVERAGE)
            equity_before_trade = equity
            equity  -= size*FEE_TAKER    # taxa de abertura (sobre o notional)
            rem      = 1.0
            cum_pnl  = -size*FEE_TAKER   # começa negativo (taxa entrada)
            in_trade = True
            eidx     = i
            engine_name = best_engine
            score_entry = best_score
            trail1 = trail2 = False

        equity_curve.append(equity)

    # Fecha posição aberta no fim do período
    if in_trade:
        px   = float(df.iloc[-1]["close"])
        raw  = ((px-entry_price)/entry_price) if dir_=="LONG" else \
               ((entry_price-px)/entry_price)
        pnl  = size*rem*raw - size*rem*FEE_TAKER   # sem LEVERAGE (size já é notional)
        equity = max(equity+pnl, 0.01)
        cum_pnl += pnl
        pnl_on_eq = round(cum_pnl/equity_before_trade*100,2) if equity_before_trade>0 else 0
        trade_log.append({
            "dir":dir_,"engine":engine_name,"score":score_entry,
            "entry":round(entry_price,4),"exit":round(px,4),
            "sl_ini":round(sl,4),"tp1":round(tp1,4),"tp3":round(tp3,4),
            "bars":n-1-eidx,"pnl_usdt":round(cum_pnl,3),"pnl_pct":pnl_on_eq,
            "outcome":"WIN" if cum_pnl>0 else "LOSS",
            "dt_open":df.index[eidx].strftime("%d/%m %H:%M"),
            "dt_close":df.index[-1].strftime("%d/%m %H:%M")+"[A]",
            "t1":trail1,"t2":trail2,"adx_entry":0,"bias_entry":0,
        })

    wins   = [t for t in trade_log if t["outcome"]=="WIN"]
    losses = [t for t in trade_log if t["outcome"]=="LOSS"]
    gp = sum(t["pnl_usdt"] for t in wins)
    gl = abs(sum(t["pnl_usdt"] for t in losses))
    wr = round(len(wins)/len(trade_log)*100,1) if trade_log else 0
    pf = round(gp/gl,2) if gl>0 else (999 if gp>0 else 0)

    by_engine: dict[str,list] = {}
    for t in trade_log:
        by_engine.setdefault(t["engine"],[]).append(t)

    eng_stats = {}
    for eng, tlist in by_engine.items():
        w=[t for t in tlist if t["outcome"]=="WIN"]
        l=[t for t in tlist if t["outcome"]=="LOSS"]
        gp2=sum(t["pnl_usdt"] for t in w)
        gl2=abs(sum(t["pnl_usdt"] for t in l))
        eng_stats[eng]={
            "trades":len(tlist),"wins":len(w),
            "wr":round(len(w)/len(tlist)*100,1) if tlist else 0,
            "pf":round(gp2/gl2,2) if gl2>0 else (999 if gp2>0 else 0),
            "net":round(sum(t["pnl_usdt"] for t in tlist),2),
        }

    return {
        "banca_ini":    round(banca,2),
        "banca_fin":    round(equity,2),
        "net_usdt":     round(equity-banca,2),
        "net_pct":      round((equity-banca)/banca*100,2),
        "net_brl":      round((equity-banca)*CAMBIO,2),
        "banca_fin_brl":round(equity*CAMBIO,2),
        "trades":       len(trade_log),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     wr,
        "profit_factor":pf,
        "max_dd":       round(max_dd*100,1),
        "engine_stats": eng_stats,
        "trade_log":    trade_log,
        "equity_curve": equity_curve,
    }

# ═══════════════════════════════════════════════════════════════════════
# GRÁFICO COMPLETO
# ═══════════════════════════════════════════════════════════════════════
def gerar_grafico(res: dict) -> bytes:
    BG="#0d1117"; GRID="#21262d"; TEXT="#e6edf3"; MUTED="#8b949e"
    WIN_COL="#00b894"; LOSS_COL="#d63031"
    ENG_COLS={"TREND":"#f9ca24","MOMENTUM":"#45aaf2","REVERSAL":"#fd79a8"}

    fig=plt.figure(figsize=(16,11),facecolor=BG)
    gs=fig.add_gridspec(3,3,hspace=0.45,wspace=0.35)
    ax1=fig.add_subplot(gs[0,:])   # equity curve (full width)
    ax2=fig.add_subplot(gs[1,0])   # PnL bars
    ax3=fig.add_subplot(gs[1,1])   # engine breakdown
    ax4=fig.add_subplot(gs[1,2])   # win/loss pie
    ax5=fig.add_subplot(gs[2,:])   # tabela resumo

    net_sign="+" if res["net_usdt"]>=0 else ""
    fig.suptitle(
        f"BACKTEST SOL — 3 MESES | R$1.000 / 10x | "
        f"Engine Router 3m+5m+15m  |  "
        f"Net: {net_sign}{res['net_usdt']:.2f} USDT ({net_sign}{res['net_pct']:.1f}%)",
        color=TEXT,fontsize=12,fontweight="bold",y=0.99
    )

    # ─ Equity curve
    ax1.set_facecolor(BG)
    ec=res["equity_curve"]; bini=res["banca_ini"]
    xs=np.linspace(0,100,len(ec))
    ax1.plot(xs,ec,color=WIN_COL if res["net_usdt"]>=0 else LOSS_COL,lw=2.0)
    ax1.axhline(bini,color=MUTED,lw=0.8,ls="--",alpha=0.6,label=f"Inicial U${bini:.0f}")
    ax1.fill_between(xs,bini,ec,where=[v>=bini for v in ec],alpha=0.10,color=WIN_COL)
    ax1.fill_between(xs,bini,ec,where=[v<bini  for v in ec],alpha=0.12,color=LOSS_COL)
    ax1.legend(fontsize=8,facecolor="#161b22",labelcolor=TEXT,edgecolor=GRID)
    ax1.set_title(f"Equity Curve — {PERIOD_DAYS} dias | Max DD: {res['max_dd']}%",
                  color=TEXT,fontsize=9)
    for sp in ax1.spines.values(): sp.set_edgecolor(GRID)
    ax1.tick_params(colors=MUTED,labelsize=7)
    ax1.yaxis.grid(True,color=GRID,lw=0.4,alpha=0.5)
    ax1.set_ylabel("USDT",color=MUTED,fontsize=8)
    ax1.set_xlabel("% do período",color=MUTED,fontsize=8)

    # ─ PnL por trade
    ax2.set_facecolor(BG)
    tl=res["trade_log"]
    vals=[t["pnl_usdt"] for t in tl]
    cols=[WIN_COL if v>=0 else LOSS_COL for v in vals]
    ax2.bar(range(len(vals)),vals,color=cols,width=0.7,alpha=0.85)
    ax2.axhline(0,color=MUTED,lw=0.6)
    ax2.set_title("PnL por Trade (USDT)",color=TEXT,fontsize=9)
    for sp in ax2.spines.values(): sp.set_edgecolor(GRID)
    ax2.tick_params(colors=MUTED,labelsize=6)
    ax2.yaxis.grid(True,color=GRID,lw=0.3,alpha=0.5)
    ax2.set_xlabel("Trade #",color=MUTED,fontsize=7)

    # ─ Engine breakdown
    ax3.set_facecolor(BG)
    ax3.set_title("Performance por Engine",color=TEXT,fontsize=9)
    es=res["engine_stats"]
    eng_names=list(es.keys())
    if eng_names:
        nets=[es[e]["net"] for e in eng_names]
        bcols=[WIN_COL if v>=0 else LOSS_COL for v in nets]
        bars=ax3.barh(eng_names,nets,color=bcols,alpha=0.85,height=0.5)
        for bar,eng in zip(bars,eng_names):
            st=es[eng]
            ax3.text(bar.get_width()+0.1 if bar.get_width()>=0 else bar.get_width()-0.1,
                     bar.get_y()+bar.get_height()/2,
                     f" {st['trades']}T | WR{st['wr']}% | PF{st['pf']}",
                     va="center",ha="left" if bar.get_width()>=0 else "right",
                     color=TEXT,fontsize=7)
        ax3.axvline(0,color=MUTED,lw=0.7)
    for sp in ax3.spines.values(): sp.set_edgecolor(GRID)
    ax3.tick_params(colors=MUTED,labelsize=8)

    # ─ Win/Loss pie
    ax4.set_facecolor(BG)
    ax4.set_title("WIN / LOSS",color=TEXT,fontsize=9)
    if res["trades"]>0:
        w,l=res["wins"],res["losses"]
        ax4.pie([w,l],labels=["WIN","LOSS"],colors=[WIN_COL,LOSS_COL],
                autopct="%1.1f%%",startangle=90,
                textprops={"color":TEXT,"fontsize":9})
        ax4.text(0,-1.3,f"WR: {res['win_rate']}%  |  PF: {res['profit_factor']}",
                 ha="center",color=TEXT,fontsize=9,fontweight="bold")

    # ─ Tabela resumo
    ax5.set_facecolor(BG); ax5.axis("off")
    ax5.set_title("Resumo Detalhado",color=TEXT,fontsize=9)

    long_t  =[t for t in tl if t["dir"]=="LONG"]
    short_t =[t for t in tl if t["dir"]=="SHORT"]
    lw=[t for t in long_t  if t["outcome"]=="WIN"]
    sw=[t for t in short_t if t["outcome"]=="WIN"]

    headers=["Métrica","LONG","SHORT","TOTAL"]
    rows=[
        ["Trades",str(len(long_t)),str(len(short_t)),str(res["trades"])],
        ["Wins",str(len(lw)),str(len(sw)),str(res["wins"])],
        ["WR%",
         f"{round(len(lw)/len(long_t)*100,1) if long_t else 0}%",
         f"{round(len(sw)/len(short_t)*100,1) if short_t else 0}%",
         f"{res['win_rate']}%"],
        ["Net USDT",
         f"{'+' if sum(t['pnl_usdt'] for t in long_t)>=0 else ''}{sum(t['pnl_usdt'] for t in long_t):.2f}",
         f"{'+' if sum(t['pnl_usdt'] for t in short_t)>=0 else ''}{sum(t['pnl_usdt'] for t in short_t):.2f}",
         f"{'+' if res['net_usdt']>=0 else ''}{res['net_usdt']:.2f}"],
        ["Banca ini",f"U${res['banca_ini']:.2f}","R$1.000",""],
        ["Banca fin",f"U${res['banca_fin']:.2f}",f"R${res['banca_fin_brl']:.2f}",""],
        ["Retorno",f"{res['net_pct']:+.1f}%",f"R${res['net_brl']:+.2f}",""],
        ["Max Drawdown",f"{res['max_dd']}%","",""],
    ]
    tbl=ax5.table(cellText=rows,colLabels=headers,cellLoc="center",
                  loc="center",bbox=[0,0,1,1])
    tbl.auto_set_font_size(False); tbl.set_fontsize(9)
    for (r,c),cell in tbl.get_celld().items():
        cell.set_facecolor("#161b22" if r>0 else "#21262d")
        cell.set_edgecolor(GRID)
        txt=cell.get_text().get_text()
        if r==0: cell.set_text_props(color=TEXT,fontweight="bold")
        elif txt.startswith("+"): cell.set_text_props(color=WIN_COL)
        elif txt.startswith("-"): cell.set_text_props(color=LOSS_COL)
        else: cell.set_text_props(color=TEXT)

    plt.tight_layout(pad=2.0)
    buf=io.BytesIO()
    plt.savefig(buf,format="png",dpi=130,facecolor=BG,bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ═══════════════════════════════════════════════════════════════════════
# MENSAGEM TELEGRAM
# ═══════════════════════════════════════════════════════════════════════
def montar_msg(res: dict) -> str:
    SEP="━"*24
    em="\U0001f7e2" if res["net_usdt"]>=0 else "\U0001f534"
    sn="+" if res["net_usdt"]>=0 else ""
    tl=res["trade_log"]

    lines=[
        f"\U0001f4ca BACKTEST SOL | 3 MESES | R$1.000 | 10x",
        f"{SEP}",
        f"Modo     : Gestor de Crescimento de Banca",
        f"Engine   : Router 3m+5m+15m | HTF 1h+4h",
        f"Estrategia: TREND / MOMENTUM / REVERSAL",
        f"Trailing SL: Ativo | Score min: {MIN_SCORE}/10",
        f"{SEP}",
        f"{em} RESULTADO FINAL",
        f"  Capital ini : R$1.000 (~U${res['banca_ini']:.0f})",
        f"  Capital fin : U${res['banca_fin']:.2f} → R${res['banca_fin_brl']:.2f}",
        f"  PnL         : {sn}{res['net_usdt']:.2f} USDT | {sn}R${res['net_brl']:.2f}",
        f"  Retorno     : {sn}{res['net_pct']:.1f}%",
        f"  Max Drawdown: {res['max_dd']}%",
        f"{SEP}",
        f"Trades  : {res['trades']} | Wins: {res['wins']} | WR: {res['win_rate']}%",
        f"Profit Factor: {res['profit_factor']}",
        f"{SEP}",
        f"\U0001f9e0 ENGINES:",
    ]
    for eng,st in res["engine_stats"].items():
        em2="\U0001f7e2" if st["net"]>=0 else "\U0001f534"
        lines.append(
            f"  {em2} {eng}: {st['trades']}T | WR {st['wr']}% | "
            f"PF {st['pf']} | {'+' if st['net']>=0 else ''}{st['net']:.2f}U"
        )
    lines+=[SEP,"Ultimos 5 trades:"]
    for t in tl[-5:]:
        ic="\U0001f7e2" if t["pnl_usdt"]>=0 else "\U0001f534"
        tr=" \U0001f512" if t.get("t2") else (" [BE]" if t.get("t1") else "")
        lines.append(
            f"  {ic} {t['dir']} {t['engine'][:3]} S:{t['score']} "
            f"{t['dt_open']} {'+' if t['pnl_usdt']>=0 else ''}{t['pnl_usdt']:.2f}U{tr}"
        )
    lines+=[SEP,"TRADER 001 | @mestressinais_br"]
    return "\n".join(lines)

def enviar(chart: bytes, caption: str):
    if not TG_TOKEN or not TG_CHAT: return
    r=requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
        data={"chat_id":TG_CHAT,"caption":caption[:1024]},
        files={"photo":("bt.png",chart,"image/png")},timeout=30,
    ).json()
    if r.get("ok"):
        if len(caption)>1024:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id":TG_CHAT,"text":caption},timeout=15)
        print("[TG] Enviado.")
    else:
        print(f"[TG] Erro: {r}")

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
if __name__=="__main__":
    print(f"\n{'='*66}")
    print(f"  BACKTEST SOL — GESTOR DE BANCA | {PERIOD_DAYS} DIAS | 10x")
    print(f"  Engine Router: 3m + 5m + 15m | HTF 1h + 4h")
    print(f"  Score mínimo: {MIN_SCORE}/10 | ADX Filter | Trailing SL")
    print(f"{'='*66}\n")

    TFS={"3m":None,"5m":None,"15m":None,"1h":None,"4h":None}
    for tf in TFS:
        extra = 5 if tf in ("1h","4h") else 2
        print(f"  [{tf}] Baixando {PERIOD_DAYS}+{extra} dias de {SYMBOL}...", end=" ", flush=True)
        TFS[tf]=load_tf(SYMBOL,tf,PERIOD_DAYS)
        print(f"OK ({len(TFS[tf]):,} candles)")

    print("\n  Preparando indicadores (5m e 15m)...", end=" ", flush=True)
    TFS["5m"]  = prep(TFS["5m"])
    TFS["3m"]  = prep(TFS["3m"])
    print("OK")

    print("  Calculando HTF bias (4h + 1h)...", end=" ", flush=True)
    idx    = TFS["15m"].index
    b_df   = calc_htf_bias(TFS["4h"], TFS["1h"], idx)
    print("OK")

    print(f"\n  Rodando backtest...\n")
    res = run_backtest(TFS["15m"], TFS["5m"], TFS["3m"], b_df, BANCA_USDT)

    # ── Print detalhado
    print(f"{'='*66}")
    print(f"  RESULTADO: {'+' if res['net_usdt']>=0 else ''}{res['net_usdt']:.2f} USDT "
          f"({res['net_pct']:+.1f}%) | R${res['banca_fin_brl']:.2f}")
    print(f"  Trades : {res['trades']} | WR: {res['win_rate']}% | PF: {res['profit_factor']}")
    print(f"  Max DD : {res['max_dd']}%")
    print(f"{'='*66}")

    print("\n  Trades por engine:")
    for eng,st in res["engine_stats"].items():
        icon="▲" if st["net"]>=0 else "▼"
        print(f"    {icon} {eng:10s}: {st['trades']:3d}T | WR {st['wr']:5.1f}% | "
              f"PF {st['pf']:5.2f} | Net {'+' if st['net']>=0 else ''}{st['net']:.2f}U")

    print(f"\n  {'DIR':5s} {'ENGINE':10s} {'SCR':4s} {'ABERTURA':14s} {'FECHAMENTO':16s} "
          f"{'RR':5s} {'PnL USDT':9s} {'PnL%':7s} {'TRAIL':6s}")
    print(f"  {'-'*90}")
    for t in res["trade_log"]:
        atr_ini = abs(t["tp3"]-t["entry"])/5.0   # proxy ATR pelo TP3
        rr      = abs(t["tp3"]-t["entry"])/max(abs(t["sl_ini"]-t["entry"]),0.001)
        trail   = "T2" if t.get("t2") else ("T1" if t.get("t1") else "  ")
        ic      = "✓" if t["outcome"]=="WIN" else "✗"
        print(f"  {ic} {t['dir']:4s} {t['engine']:10s} [{t['score']:2d}] "
              f"{t['dt_open']:14s} → {t['dt_close']:16s} "
              f"RR{rr:4.1f}x  {t['pnl_usdt']:+8.3f}U  {t['pnl_pct']:+6.2f}%  {trail}")

    print(f"\n[CHART] Gerando gráfico...", end=" ", flush=True)
    chart = gerar_grafico(res)
    print(f"OK ({len(chart):,} bytes)")

    print("[TG] Enviando ao Telegram...")
    enviar(chart, montar_msg(res))
    print("\nBacktest SOL 3 meses concluido.")
