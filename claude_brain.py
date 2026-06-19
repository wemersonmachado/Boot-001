"""
Claude Brain — API do Claude como filtro inteligente de trades.
Analisa sinal + contexto e retorna: approve / reason / confidence.
"""
import asyncio
import json
import os
import re
import time

_client = None
_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")  # haiku: 10x mais rápido, $0.80/$4.00/M tokens
# Preços por milhão de tokens (USD) — claude-haiku-4-5
_PRICE_INPUT_PER_M  = 0.80
_PRICE_OUTPUT_PER_M = 4.00

# Contadores de sessão (resetados ao desativar o brain)
_session_input_tokens:  int   = 0
_session_output_tokens: int   = 0
_session_calls:         int   = 0

# Cache de decisões por ativo — evita chamar 2x o mesmo ativo em 2min
_decision_cache: dict = {}   # {asset: {"ts": float, "result": dict}}
_CACHE_TTL = 120             # 2 minutos (era 5min — cache longo ocultava rejeições incorretas)


def get_session_usage() -> dict:
    """Retorna uso acumulado da sessão atual com custo calculado."""
    cost = (
        _session_input_tokens  * _PRICE_INPUT_PER_M  / 1_000_000 +
        _session_output_tokens * _PRICE_OUTPUT_PER_M / 1_000_000
    )
    return {
        "input_tokens":  _session_input_tokens,
        "output_tokens": _session_output_tokens,
        "calls":         _session_calls,
        "cost_usd":      round(cost, 6),
    }


def reset_session_usage():
    """Zera contadores de sessão (chamado ao desativar o brain)."""
    global _session_input_tokens, _session_output_tokens, _session_calls
    _session_input_tokens  = 0
    _session_output_tokens = 0
    _session_calls         = 0


def is_configured() -> bool:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return bool(key and len(key) > 20)


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return None
        _client = anthropic.Anthropic(api_key=key)
        return _client
    except ImportError:
        print("[CLAUDE BRAIN] Pacote 'anthropic' nao instalado. Execute: pip install anthropic")
        return None


async def analyze_signal(signal: dict, context: dict) -> dict:
    """
    Analisa um sinal e retorna decisao de trading.
    Returns: {"approve": bool, "reason": str, "confidence": float}
    """
    client = _get_client()
    if not client:
        return {"approve": True, "reason": "Claude API indisponivel — trade liberado", "confidence": 0.5}

    asset      = signal.get("asset", "?")

    # Cache: mesmo ativo + mesma direção nos últimos 5min → reutiliza resultado
    _cache_key = f"{asset}_{signal.get('direction','')}"
    _cached = _decision_cache.get(_cache_key)
    if _cached and (time.time() - _cached["ts"]) < _CACHE_TTL:
        print(f"[CLAUDE BRAIN] {asset} cache hit — reutilizando decisão ({int(time.time()-_cached['ts'])}s atrás)")
        return _cached["result"]
    direction  = signal.get("direction", "?")
    entry      = float(signal.get("entry", 0))
    sl         = float(signal.get("stop_loss", 0))
    tp1        = float(signal.get("tp1", 0))
    tp2        = float(signal.get("tp2", 0))
    rr         = float(signal.get("rr", 0))
    score      = float(signal.get("confidence", 0))
    reason_    = signal.get("reason", "")
    tf         = signal.get("timeframe", "15m")
    trade_type = signal.get("trade_type", "DAY_TRADE")

    sl_pct  = abs(entry - sl)  / entry * 100 if entry else 0
    tp1_pct = abs(tp1 - entry) / entry * 100 if entry else 0

    fg         = context.get("fear_greed", "--")
    btc_chg    = context.get("btc_change", "--")
    btc_fund   = context.get("btc_funding", "--")
    btc_oi     = context.get("btc_oi", "--")
    btc_trend  = context.get("btc_trend", "--")
    open_cnt   = context.get("open_trades", 0)
    daily_pnl  = context.get("daily_pnl", 0.0)
    consec_l   = context.get("consecutive_losses", 0)
    banca      = context.get("banca", 0.0)
    a_price    = context.get("asset_price", "--")
    a_vol24h   = context.get("asset_vol24h", "--")
    a_funding  = context.get("asset_funding", "--")
    a_oi       = context.get("asset_oi", "--")
    a_ls       = context.get("asset_ls", "--")
    a_liq      = context.get("asset_liquidations", "--")
    a_rsi      = context.get("asset_rsi", "--")
    a_ema      = context.get("asset_ema", "--")
    a_vol_rel  = context.get("asset_vol_rel", "--")
    news       = context.get("news_summary", "")
    recent_candles = context.get("recent_candles", "")

    prompt = f"""Voce e um trader quantitativo especializado em Binance Futures USDT-M.

Analise TODOS os dados abaixo e decida se deve APROVAR ou REJEITAR esta operacao, e sugira ajustes de risco e alvos.

=== SINAL TECNICO ===
Par: {asset} | Direcao: {direction} | Timeframe: {tf} | Tipo: {trade_type}
Entrada: ${entry:,.4f} | Preco atual: ${a_price}
Stop Loss: ${sl:,.4f} (-{sl_pct:.1f}%) | TP1: ${tp1:,.4f} (+{tp1_pct:.1f}%) | TP2: ${tp2:,.4f}
R:R: {rr:.1f}:1 | Score tecnico: {score:.0f}/100
Motivo tecnico: {reason_}

=== DADOS DO ATIVO — TEMPO REAL ===
RSI(14): {a_rsi} | Posicao: {a_ema}
Volume atual: {a_vol_rel}
Funding rate: {a_funding} | Open Interest: {a_oi}
Long/Short ratio: {a_ls}
Liquidacoes recentes: {a_liq}
Volume 24h: {a_vol24h}

=== PRICE ACTION RECENTE (ULTIMOS 5 CANDLES) ===
{recent_candles if recent_candles else "Indisponivel"}

=== CONTEXTO MACRO E BTC ===
Fear & Greed: {fg}/100
BTC variacao 24h: {btc_chg}% | BTC Funding: {btc_fund}% | BTC OI: {btc_oi} | BTC Trend: {btc_trend}
{f"Noticias: {news[:400]}" if news else ""}

=== SESSAO ===
Trades abertos: {open_cnt} | PnL do dia: ${daily_pnl:.2f}
Perdas consecutivas: {consec_l} | Banca efetiva: ${banca:.2f}

=== CRITERIOS OBJETIVOS ===
REJEITAR se: R:R < 1.2 (risco/retorno inviável para qualquer estratégia)
REJEITAR se: RSI > 85 em LONG ou RSI < 15 em SHORT (sobrecompra/sobrevenda EXTREMA)
REJEITAR se: funding rate > +0.20% em LONG (mercado altamente alavancado no comprado)
REJEITAR se: 3+ perdas consecutivas E score < 70 (protecao de banca em drawdown)
REJEITAR se: L/S ratio > 85% Longs (mercado todo comprado = perigoso para LONG)
APROVAR LONGs com RSI ate 80 durante bull run — RSI elevado e sinal de forca, nao de reversao
APROVAR com alta confianca se: volume > 1.5x media, funding neutro (<0.05%), OI crescendo, score >= 70
DIVERGENCIA DE FLUXO: Rejeite se a Altcoin estiver em LONG mas o BTC estiver em forte queda (Trend de baixa).

=== AJUSTES DE RISCO E ALVO (Novas Funcionalidades) ===
- leverage_multiplier: 1.0 = normal, 0.5 = corta alavancagem pela metade, etc.
- size_multiplier: 1.0 = tamanho da mao normal, 0.5 = entra com meia mao se incerto.
- tp_adjust_pct: 1.0 = mantem TP atual, 1.2 = estica o alvo 20% (use em forte tendencia).
- sl_adjust_pct: 1.0 = mantem SL atual, 0.8 = aperta o SL em 20%.
- news_sentiment: de -10 (muito negativo/bearish) a +10 (muito positivo/bullish).

Responda APENAS com JSON valido neste formato exato (nao inclua formatacao markdown fora do json):
{{
  "approve": true,
  "reason": "motivo objetivo em 1 linha curta",
  "confidence": 0.85,
  "leverage_multiplier": 1.0,
  "size_multiplier": 1.0,
  "tp_adjust_pct": 1.0,
  "sl_adjust_pct": 1.0,
  "news_sentiment": 0
}}"""

    try:
        def _call():
            # Define timeout direto no client se suportado, senão o wrap do asyncio cuida disso
            return client.messages.create(
                model=_MODEL,
                max_tokens=250,
                messages=[{"role": "user", "content": prompt}],
                timeout=15.0 # Timeout a nível de API do client
            )

        # Adiciona proteção dupla com asyncio.wait_for (máximo 15 segundos)
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=18.0)

        # Acumula uso de tokens da sessão
        global _session_input_tokens, _session_output_tokens, _session_calls
        if hasattr(response, "usage") and response.usage:
            _session_input_tokens  += getattr(response.usage, "input_tokens",  0)
            _session_output_tokens += getattr(response.usage, "output_tokens", 0)
        _session_calls += 1

        usage = get_session_usage()
        print(f"[CLAUDE BRAIN] sessao: {usage['calls']} calls | ${usage['cost_usd']:.4f} gastos")

        text = response.content[0].text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            approved = bool(data.get("approve", True))
            reason   = str(data.get("reason", ""))[:200]
            conf     = float(data.get("confidence", 0.7))
            
            # Novos campos
            leverage_mult = float(data.get("leverage_multiplier", 1.0))
            size_mult     = float(data.get("size_multiplier", 1.0))
            tp_adj        = float(data.get("tp_adjust_pct", 1.0))
            sl_adj        = float(data.get("sl_adjust_pct", 1.0))
            news_sent     = int(data.get("news_sentiment", 0))
            
            status   = "✅ APROVADO" if approved else "❌ REJEITADO"
            print(f"[CLAUDE BRAIN] {asset} {direction} → {status} | {reason} | conf={conf:.2f} | R:R={rr:.1f} | sent={news_sent}")
            if approved and (size_mult != 1.0 or tp_adj != 1.0):
                print(f"               ↳ Ajustes: Size={size_mult}x | Lev={leverage_mult}x | TP={tp_adj}x | SL={sl_adj}x")
                
            _result = {
                "approve": approved, 
                "reason": reason, 
                "confidence": conf,
                "leverage_multiplier": leverage_mult,
                "size_multiplier": size_mult,
                "tp_adjust_pct": tp_adj,
                "sl_adjust_pct": sl_adj,
                "news_sentiment": news_sent
            }
            _decision_cache[_cache_key] = {"ts": time.time(), "result": _result}
            return _result
    except Exception as e:
        print(f"[CLAUDE BRAIN] Erro na API: {e}")
        # Fallback liberal: aprova o trade quando brain indisponível
        # (antes era False — bloqueava todos os trades silenciosamente em falha de API)
        return {
            "approve": True,
            "reason": f"Brain indisponivel: {str(e)[:80]}",
            "confidence": 0.0,
            "leverage_multiplier": 1.0,
            "size_multiplier": 1.0,
            "tp_adjust_pct": 1.0,
            "sl_adjust_pct": 1.0,
            "news_sentiment": 0
        }
