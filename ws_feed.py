"""
WebSocket Feed — Binance Futures kline stream em tempo real.
Substitui o polling REST para candles, latência ~50ms vs ~500ms.

Uso:
    feed = WSFeed(["BTCUSDT", "ETHUSDT"], interval="5m")
    await feed.start()
    df = feed.get_df("BTCUSDT")   # pandas DataFrame dos últimos 500 candles
    await feed.stop()
"""
import asyncio
import json
import random
import time
from collections import deque
from typing import Dict, List, Optional
import pandas as pd
import websockets


class WSFeed:
    WS_BASE = "wss://fstream.binance.com/stream?streams="

    def __init__(self, symbols: List[str], interval: str = "5m", max_candles: int = 500):
        self.symbols     = [s.lower() for s in symbols]
        self.interval    = interval
        self.max_candles = max_candles
        self._buffers: Dict[str, deque] = {s: deque(maxlen=max_candles) for s in self.symbols}
        self._last_closed: Dict[str, dict] = {}
        self._running     = False
        self._task: Optional[asyncio.Task] = None

    # ── Public ────────────────────────────────────────────────────────────────

    async def start(self):
        """Inicia o WebSocket em background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        print(f"[WS] Feed iniciado — {len(self.symbols)} símbolos @ {self.interval}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
        print("[WS] Feed encerrado")

    def get_df(self, symbol: str) -> Optional[pd.DataFrame]:
        """Retorna DataFrame com os últimos candles fechados do símbolo."""
        buf = self._buffers.get(symbol.lower())
        if not buf or len(buf) < 5:
            return None
        rows = list(buf)
        df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume"])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df

    def is_ready(self, symbol: str, min_candles: int = 50) -> bool:
        return len(self._buffers.get(symbol.lower(), [])) >= min_candles

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run(self):
        streams = "/".join(f"{s}@kline_{self.interval}" for s in self.symbols)
        url = self.WS_BASE + streams
        attempt = 0
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    print(f"[WS] Conectado: {url[:80]}...")
                    attempt = 0   # reset em conexão bem-sucedida
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle(raw)
            except Exception as e:
                if self._running:
                    # Backoff exponencial com jitter: 3s → 6s → 12s → ... max 60s
                    delay = min(3 * (2 ** attempt), 60) + random.uniform(0, 1.5)
                    attempt = min(attempt + 1, 6)  # teto no expoente
                    print(f"[WS] Reconectando em {delay:.1f}s (tentativa {attempt}) — {e}")
                    await asyncio.sleep(delay)


    def _handle(self, raw: str):
        try:
            msg = json.loads(raw)
            k = msg.get("data", msg).get("k", {})
            if not k or not k.get("x"):   # x=True → candle fechado
                return
            sym = k["s"].lower()
            candle = [k["t"], k["o"], k["h"], k["l"], k["c"], k["v"]]
            if sym in self._buffers:
                self._buffers[sym].append(candle)
                self._last_closed[sym] = {"time": k["t"], "close": float(k["c"])}
        except Exception:
            pass


# ── Singleton global (usado pelo signal_engine quando ativo) ──────────────────

_global_feed: Optional[WSFeed] = None


def get_global_feed() -> Optional[WSFeed]:
    return _global_feed


async def start_global_feed(symbols: List[str], interval: str = "5m"):
    global _global_feed
    if _global_feed:
        await _global_feed.stop()
    _global_feed = WSFeed(symbols, interval)
    await _global_feed.start()
    print("[WS] Aguardando warm-up (50 candles)...")
    for _ in range(60):
        await asyncio.sleep(2)
        ready = sum(1 for s in symbols if _global_feed.is_ready(s.lower()))
        if ready == len(symbols):
            print(f"[WS] Warm-up completo — {ready}/{len(symbols)} símbolos prontos")
            return
    print("[WS] Warm-up parcial após 120s")


# ── markPrice feed — preços ao vivo ───────────────────────────────────────────

_mark_prices: Dict[str, float] = {}
_mark_feed_running = False
_mark_feed_task: Optional[asyncio.Task] = None


async def _mark_price_loop(symbols: List[str]):
    global _mark_feed_running
    streams = "/".join(f"{s.lower()}@markPrice" for s in symbols)
    url = WSFeed.WS_BASE + streams
    attempt = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                _mark_feed_running = True
                attempt = 0
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        sym = data.get("s", "").upper()
                        p   = float(data.get("p", 0) or data.get("mp", 0))
                        if sym and p > 0:
                            _mark_prices[sym] = p
                    except Exception:
                        pass
        except Exception as e:
            _mark_feed_running = False
            delay = min(5 * (2 ** attempt), 60) + random.uniform(0, 1.5)
            attempt = min(attempt + 1, 5)
            await asyncio.sleep(delay)



async def start_mark_price_feed(symbols: List[str]):
    """Inicia stream de markPrice em background."""
    global _mark_feed_task
    if _mark_feed_task and not _mark_feed_task.done():
        _mark_feed_task.cancel()
    _mark_feed_task = asyncio.create_task(_mark_price_loop(symbols))
    print(f"[WS] markPrice feed iniciado — {len(symbols)} símbolos")


def get_price(symbol: str) -> Optional[float]:
    """Retorna markPrice ao vivo ou None se não disponível."""
    return _mark_prices.get(symbol.upper())


# ── Liquidation feed — !forceOrder@arr (todas as liquidações do mercado) ───────
# Detecta cascatas de liquidação: liquidações em massa = sinal de reversão iminente.
# Liquidação de SHORTs (side=BUY) em volume = short squeeze → bias bullish.
# Liquidação de LONGs  (side=SELL) em volume = long flush  → bias bearish.

_liquidations: deque = deque(maxlen=2000)   # {ts, symbol, side, qty_usdt, price}
_liq_feed_running = False
_liq_feed_task: Optional[asyncio.Task] = None


async def _liquidation_loop():
    global _liq_feed_running
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    attempt = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                _liq_feed_running = True
                attempt = 0
                print("[WS] Liquidation feed conectado (!forceOrder@arr)")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        o   = msg.get("o", {})
                        if not o:
                            continue
                        sym   = o.get("s", "").upper()
                        side  = o.get("S", "")               # SELL=long liquidado | BUY=short liquidado
                        price = float(o.get("ap", 0) or o.get("p", 0) or 0)
                        qty   = float(o.get("q", 0) or 0)
                        usdt  = price * qty
                        if sym and usdt > 0:
                            _liquidations.append({
                                "ts": time.time(), "symbol": sym, "side": side,
                                "qty_usdt": usdt, "price": price,
                            })
                    except Exception:
                        pass
        except Exception as e:
            _liq_feed_running = False
            delay = min(5 * (2 ** attempt), 60) + random.uniform(0, 1.5)
            attempt = min(attempt + 1, 5)
            await asyncio.sleep(delay)



async def start_liquidation_feed():
    """Inicia o stream global de liquidações em background."""
    global _liq_feed_task
    if _liq_feed_task and not _liq_feed_task.done():
        return
    _liq_feed_task = asyncio.create_task(_liquidation_loop())
    print("[WS] Liquidation feed iniciado")


def liquidation_cascade(symbol: str, window_s: int = 300, min_usdt: float = 1_000_000) -> dict:
    """
    Analisa cascata de liquidações de um símbolo na janela recente.

    Returns:
      detected:  True se volume liquidado >= min_usdt na janela
      long_liq_usdt:  $ de LONGs liquidados (side=SELL)  → pressão de baixa exaurindo
      short_liq_usdt: $ de SHORTs liquidados (side=BUY)   → pressão de alta exaurindo
      bias: "BULLISH" (shorts liquidados dominam) | "BEARISH" (longs) | "NEUTRAL"
      total_usdt: total liquidado na janela
    """
    now = time.time()
    long_liq = 0.0
    short_liq = 0.0
    for liq in _liquidations:
        if liq["symbol"] != symbol or (now - liq["ts"]) > window_s:
            continue
        if liq["side"] == "SELL":
            long_liq += liq["qty_usdt"]
        elif liq["side"] == "BUY":
            short_liq += liq["qty_usdt"]
    total = long_liq + short_liq
    if total >= min_usdt:
        # Short squeeze (shorts liquidados) → bias bullish; long flush → bearish
        if short_liq > long_liq * 1.5:
            bias = "BULLISH"
        elif long_liq > short_liq * 1.5:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"
        detected = True
    else:
        bias = "NEUTRAL"
        detected = False
    return {
        "detected": detected, "bias": bias,
        "long_liq_usdt": round(long_liq, 0), "short_liq_usdt": round(short_liq, 0),
        "total_usdt": round(total, 0),
    }


def recent_market_liquidations(window_s: int = 60, top_n: int = 5) -> dict:
    """Resumo das liquidações de TODO o mercado na janela (para alertas/dashboard)."""
    now = time.time()
    by_symbol: Dict[str, float] = {}
    total = 0.0
    for liq in _liquidations:
        if (now - liq["ts"]) > window_s:
            continue
        by_symbol[liq["symbol"]] = by_symbol.get(liq["symbol"], 0.0) + liq["qty_usdt"]
        total += liq["qty_usdt"]
    top = sorted(by_symbol.items(), key=lambda x: -x[1])[:top_n]
    return {
        "total_usdt": round(total, 0),
        "window_s": window_s,
        "top": [{"symbol": s, "usdt": round(v, 0)} for s, v in top],
    }


def get_ws_status() -> dict:
    feed = _global_feed
    return {
        "kline_feed_running":  feed._running if feed else False,
        "kline_symbols":       len(feed.symbols) if feed else 0,
        "mark_price_running":  _mark_feed_running,
        "mark_prices_cached":  len(_mark_prices),
        "symbols_with_data":   [s for s in (feed.symbols if feed else []) if feed.is_ready(s)],
        "liquidation_feed_running": _liq_feed_running,
        "liquidations_cached":      len(_liquidations),
    }
