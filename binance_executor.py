"""
Binance Futures order execution.
Uses python-binance with USDT-M Futures.
"""
import os
import math
import time
import requests as _requests
from typing import Optional
from binance.client import Client
from binance.exceptions import BinanceAPIException

from config import BINANCE_API_KEY, BINANCE_SECRET_KEY, BINANCE_TESTNET
from models import ActiveTrade, Direction


def _force_paper() -> bool:
    """Kill-switch MANUAL e OPCIONAL de simulação (desligado por padrão).

    Por PADRÃO o real é PERMITIDO: a ativação deliberada do modo Autônomo no
    dashboard (com os parâmetros definidos pelo usuário e o paper desligado) É a
    confirmação. A abertura real só é bloqueada se FORCE_PAPER_TRADING=true for
    setado explicitamente (override manual para forçar um teste em simulação).
    Fechamento de posição (close_position) NUNCA é bloqueado.

    Obs.: a proteção contra o ACIDENTE (ativar modo desligava o paper) está na
    separação BOT_PAUSED x PAPER_TRADING no main.py/state.py — não aqui."""
    return os.getenv("FORCE_PAPER_TRADING", "").strip().lower() in ("1", "true", "yes", "on")


# ── Cache do cliente Binance — reutiliza instancia TCP por até 5 minutos ─────
_client_cache: dict = {"client": None, "ts": 0.0, "offset": 0}
_CLIENT_TTL = 300  # 5 minutos


def _sync_time_offset() -> int:
    """Consulta serverTime da Binance e retorna offset em ms. Zero se falhar."""
    try:
        server_time = _requests.get(
            "https://api.binance.com/api/v3/time", timeout=5
        ).json()["serverTime"]
        return server_time - int(time.time() * 1000)
    except Exception:
        return 0


def get_client() -> Client:
    """Retorna cliente Binance com offset de tempo sincronizado.
    Reutiliza a mesma instância por ate 5 minutos para evitar overhead
    de criação de sockets e sincronização de horário repetitiva.
    """
    global _client_cache
    now = time.time()
    if _client_cache["client"] is not None and (now - _client_cache["ts"]) < _CLIENT_TTL:
        return _client_cache["client"]

    # Novo cliente: sincroniza offset apenas 1x por TTL
    offset = _sync_time_offset()
    client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=BINANCE_TESTNET)
    client.timestamp_offset = offset
    client.requests_params = {"recvWindow": 60000}
    _client_cache = {"client": client, "ts": now, "offset": offset}
    print(f"[EXECUTOR] Novo cliente Binance criado | offset={offset}ms")
    return client


def invalidate_client_cache():
    """Força criação de novo cliente na próxima chamada (ex: após erro -1021)."""
    global _client_cache
    _client_cache = {"client": None, "ts": 0.0, "offset": 0}



def get_futures_balance(client: Client) -> float:
    try:
        account = client.futures_account_balance()
        for asset in account:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
    except BinanceAPIException as e:
        print(f"[BINANCE] Balance error: {e}")
    return 0.0


_exchange_info_cache: dict = {}
_exchange_info_ts: float = 0.0
_EXCHANGE_INFO_TTL = 1800  # 30 minutos — exchange_info quase nunca muda


def get_symbol_precision(client: Client, symbol: str) -> dict:
    global _exchange_info_cache, _exchange_info_ts
    now = time.time()
    if not _exchange_info_cache or now - _exchange_info_ts > _EXCHANGE_INFO_TTL:
        try:
            info = client.futures_exchange_info()
            _exchange_info_cache = {s["symbol"]: s for s in info["symbols"]}
            _exchange_info_ts = now
            print(f"[EXECUTOR] exchange_info cache atualizado ({len(_exchange_info_cache)} símbolos)")
        except Exception as e:
            print(f"[EXECUTOR] exchange_info error (usando fallback): {e}")
            if not _exchange_info_cache:
                return {"qty_precision": 3, "price_precision": 2, "tick_size": 0.01, "step_size": 0.001}

    s = _exchange_info_cache.get(symbol)
    if not s:
        return {"qty_precision": 3, "price_precision": 2, "tick_size": 0.01, "step_size": 0.001}

    tick_size = None
    step_size = None
    for f in s["filters"]:
        if f["filterType"] == "PRICE_FILTER":
            tick_size = float(f["tickSize"])
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
    return {
        "qty_precision": s["quantityPrecision"],
        "price_precision": s["pricePrecision"],
        "tick_size": tick_size,
        "step_size": step_size,
    }


def round_step(value: float, step: float) -> float:
    if step is None or step == 0:
        return value
    precision = max(0, round(-math.log10(step)))
    return round(round(value / step) * step, precision)


_hedge_mode_cache: dict = {"value": None, "ts": 0.0}
_HEDGE_MODE_TTL = 300  # 5 min — modo de posicao quase nunca muda


def _is_hedge_mode(client) -> bool:
    """Verifica se a conta está em Hedge Mode (dual position). Cache 5 min."""
    now = time.time()
    if _hedge_mode_cache["value"] is not None and now - _hedge_mode_cache["ts"] < _HEDGE_MODE_TTL:
        return _hedge_mode_cache["value"]
    try:
        r = client.futures_get_position_mode()
        _hedge_mode_cache["value"] = r.get("dualSidePosition", False)
        _hedge_mode_cache["ts"] = now
        return _hedge_mode_cache["value"]
    except Exception:
        return _hedge_mode_cache["value"] or False


def get_position_qty(client, symbol: str, pos_side: str = "BOTH") -> float:
    """Qty atual da posição (abs). pos_side: LONG/SHORT (hedge) ou BOTH (one-way)."""
    try:
        positions = client.futures_position_information(symbol=symbol)
        for p in positions:
            if p.get("positionSide", "BOTH") == pos_side:
                return abs(float(p.get("positionAmt", 0)))
    except Exception as e:
        print(f"[EXECUTOR] get_position_qty {symbol}: {e}")
    return 0.0


def open_trade(trade: ActiveTrade) -> dict:
    """Open a futures position with SL and TP orders. Suporta One-Way e Hedge Mode."""
    if _force_paper():
        print(f"[EXECUTOR][KILL-SWITCH] FORCE_PAPER_TRADING ativo — ABERTURA REAL BLOQUEADA ({trade.asset}). Simulando.")
        return {"status": "SIMULATED", "trade_id": trade.id, "blocked_by": "FORCE_PAPER_TRADING"}
    if not BINANCE_API_KEY or BINANCE_API_KEY == "your_api_key_here":
        print("[EXECUTOR] Skipping order — no API keys configured (simulation mode)")
        return {"status": "SIMULATED", "trade_id": trade.id}

    client = get_client()
    prec = get_symbol_precision(client, trade.asset)

    side = "BUY" if trade.direction == Direction.LONG else "SELL"
    close_side = "SELL" if trade.direction == Direction.LONG else "BUY"
    pos_side = "LONG" if trade.direction == Direction.LONG else "SHORT"

    # Detecta modo da conta
    hedge = _is_hedge_mode(client)
    print(f"[EXECUTOR] Modo: {'Hedge' if hedge else 'One-Way'}")

    # size_usdt é o NOCIONAL (margem × alavancagem), então qty = nocional / preço
    qty = round_step(
        trade.size_usdt / trade.entry_price,
        prec["step_size"],
    )
    if qty <= 0:
        return {"status": "ERROR", "msg": "qty=0"}

    # Kwargs extras para Hedge Mode
    def entry_kwargs():
        return {"positionSide": pos_side} if hedge else {}

    def close_kwargs():
        return {"positionSide": pos_side} if hedge else {"reduceOnly": True}

    def sl_kwargs():
        if hedge:
            return {"positionSide": pos_side}
        return {"closePosition": True}

    try:
        # Set leverage
        client.futures_change_leverage(symbol=trade.asset, leverage=trade.leverage)

        # Cancel any stale SL/TP orders before entry — prevents duplicate orders
        try:
            stale = client.futures_get_open_orders(symbol=trade.asset)
            for o in stale:
                if o.get("type") not in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
                    continue
                if hedge and o.get("positionSide") != pos_side:
                    continue
                client.futures_cancel_order(symbol=trade.asset, orderId=o["orderId"])
                print(f"[EXECUTOR] Cancelada ordem stale {o['type']} {o['orderId']}")
        except Exception as _ce:
            print(f"[EXECUTOR] Pre-cleanup: {_ce}")

        # Market entry
        order = client.futures_create_order(
            symbol=trade.asset,
            side=side,
            type="MARKET",
            quantity=qty,
            **entry_kwargs(),
        )

        # Stop Loss
        sl_price = round_step(trade.stop_loss, prec["tick_size"])
        sl_order = {
            "symbol": trade.asset,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": sl_price,
        }
        sl_order.update(sl_kwargs())
        if not hedge:
            pass  # closePosition já incluído via sl_kwargs
        else:
            sl_order["quantity"] = qty
        client.futures_create_order(**sl_order)

        # TP1 (45% of position)
        tp1_qty = round_step(qty * 0.45, prec["step_size"])
        client.futures_create_order(
            symbol=trade.asset,
            side=close_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=round_step(trade.tp1, prec["tick_size"]),
            quantity=tp1_qty,
            **close_kwargs(),
        )

        # TP2 (55% restante — fecha posição completa)
        tp2_qty = round_step(qty * 0.55, prec["step_size"])
        client.futures_create_order(
            symbol=trade.asset,
            side=close_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=round_step(trade.tp2, prec["tick_size"]),
            quantity=tp2_qty,
            **close_kwargs(),
        )

        print(f"[EXECUTOR] Opened {trade.direction} {trade.asset} qty={qty} @ market")
        return {"status": "OK", "order_id": order["orderId"], "qty": qty}

    except BinanceAPIException as e:
        print(f"[EXECUTOR] Order error: {e}")
        if "-1021" in str(e):   # timestamp fora de sincronia — invalida cache para resync
            invalidate_client_cache()
        return {"status": "ERROR", "msg": str(e)}



def update_stop_loss(symbol: str, side: str, new_stop: float, client: Client = None) -> bool:
    """Cancel existing SL and place a new one at new_stop. Supports Hedge and One-Way Mode."""
    if not BINANCE_API_KEY or BINANCE_API_KEY == "your_api_key_here":
        print(f"[EXECUTOR] SIM: Update SL {symbol} → {new_stop}")
        return True

    if client is None:
        client = get_client()

    hedge      = _is_hedge_mode(client)
    close_side = "SELL" if side == "LONG" else "BUY"
    pos_side   = "LONG" if side == "LONG" else "SHORT"

    try:
        # In Hedge Mode: get current position size for the SL quantity
        qty = None
        if hedge:
            positions = client.futures_position_information(symbol=symbol)
            for p in positions:
                if p.get("positionSide") == pos_side:
                    qty = abs(float(p.get("positionAmt", 0)))
                    break
            if not qty:
                print(f"[EXECUTOR] SL update: sem posicao aberta {symbol} {pos_side}")
                return False

        # Check existing stop orders — skip update if already at this price (idempotency guard)
        orders = client.futures_get_open_orders(symbol=symbol)
        existing_sl_price = None
        for o in orders:
            if o["type"] not in ("STOP_MARKET", "STOP") or o["side"] != close_side:
                continue
            if hedge and o.get("positionSide") != pos_side:
                continue
            existing_sl_price = float(o.get("stopPrice", 0))
        if existing_sl_price and abs(existing_sl_price - new_stop) / existing_sl_price < 0.0001:
            print(f"[EXECUTOR] SL ja em ${new_stop} — sem alteracao")
            return True

        # Cancel existing stop orders for this symbol+direction
        for o in orders:
            if o["type"] not in ("STOP_MARKET", "STOP") or o["side"] != close_side:
                continue
            if hedge and o.get("positionSide") != pos_side:
                continue
            client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])

        # Place new stop — Hedge Mode uses positionSide+quantity, One-Way uses closePosition
        sl_order = {
            "symbol":    symbol,
            "side":      close_side,
            "type":      "STOP_MARKET",
            "stopPrice": new_stop,
        }
        if hedge:
            sl_order["positionSide"] = pos_side
            sl_order["quantity"]     = qty
        else:
            sl_order["closePosition"] = True

        client.futures_create_order(**sl_order)
        print(f"[EXECUTOR] Updated SL {symbol} {pos_side} → {new_stop}")
        return True
    except BinanceAPIException as e:
        print(f"[EXECUTOR] SL update error: {e}")
        return False


def close_position(symbol: str, direction: Direction, client: Client = None, qty: float = None) -> bool:
    """Market close all (or custom qty) open position for symbol. Supports Hedge and One-Way Mode."""
    if not BINANCE_API_KEY or BINANCE_API_KEY == "your_api_key_here":
        print(f"[EXECUTOR] SIM: Close {symbol} qty={qty}")
        return True

    if client is None:
        client = get_client()

    hedge      = _is_hedge_mode(client)
    close_side = "SELL" if direction == Direction.LONG else "BUY"
    pos_side   = "LONG" if direction == Direction.LONG else "SHORT"

    try:
        # Cancel TPs and SL before closing ONLY if doing a full close
        if qty is None:
            try:
                client.futures_cancel_all_open_orders(symbol=symbol)
            except Exception:
                pass

        if hedge:
            # Hedge Mode: need actual position size and positionSide
            actual_qty = 0.0
            positions = client.futures_position_information(symbol=symbol)
            for p in positions:
                if p.get("positionSide") == pos_side:
                    actual_qty = abs(float(p.get("positionAmt", 0)))
                    break
            if actual_qty == 0:
                print(f"[EXECUTOR] Close: sem posicao aberta {symbol} {pos_side}")
                return True  # already closed
            
            order_qty = qty if qty is not None else actual_qty
            order_qty = min(order_qty, actual_qty)
            
            client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="MARKET",
                quantity=order_qty,
                positionSide=pos_side,
            )
        else:
            # One-Way Mode
            actual_qty = get_position_qty(client, symbol, "BOTH")
            if actual_qty == 0:
                print(f"[EXECUTOR] Close: sem posicao aberta {symbol}")
                return True  # already closed
            
            order_qty = qty if qty is not None else actual_qty
            order_qty = min(order_qty, actual_qty)
            
            client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="MARKET",
                reduceOnly=True,
                quantity=order_qty,
            )
        return True
    except BinanceAPIException as e:
        print(f"[EXECUTOR] Close error: {e}")
        return False


def get_account_balance_detail(client: Client = None) -> dict:
    """Retorna detalhes completos do saldo: wallet, available, unrealized PnL."""
    if client is None:
        client = get_client()
    try:
        acc = client.futures_account()
        return {
            "wallet_balance": float(acc.get("totalWalletBalance", 0)),
            "available_balance": float(acc.get("availableBalance", 0)),
            "unrealized_pnl": float(acc.get("totalUnrealizedProfit", 0)),
            "realized_pnl": float(acc.get("totalMarginBalance", 0)) - float(acc.get("totalWalletBalance", 0)),
            "margin_balance": float(acc.get("totalMarginBalance", 0)),
        }
    except BinanceAPIException as e:
        print(f"[BINANCE] Account detail error: {e}")
        return {"wallet_balance": 0, "available_balance": 0, "unrealized_pnl": 0, "realized_pnl": 0, "margin_balance": 0}


def get_binance_trade_history(watchlist: list, hours: int = 24) -> dict:
    """Busca histórico de PnL realizado das últimas N horas via income history."""
    client = get_client()
    since_ms = int((time.time() - hours * 3600) * 1000)
    try:
        records = client.futures_income_history(
            incomeType="REALIZED_PNL",
            startTime=since_ms,
            limit=500,
        )
    except BinanceAPIException as e:
        print(f"[BINANCE] Income history error: {e}")
        return {"trades": [], "total_pnl": 0, "wins": 0, "losses": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0}

    wins, losses = [], []
    trades_out = []
    for r in records:
        pnl = float(r.get("income", 0))
        sym = r.get("symbol", "")
        ts = r.get("time", 0)
        trades_out.append({"symbol": sym, "pnl": pnl, "time": ts})
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)

    total = len(wins) + len(losses)
    return {
        "trades": trades_out,
        "total_pnl": round(sum(wins) + sum(losses), 4),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0,
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0,
    }


def execute_dca_order(symbol: str, side: str, qty_usdt: float, new_sl: float, new_tp: float) -> dict:
    """Executes an additional market order for DCA, cancels existing SL/TP, and sets new SL/TP."""
    if _force_paper():
        print(f"[EXECUTOR][KILL-SWITCH] FORCE_PAPER_TRADING ativo — DCA REAL BLOQUEADO ({symbol}). Simulando.")
        return {"status": "SIMULATED", "msg": "FORCE_PAPER_TRADING", "blocked_by": "FORCE_PAPER_TRADING"}
    if not BINANCE_API_KEY or BINANCE_API_KEY == "your_api_key_here":
        return {"status": "SIMULATED", "msg": "Paper trading"}
    
    client = get_client()
    prec = get_symbol_precision(client, symbol)
    
    direction_side = "BUY" if side == "LONG" else "SELL"
    close_side = "SELL" if side == "LONG" else "BUY"
    pos_side = "LONG" if side == "LONG" else "SHORT"
    hedge = _is_hedge_mode(client)
    
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])
        qty = round_step(qty_usdt / price, prec["step_size"])
        if qty <= 0:
            return {"status": "ERROR", "msg": "qty=0"}
            
        kwargs = {"positionSide": pos_side} if hedge else {}
        order = client.futures_create_order(
            symbol=symbol,
            side=direction_side,
            type="MARKET",
            quantity=qty,
            **kwargs
        )
        
        # Espera um pequeno delay para execução
        time.sleep(0.5)
        
        # Busca tamanho total atual
        total_qty = get_position_qty(client, symbol, pos_side if hedge else "BOTH")
        
        # Cancela SL e TP anteriores
        stale = client.futures_get_open_orders(symbol=symbol)
        for o in stale:
            if o.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
                if hedge and o.get("positionSide") != pos_side:
                    continue
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                except Exception:
                    pass
                    
        # Cria novo SL
        sl_price = round_step(new_sl, prec["tick_size"])
        sl_order = {
            "symbol": symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": sl_price,
        }
        if hedge:
            sl_order["positionSide"] = pos_side
            sl_order["quantity"] = total_qty
        else:
            sl_order["closePosition"] = True
        client.futures_create_order(**sl_order)
        
        # Cria novo TP consolidado
        tp_price = round_step(new_tp, prec["tick_size"])
        tp_order = {
            "symbol": symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": tp_price,
            "quantity": total_qty,
        }
        if hedge:
            tp_order["positionSide"] = pos_side
        else:
            tp_order["reduceOnly"] = True
        client.futures_create_order(**tp_order)
        
        print(f"[EXECUTOR] DCA Executed for {symbol} {side} adding qty={qty} | new_total_qty={total_qty}")
        return {"status": "OK", "qty": qty}
    except Exception as e:
        print(f"[EXECUTOR] DCA Execution error: {e}")
        return {"status": "ERROR", "msg": str(e)}
