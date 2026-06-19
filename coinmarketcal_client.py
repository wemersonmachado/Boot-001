"""
CoinMarketCal API Client — TRADER 001
Calendário de eventos cripto: listings, halvings, mainnet, token unlock, parcerias, etc.

Cross com sinais: antes de enviar um sinal, verifica se há evento relevante
nas próximas 48h para a moeda → adiciona flag e ajusta recomendação.

API gratuita: https://coinmarketcal.com/en/developer
Limite free tier: ~100 req/dia — cache de 6h resolve isso facilmente.

Adicione ao .env:
  COINMARKETCAL_API_KEY=sua_key_aqui
"""
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

COINMARKETCAL_KEY  = os.getenv("COINMARKETCAL_API_KEY", "")
_BASE              = "https://developers.coinmarketcal.com"

# Cache em memória
_cache: dict = {}


def _hit(key: str, ttl: int) -> Optional[list]:
    e = _cache.get(key)
    return e["data"] if e and time.time() - e["ts"] < ttl else None


def _put(key: str, data) -> None:
    _cache[key] = {"ts": time.time(), "data": data}


# ── Categorias de eventos por impacto ────────────────────────────────────────

HIGH_IMPACT = {
    "Exchange", "Listing", "Halving", "Mainnet", "Hard Fork", "Airdrop",
    "Partnership", "Release", "Burn", "Token Unlock",
}
MEDIUM_IMPACT = {
    "Conference", "meetup", "AMA", "Update", "Rebranding", "Governance",
}


def _impact(category: str) -> str:
    if category in HIGH_IMPACT:   return "HIGH"
    if category in MEDIUM_IMPACT: return "MEDIUM"
    return "LOW"


# ── API ───────────────────────────────────────────────────────────────────────

async def get_upcoming_events(days_ahead: int = 7) -> list[dict]:
    """
    Retorna todos os eventos cripto dos próximos `days_ahead` dias.
    Cache 6h — chama apenas 4x/dia, bem dentro do limite gratuito.
    """
    cache_key = f"events_{days_ahead}"
    cached = _hit(cache_key, 21600)
    if cached is not None:
        return cached

    if not COINMARKETCAL_KEY:
        return []

    now   = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%d")
    end   = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    params = {
        "dateRangeStart": start,
        "dateRangeEnd":   end,
        "max":            100,
        "page":           1,
        "showOnly":       "hot_events",  # só eventos com votos suficientes
    }

    result = []
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{_BASE}/v1/events",
                headers={"x-api-key": COINMARKETCAL_KEY, "Accept": "application/json"},
                params=params,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    for ev in data.get("body", []):
                        coins    = [c.get("symbol", "").upper() for c in ev.get("coins", [])]
                        cats     = ev.get("categories", [])
                        cat_name = cats[0].get("name", "") if cats else ""
                        result.append({
                            "title":      ev.get("title", {}).get("en", ""),
                            "coins":      coins,
                            "date":       ev.get("date_event", ""),
                            "category":   cat_name,
                            "impact":     _impact(cat_name),
                            "confidence": ev.get("percentage", 0),
                            "hot_score":  ev.get("hot_score", 0),
                            "votes":      ev.get("vote_count", 0),
                        })
                elif r.status == 401:
                    print("[CMCal] API key inválida ou não configurada")
                else:
                    print(f"[CMCal] HTTP {r.status}")
    except Exception as e:
        print(f"[CMCal] Erro ao buscar eventos: {type(e).__name__}: {e}")

    _put(cache_key, result)
    if result:
        print(f"[CMCal] {len(result)} eventos carregados para os próximos {days_ahead}d")
    return result


def get_events_for_symbol(symbol: str, all_events: list[dict], hours_ahead: int = 48) -> list[dict]:
    """
    Filtra eventos relevantes para um símbolo específico nas próximas N horas.
    Retorna lista ordenada por impacto (HIGH primeiro).
    """
    base   = symbol.replace("USDT", "").replace("BUSD", "").upper()
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)

    relevant = []
    for ev in all_events:
        if base not in ev.get("coins", []):
            continue
        try:
            ev_dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            if now <= ev_dt <= cutoff:
                hours_until = (ev_dt - now).total_seconds() / 3600
                relevant.append({**ev, "hours_until": round(hours_until, 1)})
        except Exception:
            pass

    # HIGH impact primeiro, depois mais próximo
    relevant.sort(key=lambda x: (
        0 if x["impact"] == "HIGH" else 1 if x["impact"] == "MEDIUM" else 2,
        x["hours_until"],
    ))
    return relevant


def format_event_note(events: list[dict]) -> str:
    """
    Formata nota de eventos para adicionar ao sinal do Telegram.
    Ex: "📅 EVENTO em 12h: Listing Binance (HIGH)"
    """
    if not events:
        return ""
    lines = []
    for ev in events[:2]:  # máx 2 eventos na mensagem
        emoji = "🔴" if ev["impact"] == "HIGH" else "🟡" if ev["impact"] == "MEDIUM" else "⚪"
        h = ev.get("hours_until", 0)
        time_str = f"{h:.0f}h" if h >= 1 else f"{h*60:.0f}min"
        lines.append(f"{emoji} Evento em {time_str}: {ev['title'][:50]} ({ev['impact']})")
    return "\n".join(lines)


def get_event_score_adjustment(events: list[dict], direction: str) -> float:
    """
    Ajuste de score baseado em eventos próximos.
    Listing/Mainnet iminente = +5pts para LONG, -3pts para SHORT.
    Token Unlock iminente   = -5pts para LONG, +5pts para SHORT.
    """
    if not events:
        return 0.0

    adj = 0.0
    is_long = "LONG" in direction.upper()

    for ev in events[:3]:
        cat    = ev.get("category", "")
        impact = ev.get("impact", "LOW")
        h      = ev.get("hours_until", 999)
        weight = 1.0 if h <= 12 else 0.6 if h <= 24 else 0.3

        if cat in ("Listing", "Mainnet", "Halving", "Partnership"):
            adj += (5.0 if is_long else -3.0) * weight * (1.5 if impact == "HIGH" else 1.0)
        elif cat in ("Token Unlock", "Burn"):
            adj += (-5.0 if is_long else 5.0) * weight * (1.5 if impact == "HIGH" else 1.0)
        elif cat == "Hard Fork":
            adj += 2.0 * weight  # geralmente bullish para ambos

    return round(max(-10.0, min(10.0, adj)), 1)


# ── Cache de eventos global (atualizado pelo market_engine) ──────────────────

_events_cache: list = []
_events_ts: float   = 0.0


def set_global_events(events: list[dict]) -> None:
    global _events_cache, _events_ts
    _events_cache = events
    _events_ts    = time.time()


def get_global_events() -> list[dict]:
    return _events_cache
