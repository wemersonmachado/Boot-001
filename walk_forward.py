"""
Walk-Forward Analysis — valida estratégia em janelas deslizantes.
Detecta degradação de performance e regimes favoráveis/desfavoráveis.

Método:
  1. Divide trades fechados em janelas de N dias
  2. Calcula métricas por janela (win rate, PF, Sharpe)
  3. Detecta se performance está degradando nas janelas recentes
  4. Emite alerta se últimas 2 janelas abaixo do mínimo aceitável
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class WindowResult:
    start:      str
    end:        str
    n_trades:   int
    win_rate:   float    # 0-1
    profit_factor: float
    avg_rr:     float
    sharpe:     float
    total_pnl:  float
    status:     str      # "GOOD" | "MARGINAL" | "BAD"


@dataclass
class WalkForwardResult:
    windows:        list[WindowResult]
    overall_status: str              # "STABLE" | "DEGRADING" | "RECOVERING" | "INSUFFICIENT_DATA"
    recent_trend:   str              # "UP" | "DOWN" | "FLAT"
    recommendation: str
    alert_needed:   bool


# ── Config ─────────────────────────────────────────────────────────────────────
WINDOW_DAYS       = 14     # tamanho de cada janela em dias
MIN_TRADES_WINDOW = 5      # janela ignorada se < 5 trades
MIN_WIN_RATE      = 0.45   # abaixo disso = BAD
MIN_PF            = 1.0    # profit factor mínimo aceitável
MIN_SHARPE        = 0.3


# ── Métricas por janela ────────────────────────────────────────────────────────

def _calc_window(trades: list[dict]) -> Optional[WindowResult]:
    if len(trades) < MIN_TRADES_WINDOW:
        return None

    pnls     = [float(t.get("pnl_usdt", 0) or 0) for t in trades]
    pnl_pcts = [float(t.get("pnl_pct",  0) or 0) for t in trades]
    wins     = [p for p in pnls if p > 0]
    losses   = [p for p in pnls if p < 0]

    win_rate = len(wins) / len(pnls)
    avg_win  = sum(wins)    / len(wins)   if wins   else 0
    avg_loss = sum(losses)  / len(losses) if losses else -0.01
    pf       = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses else 9.99
    pf       = min(pf, 9.99)

    # Sharpe simplificado
    import statistics
    mean_r = statistics.mean(pnl_pcts)
    std_r  = statistics.stdev(pnl_pcts) if len(pnl_pcts) > 1 else 1e-9
    sharpe = mean_r / std_r * math.sqrt(252) if std_r > 0 else 0

    rrs = [float(t.get("realized_rr", 0) or 0) for t in trades]
    avg_rr = sum(rrs) / len(rrs) if rrs else 0

    # Classifica
    if win_rate >= MIN_WIN_RATE and pf >= MIN_PF and sharpe >= MIN_SHARPE:
        status = "GOOD"
    elif win_rate >= 0.38 or pf >= 0.85:
        status = "MARGINAL"
    else:
        status = "BAD"

    ts_list = sorted([t.get("opened_at", "") or t.get("closed_at", "") for t in trades])

    return WindowResult(
        start         = ts_list[0][:10] if ts_list else "",
        end           = ts_list[-1][:10] if ts_list else "",
        n_trades      = len(trades),
        win_rate      = round(win_rate, 3),
        profit_factor = round(pf, 2),
        avg_rr        = round(avg_rr, 2),
        sharpe        = round(sharpe, 2),
        total_pnl     = round(sum(pnls), 2),
        status        = status,
    )


# ── Walk-forward principal ─────────────────────────────────────────────────────

def run(closed_trades: list[dict], window_days: int = WINDOW_DAYS) -> WalkForwardResult:
    """
    Executa walk-forward sobre lista de trades fechados.
    closed_trades: lista de dicts com pnl_usdt, pnl_pct, opened_at/closed_at.
    """
    if len(closed_trades) < MIN_TRADES_WINDOW * 2:
        return WalkForwardResult(
            windows        = [],
            overall_status = "INSUFFICIENT_DATA",
            recent_trend   = "FLAT",
            recommendation = f"Mínimo de {MIN_TRADES_WINDOW * 2} trades necessários para análise.",
            alert_needed   = False,
        )

    # Ordena por data
    def _parse_dt(t: dict) -> datetime:
        for key in ("closed_at", "opened_at"):
            val = t.get(key, "")
            if val:
                try:
                    return datetime.fromisoformat(str(val)[:19])
                except Exception:
                    pass
        return datetime.utcnow()

    trades_sorted = sorted(closed_trades, key=_parse_dt)
    start_dt      = _parse_dt(trades_sorted[0])
    end_dt        = _parse_dt(trades_sorted[-1])

    windows_raw: list[WindowResult] = []
    cur = start_dt
    while cur < end_dt:
        next_dt = cur + timedelta(days=window_days)
        window_trades = [
            t for t in trades_sorted
            if cur <= _parse_dt(t) < next_dt
        ]
        result = _calc_window(window_trades)
        if result:
            windows_raw.append(result)
        cur = next_dt

    if not windows_raw:
        return WalkForwardResult(
            windows        = [],
            overall_status = "INSUFFICIENT_DATA",
            recent_trend   = "FLAT",
            recommendation = "Trades insuficientes por janela.",
            alert_needed   = False,
        )

    # Analisa tendência recente (últimas 3 janelas)
    recent = windows_raw[-3:]
    statuses = [w.status for w in recent]
    bad_count = statuses.count("BAD")

    pfs = [w.profit_factor for w in recent]
    if len(pfs) >= 2:
        trend = "UP" if pfs[-1] > pfs[0] * 1.1 else ("DOWN" if pfs[-1] < pfs[0] * 0.9 else "FLAT")
    else:
        trend = "FLAT"

    if bad_count >= 2:
        overall = "DEGRADING"
        alert   = True
        rec     = "⚠️ Performance degradando nas últimas janelas. Revise parâmetros ou reduza tamanho de posição."
    elif bad_count == 1 and trend == "DOWN":
        overall = "DEGRADING"
        alert   = True
        rec     = "Performance em queda. Monitore de perto — próxima janela definirá padrão."
    elif statuses.count("GOOD") >= 2:
        overall = "STABLE"
        alert   = False
        rec     = "Estratégia estável nas últimas janelas."
    else:
        overall = "RECOVERING" if trend == "UP" else "STABLE"
        alert   = False
        rec     = "Performance marginal mas estável."

    return WalkForwardResult(
        windows        = windows_raw,
        overall_status = overall,
        recent_trend   = trend,
        recommendation = rec,
        alert_needed   = alert,
    )


def to_dict(result: WalkForwardResult) -> dict:
    return {
        "overall_status":  result.overall_status,
        "recent_trend":    result.recent_trend,
        "recommendation":  result.recommendation,
        "alert_needed":    result.alert_needed,
        "n_windows":       len(result.windows),
        "windows": [
            {
                "start":         w.start,
                "end":           w.end,
                "n_trades":      w.n_trades,
                "win_rate_pct":  round(w.win_rate * 100, 1),
                "profit_factor": w.profit_factor,
                "avg_rr":        w.avg_rr,
                "sharpe":        w.sharpe,
                "total_pnl":     w.total_pnl,
                "status":        w.status,
            }
            for w in result.windows
        ]
    }
