"""
Universe Builder v2 — Universo Dinâmico Multi-Fator
Roda a cada 1h e atualiza universe_state.json com os melhores ativos da Binance Futures.

Score multi-fator (max 10 pts):
  1. Volume $20M+/24h e crescendo            (+1)
  2. Volume spike 4h vs janela 24h           (+1)
  3. Momentum ±3% em 24h                     (+1)
  4. Momentum forte ±8%                      (+1)
  5. Trades/hora acima da média              (+1)
  6. OI crescendo >5%                        (+1)
  7. Funding rate extremo                    (+1)
  8. Long/Short desequilibrado               (+1)
  9. Relative strength vs BTC (outperform)   (+1)  ← NOVO
  10. Alta volatilidade intraday (range/4h)  (+1)  ← NOVO

Filtros de qualidade (CMC se disponível):
  - Market cap mínimo $50M (Micro tier ou melhor)
  - Rank CMC ≤ 500 (evita tokens sem liquidez real)

Diversificação por setor (max 5 por setor):
  - L1, L2, DeFi, AI, Gaming, Meme, CEX, Oracle, RWA, Other

Critérios de SAÍDA:
  - Volume < $20M/24h por 2 ciclos sem compensação
  - Score < 2 por 48h
  - Sem atividade por 5 dias
"""
import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone

from data_fetcher import fetch, BINANCE_BASE

STATE_FILE = os.path.join(os.path.dirname(__file__), "universe_state.json")

# ── Configurações ─────────────────────────────────────────────────────────────
MIN_VOLUME_24H_USDT  = 20_000_000   # $20M mínimo em volume
MIN_SCORE_TO_ENTER   = 4            # pontos mínimos para entrar
MIN_SCORE_TO_STAY    = 2            # pontos mínimos para permanecer
MAX_UNIVERSE_SIZE    = 60           # reduzido de 80 → mais qualidade, menos ruído
MAX_PER_SECTOR       = 5            # diversificação: máx 5 por setor
REMOVE_AFTER_HOURS   = 48           # remove se score baixo por 48h
REMOVE_AFTER_DAYS    = 5            # remove se sem atividade por 5 dias
MIN_MARKET_CAP       = 50_000_000   # $50M market cap mínimo (Micro tier ou melhor)
MAX_CMC_RANK         = 500          # só top-500 do CMC (quando CMC disponível)

# Stablecoins e tokens a excluir
_EXCLUDE = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "FRAX",
    "USDT", "USDCUSDT", "USDTUSDT", "1000PEPE", "1000SHIB",
    "BTCDOM", "DEFI",  # índices sintéticos
}


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


async def _get_all_tickers() -> tuple[list[dict], float]:
    """
    Busca todos os tickers 24h de Futuros.
    Retorna (lista_filtrada, btc_change_pct) para cálculo de Relative Strength.
    """
    data = await fetch(f"{BINANCE_BASE}/fapi/v1/ticker/24hr")

    # Pega variação do BTC para calcular RS relativo
    btc_change = 0.0
    for d in data:
        if d.get("symbol") == "BTCUSDT":
            btc_change = float(d.get("priceChangePercent", 0))
            break

    result = []
    for d in data:
        sym = d.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym.replace("USDT", "")
        if base in _EXCLUDE or any(x in base for x in ["_", "UP", "DOWN", "BULL", "BEAR"]):
            continue
        vol_24h = float(d.get("quoteVolume", 0))
        if vol_24h < MIN_VOLUME_24H_USDT:
            continue
        high_24h   = float(d.get("highPrice", 0))
        low_24h    = float(d.get("lowPrice", 0))
        last_price = float(d.get("lastPrice", 0))
        change_pct = float(d.get("priceChangePercent", 0))

        # Volatilidade intraday: range como % do low
        volatility = (high_24h - low_24h) / low_24h * 100 if low_24h > 0 else 0

        # Relative Strength vs BTC: quanto outperforma/underperforma o BTC
        rs_vs_btc = change_pct - btc_change

        # Posição no range: 0 = no mínimo, 1 = no máximo
        price_position = (
            (last_price - low_24h) / (high_24h - low_24h)
            if high_24h > low_24h else 0.5
        )

        result.append({
            "symbol":         sym,
            "vol_24h":        vol_24h,
            "change_pct":     change_pct,
            "high_24h":       high_24h,
            "low_24h":        low_24h,
            "last_price":     last_price,
            "trades_24h":     int(d.get("count", 0)),
            "volatility_pct": round(volatility, 2),
            "rs_vs_btc":      round(rs_vs_btc, 2),
            "price_position": round(price_position, 3),  # 0=min, 1=max
        })

    return result, btc_change


async def _get_oi(symbol: str) -> float:
    """Open Interest atual em USDT."""
    try:
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/openInterest", {"symbol": symbol})
        return float(data.get("openInterest", 0))
    except Exception:
        return 0.0


async def _get_funding(symbol: str) -> float:
    """Funding rate atual."""
    try:
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data.get("lastFundingRate", 0))
    except Exception:
        return 0.0


async def _get_long_short(symbol: str) -> float:
    """Ratio long/short global — retorna fração long (0-1)."""
    try:
        data = await fetch(
            f"{BINANCE_BASE}/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": "1h", "limit": 1},
        )
        if data:
            return float(data[0].get("longAccount", 0.5))
    except Exception:
        pass
    return 0.5


async def _get_klines_4h(symbol: str, limit: int = 2) -> list:
    """Últimas N velas de 4h."""
    try:
        data = await fetch(
            f"{BINANCE_BASE}/fapi/v1/klines",
            {"symbol": symbol, "interval": "4h", "limit": limit},
        )
        return data
    except Exception:
        return []


async def _score_symbol(
    ticker: dict,
    state: dict,
    cmc_map: dict,
    trending_set: set = None,
    new_listing_set: set = None,
) -> dict:
    """
    Score multi-fator 0-12 para entrada/permanência no universo.
    Critérios extras: CMC Trending (+1), CMC New Listing (+2).
    """
    sym      = ticker["symbol"]
    vol_24h  = ticker["vol_24h"]
    change   = ticker["change_pct"]
    trades   = ticker["trades_24h"]
    rs_btc   = ticker.get("rs_vs_btc", 0)
    vol_pct  = ticker.get("volatility_pct", 0)

    score   = 0
    reasons = []

    # ── 1. Volume mínimo crescendo ────────────────────────────────────────────
    prev_vol    = state.get(sym, {}).get("vol_24h_prev", vol_24h)
    vol_growing = vol_24h >= prev_vol * 0.97
    if vol_growing and vol_24h >= MIN_VOLUME_24H_USDT:
        score += 1
        reasons.append(f"vol_{vol_24h/1e6:.0f}M")

    # ── 2. Volume spike 4h ────────────────────────────────────────────────────
    vol_4h_est  = vol_24h / 6
    prev_vol_4h = state.get(sym, {}).get("vol_4h_est", vol_4h_est)
    if vol_4h_est > prev_vol_4h * 1.5:
        score += 1
        reasons.append("vol_spike_4h")

    # ── 3. Momentum ±3% ───────────────────────────────────────────────────────
    if abs(change) >= 3.0:
        score += 1
        reasons.append(f"mom_{change:+.1f}%")

    # ── 4. Momentum forte ±8% ─────────────────────────────────────────────────
    if abs(change) >= 8.0:
        score += 1
        reasons.append("strong_mom")

    # ── 5. Trades/hora acima da média ────────────────────────────────────────
    tph      = trades / 24
    prev_tph = state.get(sym, {}).get("trades_per_hour", tph)
    if tph > prev_tph * 1.5 and tph > 500:
        score += 1
        reasons.append("trades_spike")

    # ── 6. OI crescendo >5% ──────────────────────────────────────────────────
    oi_now = 0.0
    try:
        oi_now  = await _get_oi(sym)
        oi_prev = state.get(sym, {}).get("oi", oi_now)
        if oi_now > 0 and oi_prev > 0 and oi_now > oi_prev * 1.05:
            score += 1
            reasons.append(f"oi+{((oi_now/oi_prev-1)*100):.1f}%")
    except Exception:
        pass

    # ── 7. Funding rate extremo ───────────────────────────────────────────────
    try:
        funding = await _get_funding(sym)
        if abs(funding) >= 0.0005:
            score += 1
            reasons.append(f"fr_{funding*100:.3f}%")
    except Exception:
        pass

    # ── 8. Long/Short desequilibrado ─────────────────────────────────────────
    try:
        ls = await _get_long_short(sym)
        if ls >= 0.70 or ls <= 0.30:
            score += 1
            reasons.append(f"ls_{ls:.2f}")
    except Exception:
        pass

    # ── 9. Relative Strength vs BTC ← NOVO ───────────────────────────────────
    # Moeda outperformando BTC em +3% ou mais = fluxo institucional independente
    if abs(rs_btc) >= 3.0:
        score += 1
        reasons.append(f"rs_btc{rs_btc:+.1f}%")

    # ── 10. Volatilidade intraday alta ← NOVO ────────────────────────────────
    # Range 24h > 6% do preço = ativo em movimento real (oportunidades de trade)
    if vol_pct >= 6.0:
        score += 1
        reasons.append(f"vlt_{vol_pct:.1f}%")

    # ── 9b. CMC Trending — moeda em destaque no ranking 24h ──────────────────
    base = sym.replace("USDT", "").upper()
    if trending_set and base in trending_set:
        score += 1
        reasons.append("cmc_trend")

    # ── 9c. CMC New Listing — listagem recente no CMC ─────────────────────────
    if new_listing_set and base in new_listing_set:
        score += 2
        reasons.append("cmc_new_listing")

    # ── Filtros CMC (não pontuam, mas bloqueiam entrada) ─────────────────────
    cmc_info = cmc_map.get(base, {})
    if cmc_info:
        mc = cmc_info.get("market_cap", 0)
        rk = cmc_info.get("cmc_rank", 9999)
        if mc > 0 and mc < MIN_MARKET_CAP:
            score = 0  # market cap muito pequeno — zera o score
            reasons = ["blocked_small_cap"]
        elif rk > MAX_CMC_RANK:
            score = max(0, score - 2)  # penaliza tokens fora do top-500
            reasons.append("low_rank")

    from cmc_client import get_sector
    sector = get_sector(sym)

    return {
        "symbol":          sym,
        "score":           score,
        "reasons":         reasons,
        "vol_24h":         vol_24h,
        "vol_4h_est":      vol_4h_est,
        "vol_24h_prev":    vol_24h,
        "change_pct":      change,
        "trades_per_hour": tph,
        "oi":              oi_now,
        "rs_vs_btc":       rs_btc,
        "volatility_pct":  vol_pct,
        "sector":          sector,
        "cmc_rank":        cmc_info.get("cmc_rank", 9999),
        "market_cap":      cmc_info.get("market_cap", 0),
    }


async def build_universe() -> list[str]:
    """
    Função principal — roda a cada 1h via APScheduler.
    Retorna lista de símbolos ativos no universo dinâmico.
    """
    now_ts  = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    state   = _load_state()

    print(f"[UNIVERSE] Iniciando scan v2 — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    # ── 0. Carrega dados CMC (market cap filter + setor + trending + new listings)
    cmc_map         = {}
    trending_set    = set()
    new_listing_set = set()
    try:
        from cmc_client import (
            get_listings, build_symbol_map,
            get_trending_symbols, get_new_listings, get_new_listing_symbols,
        )
        listings, trending_syms, new_lst = await asyncio.gather(
            get_listings(300),
            get_trending_symbols(),
            get_new_listings(),
            return_exceptions=True,
        )
        if isinstance(listings, list):
            cmc_map = build_symbol_map(listings)
        if isinstance(trending_syms, set):
            trending_set = trending_syms
        if isinstance(new_lst, list):
            new_listing_set = get_new_listing_symbols(new_lst)
        print(
            f"[UNIVERSE] CMC: {len(cmc_map)} moedas | "
            f"trending={len(trending_set)} | new_listings={len(new_listing_set)}"
        )
    except Exception as e:
        print(f"[UNIVERSE] CMC indisponível — sem filtro market cap: {e}")

    # ── 1. Busca todos os tickers com volume >= $20M ──────────────────────────
    try:
        tickers, btc_change = await _get_all_tickers()
    except Exception as e:
        print(f"[UNIVERSE] Erro ao buscar tickers: {e}")
        return _get_active_symbols(state)

    print(f"[UNIVERSE] {len(tickers)} ativos | BTC 24h: {btc_change:+.2f}%")

    # ── 2. Score paralelo — limita concorrência para não bater rate limit ─────
    sem = asyncio.Semaphore(12)

    async def score_with_sem(ticker):
        async with sem:
            return await _score_symbol(ticker, state, cmc_map, trending_set, new_listing_set)

    scored = await asyncio.gather(*[score_with_sem(t) for t in tickers], return_exceptions=True)
    scored = [s for s in scored if isinstance(s, dict)]

    # ── 3. Atualiza state para cada símbolo scaneado ──────────────────────────
    new_state = {}

    for s in scored:
        sym   = s["symbol"]
        entry = state.get(sym, {})

        # Calcula se está entrando, permanecendo ou saindo
        is_active   = entry.get("active", False)
        enters_now  = s["score"] >= MIN_SCORE_TO_ENTER
        stays_now   = s["score"] >= MIN_SCORE_TO_STAY

        if not is_active and enters_now:
            # ENTRADA nova
            new_state[sym] = {
                **s,
                "active":       True,
                "entered_at":   now_iso,
                "entered_ts":   now_ts,
                "last_active_ts": now_ts,
                "low_score_since": None,
            }
            print(f"[UNIVERSE] + ENTRADA: {sym} | score:{s['score']} | {', '.join(s['reasons'])}")

        elif is_active:
            low_since = entry.get("low_score_since")
            if stays_now:
                # Permanece com score ok
                new_state[sym] = {
                    **s,
                    "active":         True,
                    "entered_at":     entry.get("entered_at", now_iso),
                    "entered_ts":     entry.get("entered_ts", now_ts),
                    "last_active_ts": now_ts,
                    "low_score_since": None,
                }
            else:
                # Score caiu — inicia contagem de remoção
                if low_since is None:
                    low_since = now_ts
                hours_low = (now_ts - low_since) / 3600

                if hours_low >= REMOVE_AFTER_HOURS:
                    print(f"[UNIVERSE] - SAÍDA (score baixo {hours_low:.0f}h): {sym}")
                elif s["vol_24h"] < MIN_VOLUME_24H_USDT:
                    print(f"[UNIVERSE] - SAÍDA (vol abaixo $20M): {sym}")
                else:
                    # Ainda dentro da janela de tolerância
                    new_state[sym] = {
                        **s,
                        "active":          True,
                        "entered_at":      entry.get("entered_at", now_iso),
                        "entered_ts":      entry.get("entered_ts", now_ts),
                        "last_active_ts":  entry.get("last_active_ts", now_ts),
                        "low_score_since": low_since,
                    }

        # Sem atividade por 5 dias — já não está em scored nem em new_state → vai sair

    # ── 4. Mantém ativos anteriores que não foram escaneados (ex: API timeout) ──
    for sym, entry in state.items():
        if sym not in new_state and entry.get("active", False):
            days_inactive = (now_ts - entry.get("last_active_ts", now_ts)) / 86400
            if days_inactive < REMOVE_AFTER_DAYS:
                new_state[sym] = entry  # mantém por ora
            else:
                print(f"[UNIVERSE] - SAÍDA (inativo {days_inactive:.1f}d): {sym}")

    # ── 5. Limite MAX_UNIVERSE_SIZE com diversificação por setor ─────────────
    active = [(sym, e) for sym, e in new_state.items() if e.get("active")]
    active.sort(key=lambda x: x[1].get("score", 0), reverse=True)

    if len(active) > MAX_UNIVERSE_SIZE:
        # Aplica diversificação por setor antes de cortar pelo limite global
        sector_counts: dict = {}
        diversified = []
        overflow    = []

        for sym, entry in active:
            sector = entry.get("sector", "Other")
            count  = sector_counts.get(sector, 0)
            if count < MAX_PER_SECTOR:
                diversified.append((sym, entry))
                sector_counts[sector] = count + 1
            else:
                overflow.append((sym, entry))

        # Se ainda sobrou espaço depois da diversificação, preenche com overflow
        remaining = MAX_UNIVERSE_SIZE - len(diversified)
        if remaining > 0:
            diversified.extend(overflow[:remaining])

        # Remove os que ficaram de fora
        selected_syms = {s for s, _ in diversified}
        for sym, _ in active:
            if sym not in selected_syms:
                new_state[sym]["active"] = False
                print(f"[UNIVERSE] - SAÍDA (limite/setor): {sym}")

        active = diversified[:MAX_UNIVERSE_SIZE]

    # ── 6. Resumo por setor ───────────────────────────────────────────────────
    sector_summary: dict = {}
    for sym, e in active:
        sec = e.get("sector", "Other")
        sector_summary[sec] = sector_summary.get(sec, 0) + 1
    sec_str = " | ".join(f"{k}:{v}" for k, v in sorted(sector_summary.items()))

    # ── 7. Salva estado e retorna lista ──────────────────────────────────────
    _save_state(new_state)

    symbols = [sym for sym, _ in active]
    print(f"[UNIVERSE] {len(symbols)} ativos | Setores: {sec_str}")
    return symbols


def _get_active_symbols(state: dict = None) -> list[str]:
    """Retorna lista de símbolos ativos do state atual (leitura rápida sem API)."""
    if state is None:
        state = _load_state()
    return [sym for sym, entry in state.items() if entry.get("active", False)]


def get_universe() -> list[str]:
    """Leitura síncrona rápida do universo atual — para uso no scan_watchlist."""
    return _get_active_symbols()


def get_universe_stats() -> dict:
    """Estatísticas do universo para o dashboard."""
    state  = _load_state()
    active = [(sym, e) for sym, e in state.items() if e.get("active", False)]
    active.sort(key=lambda x: x[1].get("score", 0), reverse=True)

    now_ts = time.time()
    return {
        "total_active":   len(active),
        "last_updated":   max((e.get("last_active_ts", 0) for _, e in active), default=0),
        "top_10": [
            {
                "symbol":     sym,
                "score":      e.get("score", 0),
                "vol_24h_m":  round(e.get("vol_24h", 0) / 1_000_000, 1),
                "change_pct": round(e.get("change_pct", 0), 2),
                "reasons":    e.get("reasons", []),
                "hours_active": round((now_ts - e.get("entered_ts", now_ts)) / 3600, 1),
            }
            for sym, e in active[:10]
        ],
    }
