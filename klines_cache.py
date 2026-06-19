"""
Klines Cache — in-memory OHLCV cache para Binance Futures.

- Primeiro acesso: busca 500 candles (warm-up para EMA200 convergir)
- Dentro do TTL: retorna cache sem chamada à API
- Após TTL: busca só os últimos 10 candles para atualizar a cauda
- Resultado: ~95% menos chamadas klines durante ciclos de scan
"""
import asyncio
from datetime import datetime
from typing import Optional
import pandas as pd

from data_fetcher import get_klines

_WARMUP = 500
_REFRESH = 10
_LOCK_TIMEOUT = 15.0    # segundos máx aguardando lock antes de abortar
_LOCK_MAX_SIZE = 500    # limpa locks velhos quando dicionário excede este tamanho

_TF_SECONDS: dict[str, int] = {
    "1m": 60,   "3m": 180,   "5m": 300,  "15m": 900,
    "30m": 1800, "1h": 3600,  "2h": 7200, "4h": 14400,
    "6h": 21600, "12h": 43200, "1d": 86400, "1w": 604800,
}

_cache: dict[tuple, dict] = {}
_locks: dict[tuple, asyncio.Lock] = {}
_lock_last_used: dict[tuple, float] = {}  # tracking para cleanup


def _get_lock(key: tuple) -> asyncio.Lock:
    # Limpa locks orfaos quando o dicionario fica grande demais
    if len(_locks) > _LOCK_MAX_SIZE:
        import time as _t
        now = _t.time()
        stale_keys = [
            k for k, ts in _lock_last_used.items()
            if now - ts > 3600 and not _locks.get(k, asyncio.Lock()).locked()
        ]
        for k in stale_keys[:100]:  # remove até 100 por vez
            _locks.pop(k, None)
            _lock_last_used.pop(k, None)

    if key not in _locks:
        _locks[key] = asyncio.Lock()
    import time as _t
    _lock_last_used[key] = _t.time()
    return _locks[key]


def _ttl(timeframe: str) -> float:
    """TTL = 80% da duração do candle para garantir frescor."""
    return _TF_SECONDS.get(timeframe, 300) * 0.80


async def get_klines_cached(
    symbol: str,
    timeframe: str,
    limit: int = 300,
) -> Optional[pd.DataFrame]:
    """
    Substituto drop-in para get_klines com cache em memória.
    Primeira chamada: busca 500 candles.
    Chamadas dentro do TTL: retorna cache (zero API calls).
    Após TTL: busca 10 candles para atualizar a cauda.
    Lock com timeout de 15s: previne deadlock se API travar.
    """
    key = (symbol, timeframe)
    lock = _get_lock(key)
    try:
        # timeout previne deadlock permanente se a API Binance travar
        await asyncio.wait_for(lock.acquire(), timeout=_LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[KLINES CACHE] Timeout aguardando lock {symbol}/{timeframe} — retornando cache atual")
        entry = _cache.get(key)
        if entry is not None:
            return entry["df"].iloc[-limit:].copy()
        return None

    try:
        now = datetime.utcnow()
        entry = _cache.get(key)

        if entry is None:
            df = await get_klines(symbol, timeframe, limit=_WARMUP)
            if df is None or len(df) < 50:
                return None
            _cache[key] = {"df": df, "updated_at": now}
        else:
            age = (now - entry["updated_at"]).total_seconds()
            if age >= _ttl(timeframe):
                # Quantas velas se passaram desde o ultimo update?
                # Se mais que _REFRESH, busca o suficiente para nao deixar gap.
                tf_s   = _TF_SECONDS.get(timeframe, 300)
                needed = min(int(age // tf_s) + 2, _WARMUP)
                fetch_n = max(_REFRESH, needed)
                fresh = await get_klines(symbol, timeframe, limit=fetch_n)
                if fresh is not None and len(fresh) > 0:
                    cutoff = fresh.index[0]
                    trimmed = entry["df"][entry["df"].index < cutoff]
                    merged = pd.concat([trimmed, fresh]).iloc[-_WARMUP:]
                    _cache[key] = {"df": merged, "updated_at": now}

        df = _cache[key]["df"]
        return df.iloc[-limit:].copy()
    finally:
        lock.release()



def cache_invalidate(symbol: str = None, timeframe: str = None) -> None:
    """Invalida entradas do cache. Sem argumentos limpa tudo."""
    keys = [
        k for k in list(_cache)
        if (symbol is None or k[0] == symbol)
        and (timeframe is None or k[1] == timeframe)
    ]
    for k in keys:
        _cache.pop(k, None)


def cache_stats() -> dict:
    """Retorna estatísticas do cache para monitoramento."""
    now = datetime.utcnow()
    return {
        "count": len(_cache),
        "entries": [
            {
                "symbol": k[0],
                "timeframe": k[1],
                "rows": len(v["df"]),
                "age_s": round((now - v["updated_at"]).total_seconds(), 1),
                "ttl_s": _ttl(k[1]),
            }
            for k, v in _cache.items()
        ],
    }
