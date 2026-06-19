"""
Portfolio Risk Manager — VaR e correlação entre posições abertas.

Funcionalidades:
  - VaR paramétrico (95%) do portfólio consolidado
  - Bloqueio de nova entrada se VaR excede limite
  - Detecção de correlação alta entre ativos abertos (evita duplicar risco)
  - Concentração máxima por ativo / direção
"""
import math
from typing import Optional

import numpy as np

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_PORTFOLIO_VAR_PCT  = 5.0   # VaR 95% máximo do portfólio (% da banca)
MAX_CORR_SAME_GROUP    = 0.80  # correlação máxima permitida entre ativos abertos
MAX_SAME_DIRECTION_PCT = 0.60  # máx 60% da banca na mesma direção
MAX_SINGLE_ASSET_PCT   = 0.25  # máx 25% da banca em um único ativo

# Grupos de correlação histórica alta (BTC move → alts movem junto)
CORRELATION_GROUPS: dict[str, list[str]] = {
    "BTC":  ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"],
    "ETH":  ["ETHUSDT", "AVAXUSDT", "MATICUSDT", "LINKUSDT", "AAVEUSDT"],
    "DEFI": ["AAVEUSDT", "UNIUSDT", "SUSHIUSDT", "CRVUSDT"],
    "L1":   ["SOLUSDT", "AVAXUSDT", "NEARUSDT", "APTUSDT"],
    "MEME": ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "FLOKIUSDT"],
}


def _get_group(asset: str) -> Optional[str]:
    for group, members in CORRELATION_GROUPS.items():
        if asset.upper() in members:
            return group
    return None


# ── VaR paramétrico simplificado ──────────────────────────────────────────────

def _var_95(size_usdt: float, atr_pct: float) -> float:
    """
    VaR 95% de uma posição: tamanho × (1.645 × σ diária).
    Usa ATR% como proxy de σ diária.
    """
    sigma = atr_pct / 100
    return size_usdt * 1.645 * sigma


def portfolio_var(open_trades: list[dict], banca_usdt: float) -> dict:
    """
    Calcula VaR 95% do portfólio.
    open_trades: lista de dicts com 'asset', 'notional_usdt', 'atr_pct' (opcional)
    Retorna dict com var_usdt, var_pct, positions_detail.
    """
    if not open_trades or banca_usdt <= 0:
        return {"var_usdt": 0.0, "var_pct": 0.0, "n_positions": 0}

    vars_usdt = []
    for t in open_trades:
        notional = float(t.get("notional_usdt", t.get("margin_usdt", 10)) or 10)
        atr_pct  = float(t.get("atr_pct", 2.0) or 2.0)   # padrão 2% se não informado
        vars_usdt.append(_var_95(notional, atr_pct))

    # Soma com correlação parcial: √(Σ VaR²) assume correlação média de 0.5
    # Mais conservador que soma simples mas mais realista que independência total
    var_total = math.sqrt(sum(v * v for v in vars_usdt)) * 1.4  # fator de correlação
    var_pct   = var_total / banca_usdt * 100

    return {
        "var_usdt":    round(var_total, 2),
        "var_pct":     round(var_pct, 2),
        "n_positions": len(open_trades),
        "limit_pct":   MAX_PORTFOLIO_VAR_PCT,
        "within_limit": var_pct <= MAX_PORTFOLIO_VAR_PCT,
    }


# ── Verificação de nova entrada ────────────────────────────────────────────────

def _margin_of(trade: dict, leverage: float) -> float:
    """Margem (capital comprometido) de um trade. Usa margin_usdt se houver;
    senão deriva do notional alavancado dividindo pela alavancagem."""
    m = float(trade.get("margin_usdt", 0) or 0)
    if m > 0:
        return m
    return float(trade.get("notional_usdt", 0) or 0) / max(1.0, leverage)


def can_open_position(
    new_asset: str,
    new_direction: str,
    new_notional: float,
    new_atr_pct: float,
    open_trades: list[dict],
    banca_usdt: float,
    leverage: float = 1.0,
    max_concurrent: int = 0,
) -> tuple[bool, str]:
    """
    Verifica se abrir nova posição é seguro do ponto de vista do portfólio.
    Retorna (permitido: bool, motivo: str).

    IMPORTANTE: concentração por ativo/direção é medida em MARGEM (capital
    efetivamente comprometido = notional / alavancagem), não no notional
    alavancado. Comparar notional alavancado contra a banca quebrava o filtro:
    qualquer posição com alavancagem > 1 já estouraria 25% da banca, bloqueando
    100% das entradas (ex.: lev 5x + 3 trades ⇒ 166% sempre). VaR (item 4)
    continua em notional, pois o risco de mercado incide sobre o notional.
    """
    if banca_usdt <= 0:
        return True, "ok"

    lev = max(1.0, float(leverage or 1.0))
    new_margin = new_notional / lev

    # Cap por ativo adaptativo: se o usuário pretende N trades simultâneos, cada
    # um pode legitimamente usar ~1/N do capital. Sem isso, um cap fixo de 25%
    # tornaria inviável qualquer config com ≤4 trades.
    asset_cap = MAX_SINGLE_ASSET_PCT
    if max_concurrent and max_concurrent > 0:
        asset_cap = max(asset_cap, 1.0 / max_concurrent + 0.10)  # +10pp de folga

    # 1. Concentração por ativo (em margem)
    asset_margin = sum(
        _margin_of(t, lev)
        for t in open_trades
        if t.get("asset", "").upper() == new_asset.upper()
    ) + new_margin
    if asset_margin / banca_usdt > asset_cap:
        return False, f"Concentração por ativo: {asset_margin/banca_usdt*100:.1f}% (margem) > {asset_cap*100:.0f}%"

    # 2. Concentração por direção (em margem)
    dir_margin = sum(
        _margin_of(t, lev)
        for t in open_trades
        if t.get("direction", "").upper() == new_direction.upper()
    ) + new_margin
    if dir_margin / banca_usdt > MAX_SAME_DIRECTION_PCT:
        return False, f"Exposição {new_direction}: {dir_margin/banca_usdt*100:.1f}% (margem) > {MAX_SAME_DIRECTION_PCT*100:.0f}%"

    # 3. Correlação de grupo
    new_group = _get_group(new_asset)
    if new_group:
        group_members = CORRELATION_GROUPS[new_group]
        correlated_open = [
            t for t in open_trades
            if t.get("asset", "").upper() in group_members
            and t.get("direction", "").upper() == new_direction.upper()
        ]
        if len(correlated_open) >= 2:
            return False, f"Já há {len(correlated_open)} posições no grupo {new_group} na mesma direção"

    # 4. VaR pós-adição
    simulated_trades = list(open_trades) + [{
        "asset": new_asset,
        "notional_usdt": new_notional,
        "atr_pct": new_atr_pct,
    }]
    var_result = portfolio_var(simulated_trades, banca_usdt)
    if not var_result["within_limit"]:
        return False, f"VaR portfólio seria {var_result['var_pct']:.1f}% > limite {MAX_PORTFOLIO_VAR_PCT:.0f}%"

    return True, "ok"


def get_portfolio_summary(open_trades: list[dict], banca_usdt: float) -> dict:
    """Retorna resumo de risco do portfólio atual."""
    var = portfolio_var(open_trades, banca_usdt)

    long_exp  = sum(float(t.get("notional_usdt", 0)) for t in open_trades if t.get("direction", "") == "LONG")
    short_exp = sum(float(t.get("notional_usdt", 0)) for t in open_trades if t.get("direction", "") == "SHORT")
    total_exp = long_exp + short_exp
    net_exp   = long_exp - short_exp   # positivo = net long

    groups_open: dict[str, int] = {}
    for t in open_trades:
        g = _get_group(t.get("asset", ""))
        if g:
            groups_open[g] = groups_open.get(g, 0) + 1

    return {
        **var,
        "long_exposure_usdt":  round(long_exp, 2),
        "short_exposure_usdt": round(short_exp, 2),
        "net_exposure_usdt":   round(net_exp, 2),
        "total_exposure_usdt": round(total_exp, 2),
        "correlation_groups":  groups_open,
        "limits": {
            "max_var_pct":           MAX_PORTFOLIO_VAR_PCT,
            "max_same_direction_pct": MAX_SAME_DIRECTION_PCT * 100,
            "max_single_asset_pct":   MAX_SINGLE_ASSET_PCT * 100,
        }
    }
