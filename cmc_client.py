"""
CoinMarketCap API Client — TRADER 001
Opções 2, 3 e 4 implementadas:
  2. BTC Dominance em tempo real (substitui CoinGecko)
  3. Categorias/setores → diversificação e detecção de rotação
  4. Market cap / volume como filtro de qualidade para o universo

Cache agressivo: free tier = 333 chamadas/dia (10k/mês).
  - Global metrics : 15 min
  - Listings       : 30 min
  - Categories     : 2 h
"""
import os
import time
from typing import Optional

import aiohttp

CMC_API_KEY = os.getenv("CMC_API_KEY", "")
_BASE       = "https://pro-api.coinmarketcap.com"
_HEADERS    = lambda: {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}

# ── Cache em memória ──────────────────────────────────────────────────────────

_cache: dict = {}


def _hit(key: str, ttl: int) -> Optional[object]:
    e = _cache.get(key)
    return e["data"] if e and time.time() - e["ts"] < ttl else None


def _put(key: str, data) -> None:
    _cache[key] = {"ts": time.time(), "data": data}


async def _get(path: str, params: dict = None) -> Optional[dict]:
    if not CMC_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{_BASE}{path}",
                headers=_HEADERS(),
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                print(f"[CMC] HTTP {r.status} em {path}")
    except Exception as e:
        print(f"[CMC] Erro {path}: {type(e).__name__}")
    return None


# ── 2. BTC Dominance + métricas globais (15 min cache) ───────────────────────

async def get_global_metrics() -> dict:
    """
    Retorna dominância BTC/ETH, market cap total, volume global.
    Cache 15 min para não gastar quota.
    """
    cached = _hit("global", 900)
    if cached is not None:
        return cached

    data = await _get("/v1/global-metrics/quotes/latest")
    if not data:
        return {}

    try:
        d = data["data"]
        q = d.get("quote", {}).get("USD", {})
        result = {
            "btc_dominance":         round(d.get("btc_dominance", 50.0), 2),
            "eth_dominance":         round(d.get("eth_dominance", 20.0), 2),
            "altcoin_dominance":     round(100 - d.get("btc_dominance", 50) - d.get("eth_dominance", 20), 2),
            "total_market_cap":      q.get("total_market_cap", 0),
            "total_volume_24h":      q.get("total_volume_24h", 0),
            "altcoin_market_cap":    q.get("altcoin_market_cap", 0),
            "defi_volume_24h":       d.get("defi_volume_24h", 0),
            "defi_market_cap":       d.get("defi_market_cap", 0),
            "active_cryptocurrencies": d.get("active_cryptocurrencies", 0),
            "stablecoin_volume_24h": d.get("stablecoin_volume_24h", 0),
            # Derived signals
            "btc_dom_bearish_alt":   d.get("btc_dominance", 50) < 45,   # True = altseason
            "high_stablecoin_ratio": (
                d.get("stablecoin_volume_24h", 0) / max(q.get("total_volume_24h", 1), 1) > 0.35
            ),  # True = medo, dinheiro saindo para stablecoins
        }
        _put("global", result)
        print(f"[CMC] Global: BTC.D={result['btc_dominance']}% | AltSeason={result['btc_dom_bearish_alt']}")
        return result
    except Exception as e:
        print(f"[CMC] Erro parse global: {e}")
        return {}


# ── 3. Categorias / setores (2h cache) ───────────────────────────────────────

async def get_categories(limit: int = 60) -> list[dict]:
    """
    Top categorias/setores por market cap.
    Usado para: detectar rotação setorial e limitar exposição por setor.
    """
    cached = _hit(f"cats_{limit}", 7200)
    if cached is not None:
        return cached

    data = await _get("/v1/cryptocurrency/categories", {"limit": limit})
    if not data:
        return []

    result = []
    for c in data.get("data", []):
        chg = c.get("avg_price_change", 0) or 0
        result.append({
            "id":           c.get("id", ""),
            "name":         c.get("name", ""),
            "market_cap":   c.get("market_cap", 0),
            "volume_24h":   c.get("volume_24h", 0),
            "avg_change_24h": round(float(chg), 2),
            "num_tokens":   c.get("num_tokens", 0),
            "hot":          abs(float(chg)) >= 5,  # setor em movimento
        })

    result.sort(key=lambda x: abs(x["avg_change_24h"]), reverse=True)
    _put(f"cats_{limit}", result)

    hot = [c["name"] for c in result if c["hot"]][:5]
    print(f"[CMC] Setores quentes: {', '.join(hot) or 'Nenhum'}")
    return result


# ── 4. Listings — market cap + volume por símbolo (30 min cache) ──────────────

async def get_listings(limit: int = 300) -> list[dict]:
    """
    Top N moedas por market cap com volume, % mudança e rank CMC.
    Usado para: filtrar moedas pequenas demais e enriquecer sinais.
    """
    cached = _hit(f"lst_{limit}", 1800)
    if cached is not None:
        return cached

    data = await _get("/v1/cryptocurrency/listings/latest", {
        "limit":                limit,
        "convert":              "USD",
        "sort":                 "market_cap",
        "cryptocurrency_type":  "coins",
        "aux":                  "cmc_rank,platform",
    })
    if not data:
        return []

    result = []
    for c in data.get("data", []):
        q = c.get("quote", {}).get("USD", {})
        result.append({
            "symbol":         c.get("symbol", ""),
            "name":           c.get("name", ""),
            "cmc_rank":       c.get("cmc_rank", 9999),
            "market_cap":     q.get("market_cap") or 0,
            "volume_24h":     q.get("volume_24h") or 0,
            "pct_1h":         q.get("percent_change_1h") or 0,
            "pct_24h":        q.get("percent_change_24h") or 0,
            "pct_7d":         q.get("percent_change_7d") or 0,
            "market_cap_tier": _cap_tier(q.get("market_cap") or 0),
        })

    _put(f"lst_{limit}", result)
    print(f"[CMC] Listings: {len(result)} moedas carregadas")
    return result


def _cap_tier(market_cap: float) -> str:
    """Classifica moeda por capitalização."""
    if market_cap >= 10_000_000_000:  return "Large"   # > $10B
    if market_cap >= 1_000_000_000:   return "Mid"     # $1B–$10B
    if market_cap >= 100_000_000:     return "Small"   # $100M–$1B
    if market_cap >= 10_000_000:      return "Micro"   # $10M–$100M
    return "Nano"


# ── 5. Trending — derivado do cache de listings (zero chamadas extras) ────────

async def get_trending_symbols(min_vol_m: float = 10.0, top_n: int = 10) -> set[str]:
    """
    Top N moedas por variação 24h (absoluta) com volume >= min_vol_m M.
    Não faz chamada extra — reutiliza o cache de get_listings().
    Cache 30 min (alinhado ao listings).
    """
    cached = _hit("trending_syms", 1800)
    if cached is not None:
        return cached

    listings = await get_listings(300)
    if not listings:
        return set()

    min_vol = min_vol_m * 1_000_000
    candidates = [c for c in listings if c.get("volume_24h", 0) >= min_vol]
    candidates.sort(key=lambda x: abs(x.get("pct_24h", 0)), reverse=True)

    result = {c["symbol"].upper() for c in candidates[:top_n]}
    _put("trending_syms", result)

    syms = ", ".join(sorted(result)[:5])
    print(f"[CMC] Trending: {syms}")
    return result


# ── 6. New Listings — sort=date_added (2h cache) ──────────────────────────────

async def get_new_listings(limit: int = 20, max_days: int = 7) -> list[dict]:
    """
    Moedas recém-listadas no CMC (últimos max_days dias).
    Usa sort=date_added na mesma endpoint de listings — sem custo extra de plano.
    Cache 2h — novos listings não surgem a cada minuto.
    """
    cache_key = f"new_lst_{limit}"
    cached = _hit(cache_key, 7200)
    if cached is not None:
        return cached

    data = await _get("/v1/cryptocurrency/listings/latest", {
        "limit":    limit,
        "sort":     "date_added",
        "sort_dir": "desc",
        "convert":  "USD",
        "aux":      "date_added",
    })
    if not data:
        return []

    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)

    result = []
    for c in data.get("data", []):
        date_str = c.get("date_added", "")
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt < cutoff:
                continue
        except Exception:
            continue

        q = c.get("quote", {}).get("USD", {})
        result.append({
            "symbol":     c.get("symbol", ""),
            "name":       c.get("name", ""),
            "date_added": date_str[:10],
            "cmc_rank":   c.get("cmc_rank", 9999),
            "market_cap": q.get("market_cap") or 0,
            "volume_24h": q.get("volume_24h") or 0,
            "pct_24h":    q.get("percent_change_24h") or 0,
        })

    _put(cache_key, result)
    names = ", ".join(r["symbol"] for r in result[:5]) or "Nenhum"
    print(f"[CMC] Novos listings ({max_days}d): {names}")
    return result


def get_new_listing_symbols(new_listings: list[dict]) -> set[str]:
    """Set de símbolos recentemente listados para lookup O(1)."""
    return {c["symbol"].upper() for c in new_listings}


# ── Helpers públicos ──────────────────────────────────────────────────────────

def build_symbol_map(listings: list[dict]) -> dict:
    """Mapa symbol → dados CMC para lookup O(1)."""
    return {c["symbol"].upper(): c for c in listings}


def get_hot_sectors(categories: list[dict], min_change: float = 5.0) -> list[str]:
    """Retorna nomes dos setores com variação média >= min_change% em 24h."""
    return [c["name"] for c in categories if abs(c.get("avg_change_24h", 0)) >= min_change]


# ── Mapa estático de setor por símbolo (fallback sem API) ────────────────────
# Usado quando CMC não está disponível para limitar concentração por setor.

SECTOR_MAP: dict = {
    # Layer 1
    "BTC": "L1", "ETH": "L1", "SOL": "L1", "AVAX": "L1", "ADA": "L1",
    "NEAR": "L1", "APT": "L1", "SUI": "L1", "SEI": "L1", "TON": "L1",
    "ATOM": "L1", "ALGO": "L1", "FTM": "L1", "ONE": "L1", "KLAY": "L1",
    # Layer 2
    "MATIC": "L2", "ARB": "L2", "OP": "L2", "IMX": "L2", "STRK": "L2",
    "MANTA": "L2", "SCROLL": "L2", "ZK": "L2", "METIS": "L2",
    # DeFi
    "UNI": "DeFi", "AAVE": "DeFi", "CRV": "DeFi", "COMP": "DeFi",
    "MKR": "DeFi", "SNX": "DeFi", "SUSHI": "DeFi", "1INCH": "DeFi",
    "BAL": "DeFi", "YFI": "DeFi", "GMX": "DeFi", "DYDX": "DeFi",
    "PENDLE": "DeFi", "ENA": "DeFi", "ETHFI": "DeFi",
    # AI / Data
    "FET": "AI", "AGIX": "AI", "OCEAN": "AI", "RENDER": "AI",
    "TAO": "AI", "WLD": "AI", "RNDR": "AI",
    # Gaming / Metaverse
    "AXS": "Gaming", "SAND": "Gaming", "MANA": "Gaming", "ENJ": "Gaming",
    "GALA": "Gaming", "ILV": "Gaming", "BEAM": "Gaming", "MAGIC": "Gaming",
    # Meme
    "DOGE": "Meme", "SHIB": "Meme", "FLOKI": "Meme", "PEPE": "Meme",
    "WIF": "Meme", "BONK": "Meme", "BRETT": "Meme", "MOG": "Meme",
    # Exchange tokens
    "BNB": "CEX", "OKB": "CEX", "CRO": "CEX", "HT": "CEX",
    # Oracle / Infra
    "LINK": "Oracle", "BAND": "Oracle", "API3": "Oracle",
    # Privacy
    "XMR": "Privacy", "ZEC": "Privacy", "DASH": "Privacy",
    # RWA / Stablecoins infra
    "ONDO": "RWA", "CFG": "RWA", "MPL": "RWA",
    # Others — padrão
}


def get_sector(symbol: str) -> str:
    base = symbol.replace("USDT", "").replace("BUSD", "").upper()
    return SECTOR_MAP.get(base, "Other")
