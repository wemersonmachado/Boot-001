"""
Metrics Engine — métricas obrigatórias de avaliação sobre signal_outcomes.

Lê direto do banco (sem dependência de main.py) para poder ser usado em
relatórios, no dashboard ou via CLI (`python metrics_engine.py`).

Métricas: Profit Factor, Win Rate, Expectancy, Payoff, Drawdown máximo,
Sharpe simplificado, nº de trades, RR médio, sequência máxima de perdas,
consistência por timeframe / ativo / horário / dia da semana.
"""
import asyncio
import math
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

from config import DB_PATH

WEEKDAY_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


async def _fetch_outcomes(since_hours: Optional[float] = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = """SELECT asset, direction, timeframe, entry, exit_price, pnl_pct,
                      outcome, hour_utc, weekday, recorded_at
               FROM signal_outcomes"""
        params = ()
        if since_hours is not None:
            cutoff = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()
            q += " WHERE recorded_at >= ?"
            params = (cutoff,)
        cur = await db.execute(q, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _core_metrics(rows: list[dict]) -> dict:
    decided = [r for r in rows if r["outcome"] in ("WIN", "LOSS")]
    wins = [r for r in decided if r["outcome"] == "WIN"]
    losses = [r for r in decided if r["outcome"] == "LOSS"]
    timeouts = [r for r in rows if r["outcome"] == "TIMEOUT"]

    n_decided = len(decided)
    win_rate = (len(wins) / n_decided * 100) if n_decided else 0.0

    gross_win = sum(r["pnl_pct"] for r in wins)
    gross_loss = sum(r["pnl_pct"] for r in losses)  # negativo
    profit_factor = (gross_win / abs(gross_loss)) if gross_loss < 0 else (math.inf if gross_win > 0 else 0.0)

    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0  # negativo
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else math.inf

    p_win = win_rate / 100
    expectancy = p_win * avg_win + (1 - p_win) * avg_loss  # em % por sinal

    # Sharpe simplificado: média/desvio do pnl_pct por sinal decidido (sem free-rate, sem anualizar)
    pnls = [r["pnl_pct"] for r in decided]
    if len(pnls) > 1:
        mean_pnl = sum(pnls) / len(pnls)
        var = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = math.sqrt(var)
        sharpe = (mean_pnl / std_pnl) if std_pnl > 0 else 0.0
    else:
        sharpe = 0.0

    # Drawdown máximo sobre a curva de pnl acumulado (ordem cronológica)
    ordered = sorted(decided, key=lambda r: r["recorded_at"] or "")
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in ordered:
        equity += r["pnl_pct"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    # Sequência máxima de perdas consecutivas
    max_loss_streak = 0
    cur_streak = 0
    for r in ordered:
        if r["outcome"] == "LOSS":
            cur_streak += 1
            max_loss_streak = max(max_loss_streak, cur_streak)
        else:
            cur_streak = 0

    return {
        "n_total": len(rows),
        "n_decided": n_decided,
        "n_win": len(wins),
        "n_loss": len(losses),
        "n_timeout": len(timeouts),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else None,
        "expectancy_pct_per_signal": round(expectancy, 4),
        "payoff": round(payoff, 3) if math.isfinite(payoff) else None,
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "sharpe_simplified": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "max_consecutive_losses": max_loss_streak,
        "net_pnl_pct_sum": round(sum(r["pnl_pct"] for r in rows), 3),
        "timeout_rate_pct": round(len(timeouts) / len(rows) * 100, 2) if rows else 0.0,
    }


def _group_by(rows: list[dict], key_fn) -> dict:
    groups: dict = {}
    for r in rows:
        k = key_fn(r)
        groups.setdefault(k, []).append(r)
    return {k: _core_metrics(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


async def full_report(since_hours: Optional[float] = None) -> dict:
    rows = await _fetch_outcomes(since_hours)
    if not rows:
        return {"error": "sem dados no período", "since_hours": since_hours}

    by_tf = _group_by(rows, lambda r: r["timeframe"] or "?")
    by_asset = _group_by(rows, lambda r: r["asset"])
    by_hour = _group_by(rows, lambda r: r["hour_utc"] if r["hour_utc"] is not None else -1)
    by_weekday = _group_by(
        rows,
        lambda r: WEEKDAY_PT[r["weekday"]] if r["weekday"] is not None and 0 <= r["weekday"] <= 6 else "?",
    )

    return {
        "since_hours": since_hours,
        "overall": _core_metrics(rows),
        "by_timeframe": by_tf,
        "by_asset": by_asset,
        "by_hour_utc": by_hour,
        "by_weekday": by_weekday,
    }


def _print_report(rep: dict, title: str):
    print(f"\n=== {title} ===")
    if "error" in rep:
        print(rep["error"])
        return
    o = rep["overall"]
    print(f"n={o['n_total']} (decididos={o['n_decided']}, timeout={o['n_timeout']}, taxa timeout={o['timeout_rate_pct']}%)")
    print(f"Win Rate: {o['win_rate_pct']}%  |  Profit Factor: {o['profit_factor']}  |  Payoff: {o['payoff']}")
    print(f"Expectancy/sinal: {o['expectancy_pct_per_signal']}%  |  Sharpe: {o['sharpe_simplified']}")
    print(f"Max Drawdown: {o['max_drawdown_pct']}%  |  Max losses seguidas: {o['max_consecutive_losses']}")
    print(f"PnL acumulado: {o['net_pnl_pct_sum']}%")

    print("\n-- Por timeframe --")
    for tf, m in rep["by_timeframe"].items():
        print(f"  {tf:6s} n={m['n_decided']:4d} WR={m['win_rate_pct']:6.1f}% PF={m['profit_factor']}")

    print("\n-- Por ativo (top 10 por volume) --")
    top = sorted(rep["by_asset"].items(), key=lambda kv: kv[1]["n_total"], reverse=True)[:10]
    for asset, m in top:
        print(f"  {asset:12s} n={m['n_decided']:4d} WR={m['win_rate_pct']:6.1f}% PF={m['profit_factor']}")

    print("\n-- Por dia da semana --")
    for wd, m in rep["by_weekday"].items():
        print(f"  {wd:8s} n={m['n_decided']:4d} WR={m['win_rate_pct']:6.1f}% PF={m['profit_factor']}")


async def _main():
    rep_all = await full_report(None)
    _print_report(rep_all, "Histórico completo")
    rep_48h = await full_report(48)
    _print_report(rep_48h, "Últimas 48h")


if __name__ == "__main__":
    asyncio.run(_main())
