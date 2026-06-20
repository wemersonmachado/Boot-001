"""
Signal Filters — TRADER 001
============================
9 filtros de qualidade aplicados em job_sinais_scan antes do envio ao Telegram.

  1. Session Filter          — penaliza sessão asiática, bonifica NY/Europa
  2. BTC Real-Time Veto      — bloqueia direção contrária ao BTC dos últimos 45min
  3. Staleness Decay         — penaliza sinais velhos (−0.5pt/min, max −20pts)
  4. MTF Confirmation        — exige alinhamento TF corrente + TF superior
  5. Funding Direction       — contrarian: funding extremo penaliza direção majoritária
  7. Kelly Criterion         — multiplica tamanho de posição pela qualidade do sinal
  8. VRA (Volatility Regime) — adapta min_score ao regime BTC atual
  9. Sector Rotation         — bonifica sinais na direção do setor em momentum
 10. Structural Tag Filter   — NORMAL: obriga tag estrutural V6 confirmada

Todos os filtros com I/O de rede têm cache para zero impacto em chamadas API.
"""
import asyncio
import time
from datetime import datetime, timezone


# ── Tags estruturais V6 reconhecidas ─────────────────────────────────────────
STRUCTURAL_TAGS = {
    "BEAR-TREND", "BULL-TREND", "DEATH-X", "GOLDEN-X",
    "MACRO-BEAR", "MACRO-BULL", "MACRO-CONTRA",
    "DIV-REG", "DIV-HID", "OB/FVG", "BOS", "sweep", "struct",
    "FIB618", "FIB", "RGM-TRE", "RGM-RNG", "RGM-BRK",
    "TREND-CONT",
    # Tags dos engines VDLS e MEAN_REV (antes bloqueados por falta de tag)
    "VDLS-SWEEP", "CVD-DIV", "LQ-SWEEP", "RANGE", "BB", "RSI-EXTREME",
    "MEAN-REV", "VWAP-REJ", "DELTA-DIV",
}

# TF hierarchy para confirmação multi-TF
_TF_HIGHER = {
    "3m":  "15m",
    "5m":  "15m",
    "15m": "1h",
    "1h":  "4h",
    "4h":  "1d",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SESSION FILTER
# ═══════════════════════════════════════════════════════════════════════════════

def get_session() -> dict:
    """
    Classifica a sessão atual e retorna ajuste de score.

    UTC → BRT (-3h):
      Ásia      00–08 UTC → 21h–05h BRT  | fakeouts, manipulação, funding extremo
      Europa    08–13 UTC → 05h–10h BRT  | momentum começa, tendências se formam
      NY        13–21 UTC → 10h–18h BRT  | maior volume, breakouts confiáveis
      NY Close  21–00 UTC → 18h–21h BRT  | reversões, profit-taking
    """
    h = datetime.now(timezone.utc).hour

    if 0 <= h < 8:
        return {
            "name":              "Asia",
            "score_adj":         -5,
            "block_aggressive":  (1 <= h <= 5),   # bloqueia AGGRESSIVE 01h-05h UTC
            "note":              f"Sessão Ásia ({h}h UTC / {(h-3)%24}h BRT)",
        }
    elif 8 <= h < 13:
        return {
            "name":              "Europa",
            "score_adj":         +3,
            "block_aggressive":  False,
            "note":              f"Sessão Europa ({h}h UTC)",
        }
    elif 13 <= h < 21:
        return {
            "name":              "NY",
            "score_adj":         +5,
            "block_aggressive":  False,
            "note":              f"Sessão NY ({h}h UTC)",
        }
    else:
        return {
            "name":              "NY_Close",
            "score_adj":         -2,
            "block_aggressive":  False,
            "note":              f"Sessão NY Close ({h}h UTC)",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. BTC REAL-TIME VETO
# ═══════════════════════════════════════════════════════════════════════════════

_btc_veto_cache: dict = {
    "ts": 0.0, "block_long": False, "block_short": False, "change_45m": 0.0
}


async def refresh_btc_veto() -> dict:
    """
    Variação do BTC nas últimas 3 velas de 15m (≈45 min).
    BTC  < −1.5% → veta LONG de altcoins
    BTC  > +2.0% → veta SHORT de altcoins
    Cache 5 min — não adiciona chamadas extras por sinal.
    """
    global _btc_veto_cache
    if time.time() - _btc_veto_cache["ts"] < 300:
        return _btc_veto_cache

    try:
        from klines_cache import get_klines_cached
        df = await get_klines_cached("BTCUSDT", "15m", limit=8)
        if df is not None and len(df) >= 5:
            change = (float(df["close"].iloc[-1]) / float(df["close"].iloc[-4]) - 1) * 100
            block_long  = change < -2.5   # era -1.5 — muito sensível em volatilidade normal
            block_short = change > 3.0    # era 2.0
            _btc_veto_cache = {
                "ts":          time.time(),
                "block_long":  block_long,
                "block_short": block_short,
                "change_45m":  round(change, 2),
            }
            if block_long or block_short:
                blocked = "LONGs" if block_long else "SHORTs"
                print(f"[FILTER] BTC veto: {change:+.2f}% 45m → bloqueando {blocked}")
    except Exception as e:
        print(f"[FILTER] BTC veto erro: {e}")

    return _btc_veto_cache


def btc_veto_passes(signal: dict, veto: dict) -> bool:
    direction = signal.get("direction", "").upper()
    if "LONG"  in direction and veto.get("block_long"):
        return False
    if "SHORT" in direction and veto.get("block_short"):
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# 3. STALENESS DECAY
# ═══════════════════════════════════════════════════════════════════════════════

def apply_staleness_decay(score: float, generated_ts: float) -> tuple[float, float]:
    """
    Penaliza sinais velhos: −0.5pt por minuto, máx −20pts.
    Retorna (score_ajustado, penalidade_aplicada).
    """
    if generated_ts <= 0:
        return score, 0.0
    age_min = (time.time() - generated_ts) / 60.0
    penalty = min(20.0, age_min * 0.5)
    return max(0.0, score - penalty), penalty


# ═══════════════════════════════════════════════════════════════════════════════
# 4. MTF CONFIRMATION
# ═══════════════════════════════════════════════════════════════════════════════

def check_mtf(signal: dict, all_signals: list) -> dict:
    """
    Verifica alinhamento com o TF superior no cache de sinais.

    NORMAL:     15m precisa de 1h na mesma direção → +8 confirmado / −8 divergente
    AGGRESSIVE: 5m  precisa de 15m na mesma direção → +5 confirmado / −5 divergente
    Sem TF superior no cache → neutro (sem bônus, sem penalidade).
    """
    asset     = signal.get("asset", "")
    direction = signal.get("direction", "").upper()
    tf        = signal.get("timeframe", "")
    higher_tf = _TF_HIGHER.get(tf)

    if not higher_tf:
        return {"confirmed": True, "bonus": 0, "note": ""}

    for s in all_signals:
        if s.get("asset") != asset:
            continue
        if s.get("timeframe") != higher_tf:
            continue
        s_dir = str(s.get("direction", "")).split(".")[-1].strip().upper()
        if s_dir == direction:
            bonus = 8 if tf in ("15m", "1h") else 5
            return {"confirmed": True,  "bonus": bonus,  "note": f"MTF {tf}+{higher_tf} ✓"}
        else:
            bonus = -8 if tf in ("15m", "1h") else -5   # era -15/-10 — muito punitivo
            return {"confirmed": False, "bonus": bonus,  "note": f"MTF {tf}×{higher_tf} divergente"}

    return {"confirmed": True, "bonus": 0, "note": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. FUNDING DIRECTION (contrarian filter)
# ═══════════════════════════════════════════════════════════════════════════════

_funding_cache: dict = {}


async def get_funding_context() -> dict:
    """
    Funding rate do BTC como proxy do sentimento geral do mercado.
    Cache 15 min — uma chamada por ciclo de scan, não por sinal.

    Funding > +0.08%  → longs overextended  → penaliza LONG / bonifica SHORT
    Funding < −0.05%  → shorts excessivos   → bonifica LONG / penaliza SHORT
    """
    entry = _funding_cache.get("btc")
    if entry and time.time() - entry["ts"] < 900:
        return entry["data"]

    result = {"rate": 0.0, "label": "Neutro", "adj_long": 0, "adj_short": 0}
    try:
        from data_fetcher import fetch, BINANCE_BASE
        data = await fetch(f"{BINANCE_BASE}/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
        rate = float(data.get("lastFundingRate", 0))
        result["rate"] = rate

        if rate > 0.0008:
            result.update({"label": "Longs overextended", "adj_long": -10, "adj_short": +5})
        elif rate > 0.0003:
            result.update({"label": "Funding alto",       "adj_long": -5,  "adj_short": +2})
        elif rate < -0.0005:
            result.update({"label": "Shorts excessivos",  "adj_long": +5,  "adj_short": -10})
        elif rate < -0.0002:
            result.update({"label": "Funding negativo",   "adj_long": +2,  "adj_short": -5})

        if result["label"] != "Neutro":
            print(f"[FILTER] Funding BTC {rate*100:.4f}% → {result['label']}")
    except Exception as e:
        print(f"[FILTER] Funding erro: {e}")

    _funding_cache["btc"] = {"ts": time.time(), "data": result}
    return result


def funding_score_adj(signal: dict, funding: dict) -> float:
    direction = signal.get("direction", "").upper()
    if "LONG"  in direction: return float(funding.get("adj_long",  0))
    if "SHORT" in direction: return float(funding.get("adj_short", 0))
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 7. KELLY CRITERION — tamanho dinâmico por qualidade do sinal
# ═══════════════════════════════════════════════════════════════════════════════

def kelly_size_multiplier(score: float, rr: float) -> float:
    """
    Fator multiplicador do risco base.
    Score alto + RR alto → aposta maior. Score médio → aposta menor.

    Retorna: 0.5x (fraco) → 1.5x (excelente)
    """
    if score >= 85 and rr >= 3.0:   return 1.5
    elif score >= 78 and rr >= 2.5: return 1.2
    elif score >= 70 and rr >= 2.0: return 1.0
    elif score >= 65:               return 0.7
    else:                           return 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# 8. VRA — VOLATILITY REGIME ADAPTER
# ═══════════════════════════════════════════════════════════════════════════════

_vra_cache: dict = {"ts": 0.0, "regime": "NORMAL", "atr_ratio": 1.0}


async def get_volatility_regime() -> dict:
    """
    Compara ATR(14) atual do BTC 1h com média de 20 períodos.

    Ratio > 1.5  → EXPANSION   (breakouts + momentum funcionam bem)
    Ratio < 0.7  → COMPRESSION (range lateral, evitar breakouts)
    Outros        → NORMAL
    Cache 15 min.
    """
    global _vra_cache
    if time.time() - _vra_cache["ts"] < 900:
        return _vra_cache

    try:
        import numpy as np
        from klines_cache import get_klines_cached
        df = await get_klines_cached("BTCUSDT", "1h", limit=60)
        if df is not None and len(df) >= 30:
            hl  = df["high"] - df["low"]
            hc  = (df["high"] - df["close"].shift()).abs()
            lc  = (df["low"]  - df["close"].shift()).abs()
            tr  = np.maximum(hl, np.maximum(hc, lc))
            atr = tr.rolling(14).mean()

            atr_cur    = float(atr.iloc[-1])
            atr_avg_20 = float(atr.rolling(20).mean().iloc[-1])
            ratio      = atr_cur / atr_avg_20 if atr_avg_20 > 0 else 1.0

            regime = ("EXPANSION"   if ratio > 1.5 else
                      "COMPRESSION" if ratio < 0.7 else "NORMAL")

            _vra_cache = {"ts": time.time(), "regime": regime, "atr_ratio": round(ratio, 2)}
            print(f"[FILTER] VRA: {regime} (ATR ratio {ratio:.2f})")
    except Exception as e:
        print(f"[FILTER] VRA erro: {e}")

    return _vra_cache


def vra_adjustments(regime: str) -> dict:
    """Retorna deltas de min_score e max_signals por regime."""
    if regime == "COMPRESSION":
        return {"score_delta": +5, "max_signals": 2, "tp_mult": 0.85,
                "note": "⚠️ Regime COMPRESSÃO — exigência elevada"}
    elif regime == "EXPANSION":
        return {"score_delta": -3, "max_signals": 4, "tp_mult": 1.2,
                "note": "🚀 Regime EXPANSÃO — breakouts confiáveis"}
    return {"score_delta": 0, "max_signals": 3, "tp_mult": 1.0, "note": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# 9. SECTOR ROTATION
# ═══════════════════════════════════════════════════════════════════════════════

def sector_rotation_adj(signal: dict, hot_sectors: list, cold_sectors: list = None) -> dict:
    """
    Ajusta score conforme rotação setorial (dados do CMC).
    hot_sectors : setores com avg_change_24h >= +5%
    cold_sectors: setores com avg_change_24h <= −5%
    """
    from cmc_client import get_sector
    cold_sectors = cold_sectors or []
    sector    = get_sector(signal.get("asset", ""))
    direction = signal.get("direction", "").upper()

    if sector in hot_sectors:
        if "LONG" in direction:
            return {"adj": +5, "note": f"Setor {sector} em alta +5pts"}
        return            {"adj": -5, "note": f"SHORT em setor aquecido −5pts"}

    if sector in cold_sectors:
        if "SHORT" in direction:
            return {"adj": +5, "note": f"Setor {sector} em queda +5pts SHORT"}
        return            {"adj": -5, "note": f"LONG em setor em queda −5pts"}

    return {"adj": 0, "note": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# 10. STRUCTURAL TAG — confirmação V6 obrigatória (NORMAL)
# ═══════════════════════════════════════════════════════════════════════════════

def has_structural_tag(signal: dict) -> bool:
    """
    Retorna True se o sinal contém pelo menos 1 tag estrutural V6.
    Verifica: reason string + confirmed_signals list.
    """
    reason    = signal.get("reason", "")
    confirmed = signal.get("confirmed_signals", [])

    for tag in STRUCTURAL_TAGS:
        if tag in reason:
            return True
    for item in confirmed:
        for tag in STRUCTURAL_TAGS:
            if tag in str(item):
                return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# 11. LIQUIDATION SCORE — clusters de liquidação como catalisador
# ═══════════════════════════════════════════════════════════════════════════════

_liq_cache: dict = {}

async def get_liquidation_context(symbol: str) -> dict:
    """
    Usa liquidações recentes como sinal direcional.

    Lógica:
      > $2M liquidados na direção OPOSTA ao sinal → +10pts (stops sendo caçados = confirma)
      > $5M liquidados na MESMA direção do sinal  → −8pts  (exaustão de movimento)
    Cache 5 min por ativo.
    """
    sym = symbol.upper().replace("-", "").replace("USDT", "") + "USDT"
    entry = _liq_cache.get(sym)
    if entry and time.time() - entry["ts"] < 300:
        return entry["data"]

    result = {"long_liq_usd": 0.0, "short_liq_usd": 0.0, "label": "Neutro"}
    try:
        from data_fetcher import get_liquidations
        liqs = await get_liquidations(sym, limit=50)
        long_liq  = sum(float(l.get("qty", 0)) * float(l.get("price", 0))
                        for l in liqs if l.get("side", "").upper() == "BUY")
        short_liq = sum(float(l.get("qty", 0)) * float(l.get("price", 0))
                        for l in liqs if l.get("side", "").upper() == "SELL")
        result["long_liq_usd"]  = long_liq
        result["short_liq_usd"] = short_liq
        if long_liq > 2_000_000 or short_liq > 2_000_000:
            result["label"] = f"Liq L=${long_liq/1e6:.1f}M S=${short_liq/1e6:.1f}M"
    except Exception as e:
        print(f"[FILTER] Liquidation erro ({sym}): {e}")

    _liq_cache[sym] = {"ts": time.time(), "data": result}
    return result


def liquidation_score_adj(signal: dict, liq: dict) -> float:
    """
    Retorna ajuste de score baseado em clusters de liquidação.
    Liquidações na direção oposta confirmam o movimento (stops sendo zerados).
    Liquidações na mesma direção indicam exaustão.
    """
    direction      = signal.get("direction", "").upper()
    long_liq_usd   = liq.get("long_liq_usd", 0.0)
    short_liq_usd  = liq.get("short_liq_usd", 0.0)

    if "LONG" in direction:
        opposite = short_liq_usd   # shorts sendo liquidados → confirma LONG
        same     = long_liq_usd    # longs sendo liquidados → exaustão LONG
    else:
        opposite = long_liq_usd
        same     = short_liq_usd

    if opposite > 5_000_000:  return +10.0
    if opposite > 2_000_000:  return  +6.0
    if same     > 5_000_000:  return  -8.0
    if same     > 2_000_000:  return  -4.0
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 12. RELATIVE STRENGTH — força relativa vs BTC
# ═══════════════════════════════════════════════════════════════════════════════

_rs_cache: dict = {"ts": 0.0, "scores": {}}

async def refresh_rs_scores(symbols: list) -> dict:
    """
    Calcula RS Score de cada ativo vs BTC (retorno 24h relativo).
    RS > 0  → ativo outperformou BTC (preferência para LONG)
    RS < 0  → ativo underperformou BTC (preferência para SHORT)
    Cache 15 min.
    """
    global _rs_cache
    if time.time() - _rs_cache["ts"] < 300:  # era 900s — atualiza RS a cada 5min
        return _rs_cache["scores"]

    try:
        from klines_cache import get_klines_cached
        btc_df  = await get_klines_cached("BTCUSDT", "1h", limit=25)
        btc_ret = 0.0
        if btc_df is not None and len(btc_df) >= 2:
            btc_ret = float((btc_df["close"].iloc[-1] - btc_df["close"].iloc[-24]) /
                            btc_df["close"].iloc[-24] * 100)

        scores = {}
        for sym in symbols:
            try:
                df = await get_klines_cached(sym, "1h", limit=25)
                if df is not None and len(df) >= 24:
                    ret = float((df["close"].iloc[-1] - df["close"].iloc[-24]) /
                                df["close"].iloc[-24] * 100)
                    scores[sym] = round(ret - btc_ret, 3)
            except Exception:
                scores[sym] = 0.0

        _rs_cache = {"ts": time.time(), "scores": scores}
        print(f"[FILTER] RS scores atualizados: {len(scores)} ativos vs BTC {btc_ret:+.2f}%")
    except Exception as e:
        print(f"[FILTER] RS Score erro: {e}")

    return _rs_cache.get("scores", {})


def rs_score_adj(symbol: str, direction: str, rs_scores: dict) -> float:
    """
    Ajuste de score baseado na força relativa do ativo vs BTC.
    LONG em outperformer  → +5pts
    LONG em underperformer → -5pts
    SHORT em underperformer → +5pts
    SHORT em outperformer  → -5pts
    """
    rs = rs_scores.get(symbol, rs_scores.get(symbol.replace("USDT", ""), 0.0))
    direction = direction.upper()

    if "LONG" in direction:
        if rs >= +3.0:  return +5.0
        if rs <= -3.0:  return -5.0
    else:
        if rs <= -3.0:  return +5.0
        if rs >= +3.0:  return -5.0
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 13. SESSION THRESHOLD — SCORE_THRESH dinâmico por sessão
# ═══════════════════════════════════════════════════════════════════════════════

def session_score_threshold(base_thresh: int) -> int:
    """
    Ajusta o SCORE_THRESH mínimo baseado na sessão atual.
    Sessão Ásia (lateral/manipulação) → thresh +1 (mais seletivo)
    Sessão NY (alto volume)           → thresh mantido
    Sessão Off                        → thresh +1
    """
    session = get_session()
    name    = session.get("name", "NY")
    if name == "Asia":
        return base_thresh + 1
    if name == "NY_Close":
        return base_thresh + 1
    return base_thresh


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE — busca contexto async em paralelo (1x por ciclo de scan)
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_scan_context(symbols: list = None) -> dict:
    """
    Busca BTC veto, VRA, funding e RS scores em paralelo.
    Chamado UMA VEZ no início de job_sinais_scan, não por sinal.

    Args:
        symbols: lista de símbolos do scan atual (usado para RS scores).
                 Se None, RS scores não são calculados neste ciclo.
    """
    symbols = symbols or []

    veto, vra, funding, rs_scores = await asyncio.gather(
        refresh_btc_veto(),
        get_volatility_regime(),
        get_funding_context(),
        refresh_rs_scores(symbols),
        return_exceptions=True,
    )

    def _safe(v, default):
        return v if not isinstance(v, Exception) else default

    # Hot/cold sectors do CMC (síncrono — vem do cache do market_engine)
    hot_sectors  = []
    cold_sectors = []
    try:
        from market_engine import get_market_state
        ms  = get_market_state()
        raw = ms.get("top_sectors", [])
        hot_sectors  = [s["name"] for s in raw if s.get("chg", 0) >= 5]
        cold_sectors = [s["name"] for s in raw if s.get("chg", 0) <= -5]
    except Exception:
        pass

    # CMC trending symbols — usa cache existente, zero chamadas extras
    cmc_trending: set = set()
    try:
        from cmc_client import get_trending_symbols
        cmc_trending = await get_trending_symbols()
    except Exception:
        pass

    return {
        "session":               get_session(),
        "btc_veto":              _safe(veto,      {"block_long": False, "block_short": False, "change_45m": 0}),
        "vra":                   _safe(vra,       {"regime": "NORMAL", "atr_ratio": 1.0}),
        "funding":               _safe(funding,   {"rate": 0, "label": "Neutro", "adj_long": 0, "adj_short": 0}),
        "rs_scores":             _safe(rs_scores, {}),
        "hot_sectors":           hot_sectors,
        "cold_sectors":          cold_sectors,
        "cmc_trending":          cmc_trending,
        # get_liquidation_context é chamado por sinal (tem cache 5min por ativo)
        "get_liquidation_ctx":   get_liquidation_context,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# EVALUATE — aplica todos os filtros a um sinal
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_signal(
    signal:         dict,
    ctx:            dict,
    all_signals:    list,
    mode:           str,
    base_min_score: float,
) -> dict:
    """
    Aplica os 9 filtros e retorna:
    {
        "passes":           bool,
        "effective_score":  float,
        "kelly_mult":       float,
        "notes":            list[str],
        "block_reason":     str,
        "vra_tp_mult":      float,
        "vra_max_signals":  int,
    }
    """
    notes        = []
    score        = float(signal.get("confidence", 0))
    direction    = signal.get("direction", "").upper()

    # ── 2. BTC Real-Time Veto ─────────────────────────────────────────────────
    if not btc_veto_passes(signal, ctx["btc_veto"]):
        chg = ctx["btc_veto"].get("change_45m", 0)
        return _block(f"BTC veto {chg:+.2f}% 45m", notes, score)

    # ── 2b. RSI Overbought/Oversold Penalty ──────────────────────────────────
    # Penaliza LONGs com RSI>72 e SHORTs com RSI<28 (-15pts por nível de excesso)
    import re as _re_rsi
    # FIX jun/2026: o sinal carrega o RSI na chave 'rsi_val' (modelo TradeSignal),
    # não 'rsi'. Em scalp o reason usa "StochRSI K:" (sem "RSI:" parseável), então o
    # bloqueio ficava DESATIVADO. Agora lê rsi_val/rsi e cai no regex só como último
    # recurso (excluindo "StochRSI" para não capturar o K errado).
    _rsi_raw = float(signal.get("rsi_val", 0) or signal.get("rsi", 0) or 0)
    if _rsi_raw == 0:
        _reason_clean = _re_rsi.sub(r'Stoch[ -]?RSI', '', signal.get("reason", ""), flags=_re_rsi.IGNORECASE)
        _m_rsi = _re_rsi.search(r'RSI[=\s:]+(\d+\.?\d*)', _reason_clean, _re_rsi.IGNORECASE)
        if _m_rsi:
            _rsi_raw = float(_m_rsi.group(1))
    if _rsi_raw > 0:
        # Bloqueio só em EXTREMOS de exaustão (blow-off top / capitulação):
        # LONG>75 = comprando no topo; SHORT<25 = vendendo no fundo. A faixa 60-75
        # (momentum saudável) é permitida; o PROX-GATE estrutural cuida do resto.
        if "LONG" in direction and _rsi_raw > 75:
            notes.append(f"REJEITADO: RSI sobrecomprado/topo ({_rsi_raw:.0f} > 75)")
            return _block("RSI sobrecomprado/topo", notes, score)
        elif "SHORT" in direction and _rsi_raw < 25:
            notes.append(f"REJEITADO: RSI sobrevendido/fundo ({_rsi_raw:.0f} < 25)")
            return _block("RSI sobrevendido/fundo", notes, score)

    # ── 3. Staleness Decay ────────────────────────────────────────────────────
    gen_ts = float(signal.get("generated_ts", 0) or signal.get("ts", 0))
    score, stale_penalty = apply_staleness_decay(score, gen_ts)
    if stale_penalty > 0.5:
        age_min = (time.time() - gen_ts) / 60
        notes.append(f"Sinal {age_min:.0f}min atrás −{stale_penalty:.0f}pts")

    # ── 5. Funding Direction ──────────────────────────────────────────────────
    f_adj = funding_score_adj(signal, ctx["funding"])
    score += f_adj
    if f_adj != 0:
        notes.append(f"Funding {f_adj:+.0f}pts ({ctx['funding']['label']})")

    # ── 1. Session Adjustment ─────────────────────────────────────────────────
    sess = ctx["session"]
    if mode == "AGGRESSIVE" and sess.get("block_aggressive"):
        # Horário de risco: penalidade forte em vez de bloqueio total.
        # Setup excepcional (score alto) ainda pode passar o threshold.
        score += -8
        notes.append(f"⚠️ Horário risco {sess['name']} −8pts")
    score += sess["score_adj"]
    if sess["score_adj"] != 0:
        notes.append(f"Sessão {sess['name']} {sess['score_adj']:+d}pts")

    # ── 4. MTF Confirmation ───────────────────────────────────────────────────
    mtf = check_mtf(signal, all_signals)
    score += mtf["bonus"]
    if mtf["note"]:
        notes.append(mtf["note"])

    # ── 8. VRA ────────────────────────────────────────────────────────────────
    vra_adj = vra_adjustments(ctx["vra"]["regime"])
    score  += vra_adj["score_delta"]
    if vra_adj["note"]:
        notes.append(vra_adj["note"])

    # ── 9. Sector Rotation ────────────────────────────────────────────────────
    sec = sector_rotation_adj(signal, ctx["hot_sectors"], ctx["cold_sectors"])
    score += sec["adj"]
    if sec["note"]:
        notes.append(sec["note"])

    # ── 9b. CMC Trending boost ────────────────────────────────────────────────
    cmc_trending = ctx.get("cmc_trending", set())
    if cmc_trending:
        asset_base = signal.get("asset", "").replace("USDT", "").replace("BUSD", "").upper()
        if asset_base in cmc_trending and "LONG" in direction:
            score += 3
            notes.append("CMC trending +3pts")

    # ── 10. Structural Tag (NORMAL obrigatório) ───────────────────────────────
    if mode == "NORMAL" and not has_structural_tag(signal):
        return _block("Sem tag estrutural V6", notes, score)

    # ── Score efetivo vs threshold ─────────────────────────────────────────────
    effective_min = base_min_score + vra_adj["score_delta"]
    if score < effective_min:
        return _block(f"Score {score:.0f} < min {effective_min:.0f}", notes, score)

    # ── 7. Kelly Multiplier ───────────────────────────────────────────────────
    rr   = float(signal.get("rr", 0))
    kmul = kelly_size_multiplier(score, rr)

    return {
        "passes":          True,
        "effective_score": round(score, 1),
        "kelly_mult":      kmul,
        "notes":           notes,
        "block_reason":    "",
        "vra_tp_mult":     vra_adj.get("tp_mult", 1.0),
        "vra_max_signals": vra_adj.get("max_signals", 3),
    }


def _block(reason: str, notes: list, score: float) -> dict:
    return {
        "passes":          False,
        "effective_score": round(score, 1),
        "kelly_mult":      0.0,
        "notes":           notes,
        "block_reason":    reason,
        "vra_tp_mult":     1.0,
        "vra_max_signals": 3,
    }
