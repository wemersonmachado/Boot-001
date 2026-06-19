"""
Market Intelligence Engine — TRADER 001
========================================
Roda em background a cada 60 minutos.
NUNCA bloqueia o Signal Engine ou a execucao na Binance.
O Signal Engine apenas le o cache em memoria (leitura instantanea).

Fontes:
  - Binance Futures (tendencia multi-TF, funding, OI, L/S, movers)
  - CoinGecko   (dominancia BTC, market cap global, trending coins)
  - CoinMarketCap (metricas globais — opcional, requer CMC_API_KEY no .env)
  - CryptoPanic (headlines com votos bullish/bearish)
  - Fear & Greed (alternative.me — ja integrado)
  - Macro Calendar (eventos FED/CPI/Payroll hardcoded 2026)
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

# ── Cache em memoria — leitura instantanea, nunca bloqueia ───────────────────

_market_state: dict = {
    "market_bias":       "Neutral",
    "confidence":        50,
    "trend_score":       50,
    "trend_bias":        "Neutral",
    "trend_timeframes":  {},
    "news_score":        0,
    "news_label":        "Neutral",
    "news_headlines":    [],
    "news_bullish":      0,
    "news_bearish":      0,
    "sentiment_score":   0,
    "sentiment_value":   50,
    "sentiment_label":   "Neutral",
    "dominance_score":   0,
    "btc_dominance":     50.0,
    "mkt_cap_change_24h": 0.0,
    "total_market_cap":  0,
    "active_cryptos":    0,
    "trending_coins":    [],
    "trending_cmc":      [],
    "new_listings_cmc":  [],
    "binance_score":     0,
    "btc_funding":       0.0,
    "eth_funding":       0.0,
    "oi_change_24h":     0.0,
    "long_short_ratio":  1.0,
    "top_gainers":       [],
    "top_losers":        [],
    "macro_score":       0,
    "macro_events":      [],
    "next_macro_event":  None,
    "binance_announcements": [],
    "cmc":               {},
    "score_breakdown":   {},
    "social_score":      0.0,
    "social_label":      "Neutral",
    "social_buzz":       0,
    "social_bullish":    0,
    "social_bearish":    0,
    "social_topics":     [],
    "last_update":       None,
    "next_update":       None,
    "refresh_time_s":    0,
    "status":            "initializing",
}

_last_refresh: float  = 0
REFRESH_INTERVAL: int = 3600   # 60 min
STATE_FILE = os.path.join(os.path.dirname(__file__), "market_state.json")


# ── API publica ───────────────────────────────────────────────────────────────

def get_market_state() -> dict:
    """Leitura instantanea do cache. Zero latencia para o Signal Engine."""
    return _market_state.copy()


def get_bias_score_adjustment(direction: str) -> float:
    """
    Retorna ajuste de confianca (+/-) baseado no vies de mercado.
    Chamado pelo job_auto_trade() em main.py antes de confirmar sinal.
    Nunca bloqueia — apenas le o cache.
    """
    bias = _market_state.get("market_bias", "Neutral")
    conf = _market_state.get("confidence", 50)

    if bias == "Neutral" or conf < 55:
        return 0.0

    strength = (conf - 50) / 50.0          # 0.0 a 1.0
    max_adj  = 10.0 * strength              # max +/-10 pts

    is_long  = "LONG"  in direction.upper()
    is_short = "SHORT" in direction.upper()

    if bias == "Bullish":
        return +max_adj if is_long else -max_adj * 0.5
    elif bias == "Bearish":
        return +max_adj if is_short else -max_adj * 0.5
    return 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _fetch(session: aiohttp.ClientSession, url: str, headers: dict = None,
                 params: dict = None) -> Optional[dict]:
    try:
        kw = {"timeout": aiohttp.ClientTimeout(total=10)}
        if headers: kw["headers"] = headers
        if params:  kw["params"]  = params
        async with session.get(url, **kw) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception as e:
        print(f"[MIE] fetch err {url[:60]}: {type(e).__name__}")
    return None


def _ema(data: list, period: int) -> list:
    if len(data) < period:
        return [data[-1]] * len(data) if data else [0]
    k = 2 / (period + 1)
    result = [sum(data[:period]) / period]
    for v in data[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


# ── 1. Trend Analysis — Binance multi-TF ────────────────────────────────────

async def _analyze_trend(session: aiohttp.ClientSession) -> dict:
    """
    BTC em 5 timeframes (1M/1W/1D/4H/1H) para determinar vies macro.
    Score 0-100. Peso maior para TFs maiores.
    """
    configs = [
        ("1M", 0.30, "Mensal"),
        ("1w", 0.25, "Semanal"),
        ("1d", 0.20, "Diario"),
        ("4h", 0.15, "4 Horas"),
        ("1h", 0.10, "1 Hora"),
    ]

    tf_results = {}
    tasks = []
    for tf, _, _ in configs:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={tf}&limit=50"
        tasks.append(_fetch(session, url))

    raw = await asyncio.gather(*tasks, return_exceptions=True)

    weighted = 0.0
    for i, (tf, weight, label) in enumerate(configs):
        data = raw[i] if not isinstance(raw[i], Exception) else None
        score = 50  # default neutral
        if data and len(data) >= 20:
            try:
                closes = [float(k[4]) for k in data]
                n = len(closes)
                ema20 = _ema(closes, min(20, n))[-1]
                ema50 = _ema(closes, min(50, n))[-1]
                cur   = closes[-1]

                # Posicao relativa do preco em relacao as EMAs
                if cur > ema20 > ema50:
                    score = min(100, 72 + (cur - ema20) / ema20 * 500)
                elif cur > ema20:
                    score = 62
                elif cur > ema50:
                    score = 50
                elif cur < ema20 < ema50:
                    score = max(0, 28 - (ema20 - cur) / ema20 * 500)
                else:
                    score = 38
                score = max(0, min(100, score))
            except Exception:
                pass

        tf_results[tf] = {"score": round(score), "label": label}
        weighted += score * weight

    weighted = round(weighted)
    bias = "Bullish" if weighted > 58 else "Bearish" if weighted < 42 else "Neutral"
    return {"score": weighted, "bias": bias, "timeframes": tf_results}


# ── 2. News — CryptoPanic + fallback keyword ─────────────────────────────────

_BULLISH_KW = {"rally","surge","gain","inflow","high","bull","pump","up","rise",
               "adopt","approve","etf","accumulate","buy","long"}
_BEARISH_KW = {"crash","drop","dump","outflow","bear","sell","short","hack",
               "breach","lawsuit","ban","decline","loss","fear","panic","warning"}


async def _analyze_news(session: aiohttp.ClientSession) -> dict:
    """
    1) Tenta CryptoPanic (votes-based sentiment — mais preciso)
    2) Fallback: analise de keywords nas headlines existentes
    """
    url  = "https://cryptopanic.com/api/v1/posts/?public=true&filter=hot&currencies=BTC,ETH,SOL"
    data = await _fetch(session, url)

    headlines = []
    bull_count = 0
    bear_count = 0

    if data and "results" in data:
        results = data["results"][:20]
        for r in results:
            title = r.get("title", "")
            headlines.append(title[:90])
            pos = r.get("votes", {}).get("positive", 0) or 0
            neg = r.get("votes", {}).get("negative", 0) or 0
            if pos > neg:   bull_count += 1
            elif neg > pos: bear_count += 1
    else:
        # Fallback: CryptoCompare news via data_fetcher
        try:
            from data_fetcher import get_crypto_news
            news_items = await get_crypto_news()
            for item in news_items[:15]:
                title = (item.get("title") or "").lower()
                headlines.append(title[:90])
                b = sum(1 for w in _BULLISH_KW if w in title)
                n = sum(1 for w in _BEARISH_KW if w in title)
                if b > n:   bull_count += 1
                elif n > b: bear_count += 1
        except Exception:
            pass

    total = bull_count + bear_count or 1
    ratio = (bull_count - bear_count) / total
    score = round(max(-2.0, min(2.0, ratio * 2)), 1)
    label = "Bullish" if score > 0.3 else "Bearish" if score < -0.3 else "Neutral"

    return {
        "score": score, "label": label,
        "headlines": headlines[:6],
        "bullish": bull_count, "bearish": bear_count,
    }


# ── 3. Sentiment — Fear & Greed (contrarian) ─────────────────────────────────

async def _analyze_sentiment(session: aiohttp.ClientSession) -> dict:
    try:
        from fear_greed import get_fear_greed
        fg    = await get_fear_greed()
        val   = fg.get("value", 50)
        label = fg.get("label", "Neutral")
        # Contrarian: Extreme Fear = oportunidade de compra (bullish)
        if   val <= 20: score = +2.0
        elif val <= 35: score = +1.0
        elif val <= 55: score =  0.0
        elif val <= 75: score = -1.0
        else:           score = -2.0
        return {"score": score, "value": val, "label": label}
    except Exception:
        return {"score": 0.0, "value": 50, "label": "Neutral"}


# ── 3b. Social Sentiment — CryptoPanic engagement-weighted ──────────────────

async def _analyze_social_sentiment(session: aiohttp.ClientSession) -> dict:
    """
    Sentimento social via CryptoPanic (rising/hot posts) ponderado por engajamento.
    Diferente de _analyze_news: foca em likes, saves e votos — não só em keywords.
    Identifica também quais moedas estão dominando a conversa social.
    """
    result = {
        "score": 0.0, "label": "Neutral",
        "buzz": 0, "bullish": 0, "bearish": 0, "topics": [],
    }
    try:
        # Rising = posts com maior aceleração de engajamento recente
        url  = "https://cryptopanic.com/api/v1/posts/?public=true&filter=rising&kind=news,media"
        data = await _fetch(session, url)
        if not data or "results" not in data:
            url  = "https://cryptopanic.com/api/v1/posts/?public=true&filter=hot"
            data = await _fetch(session, url)

        if not data or "results" not in data:
            return result

        posts = data["results"][:30]
        bull = 0
        bear = 0
        topic_counts: dict = {}

        for post in posts:
            title  = (post.get("title") or "").lower()
            votes  = post.get("votes", {}) or {}
            pos    = int(votes.get("positive", 0) or 0)
            neg    = int(votes.get("negative", 0) or 0)
            saved  = int(votes.get("saved", 0) or 0)
            liked  = int(votes.get("liked", 0) or 0)

            # Peso proporcional ao engajamento (posts virais valem mais)
            weight = 1 + min((pos + neg + saved + liked) // 5, 5)

            b_kw  = sum(1 for w in _BULLISH_KW if w in title)
            ba_kw = sum(1 for w in _BEARISH_KW if w in title)

            if pos > neg * 1.3 or b_kw > ba_kw:
                bull += weight
            elif neg > pos * 1.3 or ba_kw > b_kw:
                bear += weight

            for c in post.get("currencies", [])[:3]:
                code = c.get("code", "")
                if code:
                    topic_counts[code] = topic_counts.get(code, 0) + 1

        total = bull + bear or 1
        ratio = (bull - bear) / total
        score = round(max(-2.0, min(2.0, ratio * 2)), 1)
        label = "Bullish" if score > 0.3 else "Bearish" if score < -0.3 else "Neutral"
        topics = [
            {"symbol": k, "mentions": v}
            for k, v in sorted(topic_counts.items(), key=lambda x: -x[1])[:5]
        ]
        result.update({"score": score, "label": label,
                        "buzz": bull + bear, "bullish": bull,
                        "bearish": bear, "topics": topics})
    except Exception as e:
        print(f"[MIE] social_sentiment err: {type(e).__name__}")

    return result


# ── 4. CoinGecko — Global + Trending ─────────────────────────────────────────

async def _analyze_coingecko(session: aiohttp.ClientSession) -> dict:
    headers = {"accept": "application/json"}
    g_url = "https://api.coingecko.com/api/v3/global"
    t_url = "https://api.coingecko.com/api/v3/search/trending"

    g_data, t_data = await asyncio.gather(
        _fetch(session, g_url, headers=headers),
        _fetch(session, t_url, headers=headers),
        return_exceptions=True
    )

    result = {
        "btc_dominance": 50.0, "total_market_cap_usd": 0,
        "market_cap_change_24h": 0.0, "active_cryptos": 0,
        "trending": [], "dominance_score": 0,
        "eth_dominance": 0.0, "defi_vol_pct": 0.0,
    }

    if isinstance(g_data, dict) and "data" in g_data:
        d       = g_data["data"]
        btc_dom = float(d.get("market_cap_percentage", {}).get("btc", 50))
        eth_dom = float(d.get("market_cap_percentage", {}).get("eth", 0))
        mkt_chg = float(d.get("market_cap_change_percentage_24h_usd", 0))
        result.update({
            "btc_dominance":         round(btc_dom, 1),
            "eth_dominance":         round(eth_dom, 1),
            "total_market_cap_usd":  d.get("total_market_cap", {}).get("usd", 0),
            "market_cap_change_24h": round(mkt_chg, 2),
            "active_cryptos":        d.get("active_cryptocurrencies", 0),
        })
        # Dominancia BTC: >58% = alts sofrendo (bearish alts)
        # <45% = alt season (bullish)
        if   btc_dom > 58: result["dominance_score"] = -1
        elif btc_dom < 45: result["dominance_score"] = +1
        else:              result["dominance_score"] = 0

    if isinstance(t_data, dict) and "coins" in t_data:
        result["trending"] = [
            {
                "name":   c["item"]["name"],
                "symbol": c["item"]["symbol"].upper(),
                "rank":   c["item"].get("market_cap_rank") or 0,
                "thumb":  c["item"].get("small", ""),
            }
            for c in t_data["coins"][:7]
        ]

    return result


# ── 5. CoinMarketCap — dominância, categorias, métricas globais ───────────────

async def _analyze_cmc(session: aiohttp.ClientSession, api_key: str) -> dict:
    """
    Usa o cmc_client centralizado. Retorna métricas globais + categorias quentes.
    Substituiu chamada direta — cache inteligente no cmc_client evita desperdício de quota.
    """
    if not api_key:
        return {}
    try:
        from cmc_client import (
            get_global_metrics, get_categories, get_hot_sectors,
            get_trending_symbols, get_new_listings,
        )
        metrics, categories, trending_syms, new_lst = await asyncio.gather(
            get_global_metrics(),
            get_categories(60),
            get_trending_symbols(),
            get_new_listings(),
            return_exceptions=True,
        )

        def _safe_r(v, default): return v if not isinstance(v, Exception) else default
        metrics      = _safe_r(metrics, {})
        categories   = _safe_r(categories, [])
        trending_syms= _safe_r(trending_syms, set())
        new_lst      = _safe_r(new_lst, [])

        if not metrics:
            return {}

        hot      = get_hot_sectors(categories, min_change=5.0)
        top_cats = sorted(categories, key=lambda c: abs(c.get("avg_change_24h", 0)), reverse=True)[:5]

        btc_dom = metrics.get("btc_dominance", 50.0)
        dom_signal = (
            "BTC_SEASON" if btc_dom > 55 else
            "ALT_SEASON" if btc_dom < 45 else
            "NEUTRAL"
        )

        return {
            "total_market_cap":        round(metrics.get("total_market_cap", 0) / 1e12, 2),
            "total_volume_24h":        round(metrics.get("total_volume_24h", 0) / 1e9, 1),
            "btc_dominance":           metrics.get("btc_dominance", 50.0),
            "eth_dominance":           metrics.get("eth_dominance", 20.0),
            "altcoin_dominance":       metrics.get("altcoin_dominance", 30.0),
            "active_cryptocurrencies": metrics.get("active_cryptocurrencies", 0),
            "dom_signal":              dom_signal,
            "altseason":               metrics.get("btc_dom_bearish_alt", False),
            "high_stablecoin_ratio":   metrics.get("high_stablecoin_ratio", False),
            "hot_sectors":             hot[:5],
            "top_sectors":             [{"name": c["name"], "chg": c["avg_change_24h"]} for c in top_cats],
            "defi_volume_24h_b":       round(metrics.get("defi_volume_24h", 0) / 1e9, 1),
            "trending_cmc":            sorted(trending_syms),
            "new_listings_cmc":        new_lst[:10],
        }
    except Exception as e:
        print(f"[MIE] CMC client erro: {e}")
        return {}


# ── 5b. CoinMarketCal — Calendário de eventos ────────────────────────────────

async def _fetch_crypto_events() -> list[dict]:
    """Busca eventos cripto dos próximos 7 dias via CoinMarketCal."""
    try:
        from coinmarketcal_client import get_upcoming_events, set_global_events
        events = await get_upcoming_events(days_ahead=7)
        set_global_events(events)  # disponibiliza para o signal_engine
        return events
    except Exception as e:
        print(f"[MIE] CoinMarketCal erro: {e}")
        return []


# ── 6. Binance Metrics ────────────────────────────────────────────────────────

async def _analyze_binance(session: aiohttp.ClientSession) -> dict:
    urls = {
        "fr_btc":  "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=3",
        "fr_eth":  "https://fapi.binance.com/fapi/v1/fundingRate?symbol=ETHUSDT&limit=3",
        "oi_hist": "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=24",
        "ls_ratio":"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=1",
        "tickers": "https://fapi.binance.com/fapi/v1/ticker/24hr",
    }
    results = {}
    for k, url in urls.items():
        results[k] = await _fetch(session, url)

    out = {
        "btc_funding": 0.0, "eth_funding": 0.0,
        "oi_change_24h": 0.0, "long_short_ratio": 1.0,
        "top_gainers": [], "top_losers": [], "score": 0.0,
    }

    if results["fr_btc"]:
        rates = [float(r["fundingRate"]) for r in results["fr_btc"]]
        out["btc_funding"] = round(sum(rates) / len(rates) * 100, 4)
    if results["fr_eth"]:
        rates = [float(r["fundingRate"]) for r in results["fr_eth"]]
        out["eth_funding"] = round(sum(rates) / len(rates) * 100, 4)

    if results["oi_hist"] and len(results["oi_hist"]) >= 2:
        oi_now = float(results["oi_hist"][-1]["sumOpenInterest"])
        oi_24h = float(results["oi_hist"][0]["sumOpenInterest"])
        if oi_24h > 0:
            out["oi_change_24h"] = round((oi_now - oi_24h) / oi_24h * 100, 2)

    if results["ls_ratio"] and isinstance(results["ls_ratio"], list) and results["ls_ratio"]:
        out["long_short_ratio"] = round(float(results["ls_ratio"][0].get("longShortRatio", 1)), 3)

    if results["tickers"]:
        tickers = [t for t in results["tickers"]
                   if t.get("symbol", "").endswith("USDT")
                   and float(t.get("quoteVolume", 0)) > 30_000_000]
        sorted_t = sorted(tickers, key=lambda t: float(t.get("priceChangePercent", 0)), reverse=True)
        out["top_gainers"] = [
            {"symbol": t["symbol"], "change": round(float(t["priceChangePercent"]), 2),
             "vol_m": round(float(t.get("quoteVolume", 0)) / 1e6, 0)}
            for t in sorted_t[:5]
        ]
        out["top_losers"] = [
            {"symbol": t["symbol"], "change": round(float(t["priceChangePercent"]), 2),
             "vol_m": round(float(t.get("quoteVolume", 0)) / 1e6, 0)}
            for t in sorted_t[-5:][::-1]
        ]

    # Score Binance
    fr_avg = (out["btc_funding"] + out["eth_funding"]) / 2
    score  = 0.0
    if fr_avg > 0.05:    score -= 1.0   # funding alto = longs overextended
    elif fr_avg < -0.01: score += 1.0   # funding negativo = contrarian bullish
    if out["oi_change_24h"] > 5:    score += 1.0   # OI crescendo = momentum
    elif out["oi_change_24h"] < -5: score -= 1.0   # OI caindo = desmontagem
    if out["long_short_ratio"] > 1.25: score -= 0.5  # excesso de longs
    elif out["long_short_ratio"] < 0.80: score += 0.5 # excesso de shorts
    out["score"] = round(max(-2.0, min(2.0, score)), 1)
    return out


# ── 7. Binance Announcements ──────────────────────────────────────────────────

async def _fetch_binance_announcements(session: aiohttp.ClientSession) -> list:
    url    = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    params = {"type": "1", "pageNo": "1", "pageSize": "6"}
    data   = await _fetch(session, url, params=params)
    if not data:
        return []
    articles = data.get("data", {}).get("articles", []) or []
    return [
        {
            "title": (a.get("title") or "")[:80],
            "date":  str(a.get("releaseDate", ""))[:10],
            "id":    a.get("id", ""),
        }
        for a in articles[:5]
    ]


# ── 8. Macro Events Calendar ──────────────────────────────────────────────────

_MACRO_CALENDAR_2026 = [
    # FED Meetings
    {"name": "FED Meeting",    "date": "2026-06-17", "impact": "HIGH",   "type": "FED"},
    {"name": "FED Meeting",    "date": "2026-07-29", "impact": "HIGH",   "type": "FED"},
    {"name": "FED Meeting",    "date": "2026-09-16", "impact": "HIGH",   "type": "FED"},
    {"name": "FED Meeting",    "date": "2026-11-04", "impact": "HIGH",   "type": "FED"},
    {"name": "FED Meeting",    "date": "2026-12-16", "impact": "HIGH",   "type": "FED"},
    # CPI
    {"name": "CPI Release",    "date": "2026-06-10", "impact": "HIGH",   "type": "CPI"},
    {"name": "CPI Release",    "date": "2026-07-14", "impact": "HIGH",   "type": "CPI"},
    {"name": "CPI Release",    "date": "2026-08-12", "impact": "HIGH",   "type": "CPI"},
    {"name": "CPI Release",    "date": "2026-09-10", "impact": "HIGH",   "type": "CPI"},
    # PPI
    {"name": "PPI Release",    "date": "2026-06-11", "impact": "MEDIUM", "type": "PPI"},
    {"name": "PPI Release",    "date": "2026-07-15", "impact": "MEDIUM", "type": "PPI"},
    # Payroll (NFP)
    {"name": "Payroll (NFP)",  "date": "2026-07-02", "impact": "MEDIUM", "type": "MACRO"},
    {"name": "Payroll (NFP)",  "date": "2026-08-07", "impact": "MEDIUM", "type": "MACRO"},
    {"name": "Payroll (NFP)",  "date": "2026-09-04", "impact": "MEDIUM", "type": "MACRO"},
    # ETF Reviews
    {"name": "BTC ETF Review", "date": "2026-07-01", "impact": "HIGH",   "type": "ETF"},
]


_dynamic_macro_events = []


async def _fetch_forex_factory_calendar(session: aiohttp.ClientSession) -> list[dict]:
    """Busca calendário econômico da semana e filtra eventos relevantes de USD."""
    global _dynamic_macro_events
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        kw = {"timeout": aiohttp.ClientTimeout(total=10), "headers": {"User-Agent": "Mozilla/5.0"}}
        async with session.get(url, **kw) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                events = []
                for ev in data:
                    if ev.get("country") == "USD" and ev.get("impact") in ("High", "Medium"):
                        events.append({
                            "name": ev.get("title"),
                            "date": ev.get("date"),
                            "impact": ev.get("impact").upper(),
                            "type": "MACRO"
                        })
                _dynamic_macro_events = events
                print(f"[MACRO-SCRAPER] {len(events)} eventos macro carregados de Forex Factory.")
                return events
    except Exception as e:
        print(f"[MACRO-SCRAPER] Erro ao buscar Forex Factory calendar: {e}")
    return []


def _get_macro_events() -> dict:
    now      = datetime.utcnow()
    upcoming = []
    score    = 0.0
    
    # Une a lista estática com a lista dinâmica (Forex Factory)
    all_events = list(_MACRO_CALENDAR_2026)
    seen_names = {ev["name"] + "@" + ev["date"][:10] for ev in _MACRO_CALENDAR_2026}
    
    for ev in _dynamic_macro_events:
        ev_date_str = ev["date"][:10]
        key = ev["name"] + "@" + ev_date_str
        if key not in seen_names:
            seen_names.add(key)
            all_events.append(ev)

    for ev in all_events:
        try:
            if "T" in ev["date"]:
                # Ex: 2026-06-14T18:30:00-04:00
                ev_dt = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc).replace(tzinfo=None)
            else:
                ev_dt = datetime.strptime(ev["date"], "%Y-%m-%d")
                
            days_away = (ev_dt - now).days
            time_diff = ev_dt - now
            hours_away = time_diff.total_seconds() / 3600.0
            
            # Se for hoje, amanhã ou até 30 dias pra frente
            if -24 <= hours_away <= 720:
                entry = {
                    "name": ev["name"],
                    "date": ev["date"],
                    "impact": ev["impact"],
                    "type": ev.get("type", "MACRO"),
                    "days_away": max(0, days_away),
                    "hours_away": round(hours_away, 2)
                }
                upcoming.append(entry)
                
                if ev["impact"] == "HIGH":
                    if days_away <= 1:
                        score -= 1.0
                    elif days_away <= 3:
                        score -= 0.5
                    elif days_away <= 7:
                        score -= 0.2
        except Exception:
            pass

    upcoming.sort(key=lambda e: e.get("hours_away", e.get("days_away") * 24))
    return {
        "upcoming":   upcoming[:10],
        "score":      round(max(-2.0, min(2.0, score)), 1),
        "next_event": upcoming[0] if upcoming else None,
    }


# ── Master Refresh ────────────────────────────────────────────────────────────

async def refresh_market_intelligence() -> dict:
    """
    Atualiza TODOS os dados em paralelo.
    Chamado a cada 60 min pelo scheduler do main.py.
    Tempo medio: 8-15 segundos (assincorno, nao bloqueia nada).
    """
    global _market_state, _last_refresh

    print("[MIE] Iniciando refresh Market Intelligence Engine...")
    t0 = time.time()

    # Config opcional
    try:
        from config import CMC_API_KEY
    except (ImportError, AttributeError):
        CMC_API_KEY = ""

    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        (
            trend, news, sentiment, social, coingecko,
            binance_m, cmc, announcements, crypto_events,
            forex_macro,
        ) = await asyncio.gather(
            _analyze_trend(session),
            _analyze_news(session),
            _analyze_sentiment(session),
            _analyze_social_sentiment(session),
            _analyze_coingecko(session),
            _analyze_binance(session),
            _analyze_cmc(session, CMC_API_KEY),
            _fetch_binance_announcements(session),
            _fetch_crypto_events(),
            _fetch_forex_factory_calendar(session),
            return_exceptions=True,
        )

    # Fallbacks seguros se alguma fonte falhar
    def _safe(v, default):
        return v if not isinstance(v, Exception) else default

    trend       = _safe(trend,       {"score": 50, "bias": "Neutral", "timeframes": {}})
    news        = _safe(news,        {"score": 0, "label": "Neutral", "headlines": [], "bullish": 0, "bearish": 0})
    sentiment   = _safe(sentiment,   {"score": 0, "value": 50, "label": "Neutral"})
    social      = _safe(social,      {"score": 0.0, "label": "Neutral", "buzz": 0,
                                      "bullish": 0, "bearish": 0, "topics": []})
    coingecko   = _safe(coingecko,   {"btc_dominance": 50, "trending": [], "dominance_score": 0,
                                      "market_cap_change_24h": 0, "total_market_cap_usd": 0, "active_cryptos": 0})
    binance_m   = _safe(binance_m,   {"score": 0, "btc_funding": 0, "eth_funding": 0,
                                      "oi_change_24h": 0, "long_short_ratio": 1.0,
                                      "top_gainers": [], "top_losers": []})
    cmc           = _safe(cmc,           {})
    announcements = _safe(announcements, [])
    crypto_events = _safe(crypto_events, [])
    forex_macro   = _safe(forex_macro, [])
    macro         = _get_macro_events()

    # ── Score Global Ponderado ─────────────────────────────────────────────
    # Normaliza trend (0-100) -> (-2 a +2)
    trend_norm = (trend["score"] - 50) / 25.0

    weights = {
        "trend":     (trend_norm,                           0.38),
        "news":      (news["score"],                        0.12),
        "sentiment": (sentiment["score"],                   0.18),
        "social":    (social.get("score", 0.0),             0.07),
        "dominance": (coingecko.get("dominance_score", 0),  0.10),
        "binance":   (binance_m.get("score", 0),            0.10),
        "macro":     (macro["score"],                       0.05),
    }

    total_score    = sum(v * w for v, w in weights.values())
    score_breakdown = {k: round(v * w, 3) for k, (v, w) in weights.items()}

    if total_score > 0.40:
        bias       = "Bullish"
        confidence = min(100, int(50 + total_score * 25))
    elif total_score < -0.40:
        bias       = "Bearish"
        confidence = min(100, int(50 + abs(total_score) * 25))
    else:
        bias       = "Neutral"
        confidence = max(30, int(50 - abs(total_score) * 10))

    now_str  = datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")
    next_str = (datetime.utcnow() + timedelta(hours=1)).strftime("%H:%M UTC")
    elapsed  = round(time.time() - t0, 1)

    new_state = {
        "market_bias":        bias,
        "confidence":         confidence,
        "trend_score":        trend["score"],
        "trend_bias":         trend.get("bias", "Neutral"),
        "trend_timeframes":   trend.get("timeframes", {}),
        "news_score":         news["score"],
        "news_label":         news.get("label", "Neutral"),
        "news_headlines":     news.get("headlines", []),
        "news_bullish":       news.get("bullish", 0),
        "news_bearish":       news.get("bearish", 0),
        "sentiment_score":    sentiment["score"],
        "sentiment_value":    sentiment.get("value", 50),
        "sentiment_label":    sentiment.get("label", "Neutral"),
        "dominance_score":    coingecko.get("dominance_score", 0),
        # BTC dominance: CMC tem prioridade sobre CoinGecko (mais preciso)
        "btc_dominance":      cmc.get("btc_dominance") or coingecko.get("btc_dominance", 50.0),
        "eth_dominance":      cmc.get("eth_dominance") or coingecko.get("eth_dominance", 0.0),
        "altcoin_dominance":  cmc.get("altcoin_dominance", 0.0),
        "dom_signal":         cmc.get("dom_signal", "NEUTRAL"),
        "altseason":          cmc.get("altseason", False),
        "high_stablecoin_ratio": cmc.get("high_stablecoin_ratio", False),
        "hot_sectors":        cmc.get("hot_sectors", []),
        "top_sectors":        cmc.get("top_sectors", []),
        "mkt_cap_change_24h": coingecko.get("market_cap_change_24h", 0.0),
        "total_market_cap":   cmc.get("total_market_cap") or coingecko.get("total_market_cap_usd", 0),
        "active_cryptos":     cmc.get("active_cryptocurrencies") or coingecko.get("active_cryptos", 0),
        "trending_coins":     coingecko.get("trending", []),
        "trending_cmc":       cmc.get("trending_cmc", []),
        "new_listings_cmc":   cmc.get("new_listings_cmc", []),
        # Calendário de eventos
        "crypto_events":      crypto_events[:20],
        "events_count":       len(crypto_events),
        "binance_score":      binance_m.get("score", 0),
        "btc_funding":        binance_m.get("btc_funding", 0.0),
        "eth_funding":        binance_m.get("eth_funding", 0.0),
        "oi_change_24h":      binance_m.get("oi_change_24h", 0.0),
        "long_short_ratio":   binance_m.get("long_short_ratio", 1.0),
        "top_gainers":        binance_m.get("top_gainers", []),
        "top_losers":         binance_m.get("top_losers", []),
        "macro_score":        macro["score"],
        "macro_events":       macro.get("upcoming", []),
        "next_macro_event":   macro.get("next_event"),
        "binance_announcements": announcements,
        "cmc":                cmc,
        "score_breakdown":    score_breakdown,
        "social_score":       social.get("score", 0.0),
        "social_label":       social.get("label", "Neutral"),
        "social_buzz":        social.get("buzz", 0),
        "social_bullish":     social.get("bullish", 0),
        "social_bearish":     social.get("bearish", 0),
        "social_topics":      social.get("topics", []),
        "last_update":        now_str,
        "next_update":        next_str,
        "refresh_time_s":     elapsed,
        "status":             "ok",
    }

    _market_state.update(new_state)
    _last_refresh = time.time()

    # Persiste backup em disco
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass

    print(f"[MIE] OK em {elapsed}s | Vies: {bias} ({confidence}%) | "
          f"Trend={trend['score']} News={news['score']:+.1f} "
          f"Sent={sentiment['score']:+.1f} BNB={binance_m.get('score',0):+.1f}")
    return _market_state


async def mini_refresh() -> None:
    """
    Refresh leve a cada 15 min: apenas news, sentiment e Binance funding.
    Nao atualiza trend (custoso) nem CoinGecko (rate limit).
    """
    try:
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            news, sentiment, binance_m = await asyncio.gather(
                _analyze_news(session),
                _analyze_sentiment(session),
                _analyze_binance(session),
                return_exceptions=True,
            )
        def _safe(v, d): return v if not isinstance(v, Exception) else d
        n = _safe(news,      {"score": 0, "label": "Neutral", "headlines": [], "bullish": 0, "bearish": 0})
        s = _safe(sentiment, {"score": 0, "value": 50, "label": "Neutral"})
        b = _safe(binance_m, {"score": 0, "btc_funding": 0, "eth_funding": 0,
                              "oi_change_24h": 0, "long_short_ratio": 1.0,
                              "top_gainers": [], "top_losers": []})
        _market_state.update({
            "news_score":     n["score"],
            "news_label":     n.get("label","Neutral"),
            "news_headlines": n.get("headlines",[]),
            "sentiment_score":s["score"],
            "sentiment_value":s.get("value",50),
            "sentiment_label":s.get("label","Neutral"),
            "binance_score":  b.get("score",0),
            "btc_funding":    b.get("btc_funding",0),
            "eth_funding":    b.get("eth_funding",0),
            "oi_change_24h":  b.get("oi_change_24h",0),
            "long_short_ratio": b.get("long_short_ratio",1),
            "top_gainers":    b.get("top_gainers",[]),
            "top_losers":     b.get("top_losers",[]),
        })
        print("[MIE] Mini-refresh OK")
    except Exception as e:
        print(f"[MIE] Mini-refresh err: {e}")


async def initialize() -> None:
    """
    Inicializacao no startup do bot.
    Carrega estado do disco (instantaneo) e agenda refresh completo.
    """
    global _market_state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("status") == "ok":
                _market_state.update(cached)
                _market_state["status"] = "cached"
                print(f"[MIE] Estado carregado do disco: {cached.get('last_update','?')}")
                return
    except Exception:
        pass
    # Sem cache: refresh imediato em background
    asyncio.create_task(refresh_market_intelligence())
