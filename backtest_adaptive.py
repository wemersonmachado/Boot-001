"""
TRADER 001 — Adaptive Walk-Forward Backtester
=================================================
Estratégia: Scalp Contra-Tendência (15m)
Estatística de Performance por ativo com Otimização In-Sample / Out-of-Sample.
Banca inicial: R$1000 (~$185 USDT) | Alavancagem: 10x | Período: 90 dias
"""
import io
import os
import sys
import json
import zipfile
import urllib.request
import datetime as _dt
import numpy as np
import pandas as pd
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# CONFIGURAÇÕES DE BACKTEST
TIMEFRAME       = "15m"
SINCE_DAYS      = 90
BANCA_BRL       = 1000.0
CAMBIO_USDT_BRL = 5.40
BANCA_USDT      = BANCA_BRL / CAMBIO_USDT_BRL   # ~185.19 USDT
LEVERAGE        = 10
FEE_TAKER       = 0.0004    # 0.04% por lado
RISK_PER_TRADE  = 0.02       # 2% de risco real por trade
COOLDOWN_BARS   = 5

# 30 principais criptomoedas por volume e liquidez no mercado de futuros da Binance
WATCHLIST_30 = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "DOGEUSDT",
    "NEARUSDT", "SUIUSDT", "TONUSDT", "XLMUSDT", "TRXUSDT",
    "LTCUSDT", "APTUSDT", "OPUSDT", "ARBUSDT", "FTMUSDT",
    "WIFUSDT", "PEPEUSDT", "FILUSDT", "ETCUSDT", "RENDERUSDT",
    "FETUSDT", "JUPUSDT", "TIAUSDT", "SEIUSDT", "ATOMUSDT"
]

CACHE_ZIP_DIR = Path(__file__).parent / ".klines_cache" / "backtest_zip"
CACHE_ZIP_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_JSON = Path(__file__).parent / "backtest_adaptive_results.json"


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def stoch_rsi(series: pd.Series, rsi_period: int = 14, stoch_period: int = 14, smooth_k: int = 3):
    rsi_vals = rsi(series, rsi_period)
    min_rsi = rsi_vals.rolling(stoch_period).min()
    max_rsi = rsi_vals.rolling(stoch_period).max()
    rng = max_rsi - min_rsi
    k_vals = np.where(rng == 0, 50.0, 100 * (rsi_vals - min_rsi) / rng.replace(0, np.nan))
    k = pd.Series(k_vals, index=series.index).rolling(smooth_k).mean()
    return k


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(period).mean()


def adx_calc(df: pd.DataFrame, period: int = 14) -> pd.Series:
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


def baixar_dados_dia(symbol: str, date_str: str) -> Optional[pd.DataFrame]:
    """Baixa um único dia de dados e salva em cache zip local."""
    zip_path = CACHE_ZIP_DIR / f"{symbol}-{TIMEFRAME}-{date_str}.zip"
    if zip_path.exists():
        try:
            with zipfile.ZipFile(zip_path) as z:
                with z.open(z.namelist()[0]) as f:
                    raw = pd.read_csv(f, header=None)
                    if not str(raw.iloc[0, 0]).lstrip("-").isdigit():
                        raw = raw.iloc[1:].reset_index(drop=True)
                    chunk = raw.iloc[:, :6].copy()
                    chunk.columns = ["ts", "open", "high", "low", "close", "volume"]
                    return chunk
        except Exception:
            zip_path.unlink(missing_ok=True) # deleta zip corrompido

    # Download
    url = f"https://data.binance.vision/data/futures/um/daily/klines/{symbol}/{TIMEFRAME}/{symbol}-{TIMEFRAME}-{date_str}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        with open(zip_path, "wb") as f:
            f.write(data)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            with z.open(z.namelist()[0]) as f:
                raw = pd.read_csv(f, header=None)
                if not str(raw.iloc[0, 0]).lstrip("-").isdigit():
                    raw = raw.iloc[1:].reset_index(drop=True)
                chunk = raw.iloc[:, :6].copy()
                chunk.columns = ["ts", "open", "high", "low", "close", "volume"]
                return chunk
    except Exception:
        return None


def baixar_dados_full(symbol: str) -> pd.DataFrame:
    """Baixa em paralelo todos os dias e concatena."""
    end_d = _dt.datetime.utcnow().date() - _dt.timedelta(days=1)
    start_d = (_dt.datetime.utcnow() - _dt.timedelta(days=SINCE_DAYS)).date()

    dates = []
    d = start_d
    while d <= end_d:
        dates.append(d.strftime("%Y-%m-%d"))
        d += _dt.timedelta(days=1)

    chunks = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(baixar_dados_dia, symbol, dt): dt for dt in dates}
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                chunks.append(res)

    if not chunks:
        return pd.DataFrame()

    df = pd.concat(chunks, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True)
    df = df.drop(columns=["ts"]).set_index("timestamp")
    df = df[~df.index.duplicated()].sort_index().astype(float)
    return df


def simular_trades_engine(df: pd.DataFrame, params: dict) -> tuple[float, int, float, list]:
    """
    Motor de simulação realista de ordens.
    params: {"oversold", "overbought", "sl_atr", "tp1_atr", "tp2_atr", "tp3_atr"}
    """
    banca = BANCA_USDT
    posicao = None
    cooldown = 0
    trades_hist = []

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    atr_vals = df["atr"].values
    adx_vals = df["adx"].values
    
    # Sinais baseados nos parâmetros adaptativos
    stoch_k = df["stoch_k"].values
    stoch_k_prev = df["stoch_k_prev"].values
    
    # Cruzamentos
    ema200 = df["ema200"].values
    
    for i in range(20, len(df)):
        if cooldown > 0:
            cooldown -= 1
            continue

        price = closes[i]
        
        # 1. Gerenciamento de posição aberta
        if posicao is not None:
            # Verifica Stop Loss
            sl_hit = False
            if posicao["tipo"] == "LONG" and lows[i] <= posicao["sl"]:
                sl_hit = True
                exit_price = posicao["sl"]
            elif posicao["tipo"] == "SHORT" and highs[i] >= posicao["sl"]:
                sl_hit = True
                exit_price = posicao["sl"]

            if sl_hit:
                # Calcula prejuízo
                pnl = 0.0
                qty_rem = posicao["qty"]
                if posicao["tipo"] == "LONG":
                    pnl_pct = (exit_price - posicao["entrada"]) / posicao["entrada"] * 100 * LEVERAGE
                else:
                    pnl_pct = (posicao["entrada"] - exit_price) / posicao["entrada"] * 100 * LEVERAGE
                
                trade_loss = (pnl_pct / 100 / LEVERAGE) * (qty_rem * exit_price)
                # Taker fee de fechamento
                fee = (qty_rem * exit_price) * FEE_TAKER
                
                posicao["pnl_acumulado"] += trade_loss - fee
                banca += posicao["pnl_acumulado"]
                
                trades_hist.append({
                    "tipo": posicao["tipo"],
                    "entrada": posicao["entrada"],
                    "saida": exit_price,
                    "pnl": posicao["pnl_acumulado"],
                    "pnl_pct": round((posicao["pnl_acumulado"] / posicao["margin_inicial"]) * 100, 2) if posicao["margin_inicial"] > 0 else 0.0,
                    "resultado": "SL"
                })
                posicao = None
                cooldown = COOLDOWN_BARS
                continue

            # Verifica Take Profits (Scale-out)
            # TP1: fecha 35%, move SL para o break-even
            if not posicao["t1"]:
                tp1_hit = False
                if posicao["tipo"] == "LONG" and highs[i] >= posicao["tp1"]:
                    tp1_hit = True
                elif posicao["tipo"] == "SHORT" and lows[i] <= posicao["tp1"]:
                    tp1_hit = True
                
                if tp1_hit:
                    posicao["t1"] = True
                    # Realiza lucro parcial de 35%
                    qty_t1 = posicao["qty"] * 0.35
                    if posicao["tipo"] == "LONG":
                        pnl_pct = (posicao["tp1"] - posicao["entrada"]) / posicao["entrada"] * 100 * LEVERAGE
                    else:
                        pnl_pct = (posicao["entrada"] - posicao["tp1"]) / posicao["entrada"] * 100 * LEVERAGE
                    
                    trade_pnl = (pnl_pct / 100 / LEVERAGE) * (qty_t1 * posicao["tp1"])
                    fee = (qty_t1 * posicao["tp1"]) * FEE_TAKER
                    
                    posicao["pnl_acumulado"] += trade_pnl - fee
                    posicao["qty"] -= qty_t1
                    # Move SL para break-even
                    posicao["sl"] = posicao["entrada"]
            
            # TP2: fecha mais 35%
            elif not posicao["t2"]:
                tp2_hit = False
                if posicao["tipo"] == "LONG" and highs[i] >= posicao["tp2"]:
                    tp2_hit = True
                elif posicao["tipo"] == "SHORT" and lows[i] <= posicao["tp2"]:
                    tp2_hit = True
                
                if tp2_hit:
                    posicao["t2"] = True
                    qty_t2 = posicao["qty_inicial"] * 0.35  # fecha 35% do tamanho original
                    # Garante que não zere antes do TP3
                    qty_t2 = min(qty_t2, posicao["qty"])
                    
                    if posicao["tipo"] == "LONG":
                        pnl_pct = (posicao["tp2"] - posicao["entrada"]) / posicao["entrada"] * 100 * LEVERAGE
                    else:
                        pnl_pct = (posicao["entrada"] - posicao["tp2"]) / posicao["entrada"] * 100 * LEVERAGE
                    
                    trade_pnl = (pnl_pct / 100 / LEVERAGE) * (qty_t2 * posicao["tp2"])
                    fee = (qty_t2 * posicao["tp2"]) * FEE_TAKER
                    
                    posicao["pnl_acumulado"] += trade_pnl - fee
                    posicao["qty"] -= qty_t2

            # TP3: fecha os 30% restantes
            else:
                tp3_hit = False
                if posicao["tipo"] == "LONG" and highs[i] >= posicao["tp3"]:
                    tp3_hit = True
                elif posicao["tipo"] == "SHORT" and lows[i] <= posicao["tp3"]:
                    tp3_hit = True
                
                if tp3_hit:
                    qty_t3 = posicao["qty"]
                    if posicao["tipo"] == "LONG":
                        pnl_pct = (posicao["tp3"] - posicao["entrada"]) / posicao["entrada"] * 100 * LEVERAGE
                    else:
                        pnl_pct = (posicao["entrada"] - posicao["tp3"]) / posicao["entrada"] * 100 * LEVERAGE
                    
                    trade_pnl = (pnl_pct / 100 / LEVERAGE) * (qty_t3 * posicao["tp3"])
                    fee = (qty_t3 * posicao["tp3"]) * FEE_TAKER
                    
                    posicao["pnl_acumulado"] += trade_pnl - fee
                    banca += posicao["pnl_acumulado"]
                    
                    trades_hist.append({
                        "tipo": posicao["tipo"],
                        "entrada": posicao["entrada"],
                        "saida": posicao["tp3"],
                        "pnl": posicao["pnl_acumulado"],
                        "pnl_pct": round((posicao["pnl_acumulado"] / posicao["margin_inicial"]) * 100, 2) if posicao["margin_inicial"] > 0 else 0.0,
                        "resultado": "TP3"
                    })
                    posicao = None
                    cooldown = COOLDOWN_BARS
                    continue

        # 2. Entrada de nova posição
        else:
            if price <= 0 or atr_vals[i] <= 0:
                continue
                
            # Identifica tendência macro (EMA200)
            is_uptrend = price > ema200[i]
            is_downtrend = price < ema200[i]
            
            # Sinais de Contra-Tendência (Stoch RSI)
            # LONG na queda
            long_signal = is_downtrend and (stoch_k_prev[i] < stoch_k[i]) and (stoch_k[i] <= params["oversold"])
            # SHORT na alta
            short_signal = is_uptrend and (stoch_k_prev[i] > stoch_k[i]) and (stoch_k[i] >= params["overbought"])
            
            if long_signal or short_signal:
                # ADX Trend Filter (Veto): evita entrar contra tendências fortes
                if adx_vals[i] >= 25.0:
                    continue

                tipo = "LONG" if long_signal else "SHORT"
                
                # SL & TPs
                sl_dist = atr_vals[i] * params["sl_atr"]
                sl = price - sl_dist if tipo == "LONG" else price + sl_dist
                tp1 = price + atr_vals[i] * params["tp1_atr"] if tipo == "LONG" else price - atr_vals[i] * params["tp1_atr"]
                tp2 = price + atr_vals[i] * params["tp2_atr"] if tipo == "LONG" else price - atr_vals[i] * params["tp2_atr"]
                tp3 = price + atr_vals[i] * params["tp3_atr"] if tipo == "LONG" else price - atr_vals[i] * params["tp3_atr"]
                
                # Tamanho da posição (Sizing corrigido de 2% de risco)
                risk_usdt = banca * RISK_PER_TRADE
                sl_distance_pct = (sl_dist / price) * 100
                if sl_distance_pct <= 0:
                    continue
                notional = (risk_usdt / sl_distance_pct * 100)
                
                # Margem necessária
                margin = notional / LEVERAGE
                if margin > banca * 0.95: # cap de colateral
                    margin = banca * 0.95
                    notional = margin * LEVERAGE
                
                qty = notional / price
                fee_ent = notional * FEE_TAKER
                
                posicao = {
                    "tipo": tipo,
                    "entrada": price,
                    "qty_inicial": qty,
                    "qty": qty,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": tp3,
                    "margin_inicial": margin,
                    "pnl_acumulado": -fee_ent, # inicia negativo pela taxa taker de entrada
                    "t1": False,
                    "t2": False
                }

    # Calcula retorno final
    pnl_total = banca - BANCA_USDT
    retorno_pct = (pnl_total / BANCA_USDT) * 100
    
    return banca, len(trades_hist), retorno_pct, trades_hist


def otimizar_ativo(df: pd.DataFrame) -> dict:
    """
    Otimização in-sample (primeiros 2/3 dos dados) dos parâmetros de mercado.
    Busca padrões e retorna o melhor setup para o ativo.
    """
    n_in_sample = int(len(df) * 0.67)
    df_in = df.iloc[:n_in_sample].copy()
    
    # Grade de Parâmetros
    grid = [
        # (oversold, overbought, sl_atr, tp1_atr, tp2_atr, tp3_atr)
        {"oversold": 20, "overbought": 80, "sl_atr": 1.2, "tp1_atr": 1.5, "tp2_atr": 2.5, "tp3_atr": 3.5},
        {"oversold": 25, "overbought": 75, "sl_atr": 1.2, "tp1_atr": 1.5, "tp2_atr": 2.5, "tp3_atr": 3.5},
        {"oversold": 30, "overbought": 70, "sl_atr": 1.2, "tp1_atr": 1.5, "tp2_atr": 2.5, "tp3_atr": 3.5},
        {"oversold": 20, "overbought": 80, "sl_atr": 1.5, "tp1_atr": 2.0, "tp2_atr": 3.5, "tp3_atr": 5.0},
        {"oversold": 25, "overbought": 75, "sl_atr": 1.5, "tp1_atr": 2.0, "tp2_atr": 3.5, "tp3_atr": 5.0},
        {"oversold": 30, "overbought": 70, "sl_atr": 1.5, "tp1_atr": 2.0, "tp2_atr": 3.5, "tp3_atr": 5.0},
        {"oversold": 20, "overbought": 80, "sl_atr": 1.8, "tp1_atr": 2.5, "tp2_atr": 4.5, "tp3_atr": 6.5},
        {"oversold": 25, "overbought": 75, "sl_atr": 1.8, "tp1_atr": 2.5, "tp2_atr": 4.5, "tp3_atr": 6.5},
        {"oversold": 30, "overbought": 70, "sl_atr": 1.8, "tp1_atr": 2.5, "tp2_atr": 4.5, "tp3_atr": 6.5},
    ]

    best_pnl = -999999.0
    best_params = grid[1] # default
    
    for params in grid:
        _, _, ret, _ = simular_trades_engine(df_in, params)
        if ret > best_pnl:
            best_pnl = ret
            best_params = params
            
    return best_params


def processar_ativo(symbol: str) -> dict:
    """Pipeline completo para um ativo."""
    df = baixar_dados_full(symbol)
    if df.empty or len(df) < 300:
        return {"symbol": symbol, "error": "dados insuficientes"}

    # 1. Calcula indicadores necessários
    df["ema200"] = ema(df["close"], 200)
    df["stoch_k"] = stoch_rsi(df["close"])
    df["stoch_k_prev"] = df["stoch_k"].shift(1)
    df["atr"] = atr(df, 14)
    df["adx"] = adx_calc(df)
    df = df.dropna()

    if len(df) < 200:
        return {"symbol": symbol, "error": "dados insuficientes após indicadores"}

    # 2. Otimização In-Sample (Adaptação ao ativo)
    best_params = otimizar_ativo(df)

    # 3. Teste Out-of-Sample (Validação)
    n_in_sample = int(len(df) * 0.67)
    df_out = df.iloc[n_in_sample:].copy()
    
    banca_final, total_trades, ret_pct, trades = simular_trades_engine(df_out, best_params)

    # Métricas
    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    
    gross_profits = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_losses = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else (gross_profits if gross_profits > 0 else 1.0)
    
    # Max Drawdown
    banca_hist = [BANCA_USDT]
    b = BANCA_USDT
    for t in trades:
        b += t["pnl"]
        banca_hist.append(b)
    
    peaks = np.maximum.accumulate(banca_hist)
    drawdowns = (peaks - banca_hist) / peaks * 100
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

    return {
        "symbol": symbol,
        "success": True,
        "trades": total_trades,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "retorno_pct": round(ret_pct, 2),
        "max_drawdown": round(max_dd, 1),
        "best_params": best_params,
        "banca_final_usdt": round(banca_final, 2),
        "n_candles": len(df)
    }


def main():
    print("=" * 70)
    print("  TRADER 001 — BACKTESTING ADAPTATIVO WALK-FORWARD (TOP 30)")
    print(f"  Período: {SINCE_DAYS} dias | Timeframe: {TIMEFRAME} (Scalp Contra-Tendência)")
    print(f"  Banca Inicial: R${BANCA_BRL:.0f} / {CAMBIO_USDT_BRL} = U${BANCA_USDT:.2f} | Alavancagem: {LEVERAGE}x")
    print(f"  Risco por Trade: {RISK_PER_TRADE*100:.1f}% da banca (Sizing Corrigido)")
    print(f"  Método: Otimização In-Sample (67%) + Validação Out-of-Sample (33%)")
    print("=" * 70)

    todos = []

    for idx, sym in enumerate(WATCHLIST_30):
        print(f"  [{idx+1:02d}/{len(WATCHLIST_30)}] Processando {sym}...", end=" ", flush=True)
        res = processar_ativo(sym)
        if "error" in res:
            print(f"IGNORADO ({res['error']})")
        else:
            print(f"OK | Trades: {res['trades']} | WR: {res['win_rate']}% | PF: {res['profit_factor']} | Retorno: {res['retorno_pct']:+.1f}% | Setup: Stoch({res['best_params']['oversold']}/{res['best_params']['overbought']}) SL: {res['best_params']['sl_atr']}x")
            todos.append(res)

    if not todos:
        print("\nNenhum resultado gerado.")
        return

    # Consolida resultados
    todos.sort(key=lambda x: x.get("retorno_pct", -99999), reverse=True)
    
    wr_g  = float(np.mean([r["win_rate"] for r in todos]))
    pf_g  = float(np.mean([r["profit_factor"] for r in todos]))
    ret_g = float(np.mean([r["retorno_pct"] for r in todos]))
    dd_g  = float(np.mean([r["max_drawdown"] for r in todos]))
    total_t = sum(r["trades"] for r in todos)

    print("\n" + "=" * 70)
    print("  CONSOLIDADO OUT-OF-SAMPLE (PERÍODO DE VALIDAÇÃO)")
    print("=" * 70)
    print(f"  {'Par':<10} {'WR':>6} {'PF':>6} {'Retorno':>10} {'DD':>8} {'Trades':>8} {'Setup (Stoch/SL)':<18}")
    print(f"  {'-'*66}")
    for r in todos:
        sym_s = r["symbol"].replace("USDT", "")
        grade = "OK" if r.get("profit_factor", 0) >= 1.5 else ("~" if r.get("profit_factor", 0) >= 1.0 else "X")
        setup_str = f"({r['best_params']['oversold']}/{r['best_params']['overbought']}) {r['best_params']['sl_atr']}x"
        print(f"  {sym_s:<10} {r.get('win_rate',0):>5.1f}% {r.get('profit_factor',0):>6.2f}"
              f" {r.get('retorno_pct',0):>+9.1f}% {r.get('max_drawdown',0):>7.1f}%"
              f" {r.get('trades',0):>8}  {setup_str:<18} [{grade}]")

    print(f"\n  Média Consolidada (OOS) -> WR: {wr_g:.1f}% | PF: {pf_g:.2f} | Retorno: {ret_g:+.1f}% | DD: {dd_g:.1f}%")
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
            "fee_taker":    FEE_TAKER,
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

    print(f"\n  Resultados detalhados salvos em: {OUTPUT_JSON}")
    print("=" * 70)


if __name__ == "__main__":
    main()
