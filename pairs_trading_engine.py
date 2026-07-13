"""
Pairs Trading Engine — Arbitragem Estatística por Cointegração e Z-Score.
Módulo autônomo para operar pares market-neutral (Long A + Short B ou vice-versa).
"""
import asyncio
import time
import math
import numpy as np
import pandas as pd
from typing import Optional

from klines_cache import get_klines_cached as get_klines
from binance_executor import open_trade, close_position, get_position_qty, _sync_time_offset, get_client
from database import get_open_trades, save_trade, update_trade_close
from notifier import send_alert

# Configurações de Arbitragem
PAIRS_CONFIG = [
    ("SOLUSDT", "AVAXUSDT"),
    ("ETHUSDT", "LDOUSDT"),
    ("BTCUSDT", "ETHUSDT")
]
Z_ENTRY_THRESHOLD = 2.0  # desvio padrão para entrar
Z_EXIT_THRESHOLD = 0.2   # desvio padrão para sair (reversão à média)
LOOKBACK_PERIOD = 120    # 120 candles de 1h (~5 dias)

_pairs_trades: dict = {}  # key: (asset_a, asset_b) -> state


async def get_spread_stats(asset_a: str, asset_b: str) -> Optional[dict]:
    """Calcula beta, spread e Z-Score atual entre dois ativos usando log-preços de 1h."""
    try:
        df_a = await get_klines(asset_a, "1h", limit=LOOKBACK_PERIOD)
        df_b = await get_klines(asset_b, "1h", limit=LOOKBACK_PERIOD)
        
        if df_a is None or df_b is None or len(df_a) < 50 or len(df_b) < 50:
            return None
            
        # Alinha índices
        common_idx = df_a.index.intersection(df_b.index)
        if len(common_idx) < 30:
            return None
            
        y = np.log(df_a.loc[common_idx, "close"].values)
        x = np.log(df_b.loc[common_idx, "close"].values)
        
        # OLS simples para obter o beta (hedge ratio)
        cov = np.cov(x, y)
        beta = cov[0, 1] / cov[0, 0]
        
        # Calcula spreads históricos
        spreads = y - beta * x
        mean_spread = np.mean(spreads)
        std_spread = np.std(spreads) or 1e-9
        
        current_spread = y[-1] - beta * x[-1]
        z_score = (current_spread - mean_spread) / std_spread
        
        return {
            "beta": float(beta),
            "z_score": float(z_score),
            "price_a": float(df_a["close"].iloc[-1]),
            "price_b": float(df_b["close"].iloc[-1]),
            "mean_spread": float(mean_spread),
            "std_spread": float(std_spread)
        }
    except Exception as e:
        print(f"[PAIRS STATS] Erro calculando spread {asset_a}/{asset_b}: {e}")
        return None


async def run_pairs_trading_cycle(banca_total_usdt: float, paper_trading: bool = False):
    """Varre todos os pares configurados, gerencia posições de arbitragem abertas e executa novas ordens."""
    from main import _active_trades_cache, _executing_assets, OPERATION_MODE
    if OPERATION_MODE != "GRID" and OPERATION_MODE != "AUTONOMOUS":
        # Arbitragem ativa apenas em GRID ou AUTONOMOUS
        return

    # Busca posições ativas de arbitragem de pares
    open_trades_db = await get_open_trades()
    pairs_trades_active = [t for t in open_trades_db if t.get("trade_type") == "PAIRS_ARB"]

    # Agrupa posições abertas por par
    active_pairs_map = {}
    for t in pairs_trades_active:
        # A tag extra indica qual o outro ativo do par
        extra = t.get("reason", "")
        if "PAIR:" in extra:
            other_asset = extra.replace("PAIR:", "").strip()
            pair = tuple(sorted([t["asset"], other_asset]))
            active_pairs_map.setdefault(pair, []).append(t)

    # FIX 2026-07-13 (INCIDENTE REAL — dinheiro real perdido): `pair` abaixo era
    # (asset_a, asset_b) NA ORDEM DO CONFIG, mas `active_pairs_map` é chaveado
    # por tuple(sorted(...)). Para o par ("SOLUSDT","AVAXUSDT") do config, a
    # chave ordenada é ("AVAXUSDT","SOLUSDT") — NUNCA batia com a chave não
    # ordenada, então `pair in active_pairs_map` era SEMPRE False mesmo com a
    # posição já aberta. Resultado: a cada ciclo de 15min (job_pairs_arbitrage)
    # em que o Z-score continuasse além do gatilho, o bot abria UM PAR NOVO em
    # cima do anterior, sem limite — confirmado em produção: par SOL/AVAX
    # aberto 2x seguidas (03:47 e 04:02, exatos 15min de intervalo), turbinando
    # o número de trades abertos e o capital usado muito além do configurado.
    #
    # TRAVA: (1) chave de lookup agora usa a MESMA ordenação (sorted) da chave
    # de registro — dedup real. (2) teto explícito de posições simultâneas de
    # arbitragem, reconciliado contra o banco (fonte de verdade) a cada ciclo,
    # como defesa em profundidade caso outro bug de chave apareça no futuro.
    _MAX_CONCURRENT_PAIRS = len(PAIRS_CONFIG)  # 1 posição (2 pernas) por par configurado
    _active_pair_count = len(active_pairs_map)

    for asset_a, asset_b in PAIRS_CONFIG:
        pair = tuple(sorted([asset_a, asset_b]))
        if _active_pair_count >= _MAX_CONCURRENT_PAIRS and pair not in active_pairs_map:
            continue  # teto de segurança: não abre par novo além do limite configurado
        stats = await get_spread_stats(asset_a, asset_b)
        if not stats:
            continue
            
        z = stats["z_score"]
        # print(f"[PAIRS SCAN] {asset_a}/{asset_b} Z-Score: {z:+.2f} | A=${stats['price_a']:.4f} B=${stats['price_b']:.4f}")
        
        # 1. Gerenciar posições abertas para este par
        if pair in active_pairs_map:
            trades = active_pairs_map[pair]
            if len(trades) < 2:
                # Caso ocorra falha e apenas um lado esteja aberto, encerra por segurança
                print(f"[PAIRS] Erro de paridade para {pair}: apenas uma perna aberta. Fechando...")
                for t in trades:
                    await _close_perna(t, stats["price_a"] if t["asset"] == asset_a else stats["price_b"], paper_trading)
                continue
                
            # Verifica saída (reversão à média)
            if abs(z) <= Z_EXIT_THRESHOLD:
                print(f"[PAIRS] Z-Score de {pair} reverteu para {z:+.2f}. Fechando posições de arbitragem...")
                for t in trades:
                    await _close_perna(t, stats["price_a"] if t["asset"] == asset_a else stats["price_b"], paper_trading)
                await send_alert(
                    f"🎯 *Arbitragem Estatística Fechada* para {asset_a}/{asset_b}\n"
                    f"Z-Score final: `{z:+.2f}` (reversão à média concluída)"
                )
            continue
            
        # 2. Avaliar novas entradas de arbitragem
        # Evita entrar se algum dos ativos estiver no meio de execução
        if asset_a in _executing_assets or asset_b in _executing_assets:
            continue
            
        # Se Z-Score > +2.0: Short A, Long B
        # Se Z-Score < -2.0: Long A, Short B
        signal = None
        if z >= Z_ENTRY_THRESHOLD:
            signal = "SHORT_A_LONG_B"
        elif z <= -Z_ENTRY_THRESHOLD:
            signal = "LONG_A_SHORT_B"
            
        if signal:
            print(f"[PAIRS ENTRY] {asset_a}/{asset_b} Z-Score={z:+.2f} -> Iniciando {signal}")
            
            # Sizing: Aloca 15% da banca disponível por perna
            leverage = 5 # arbitragem usa alavancagem menor por segurança
            margin_per_leg = round(banca_total_usdt * 0.15, 2)
            if margin_per_leg < 5.0:
                continue # banca insuficiente
                
            notional = margin_per_leg * leverage
            
            # Prepara ordens
            if signal == "SHORT_A_LONG_B":
                dir_a, dir_b = "SHORT", "LONG"
            else:
                dir_a, dir_b = "LONG", "SHORT"
                
            # Executa trade Perna A
            await _open_perna(asset_a, dir_a, stats["price_a"], notional, leverage, f"PAIR:{asset_b}", paper_trading)
            # Executa trade Perna B
            await _open_perna(asset_b, dir_b, stats["price_b"], notional, leverage, f"PAIR:{asset_a}", paper_trading)
            
            await send_alert(
                f"⚖️ *Arbitragem Estatística Iniciada* ({asset_a}/{asset_b})\n"
                f"Z-Score de entrada: `{z:+.2f}` (beta={stats['beta']:.2f})\n"
                f"Perna A ({asset_a}): `{dir_a}` | Preço: `${stats['price_a']:.4f}`\n"
                f"Perna B ({asset_b}): `{dir_b}` | Preço: `${stats['price_b']:.4f}`\n"
                f"Margem alocada: `${margin_per_leg * 2:.2f} USDT` (alavancado {leverage}x)"
            )


async def _open_perna(asset: str, direction: str, price: float, notional: float, leverage: int, tag: str, paper_trading: bool):
    """Executa e grava no DB a perna de uma operação de arbitragem de pares."""
    from main import _active_trades_cache
    from models import ActiveTrade, Direction as Dir
    import uuid
    
    trade = ActiveTrade(
        id=str(uuid.uuid4())[:8],
        asset=asset,
        direction=Dir.LONG if direction == "LONG" else Dir.SHORT,
        entry_price=price,
        current_price=price,
        stop_loss=price * 0.70 if direction == "LONG" else price * 1.30,  # SL super largo (arbitragem fecha por Z-score)
        tp1=price * 1.30 if direction == "LONG" else price * 0.70,
        tp2=price * 1.30 if direction == "LONG" else price * 0.70,
        tp3=price * 1.30 if direction == "LONG" else price * 0.70,
        rr=2.0,
        leverage=leverage,
        size_usdt=notional,
        reason=tag,
        confidence=90.0,
    )
    
    trade_dict = trade.model_dump()
    trade_dict["opened_at"] = trade.opened_at.isoformat()
    trade_dict["score_json"] = "{}"
    trade_dict["timeframe"]  = "1h"
    trade_dict["trade_type"] = "PAIRS_ARB"
    trade_dict["paper"] = paper_trading
    
    if not paper_trading:
        result = await asyncio.to_thread(open_trade, trade)
        if result.get("status") == "OK":
            await save_trade(trade_dict)
            _active_trades_cache[trade.id] = trade_dict
    else:
        await save_trade(trade_dict)
        _active_trades_cache[trade.id] = trade_dict


async def _close_perna(trade_data: dict, exit_price: float, paper_trading: bool):
    """Fecha a perna e calcula lucros no banco de dados."""
    from main import _active_trades_cache
    from models import Direction as Dir
    
    trade_id = trade_data["id"]
    symbol = trade_data["asset"]
    direction = trade_data["direction"]
    entry_price = float(trade_data["entry_price"])
    size_usdt = float(trade_data["size_usdt"])
    leverage = int(trade_data["leverage"])
    
    raw_pct = (exit_price - entry_price) / entry_price if direction == "LONG" else (entry_price - exit_price) / entry_price
    pnl_usdt = size_usdt * raw_pct - size_usdt * 0.0004 * 2
    pnl_pct = raw_pct * leverage * 100
    
    if not paper_trading:
        await asyncio.to_thread(
            close_position, symbol, Dir.LONG if direction == "LONG" else Dir.SHORT, get_client()
        )
        
    await update_trade_close(trade_id, exit_price, pnl_usdt, pnl_pct)
    _active_trades_cache.pop(trade_id, None)
    print(f"[PAIRS CLOSE] Perna {symbol} {direction} fechada em ${exit_price:.4f} | PnL: ${pnl_usdt:+.2f}")
