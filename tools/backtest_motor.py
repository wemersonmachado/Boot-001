import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # acha modulos do bot na pasta-mae
"""
TRADER 001 — Motor de Backtesting Customizado V2
=================================================
Replica FIELMENTE a estrategia do bot em producao:
  - EMA 9/21/50 (tendencia)
  - RSI 14 (zona 45-70 long / 30-55 short)
  - Volume ratio >= 1.5x media 20 periodos
  - MACD histograma acelerando
  - Score composto >= 3/5 componentes
  - Anti pump/dump: RSI > 76 OU vol_ratio > 6.5 -> bloqueia
  - Stop: 1.5x ATR14 | TP1: 2x ATR (fecha 50%) | TP2: 3.5x ATR (fecha 50%)
  - Move SL para breakeven apos TP1
  - Cooldown de 5 velas apos fechar posicao
  - Alavancagem 10x | Taxa taker 0.04% por lado
  - Risk per trade: 2% da banca atual
  - Banca inicial: R$1.000 / cambio 5.40 = ~U$185

Fonte de dados: CDN publico data.binance.vision (futures UM)
  - Sem API key, sem rate limit, sem IP ban
  - Download por arquivo zip diario (1 zip por dia por par)

Periodo: ultimos 90 dias | Timeframe: 15m
Pares: Top 15 por volume + liquidez Binance Futures
"""

import io
import json
import sys
import zipfile
import urllib.request
import datetime as _dt
import numpy as np
import pandas as pd
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACOES
# ─────────────────────────────────────────────────────────────────────────────
TIMEFRAME       = "15m"
SINCE_DAYS      = 90
BANCA_BRL       = 1000.0
CAMBIO_USDT_BRL = 5.40
BANCA_USDT      = BANCA_BRL / CAMBIO_USDT_BRL   # ~185 USDT
LEVERAGE        = 10
FEE_TAKER       = 0.0004    # 0.04% por lado (taker Binance Futures)
SCORE_THRESH    = 3          # minimo de 3/5 componentes alinhados
STOP_ATR_MULT   = 1.5        # SL = 1.5x ATR14
TP1_ATR_MULT    = 2.0        # TP1 = 2x ATR (fecha 35% — igual ao config.py SCALE_OUT_MILESTONES)
TP2_ATR_MULT    = 3.0        # TP2 = 3x ATR (fecha 35%)
TP3_ATR_MULT    = 4.5        # TP3 = 4.5x ATR (fecha 30% restante)
# FIX #11: scale-out 35/35/30 identico ao bot real (config.py SCALE_OUT_MILESTONES)
TP1_PCT         = 0.35       # fecha 35% em TP1
TP2_PCT         = 0.35       # fecha 35% em TP2
TP3_PCT         = 0.30       # fecha 30% em TP3
COOLDOWN_BARS   = 5          # velas de cooldown apos fechar posicao
RISK_PER_TRADE  = 0.02       # arrisca 2% da banca por trade

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT",
    "DOTUSDT", "LTCUSDT", "NEARUSDT", "SUIUSDT", "TONUSDT",
]

OUTPUT_JSON = Path(__file__).parent / "backtest_results.json"


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD — CDN data.binance.vision (nao usa API REST, sem ban de IP)
# ─────────────────────────────────────────────────────────────────────────────
def baixar_dados(symbol: str) -> pd.DataFrame:
    """
    Baixa dados OHLCV do CDN publico data.binance.vision (Futures UM).
    Um arquivo zip por dia — sem API key, sem rate limit, sem IP ban.
    """
    base   = f"https://data.binance.vision/data/futures/um/daily/klines/{symbol}/{TIMEFRAME}/"
    end_d  = _dt.datetime.utcnow().date()
    start_d = ((_dt.datetime.utcnow()) - _dt.timedelta(days=SINCE_DAYS)).date()

    rows = []
    d = start_d
    while d <= end_d:
        url = f"{base}{symbol}-{TIMEFRAME}-{d.strftime('%Y-%m-%d')}.zip"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                with z.open(z.namelist()[0]) as f:
                    raw = pd.read_csv(f, header=None)
                    # CDN pode incluir linha de header textual
                    if not str(raw.iloc[0, 0]).lstrip("-").isdigit():
                        raw = raw.iloc[1:].reset_index(drop=True)
                    chunk = raw.iloc[:, :6].copy()
                    chunk.columns = ["ts", "open", "high", "low", "close", "volume"]
                    rows.append(chunk)
        except Exception:
            pass  # dia sem dados (feriado, gap) — ignora silenciosamente
        d += _dt.timedelta(days=1)

    if not rows:
        return pd.DataFrame()

    df = pd.concat(rows, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True)
    df = df.drop(columns=["ts"]).set_index("timestamp")
    df = df[~df.index.duplicated()].sort_index().astype(float)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# INDICADORES TECNICOS
# ─────────────────────────────────────────────────────────────────────────────
def calcular_indicadores(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    df["ema9"]  = c.ewm(span=9,  adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()

    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    hl  = h - l
    hc  = (h - c.shift()).abs()
    lc  = (l - c.shift()).abs()
    df["atr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean()

    df["vol_avg"]   = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_avg"].replace(0, np.nan)

    macd_line       = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    macd_signal     = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist       = macd_line - macd_signal
    df["macd_hist"] = macd_hist
    df["macd_up"]   = macd_hist > macd_hist.shift(1)

    return df


# ─────────────────────────────────────────────────────────────────────────────
# GERACAO DE SINAIS (replica signal_engine.py do bot)
# ─────────────────────────────────────────────────────────────────────────────
def gerar_sinais(df: pd.DataFrame) -> pd.DataFrame:
    # Score LONG (5 componentes — identico ao bot)
    s_trend = (df["ema9"] > df["ema21"]) & (df["ema21"] > df["ema50"])
    s_rsi   = (df["rsi"] >= 45) & (df["rsi"] <= 70)
    s_vol   = df["vol_ratio"] >= 1.5
    s_macd  = df["macd_up"]
    s_preco = df["close"] > df["ema21"]
    score_long = s_trend.astype(int) + s_rsi.astype(int) + s_vol.astype(int) \
               + s_macd.astype(int) + s_preco.astype(int)

    # Score SHORT (inverso)
    s_trend_s = (df["ema9"] < df["ema21"]) & (df["ema21"] < df["ema50"])
    s_rsi_s   = (df["rsi"] >= 30) & (df["rsi"] <= 55)
    s_vol_s   = df["vol_ratio"] >= 1.5
    s_macd_s  = ~df["macd_up"]
    s_preco_s = df["close"] < df["ema21"]
    score_short = s_trend_s.astype(int) + s_rsi_s.astype(int) + s_vol_s.astype(int) \
                + s_macd_s.astype(int) + s_preco_s.astype(int)

    # Anti pump/dump (identico ao bot)
    anti_pd = (df["rsi"] > 76) | (df["vol_ratio"] > 6.5)

    df["score_long"]  = score_long
    df["score_short"] = score_short
    df["long_entry"]  = (score_long  >= SCORE_THRESH) & ~anti_pd
    df["short_entry"] = (score_short >= SCORE_THRESH) & ~anti_pd

    return df


# ─────────────────────────────────────────────────────────────────────────────
# SIMULACAO DE TRADES — barra a barra (replica trade manager do bot)
# ─────────────────────────────────────────────────────────────────────────────
def simular_trades(df: pd.DataFrame, symbol: str) -> dict:
    """
    Loop barra a barra com:
    - Risk sizing: 2% da banca atual
    - Scale-out: 50% em TP1, 50% em TP2
    - Breakeven: SL move para entrada apos TP1
    - Cooldown: 5 velas sem nova entrada apos fechar
    """
    banca    = BANCA_USDT
    posicao  = None
    cooldown = 0
    trades_hist: list[dict] = []

    fechamento = df["close"].values
    maximo     = df["high"].values
    minimo     = df["low"].values
    atr_vals   = df["atr"].values
    long_sig   = df["long_entry"].values
    short_sig  = df["short_entry"].values
    rsi_vals   = df["rsi"].values
    vol_vals   = df["vol_ratio"].values

    for i in range(50, len(df)):
        preco = fechamento[i]

        # Gerencia posicao aberta
        if posicao is not None:
            posicao = _atualizar_posicao(posicao, maximo[i], minimo[i], preco, i)
            if posicao["status"] != "OPEN":
                banca, rec = _fechar_posicao(posicao, banca)
                trades_hist.append(rec)
                posicao  = None
                cooldown = COOLDOWN_BARS
            continue

        if cooldown > 0:
            cooldown -= 1
            continue

        if banca < 5:
            break

        atr = atr_vals[i]
        if atr <= 0 or np.isnan(atr):
            continue

        sl_dist  = atr * STOP_ATR_MULT
        sl_pct   = sl_dist / preco
        risco_u  = banca * RISK_PER_TRADE
        nocional = min(risco_u / sl_pct, banca * LEVERAGE * 0.8)

        if nocional < 5:
            continue

        fee_entrada = nocional * FEE_TAKER

        if long_sig[i]:
            posicao = {
                "tipo":    "LONG",
                "entrada": preco,
                "sl":      preco - sl_dist,
                "tp1":     preco + atr * TP1_ATR_MULT,
                "tp2":     preco + atr * TP2_ATR_MULT,
                "tp3":     preco + atr * TP3_ATR_MULT,
                "nocional": nocional,
                "fee_e":   fee_entrada,
                "status":  "OPEN",
                "tp1_hit": False,
                "tp2_hit": False,
                "barra_e": i,
                "rsi_e":   float(rsi_vals[i]),
                "vol_e":   float(vol_vals[i]) if not np.isnan(vol_vals[i]) else 1.0,
                "symbol":  symbol,
            }
            banca -= fee_entrada

        elif short_sig[i]:
            posicao = {
                "tipo":    "SHORT",
                "entrada": preco,
                "sl":      preco + sl_dist,
                "tp1":     preco - atr * TP1_ATR_MULT,
                "tp2":     preco - atr * TP2_ATR_MULT,
                "tp3":     preco - atr * TP3_ATR_MULT,
                "nocional": nocional,
                "fee_e":   fee_entrada,
                "status":  "OPEN",
                "tp1_hit": False,
                "tp2_hit": False,
                "barra_e": i,
                "rsi_e":   float(rsi_vals[i]),
                "vol_e":   float(vol_vals[i]) if not np.isnan(vol_vals[i]) else 1.0,
                "symbol":  symbol,
            }
            banca -= fee_entrada

    # Fecha posicao aberta ao fim do periodo (timeout)
    if posicao is not None and posicao["status"] == "OPEN":
        posicao["status"]  = "TIMEOUT"
        posicao["saida"]   = fechamento[-1]
        posicao["barra_s"] = len(df) - 1
        if "tp2_hit" not in posicao:
            posicao["tp2_hit"] = False
        banca, rec = _fechar_posicao(posicao, banca)
        trades_hist.append(rec)

    return _calcular_stats(trades_hist, banca, symbol)


def _atualizar_posicao(pos: dict, high: float, low: float, close: float, barra: int) -> dict:
    """
    Verifica SL e TPs (SL primeiro, depois TPs).
    FIX #11: 3 TPs identicos ao bot real (35%/35%/30%).
    Apos TP1: SL -> breakeven. Apos TP2: SL -> TP1.
    """
    tipo = pos["tipo"]

    if tipo == "LONG":
        if low <= pos["sl"]:
            pos["status"]  = "SL"
            pos["saida"]   = pos["sl"]
            pos["barra_s"] = barra
            return pos
        if not pos["tp1_hit"] and high >= pos["tp1"]:
            pos["tp1_hit"] = True
            pos["sl"]      = pos["entrada"]  # breakeven
        if pos["tp1_hit"] and not pos["tp2_hit"] and high >= pos["tp2"]:
            pos["tp2_hit"] = True
            pos["sl"]      = pos["tp1"]      # SL sobe para TP1
        if pos["tp2_hit"] and high >= pos["tp3"]:
            pos["status"]  = "TP3"
            pos["saida"]   = pos["tp3"]
            pos["barra_s"] = barra
    else:  # SHORT
        if high >= pos["sl"]:
            pos["status"]  = "SL"
            pos["saida"]   = pos["sl"]
            pos["barra_s"] = barra
            return pos
        if not pos["tp1_hit"] and low <= pos["tp1"]:
            pos["tp1_hit"] = True
            pos["sl"]      = pos["entrada"]  # breakeven
        if pos["tp1_hit"] and not pos["tp2_hit"] and low <= pos["tp2"]:
            pos["tp2_hit"] = True
            pos["sl"]      = pos["tp1"]      # SL desce para TP1
        if pos["tp2_hit"] and low <= pos["tp3"]:
            pos["status"]  = "TP3"
            pos["saida"]   = pos["tp3"]
            pos["barra_s"] = barra

    return pos


def _fechar_posicao(pos: dict, banca: float) -> tuple[float, dict]:
    """
    FIX #11: scale-out 35%/35%/30% identico ao bot real.
    tp1_hit: fecha 35% no preco do TP1.
    tp2_hit: fecha 35% no preco do TP2.
    Fechamento final: 30% restante no preco de saida (TP3/SL/timeout).
    """
    entrada  = pos["entrada"]
    saida    = pos.get("saida", entrada)
    nocional = pos["nocional"]
    tipo     = pos["tipo"]
    tp1_hit  = pos["tp1_hit"]
    tp2_hit  = pos["tp2_hit"]

    def _mv(p_ref: float, p_exit: float) -> float:
        if tipo == "LONG":
            return (p_exit - entrada) / entrada
        return (entrada - p_exit) / entrada

    pnl = 0.0
    if tp1_hit:
        pnl += nocional * TP1_PCT * _mv(entrada, pos["tp1"])
        fee_tp1 = nocional * TP1_PCT * FEE_TAKER
    else:
        fee_tp1 = 0.0

    if tp2_hit:
        pnl += nocional * TP2_PCT * _mv(entrada, pos["tp2"])
        fee_tp2 = nocional * TP2_PCT * FEE_TAKER
    else:
        fee_tp2 = 0.0

    # Fracao restante fecha no saida final
    frac_rem = 1.0 - (TP1_PCT if tp1_hit else 0.0) - (TP2_PCT if tp2_hit else 0.0)
    pnl += nocional * frac_rem * _mv(entrada, saida)
    fee_saida = nocional * FEE_TAKER  # fee total de saida (simplificado)

    pnl_liq    = pnl - fee_tp1 - fee_tp2 - fee_saida
    nova_banca = banca + pnl_liq
    pnl_pct    = pnl_liq / BANCA_USDT * 100

    rec = {
        "symbol":            pos["symbol"],
        "tipo":              tipo,
        "status":            pos["status"],
        "entrada":           round(entrada, 6),
        "saida":             round(saida, 6),
        "pnl_usdt":          round(pnl_liq, 4),
        "pnl_pct":           round(pnl_pct, 3),
        "banca_pos":         round(nova_banca, 4),
        "tp1_hit":           tp1_hit,
        "tp2_hit":           tp2_hit,
        "barra_e":           pos.get("barra_e", 0),
        "barra_s":           pos.get("barra_s", 0),
        "duracao_barras":    pos.get("barra_s", 0) - pos.get("barra_e", 0),
        "rsi_entrada":       round(pos.get("rsi_e", 50.0), 1),
        "vol_ratio_entrada": round(pos.get("vol_e", 1.0), 2),
    }
    return nova_banca, rec


def _calcular_stats(trades: list[dict], banca_final: float, symbol: str) -> dict:
    if not trades:
        return {"symbol": symbol, "trades": 0, "erro": "Sem trades gerados"}

    pnls   = [t["pnl_usdt"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    n     = len(trades)
    n_win = len(wins)
    wr    = n_win / n * 100 if n > 0 else 0.0

    avg_win  = float(np.mean(wins))   if wins   else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    pf       = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

    # Curva de equity e max drawdown
    equity = [BANCA_USDT]
    for p in pnls:
        equity.append(equity[-1] + p)

    pico   = BANCA_USDT
    max_dd = 0.0
    for e in equity:
        if e > pico:
            pico = e
        dd = (pico - e) / pico * 100 if pico > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    retorno_pct = (banca_final - BANCA_USDT) / BANCA_USDT * 100
    retorno_brl = retorno_pct / 100 * BANCA_BRL

    # Sharpe simplificado
    sharpe = 0.0
    if n > 2:
        std_pnl = float(np.std(pnls))
        if std_pnl > 0:
            sharpe = float(np.mean(pnls) / std_pnl) * float(np.sqrt(n))

    tipos_saida = {}
    for t in trades:
        k = t["status"]
        tipos_saida[k] = tipos_saida.get(k, 0) + 1

    longs  = [t for t in trades if t["tipo"] == "LONG"]
    shorts = [t for t in trades if t["tipo"] == "SHORT"]

    return {
        "symbol":             symbol,
        "trades":             n,
        "wins":               n_win,
        "losses":             n - n_win,
        "win_rate":           round(wr, 1),
        "profit_factor":      round(pf, 2),
        "retorno_pct":        round(retorno_pct, 2),
        "retorno_brl":        round(retorno_brl, 2),
        "max_drawdown":       round(max_dd, 2),
        "sharpe":             round(sharpe, 2),
        "banca_inicial_usdt": round(BANCA_USDT, 2),
        "banca_final_usdt":   round(banca_final, 2),
        "avg_win_usdt":       round(avg_win, 4),
        "avg_loss_usdt":      round(avg_loss, 4),
        "maior_ganho":        round(max(pnls), 4) if pnls else 0,
        "maior_perda":        round(min(pnls), 4) if pnls else 0,
        "dur_media_velas":    round(float(np.mean([t["duracao_barras"] for t in trades])), 1),
        "tp1_hits":           sum(1 for t in trades if t["tp1_hit"]),
        "tp2_hits":           sum(1 for t in trades if t.get("tp2_hit", False)),
        "saidas_sl":          tipos_saida.get("SL", 0),
        "saidas_tp3":         tipos_saida.get("TP3", 0),
        "saidas_timeout":     tipos_saida.get("TIMEOUT", 0),
        "longs_n":            len(longs),
        "shorts_n":           len(shorts),
        "longs_wr":           round(sum(1 for t in longs  if t["pnl_usdt"] > 0) / len(longs)  * 100, 1) if longs  else 0.0,
        "shorts_wr":          round(sum(1 for t in shorts if t["pnl_usdt"] > 0) / len(shorts) * 100, 1) if shorts else 0.0,
        "equity_curve":       [round(e, 2) for e in equity[-60:]],
        "trades_hist":        trades,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXECUCAO PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 62)
    print("  TRADER 001 — MOTOR DE BACKTESTING CUSTOMIZADO V2")
    print(f"  Periodo: {SINCE_DAYS} dias | Timeframe: {TIMEFRAME}")
    print(f"  Banca: R${BANCA_BRL:.0f} / {CAMBIO_USDT_BRL} = U${BANCA_USDT:.0f} | Alavancagem: {LEVERAGE}x")
    print(f"  Score: {SCORE_THRESH}/5 | SL: {STOP_ATR_MULT}x ATR | TP1: {TP1_ATR_MULT}x (50%) | TP2: {TP2_ATR_MULT}x (50%)")
    print(f"  Fonte: data.binance.vision (CDN publico — sem IP ban)")
    print("=" * 62)

    todos = []

    for idx, sym in enumerate(WATCHLIST):
        print(f"\n  [{idx+1:02d}/{len(WATCHLIST)}] {sym}...", end=" ", flush=True)
        try:
            df = baixar_dados(sym)
            if df.empty or len(df) < 200:
                print(f"sem dados ({len(df) if not df.empty else 0} candles)")
                continue
            print(f"{len(df):,} candles ({df.index[0].date()} -> {df.index[-1].date()})")

            df  = calcular_indicadores(df)
            df  = gerar_sinais(df)
            res = simular_trades(df, sym)
            todos.append(res)

            wr  = res.get("win_rate", 0)
            pf  = res.get("profit_factor", 0)
            ret = res.get("retorno_pct", 0)
            dd  = res.get("max_drawdown", 0)
            print(f"     Trades:{res['trades']} | WR:{wr:.1f}% | PF:{pf:.2f} | Retorno:{ret:+.1f}% | DD:{dd:.1f}%")

        except Exception as e:
            print(f"ERRO: {e}")

    if not todos:
        print("\nNenhum resultado gerado.")
        return

    todos.sort(key=lambda x: x.get("retorno_pct", -999), reverse=True)

    wr_g    = float(np.mean([r["win_rate"]       for r in todos]))
    pf_g    = float(np.mean([r["profit_factor"]  for r in todos]))
    ret_g   = float(np.mean([r["retorno_pct"]    for r in todos]))
    dd_g    = float(np.mean([r["max_drawdown"]   for r in todos]))
    total_t = sum(r["trades"] for r in todos)

    print("\n" + "=" * 62)
    print("  CONSOLIDADO")
    print("=" * 62)
    print(f"  {'Par':<10} {'WR':>6} {'PF':>6} {'Retorno':>10} {'DD':>8} {'Trades':>8}")
    print(f"  {'-'*56}")
    for r in todos:
        sym_s = r["symbol"].replace("USDT", "")
        grade = "OK" if r.get("profit_factor", 0) >= 1.5 else ("~" if r.get("profit_factor", 0) >= 1.0 else "X")
        print(f"  {sym_s:<10} {r.get('win_rate',0):>5.1f}% {r.get('profit_factor',0):>6.2f}"
              f" {r.get('retorno_pct',0):>+9.1f}% {r.get('max_drawdown',0):>7.1f}%"
              f" {r.get('trades',0):>8}  [{grade}]")

    print(f"\n  Media -> WR:{wr_g:.1f}% | PF:{pf_g:.2f} | Retorno:{ret_g:+.1f}% | DD:{dd_g:.1f}%")
    print(f"  Total trades simulados: {total_t:,}")

    saida = {
        "meta": {
            "gerado_em":    _dt.datetime.now().isoformat(),
            "periodo_dias": SINCE_DAYS,
            "timeframe":    TIMEFRAME,
            "banca_brl":    BANCA_BRL,
            "banca_usdt":   round(BANCA_USDT, 2),
            "cambio":       CAMBIO_USDT_BRL,
            "alavancagem":  LEVERAGE,
            "score_thresh": SCORE_THRESH,
            "sl_atr":       STOP_ATR_MULT,
            "tp1_atr":      TP1_ATR_MULT,
            "tp2_atr":      TP2_ATR_MULT,
            "fee_taker":    FEE_TAKER,
            "fonte":        "data.binance.vision (CDN)",
        },
        "consolidado": {
            "wr_medio":      round(wr_g, 1),
            "pf_medio":      round(pf_g, 2),
            "retorno_medio": round(ret_g, 2),
            "dd_medio":      round(dd_g, 1),
            "total_trades":  total_t,
        },
        "resultados": todos,
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=2)

    print(f"\n  Resultados salvos: {OUTPUT_JSON}")
    print("=" * 62)


if __name__ == "__main__":
    main()
