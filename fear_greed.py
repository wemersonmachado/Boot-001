"""
Fear & Greed Index — alternative.me API (100% gratuito, sem chave).
Retorna: 0-24=Extreme Fear, 25-49=Fear, 50-74=Greed, 75-100=Extreme Greed.

Uso:
    fg = await get_fear_greed()
    # {'value': 72, 'label': 'Greed', 'score_bonus': +8, 'filter_pass': True}
"""
import asyncio
import time
from typing import Optional

_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 3600  # 1 hora


async def get_fear_greed() -> dict:
    """Busca Fear & Greed Index com cache de 1h."""
    global _cache
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]

    try:
        import aiohttp
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ssl=False)
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                js = await r.json(content_type=None)
        item = js["data"][0]
        val   = int(item["value"])
        label = item["value_classification"]
        result = {
            "value":       val,
            "label":       label,
            "score_bonus": _fg_bonus(val),
            "filter_pass": True,
            "raw":         item,
        }
        _cache["data"] = result
        _cache["ts"]   = now
        return result
    except Exception as e:
        print(f"[F&G] Erro: {e}")
        return {"value": 50, "label": "Neutral", "score_bonus": 0, "filter_pass": True}


def _fg_bonus(val: int) -> float:
    """
    LONG bias quando mercado está com medo (ótimo para compras).
    SHORT bias quando há ganância extrema.
    Retorna bônus (+/-) para aplicar no score_funding_oi já existente.
    """
    if val <= 10:   return +15   # Extreme Fear → ótimo pra LONG contrarian
    if val <= 24:   return +8    # Fear
    if val <= 49:   return +3    # Slight Fear
    if val <= 74:   return 0     # Neutro / leve Greed
    if val <= 89:   return -5    # Greed → cuidado SHORT
    return -12                   # Extreme Greed → muito cuidado


def fg_label_emoji(label: str) -> str:
    m = {
        "Extreme Fear":  "😱",
        "Fear":          "😨",
        "Neutral":       "😐",
        "Greed":         "😏",
        "Extreme Greed": "🤑",
    }
    return m.get(label, "❓")


# ── Funding rate cache ────────────────────────────────────────────────────────
_funding_cache: dict = {}   # asset → {rate, ts}
FUNDING_TTL = 60.0
FUNDING_ALERT_HIGH =  0.003
FUNDING_ALERT_LOW  = -0.003


def update_funding_cache(asset: str, rate: float):
    """Chamado pelo data_fetcher ao obter funding rate."""
    _funding_cache[asset.upper()] = {"rate": rate, "ts": time.time()}


def get_funding_adj(asset: str, direction: str) -> tuple:
    """Retorna (ajuste_pts, motivo) baseado no funding rate + direção."""
    entry = _funding_cache.get(asset.upper())
    if not entry or (time.time() - entry["ts"]) > FUNDING_TTL:
        return 0.0, ""
    rate = entry["rate"]
    pct  = rate * 100
    if direction == "LONG":
        if rate > 0.002:
            return -8.0, f"Funding={pct:.3f}% (caro para long)"
        elif rate > 0.001:
            return -3.0, f"Funding={pct:.3f}% (alto)"
        elif rate < -0.001:
            return +4.0, f"Funding={pct:.3f}% (shorts pagando)"
    else:
        if rate < -0.002:
            return -8.0, f"Funding={pct:.3f}% (caro para short)"
        elif rate < -0.001:
            return -3.0, f"Funding={pct:.3f}% (baixo)"
        elif rate > 0.001:
            return +4.0, f"Funding={pct:.3f}% (longs pagando)"
    return 0.0, f"Funding={pct:.3f}%"


def funding_needs_alert(asset: str) -> Optional[str]:
    entry = _funding_cache.get(asset.upper())
    if not entry:
        return None
    rate = entry["rate"]
    if rate >= FUNDING_ALERT_HIGH:
        return f"Funding alto {asset}: {rate*100:.3f}% — risco cascata short"
    if rate <= FUNDING_ALERT_LOW:
        return f"Funding negativo {asset}: {rate*100:.3f}% — risco cascata long"
    return None


# ── Score ajuste com direção ──────────────────────────────────────────────────

async def fg_score_adjustment(direction: str) -> tuple:
    """
    Retorna (pts_ajuste, texto_motivo) baseado no F&G e direção.
    Long em Extreme Greed recebe penalidade; Short em Extreme Fear recebe penalidade.
    """
    fg = await get_fear_greed()
    v    = fg["value"]
    cls  = fg["label"]
    tag  = f"F&G={v} ({cls})"
    if direction == "LONG":
        if v <= 20:
            return +10.0, f"{tag} — pânico: bonus LONG"
        elif v <= 35:
            return +5.0,  f"{tag} — medo: bonus leve LONG"
        elif v <= 65:
            return 0.0,   tag
        elif v <= 80:
            return -5.0,  f"{tag} — ganância: penalidade LONG"
        else:
            return -12.0, f"{tag} — ganância extrema: risco alto"
    else:
        if v >= 80:
            return +10.0, f"{tag} — ganância extrema: bonus SHORT"
        elif v >= 65:
            return +5.0,  f"{tag} — ganância: bonus leve SHORT"
        elif v >= 35:
            return 0.0,   tag
        elif v >= 20:
            return -5.0,  f"{tag} — medo: penalidade SHORT"
        else:
            return -12.0, f"{tag} — pânico extremo: risco alto"


if __name__ == "__main__":
    async def _test():
        d = await get_fear_greed()
        print(f"Fear & Greed: {d['value']} — {d['label']} {fg_label_emoji(d['label'])}")
        print(f"Score bonus: {d['score_bonus']:+.0f}pts")
    asyncio.run(_test())
