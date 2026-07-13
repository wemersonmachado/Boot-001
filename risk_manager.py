"""
Risk Manager — position sizing, trailing stops, TP monitoring.
"""
import uuid
from datetime import datetime
from typing import Optional

from config import (
    LEVERAGE_MAP, LEVERAGE_MAX, DEFAULT_RISK_PCT,
    MAX_OPEN_TRADES, TRAILING_MILESTONES, SCALE_OUT_MILESTONES,
    EARLY_BREAKEVEN_PROGRESS, EARLY_BREAKEVEN_MARGIN_PCT,
    ATR_TRAIL_MULT, ATR_TRAIL_MIN_PROFIT_PCT,
)
from models import ActiveTrade, Direction, TradeSignal
from database import save_trade, get_open_trades


def _trade_to_dict(trade: ActiveTrade) -> dict:
    """Serializes ActiveTrade to a dict safe for save_trade() — avoids datetime / enum issues."""
    return {
        "id":          trade.id,
        "asset":       trade.asset,
        "direction":   trade.direction.value,
        "entry_price": trade.entry_price,
        "exit_price":  None,
        "stop_loss":   trade.stop_loss,
        "tp1":         trade.tp1,
        "tp2":         trade.tp2,
        "tp3":         trade.tp3,
        "rr":          trade.rr,
        "leverage":    trade.leverage,
        "size_usdt":   trade.size_usdt,
        "pnl_pct":     trade.pnl_pct,
        "pnl_usdt":    trade.pnl_usdt,
        "status":      trade.status,
        "reason":      trade.reason,
        "confidence":  trade.confidence,
        "timeframe":   "",
        "score_json":  None,
        "opened_at":   trade.opened_at.isoformat() if isinstance(trade.opened_at, datetime) else trade.opened_at,
        "closed_at":   trade.closed_at.isoformat() if isinstance(trade.closed_at, datetime) else trade.closed_at,
    }


def get_leverage(symbol: str) -> int:
    base = symbol.replace("USDT", "").replace("PERP", "")
    return LEVERAGE_MAP.get(base, LEVERAGE_MAP["DEFAULT"])


def suggest_leverage(
    symbol: str,
    score: float,
    body_pct: float,
    vol_ratio: float,
    rsi_val: float,
    sl_pct: float,
    rr: float,
    trade_type: str = "DAY_TRADE",
    v6_tags: list = None,
) -> dict:
    """
    Sugere alavancagem ideal com base no ativo e na qualidade do sinal.

    Fatores considerados (ordem de peso):
      1. Tipo de ativo (tier): BTC/ETH/SOL=premium, altcoins=$5M+=mid, microcaps=low
      2. % distância do SL (quanto mais longe, menos alavancagem)
      3. Score do sinal (qualidade geral)
      4. Corpo/range da vela (limpeza do movimento)
      5. Volume ratio (confirmação)
      6. RSI (sobrevenda/sobrecompra)
      7. R:R (relação risco/retorno)
      8. Tipo de trade (SCALP=maior, SWING=menor)

    Retorna: {"leverage": int, "reason": str, "max_safe": int}
    """
    v6_tags = v6_tags or []
    base    = symbol.replace("USDT", "").replace("PERP", "")

    # ── Tier do ativo ────────────────────────────────────────────────────────
    PREMIUM = {"BTC", "ETH", "SOL"}
    MID     = {"BNB", "XRP", "AVAX", "DOT", "LINK", "ADA", "MATIC", "DOGE",
                "HYPE", "TON", "SUI", "PEPE", "WIF", "ARB", "OP", "TIA",
                "SEI", "JUP", "RENDER", "FET", "NEAR"}

    if base in PREMIUM:
        tier = "premium"
        base_lev = 15
        max_safe  = 20
    elif base in MID:
        tier = "mid"
        base_lev = 10
        max_safe  = 15
    else:
        tier = "micro"
        base_lev = 5
        max_safe  = 10

    # ── Ajustes por fator ────────────────────────────────────────────────────
    adj     = 0
    reasons = []

    # SL distância: SL muito largo = reduz alavancagem
    if sl_pct > 6.0:
        adj -= 3
        reasons.append(f"SL largo ({sl_pct:.1f}%) -3x")
    elif sl_pct > 4.0:
        adj -= 2
        reasons.append(f"SL médio ({sl_pct:.1f}%) -2x")
    elif sl_pct < 2.0:
        adj += 2
        reasons.append(f"SL apertado ({sl_pct:.1f}%) +2x")

    # Score do sinal
    if score >= 85:
        adj += 3
        reasons.append("Score excelente +3x")
    elif score >= 75:
        adj += 2
        reasons.append("Score alto +2x")
    elif score >= 65:
        adj += 1
        reasons.append("Score bom +1x")
    elif score < 60:
        adj -= 2
        reasons.append("Score baixo -2x")

    # Qualidade da vela
    if body_pct >= 0.80:
        adj += 1
        reasons.append("Vela cheia +1x")
    elif body_pct < 0.40:
        adj -= 1
        reasons.append("Vela fraca -1x")

    # Volume
    if vol_ratio >= 3.0:
        adj += 1
        reasons.append(f"Volume {vol_ratio:.1f}x +1x")
    elif vol_ratio < 1.2:
        adj -= 1
        reasons.append("Volume fraco -1x")

    # RSI extremo = risco de reversão = reduz
    if rsi_val > 78 or rsi_val < 22:
        adj -= 2
        reasons.append(f"RSI extremo ({rsi_val:.0f}) -2x")
    elif rsi_val > 70 or rsi_val < 30:
        adj -= 1
        reasons.append(f"RSI {'sobrecomprado' if rsi_val>70 else 'sobrevendido'} -1x")

    # R:R
    if rr >= 3.0:
        adj += 1
        reasons.append(f"R:R {rr:.1f} +1x")
    elif rr < 2.0:
        adj -= 1
        reasons.append(f"R:R baixo ({rr:.1f}) -1x")

    # Trade type
    if trade_type == "SCALP":
        adj += 2
        reasons.append("SCALP +2x")
    elif trade_type == "SWING":
        adj -= 2
        reasons.append("SWING -2x")

    # V6 estrutural confirmado = bônus
    if any(t in v6_tags for t in ["OB/FVG", "BOS", "GOLDEN-X"]):
        adj += 1
        reasons.append("V6 estrutural +1x")

    # ── Calcula leverage final ────────────────────────────────────────────────
    lev = max(1, min(base_lev + adj, max_safe))

    # Arredonda para múltiplo "round" comum
    if lev <= 3:   lev = max(1, round(lev))
    elif lev <= 7: lev = round(lev / 1) * 1
    else:          lev = round(lev / 5) * 5
    lev = max(1, min(lev, max_safe))

    reason_str = " | ".join(reasons[:4]) if reasons else "Alavancagem padrão"

    return {
        "leverage":    lev,
        "max_safe":    max_safe,
        "tier":        tier,
        "base_lev":    base_lev,
        "adj":         adj,
        "reason":      reason_str,
    }


def calc_engine_margin(
    banca_usdt: float,
    trades_per_session: int,
    max_open_trades: int,
    extra_capital: float = 0.0,
    anti_martingale_mult: float = 1.0,
) -> float:
    """
    PONTO ÚNICO de cálculo de margem por trade (2026-07-13, trava definitiva
    pós-incidente real). TODO motor que abre trade REAL (Autônomo, Pares,
    Grid, e qualquer motor futuro) DEVE chamar esta função — nunca
    reimplementar a conta de sizing.

    Antes existiam 3 fórmulas diferentes:
      - Autônomo: banca_usdt / trades_per_session (a referência correta)
      - Pares:    banca_usdt × 15% por perna (motor próprio, alheio ao painel)
      - Grid:     banca_usdt / GRID_SETTINGS[perfil]["max_concurrent"]
    Cada uma gerava um tamanho de posição diferente do que o usuário configurou
    pensando que "banca + trades por sessão" controlava TUDO. Resultado real:
    trades de $6 quando o usuário pediu $20 (banca $40 / 2 trades).

    Regra (idêntica ao Autônomo, que é a referência definitiva por pedido
    explícito do usuário): margem = (banca_usdt + extra_capital) / n, onde
    n = trades_per_session se > 0, senão max_open_trades como fallback.
    extra_capital é usado só pelo Grid para somar um bônus de reinvestimento
    já existente — nunca uma fórmula alternativa de tamanho.
    anti_martingale_mult (0 < mult <= 1) reduz a margem após sequência de
    perdas — nunca aumenta.
    """
    if banca_usdt <= 0:
        return 0.0
    n = trades_per_session if trades_per_session > 0 else max(int(max_open_trades or 1), 1)
    margin = (banca_usdt + max(extra_capital, 0.0)) / n
    mult = min(max(anti_martingale_mult, 0.0), 1.0)
    return round(margin * mult, 2)


def calculate_position_size(
    balance_usdt: float,
    entry: float,
    stop_loss: float,
    leverage: int,
    risk_pct: float = None,
) -> dict:
    """Returns size in contracts and USDT notional."""
    risk_pct = risk_pct or DEFAULT_RISK_PCT
    risk_usdt = balance_usdt * risk_pct / 100
    sl_distance_pct = abs(entry - stop_loss) / entry * 100
    if sl_distance_pct == 0:
        return {"qty": 0, "notional": 0, "margin": 0}

    # Size so that SL hit = risk_usdt (without multiplying by leverage, since PnL is based on notional size)
    notional = (risk_usdt / sl_distance_pct * 100)
    margin = notional / leverage
    qty = notional / entry

    return {
        "qty": round(qty, 6),
        "notional": round(notional, 2),
        "margin": round(margin, 2),
    }


def create_trade(signal: TradeSignal, balance_usdt: float, risk_pct: float = None) -> ActiveTrade:
    lev = signal.suggested_leverage if getattr(signal, "suggested_leverage", 0) > 0 else get_leverage(signal.asset)
    sizing = calculate_position_size(
        balance_usdt, signal.entry, signal.stop_loss, lev, risk_pct=risk_pct
    )
    return ActiveTrade(
        id=str(uuid.uuid4())[:8],
        asset=signal.asset,
        direction=signal.direction,
        entry_price=signal.entry,
        current_price=signal.entry,
        stop_loss=signal.stop_loss,
        tp1=signal.tp1,
        tp2=signal.tp2,
        tp3=signal.tp3,
        rr=signal.rr,
        leverage=lev,
        size_usdt=sizing["notional"],
        reason=signal.reason,
        confidence=signal.confidence,
        atr=getattr(signal, "atr", 0.0) or 0.0,
    )


def update_pnl(trade: ActiveTrade, current_price: float) -> ActiveTrade:
    trade.current_price = current_price
    if trade.direction == Direction.LONG:
        pnl_pct = (current_price - trade.entry_price) / trade.entry_price * 100 * trade.leverage
    else:
        pnl_pct = (trade.entry_price - current_price) / trade.entry_price * 100 * trade.leverage

    trade.pnl_pct = round(pnl_pct, 2)
    trade.pnl_usdt = round(trade.size_usdt * pnl_pct / 100 / trade.leverage, 2)
    return trade


def check_trailing_stop(trade: ActiveTrade) -> Optional[float]:
    """
    Returns new stop_loss price if a better (profit-protecting) stop is found,
    otherwise None. Combina TRÊS fontes de candidato ao novo stop, sempre
    escolhendo o mais favorável e nunca piorando o stop atual:

      1. Tabela de milestones (TRAILING_MILESTONES) — trava % crescente do lucro.
      2. Trailing por ATR — persegue o preço a ATR_TRAIL_MULT×ATR de distância,
         adaptando à volatilidade (só se o trade tiver ATR gravado, trades novos).
      3. Breakeven ANTECIPADO — assim que o preço percorre EARLY_BREAKEVEN_PROGRESS
         do caminho até o TP1 (antes de bater o TP1), sobe o stop pra breakeven +
         folga de taxa. Protege trades que avançam e revertem antes do TP1.

    Direção-aware: para LONG o "melhor" stop é o MAIOR; para SHORT, o MENOR.
    O guard final só aceita se for estritamente melhor que o stop atual — isso
    também evita re-disparar o mesmo nível repetidamente.
    """
    raw_pnl_pct = trade.pnl_pct / trade.leverage if trade.leverage else 0
    is_long = trade.direction == Direction.LONG
    entry = trade.entry_price

    candidates = []

    # ── 1. Milestones de lucro bruto ──────────────────────────────────────────
    for profit_trigger, stop_lock_pct in TRAILING_MILESTONES:
        if raw_pnl_pct >= profit_trigger:
            if is_long:
                candidates.append(round(entry * (1 + stop_lock_pct / 100), 6))
            else:
                candidates.append(round(entry * (1 - stop_lock_pct / 100), 6))

    # ── 2. Trailing por ATR (adaptativo) ──────────────────────────────────────
    atr_val = getattr(trade, "atr", 0.0) or 0.0
    if atr_val > 0 and raw_pnl_pct >= ATR_TRAIL_MIN_PROFIT_PCT and trade.current_price:
        if is_long:
            candidates.append(round(trade.current_price - atr_val * ATR_TRAIL_MULT, 6))
        else:
            candidates.append(round(trade.current_price + atr_val * ATR_TRAIL_MULT, 6))

    # ── 3. Breakeven antecipado (antes do TP1) ────────────────────────────────
    if not trade.tp1_hit and trade.tp1 and entry:
        dist_tp1 = abs(trade.tp1 - entry)
        if dist_tp1 > 0:
            moved = (trade.current_price - entry) if is_long else (entry - trade.current_price)
            progress = moved / dist_tp1
            if progress >= EARLY_BREAKEVEN_PROGRESS:
                if is_long:
                    candidates.append(round(entry * (1 + EARLY_BREAKEVEN_MARGIN_PCT / 100), 6))
                else:
                    candidates.append(round(entry * (1 - EARLY_BREAKEVEN_MARGIN_PCT / 100), 6))

    if not candidates:
        return None

    best_stop = max(candidates) if is_long else min(candidates)

    # Só atualiza se for estritamente melhor que o stop atual (evita re-disparo).
    if is_long and best_stop <= trade.stop_loss:
        return None
    if not is_long and best_stop >= trade.stop_loss:
        return None

    trade.stop_loss = best_stop
    return best_stop


def check_stop_hit(trade: ActiveTrade) -> bool:
    if trade.direction == Direction.LONG:
        return trade.current_price <= trade.stop_loss
    return trade.current_price >= trade.stop_loss


def check_tp_hit(trade: ActiveTrade) -> Optional[str]:
    """Retorna o proximo TP a ser processado (respeitando tp1_hit/tp2_hit)."""
    price = trade.current_price
    if trade.direction == Direction.LONG:
        if price >= trade.tp3 and trade.tp2_hit:
            return "TP3"
        if price >= trade.tp2 and trade.tp1_hit and not trade.tp2_hit:
            return "TP2"
        if price >= trade.tp1 and not trade.tp1_hit:
            return "TP1"
    else:
        if price <= trade.tp3 and trade.tp2_hit:
            return "TP3"
        if price <= trade.tp2 and trade.tp1_hit and not trade.tp2_hit:
            return "TP2"
        if price <= trade.tp1 and not trade.tp1_hit:
            return "TP1"
    return None


async def process_trade_update(trade: ActiveTrade, current_price: float) -> dict:
    """
    Full trade lifecycle update. Returns action dict for executor.
    """
    trade = update_pnl(trade, current_price)
    actions = []

    # Check stop loss
    if check_stop_hit(trade):
        trade.status = "CLOSED"
        trade.closed_at = datetime.utcnow()
        actions.append({"action": "CLOSE", "reason": "STOP_LOSS"})
        await save_trade(_trade_to_dict(trade))
        return {"trade": trade, "actions": actions}

    # Check trailing stop
    new_stop = check_trailing_stop(trade)
    if new_stop:
        actions.append({"action": "UPDATE_STOP", "new_stop": new_stop})

    scale_map = {int(level): float(pct) for level, pct in SCALE_OUT_MILESTONES}
    tp1_abs = scale_map.get(1, 0.35)
    tp2_abs = scale_map.get(2, 0.35)

    # Scale-Out por TP alinhado ao executor real da Binance.
    tp_hit = check_tp_hit(trade)
    if tp_hit == "TP1":
        pct = tp1_abs
        trade.tp1_hit = True
        actions.append({"action": "PARTIAL_CLOSE", "reason": "TP1", "pct": pct, "original_size": trade.size_usdt})
        trade.size_usdt = round(trade.size_usdt * max(1.0 - pct, 0.0), 2)
        # Move SL para breakeven
        if trade.direction == Direction.LONG:
            trade.stop_loss = max(trade.stop_loss, trade.entry_price)
        else:
            trade.stop_loss = min(trade.stop_loss, trade.entry_price)
    elif tp_hit == "TP2":
        remaining_after_tp1 = max(1.0 - tp1_abs, 0.0001)
        pct = min(tp2_abs / remaining_after_tp1, 1.0)
        trade.tp2_hit = True
        actions.append({"action": "PARTIAL_CLOSE", "reason": "TP2", "pct": pct, "original_size": trade.size_usdt})
        trade.size_usdt = round(trade.size_usdt * max(1.0 - pct, 0.0), 2)
        # Move SL para TP1 (garante lucro)
        if trade.direction == Direction.LONG:
            trade.stop_loss = max(trade.stop_loss, trade.tp1)
        else:
            trade.stop_loss = min(trade.stop_loss, trade.tp1)
    elif tp_hit == "TP3":
        trade.status = "CLOSED"
        trade.closed_at = datetime.utcnow()
        actions.append({"action": "CLOSE", "reason": "TP3"})

    await save_trade(_trade_to_dict(trade))
    return {"trade": trade, "actions": actions}


async def can_open_trade(max_open: int = None) -> bool:
    """Limite de posições simultâneas. max_open vem do perfil ativo (main);
    fallback no MAX_OPEN_TRADES global do config."""
    limit = max_open if max_open is not None else MAX_OPEN_TRADES
    open_trades = await get_open_trades()
    return len(open_trades) < limit
