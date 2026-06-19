"""
Fetches market data from Binance Futures REST API.
No third-party keys required — uses public endpoints.
"""
import asyncio
import aiohttp
import pandas as pd
import numpy as np
import time
from datetime import datetime
from typing import Optional

BINANCE_BASE = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"

_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        # Use ThreadedResolver to avoid aiodns DNS issues on Windows/Python 3.14
        connector = aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(),
            ssl=True,
            limit=50,
            keepalive_timeout=30
        )
        _session = aiohttp.ClientSession(
            connector=connector,
            connector_owner=True,
            timeout=aiohttp.ClientTimeout(total=15, connect=5)
        )
    return _session


async def close_session():
    """Fecha a session HTTP global do data_fetcher."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


async def fetch(url: str, params: dict = None) -> dict | list:
    session = await get_session()
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
        r.raise_for_status()
        return await r.json()


# ── Price & Ticker ────────────────────────────────────────────────────────────

async def get_ticker(symbol: str) -> dict:
    data = await fetch(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", {"symbol": symbol})
    return {
        "symbol": symbol,
        "price": float(data["lastPrice"]),
        "change_24h": float(data["priceChangePercent"]),
        "volume_24h": float(data["quoteVolume"]),
        "high_24h": float(data["highPrice"]),
        "low_24h": float(data["lowPrice"]),
    }


async def get_all_tickers() -> list:
    data = await fetch(f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
    return [
        {
            "symbol": d["symbol"],
            "price": float(d["lastPrice"]),
            "change_24h": float(d["priceChangePercent"]),
            "volume_24h": float(d["quoteVolume"]),
        }
        for d in data
        if d["symbol"].endswith("USDT")
    ]


# ── OHLCV ─────────────────────────────────────────────────────────────────────

async def get_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    data = await fetch(
        f"{BINANCE_BASE}/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    df = pd.DataFrame(data, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    for col in ["open", "high", "low", "close", "volume", "taker_buy_base", "taker_buy_quote"]:
        df[col] = df[col].astype(float)
    return df.set_index("timestamp")


# ── Book Ticker (spread bid/ask) ──────────────────────────────────────────────

async def get_book_ticker(symbol: str) -> dict:
    """Retorna melhor bid/ask e spread % atual."""
    try:
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/bookTicker", {"symbol": symbol})
        bid  = float(data["bidPrice"])
        ask  = float(data["askPrice"])
        mid  = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid > 0 else 0.0
        return {"bid": bid, "ask": ask, "spread_pct": round(spread_pct, 4)}
    except Exception:
        return {"bid": 0.0, "ask": 0.0, "spread_pct": 0.0}


_depth_cache = {}

async def get_orderbook_depth(symbol: str, limit: int = 20) -> dict:
    """
    Profundidade do orderbook com cache em memória de 5 segundos para evitar latência.
    """
    global _depth_cache
    now = time.time()
    cache_key = (symbol, limit)
    cached = _depth_cache.get(cache_key)
    if cached and now - cached["ts"] < 5.0:
        return cached["data"]

    try:
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        bids = [(float(b[0]), float(b[1])) for b in data.get("bids", [])]
        asks = [(float(a[0]), float(a[1])) for a in data.get("asks", [])]
        bid_vol   = sum(p * q for p, q in bids)
        ask_vol   = sum(p * q for p, q in asks)
        total_vol = bid_vol + ask_vol + 1e-9
        imbalance = (bid_vol - ask_vol) / total_vol
        best_bid  = bids[0][0] if bids else 0.0
        best_ask  = asks[0][0] if asks else 0.0
        mid       = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 0.0
        
        result = {
            "bid_depth_usdt": round(bid_vol, 0),
            "ask_depth_usdt": round(ask_vol, 0),
            "imbalance":      round(imbalance, 4),
            "spread_pct":     round(spread_pct, 4),
            "best_bid":       best_bid,
            "best_ask":       best_ask,
        }
        _depth_cache[cache_key] = {"ts": now, "data": result}
        return result
    except Exception:
        return {
            "bid_depth_usdt": 0.0, "ask_depth_usdt": 0.0,
            "imbalance": 0.0, "spread_pct": 0.0,
            "best_bid": 0.0, "best_ask": 0.0,
        }


# ── Funding Rate ──────────────────────────────────────────────────────────────

async def get_funding_rate(symbol: str) -> float:
    try:
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["lastFundingRate"]) * 100  # as %
    except Exception:
        return 0.0


# ── Open Interest ─────────────────────────────────────────────────────────────

async def get_open_interest(symbol: str) -> dict:
    try:
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/openInterest", {"symbol": symbol})
        oi = float(data["openInterest"])
        ticker = await get_ticker(symbol)
        oi_usdt = oi * ticker["price"]
        return {"oi_contracts": oi, "oi_usdt": oi_usdt}
    except Exception:
        return {"oi_contracts": 0, "oi_usdt": 0}


async def get_oi_history(symbol: str, period: str = "15m", limit: int = 50) -> list:
    try:
        data = await fetch(
            f"{BINANCE_BASE}/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "limit": limit},
        )
        return [{"time": d["timestamp"], "oi": float(d["sumOpenInterest"])} for d in data]
    except Exception:
        return []


# ── Long/Short Ratio ──────────────────────────────────────────────────────────

async def get_long_short_ratio(symbol: str, period: str = "15m") -> dict:
    try:
        data = await fetch(
            f"{BINANCE_BASE}/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": period, "limit": 1},
        )
        if data:
            return {
                "long_pct": float(data[0]["longAccount"]) * 100,
                "short_pct": float(data[0]["shortAccount"]) * 100,
                "ratio": float(data[0]["longShortRatio"]),
            }
    except Exception:
        pass
    return {"long_pct": 50, "short_pct": 50, "ratio": 1.0}


# ── Liquidations (approx via large trades) ───────────────────────────────────

async def get_liquidations(symbol: str, limit: int = 20) -> list:
    try:
        data = await fetch(
            f"{BINANCE_BASE}/fapi/v1/allForceOrders",
            {"symbol": symbol, "limit": limit},
        )
        return [
            {
                "side": d["side"],
                "qty": float(d["origQty"]),
                "price": float(d["price"]),
                "time": d["time"],
            }
            for d in data
        ]
    except Exception:
        return []


# ── News (CryptoCompare public) ───────────────────────────────────────────────

async def get_crypto_news(limit: int = 10) -> list:
    try:
        data = await fetch(
            "https://min-api.cryptocompare.com/data/v2/news/",
            {"lang": "EN", "sortOrder": "latest"},
        )
        items = data.get("Data", [])[:limit]
        return [
            {
                "title": n["title"],
                "source": n["source_info"]["name"],
                "url": n["url"],
                "published": datetime.utcfromtimestamp(n["published_on"]).isoformat(),
                "sentiment": _score_news_sentiment(n["title"]),
            }
            for n in items
        ]
    except Exception:
        return []


def _score_news_sentiment(title: str) -> str:
    title_lower = title.lower()
    bullish_kw = ["rally", "surge", "pump", "bull", "breakout", "ath", "adoption", "buy",
                   "rise", "gain", "upside", "long", "positive", "approve", "etf", "launch"]
    bearish_kw = ["crash", "dump", "bear", "sell", "drop", "fall", "hack", "ban", "risk",
                   "fear", "negative", "liquidat", "short", "warning", "fraud", "lawsuit"]
    bull = sum(1 for w in bullish_kw if w in title_lower)
    bear = sum(1 for w in bearish_kw if w in title_lower)
    if bull > bear: return "BULLISH"
    if bear > bull: return "BEARISH"
    return "NEUTRAL"


# ── Trending Futures (top movers da Binance) ─────────────────────────────────

async def get_trending_futures(top_n: int = 10) -> list[str]:
    """
    Retorna os top N símbolos da Binance Futuros com movimentação atípica:
    - Maior variação de preço nas últimas 24h (abs)
    - Maior variação de volume (janela 4h)
    Exclui stablecoins e tokens muito pequenos.
    """
    try:
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
        tickers = []
        for d in data:
            sym = d["symbol"]
            if not sym.endswith("USDT"):
                continue
            # Exclui stablecoins e tokens suspeitos
            base = sym.replace("USDT", "")
            if base in {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "FRAX"}:
                continue
            vol_24h = float(d.get("quoteVolume", 0))
            vol_4h  = vol_24h / 6  # aprox. volume das ultimas 4h
            if vol_4h < 833_000:  # minimo ~$833K/4h (equiv. $5M/24h)
                continue
            change = abs(float(d.get("priceChangePercent", 0)))
            count = int(d.get("count", 0))
            tickers.append({
                "symbol": sym,
                "change_abs": change,
                "volume_usdt": vol_4h,  # usa 4h para score e filtro
                "trades": count,
            })

        # Score combinado: variação * log(volume)
        import math
        for t in tickers:
            t["score"] = t["change_abs"] * math.log10(max(t["volume_usdt"], 1))

        tickers.sort(key=lambda x: x["score"], reverse=True)
        return [t["symbol"] for t in tickers[:top_n]]
    except Exception as e:
        print(f"[TRENDING] Erro: {e}")
        return []


async def get_oi_spike_symbols(top_n: int = 5) -> list[str]:
    """Símbolos com maior crescimento de OI recente (possível pump/dump iminente)."""
    try:
        # Pega OI atual de todos os futuros USDT
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/openInterest")
        # Não existe endpoint de lista; usamos tickers com volume alto e pegamos OI individualmente
        tickers_raw = await fetch(f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
        high_vol = [
            d["symbol"] for d in tickers_raw
            if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) > 20_000_000
        ][:30]

        oi_data = await asyncio.gather(*[
            fetch(f"{BINANCE_BASE}/fapi/v1/openInterest", {"symbol": s})
            for s in high_vol
        ], return_exceptions=True)

        results = []
        for sym, oi in zip(high_vol, oi_data):
            if isinstance(oi, Exception):
                continue
            results.append({"symbol": sym, "oi": float(oi.get("openInterest", 0))})

        # Sem histórico neste endpoint, retornamos os maiores OI absolutos
        results.sort(key=lambda x: x["oi"], reverse=True)
        return [r["symbol"] for r in results[:top_n]]
    except Exception:
        return []


# ── Market Snapshot ───────────────────────────────────────────────────────────

async def get_market_snapshot() -> dict:
    btc_ticker, eth_ticker, sol_ticker, btc_fr, eth_fr, btc_oi, ls_ratio = await asyncio.gather(
        get_ticker("BTCUSDT"),
        get_ticker("ETHUSDT"),
        get_ticker("SOLUSDT"),
        get_funding_rate("BTCUSDT"),
        get_funding_rate("ETHUSDT"),
        get_open_interest("BTCUSDT"),
        get_long_short_ratio("BTCUSDT"),
        return_exceptions=True,
    )

    def safe(v, default=0):
        return default if isinstance(v, Exception) else v

    btc_ticker = safe(btc_ticker, {"price": 0, "change_24h": 0})
    eth_ticker = safe(eth_ticker, {"price": 0, "change_24h": 0})
    sol_ticker = safe(sol_ticker, {"price": 0, "change_24h": 0})
    btc_fr = safe(btc_fr, 0)
    eth_fr = safe(eth_fr, 0)
    btc_oi = safe(btc_oi, {"oi_usdt": 0})
    ls_ratio = safe(ls_ratio, {"long_pct": 50, "short_pct": 50})

    # Sentiment heuristic
    avg_change = (btc_ticker["change_24h"] + eth_ticker["change_24h"]) / 2
    if avg_change > 3:
        sentiment = "GREED"
    elif avg_change > 0:
        sentiment = "NEUTRAL-BULLISH"
    elif avg_change > -3:
        sentiment = "NEUTRAL-BEARISH"
    else:
        sentiment = "FEAR"

    return {
        "btc_price": btc_ticker["price"],
        "btc_change_24h": btc_ticker["change_24h"],
        "btc_funding": btc_fr,
        "btc_oi_usdt": btc_oi["oi_usdt"],
        "eth_price": eth_ticker["price"],
        "eth_funding": eth_fr,
        "eth_change_24h": eth_ticker["change_24h"],
        "sol_price": sol_ticker["price"],
        "sol_change_24h": sol_ticker["change_24h"],
        "market_sentiment": sentiment,
        "long_bias": ls_ratio["long_pct"],
        "short_bias": ls_ratio["short_pct"],
        "timestamp": datetime.utcnow().isoformat(),
    }
