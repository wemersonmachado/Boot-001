import asyncio
import io
import os
import sys
import json
import zipfile
import urllib.request
import time
import datetime as _dt
import numpy as np
import pandas as pd
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project path to sys.path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import engine_router
from models import Direction

# CONFIGURAÇÕES DE PORTFÓLIO
BANCA_BRL       = 1000.0
CAMBIO_USDT_BRL = 5.40
BANCA_USDT      = BANCA_BRL / CAMBIO_USDT_BRL   # ~185.19 USDT
LEVERAGE        = 10
FEE_TAKER       = 0.0004    # 0.04% por lado
RISK_PER_TRADE  = 0.02       # 2% de risco por trade
COOLDOWN_BARS   = 5

WATCHLIST_10 = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "NEARUSDT", "SUIUSDT", "TONUSDT", "ADAUSDT", "DOGEUSDT"
]
WATCHLIST_1M = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

CACHE_ZIP_DIR = Path(__file__).parent / ".klines_cache" / "backtest_zip"
CACHE_ZIP_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_JSON = Path(__file__).parent / "backtest_router_scalp_results.json"

def baixar_dados_dia(symbol, tf, date_str):
    zip_path = CACHE_ZIP_DIR / f"{symbol}-{tf}-{date_str}.zip"
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
            zip_path.unlink(missing_ok=True)

    url = f"https://data.binance.vision/data/futures/um/daily/klines/{symbol}/{tf}/{symbol}-{tf}-{date_str}.zip"
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

def ler_dados_cached(symbol, tf, since_days):
    dates = []
    end_d = _dt.datetime.utcnow().date() - _dt.timedelta(days=1)
    start_d = (_dt.datetime.utcnow() - _dt.timedelta(days=since_days)).date()
    d = start_d
    while d <= end_d:
        dates.append(d.strftime("%Y-%m-%d"))
        d += _dt.timedelta(days=1)
    
    chunks = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(baixar_dados_dia, symbol, tf, dt): dt for dt in dates}
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

async def simular_trades_router(df, symbol, tf):
    banca = BANCA_USDT
    posicao = None
    cooldown = 0
    trades_hist = []

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    
    for i in range(150, len(df)):
        if cooldown > 0:
            cooldown -= 1
            continue

        price = closes[i]
        
        # 1. Gerenciamento de Posição
        if posicao is not None:
            sl_hit = False
            if posicao["tipo"] == "LONG" and lows[i] <= posicao["sl"]:
                sl_hit = True
                exit_price = posicao["sl"]
            elif posicao["tipo"] == "SHORT" and highs[i] >= posicao["sl"]:
                sl_hit = True
                exit_price = posicao["sl"]

            if sl_hit:
                qty_rem = posicao["qty"]
                if posicao["tipo"] == "LONG":
                    pnl_pct = (exit_price - posicao["entrada"]) / posicao["entrada"] * 100 * posicao["leverage"]
                else:
                    pnl_pct = (posicao["entrada"] - exit_price) / posicao["entrada"] * 100 * posicao["leverage"]
                
                trade_loss = (pnl_pct / 100 / posicao["leverage"]) * (qty_rem * exit_price)
                fee = (qty_rem * exit_price) * FEE_TAKER
                posicao["pnl_acumulado"] += trade_loss - fee
                banca += posicao["pnl_acumulado"]
                trades_hist.append({
                    "tipo": posicao["tipo"], "entrada": posicao["entrada"], "saida": exit_price,
                    "pnl": posicao["pnl_acumulado"],
                    "pnl_pct": round(posicao["pnl_acumulado"] / posicao["margin_inicial"] * 100, 2) if posicao["margin_inicial"] > 0 else 0.0,
                    "resultado": "SL", "reason": posicao["reason"]
                })
                posicao = None
                cooldown = COOLDOWN_BARS
                continue

            # TP1
            if not posicao["t1"]:
                tp1_hit = False
                if posicao["tipo"] == "LONG" and highs[i] >= posicao["tp1"]:
                    tp1_hit = True
                elif posicao["tipo"] == "SHORT" and lows[i] <= posicao["tp1"]:
                    tp1_hit = True
                
                if tp1_hit:
                    posicao["t1"] = True
                    qty_t1 = posicao["qty"] * 0.35
                    if posicao["tipo"] == "LONG":
                        pnl_pct = (posicao["tp1"] - posicao["entrada"]) / posicao["entrada"] * 100 * posicao["leverage"]
                    else:
                        pnl_pct = (posicao["entrada"] - posicao["tp1"]) / posicao["entrada"] * 100 * posicao["leverage"]
                    trade_pnl = (pnl_pct / 100 / posicao["leverage"]) * (qty_t1 * posicao["tp1"])
                    fee = (qty_t1 * posicao["tp1"]) * FEE_TAKER
                    posicao["pnl_acumulado"] += trade_pnl - fee
                    posicao["qty"] -= qty_t1
                    posicao["sl"] = posicao["entrada"]
            
            # TP2
            elif not posicao["t2"]:
                tp2_hit = False
                if posicao["tipo"] == "LONG" and highs[i] >= posicao["tp2"]:
                    tp2_hit = True
                elif posicao["tipo"] == "SHORT" and lows[i] <= posicao["tp2"]:
                    tp2_hit = True
                
                if tp2_hit:
                    posicao["t2"] = True
                    qty_t2 = min(posicao["qty_inicial"] * 0.35, posicao["qty"])
                    if posicao["tipo"] == "LONG":
                        pnl_pct = (posicao["tp2"] - posicao["entrada"]) / posicao["entrada"] * 100 * posicao["leverage"]
                    else:
                        pnl_pct = (posicao["entrada"] - posicao["tp2"]) / posicao["entrada"] * 100 * posicao["leverage"]
                    trade_pnl = (pnl_pct / 100 / posicao["leverage"]) * (qty_t2 * posicao["tp2"])
                    fee = (qty_t2 * posicao["tp2"]) * FEE_TAKER
                    posicao["pnl_acumulado"] += trade_pnl - fee
                    posicao["qty"] -= qty_t2

            # TP3
            else:
                tp3_hit = False
                if posicao["tipo"] == "LONG" and highs[i] >= posicao["tp3"]:
                    tp3_hit = True
                elif posicao["tipo"] == "SHORT" and lows[i] <= posicao["tp3"]:
                    tp3_hit = True
                
                if tp3_hit:
                    qty_t3 = posicao["qty"]
                    if posicao["tipo"] == "LONG":
                        pnl_pct = (posicao["tp3"] - posicao["entrada"]) / posicao["entrada"] * 100 * posicao["leverage"]
                    else:
                        pnl_pct = (posicao["entrada"] - posicao["tp3"]) / posicao["entrada"] * 100 * posicao["leverage"]
                    trade_pnl = (pnl_pct / 100 / posicao["leverage"]) * (qty_t3 * posicao["tp3"])
                    fee = (qty_t3 * posicao["tp3"]) * FEE_TAKER
                    posicao["pnl_acumulado"] += trade_pnl - fee
                    banca += posicao["pnl_acumulado"]
                    trades_hist.append({
                        "tipo": posicao["tipo"], "entrada": posicao["entrada"], "saida": posicao["tp3"],
                        "pnl": posicao["pnl_acumulado"],
                        "pnl_pct": round(posicao["pnl_acumulado"] / posicao["margin_inicial"] * 100, 2) if posicao["margin_inicial"] > 0 else 0.0,
                        "resultado": "TP3", "reason": posicao["reason"]
                    })
                    posicao = None
                    cooldown = COOLDOWN_BARS
                    continue

        # 2. Entrada de nova posição
        else:
            # Capped lookback slice (max 250 candles) for 100x calculation speedup
            df_slice = df.iloc[max(0, i-250) : i+1]
            # Chamada direta sem threading para velocidade máxima
            signal = await engine_router.route(symbol, tf, df_slice, mode="NORMAL")

            if signal is not None:
                tipo = signal.direction.value
                sl = signal.stop_loss
                tp1 = signal.tp1
                tp2 = signal.tp2
                tp3 = signal.tp3
                
                risk_usdt = banca * RISK_PER_TRADE
                sl_dist = abs(signal.entry - sl)
                sl_distance_pct = (sl_dist / signal.entry) * 100
                if sl_distance_pct <= 0:
                    continue
                
                notional = (risk_usdt / sl_distance_pct * 100)
                lev = signal.suggested_leverage if getattr(signal, "suggested_leverage", 0) > 0 else LEVERAGE
                margin = notional / lev
                
                if margin > banca * 0.95:
                    margin = banca * 0.95
                    notional = margin * lev
                
                qty = notional / signal.entry
                fee_ent = notional * FEE_TAKER
                
                posicao = {
                    "tipo": tipo, "entrada": signal.entry, "qty_inicial": qty, "qty": qty,
                    "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "leverage": lev, "margin_inicial": margin, "pnl_acumulado": -fee_ent,
                    "t1": False, "t2": False, "reason": signal.reason
                }

    pnl_total = banca - BANCA_USDT
    retorno_pct = (pnl_total / BANCA_USDT) * 100
    return banca, len(trades_hist), retorno_pct, trades_hist

async def run_backtest_symbol(symbol, tf, since_days):
    # Carrega dados
    df = ler_dados_cached(symbol, tf, since_days)
    if df.empty or len(df) < 200:
        return {"symbol": symbol, "error": "dados insuficientes"}
    
    t_start = time.time()
    banca_final, total_trades, ret_pct, trades = await simular_trades_router(df, symbol, tf)
    t_duration = time.time() - t_start
    
    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    
    gross_profits = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_losses = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else (gross_profits if gross_profits > 0 else 1.0)
    
    # Drawdown
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
        "banca_final_usdt": round(banca_final, 2),
        "duration_s": round(t_duration, 1),
        "trades_details": trades
    }

async def main_async():
    # Setup de timeframes e escopos para as 2 melhores moedas (XRPUSDT e SOLUSDT) nos últimos 14 dias
    config_tf = {
        "5m": {"watchlist": ["XRPUSDT", "SOLUSDT"], "days": 14},
        "3m": {"watchlist": ["XRPUSDT", "SOLUSDT"], "days": 14},
        "1m": {"watchlist": ["XRPUSDT", "SOLUSDT"], "days": 14}
    }
    
    resultados_tf = {}

    print("=" * 80)
    print("  TRADER 001 — MULTI-TIMEFRAME SCALP BACKTEST (UNIFIED EVENT LOOP)")
    print(f"  Banca Inicial por Ativo: R$ {BANCA_BRL:.0f} (~${BANCA_USDT:.2f} USDT)")
    print("=" * 80)

    for tf, cfg in config_tf.items():
        print(f"\n>>> INICIANDO TESTES NO TIMEFRAME: {tf} ({cfg['days']} dias) <<<")
        todos = []
        wl = cfg["watchlist"]
        days = cfg["days"]
        
        for idx, sym in enumerate(wl):
            print(f"  [{idx+1:02d}/{len(wl)}] Executando {sym:<10}...", end=" ", flush=True)
            res = await run_backtest_symbol(sym, tf, days)
            if "error" in res:
                print("IGNORADO (Dados insuficientes)")
            else:
                print(f"OK | Trades: {res['trades']:<3} | WR: {res['win_rate']:.1f}% | PF: {res['profit_factor']:.2f} | Retorno: {res['retorno_pct']:+5.1f}% | Tempo: {res['duration_s']}s")
                todos.append(res)
                
        if not todos:
            print(f"Nenhum ativo processado no timeframe {tf}.")
            continue
            
        wr_g = np.mean([r["win_rate"] for r in todos])
        pf_g = np.mean([r["profit_factor"] for r in todos])
        ret_g = np.mean([r["retorno_pct"] for r in todos])
        dd_g = np.mean([r["max_drawdown"] for r in todos])
        total_t = sum(r["trades"] for r in todos)
        
        banca_final_media = np.mean([r["banca_final_usdt"] for r in todos])
        banca_final_brl = banca_final_media * CAMBIO_USDT_BRL
        
        resultados_tf[tf] = {
            "wr": wr_g, "pf": pf_g, "retorno": ret_g, "drawdown": dd_g,
            "trades": total_t, "banca_usdt": banca_final_media, "banca_brl": banca_final_brl
        }

    # Apresenta tabela consolidada comparativa
    print("\n" + "=" * 80)
    print("  COMPARATIVO FINAL DE TIMEFRAMES DE SCALP (ENGINE ROUTER REAL)")
    print("=" * 80)
    print(f"  {'TF':<6} {'WR':>6} {'PF':>6} {'Retorno':>10} {'Max DD':>8} {'Trades':>8} {'Banca Final (BRL)':>20}")
    print(f"  {'-'*76}")
    for tf, r in resultados_tf.items():
        print(f"  {tf:<6} {r['wr']:>5.1f}% {r['pf']:>6.2f} {r['retorno']:>+9.2f}% {r['drawdown']:>7.1f}% {r['trades']:>8}  R$ {r['banca_brl']:>16.2f}")
    print("=" * 80)

    # Salva resultados detalhados
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(resultados_tf, f, ensure_ascii=False, indent=2)
        
    print(f"\nTabela comparativa salva em: {OUTPUT_JSON}\n")

if __name__ == "__main__":
    asyncio.run(main_async())
