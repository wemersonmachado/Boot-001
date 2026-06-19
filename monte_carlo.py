"""
Monte Carlo Simulation — validação estatística de backtest (estilo Jesse)
Simula N variações da sequência de trades com ruído aleatório para medir robustez.
Retorna distribuições de ROI, max drawdown, Sharpe e probabilidade de lucro.
"""
import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class MonteCarloResult:
    n_simulations:   int
    n_trades:        int

    # ROI
    roi_mean:        float
    roi_median:      float
    roi_p5:          float    # pior 5%
    roi_p95:         float    # melhor 5%

    # Drawdown
    max_dd_mean:     float
    max_dd_p95:      float    # 95% das sims ficaram abaixo desse DD

    # Métricas de risco
    sharpe_mean:     float
    sortino_mean:    float
    prob_profit:     float    # % das sims que terminaram no positivo
    robustness_score: float   # 0-10 (índice composto)

    # Raw arrays (para plotar se quiser)
    roi_array:       list
    max_dd_array:    list


def _equity_curve(returns: np.ndarray, initial: float = 1.0) -> np.ndarray:
    """Converte série de retornos percentuais em curva de capital."""
    equity = [initial]
    for r in returns:
        equity.append(equity[-1] * (1 + r / 100))
    return np.array(equity)


def _max_drawdown(equity: np.ndarray) -> float:
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe(returns: np.ndarray, risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    excess = returns - risk_free
    std    = np.std(excess)
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * np.sqrt(252))


def _sortino(returns: np.ndarray, risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    excess    = returns - risk_free
    downside  = excess[excess < 0]
    down_std  = np.std(downside) if len(downside) > 0 else 1e-9
    return float(np.mean(excess) / down_std * np.sqrt(252))


def run(
    trade_returns: list[float],
    n_simulations: int = 1000,
    noise_std:     float = 0.5,    # desvio padrão do ruído aditivo (em %)
    resample:      bool  = True,   # True = reordena aleatoriamente + ruído
    initial_capital: float = 100.0,
) -> Optional[MonteCarloResult]:
    """
    Executa simulação Monte Carlo.

    Args:
        trade_returns: lista de retornos por trade em % (ex: [2.3, -1.1, 4.5, ...])
        n_simulations: número de simulações (padrão 1000)
        noise_std: desvio padrão do ruído gaussiano adicionado a cada retorno (%)
        resample: se True, embaralha a ordem dos trades em cada simulação
        initial_capital: capital inicial para cálculo da curva de capital

    Returns:
        MonteCarloResult com distribuições completas
    """
    returns = np.array(trade_returns, dtype=float)
    n = len(returns)
    if n < 5:
        return None

    roi_list    = []
    max_dd_list = []
    sharpe_list = []
    sortino_list= []

    rng = np.random.default_rng(42)

    for _ in range(n_simulations):
        # Reordena e adiciona ruído
        sim_returns = returns.copy()
        if resample:
            rng.shuffle(sim_returns)
        noise = rng.normal(0, noise_std, size=n)
        sim_returns = sim_returns + noise

        eq    = _equity_curve(sim_returns, initial_capital)
        roi   = (eq[-1] / initial_capital - 1) * 100
        dd    = _max_drawdown(eq)
        sh    = _sharpe(sim_returns)
        so    = _sortino(sim_returns)

        roi_list.append(roi)
        max_dd_list.append(dd)
        sharpe_list.append(sh)
        sortino_list.append(so)

    roi_arr    = np.array(roi_list)
    dd_arr     = np.array(max_dd_list)
    sh_arr     = np.array(sharpe_list)
    so_arr     = np.array(sortino_list)

    prob_profit = float((roi_arr > 0).mean() * 100)
    roi_mean    = float(np.mean(roi_arr))
    roi_median  = float(np.median(roi_arr))
    roi_p5      = float(np.percentile(roi_arr, 5))
    roi_p95     = float(np.percentile(roi_arr, 95))
    max_dd_mean = float(np.mean(dd_arr))
    max_dd_p95  = float(np.percentile(dd_arr, 95))
    sharpe_mean = float(np.mean(sh_arr))
    sortino_mean= float(np.mean(so_arr))

    # Robustness Score (0–10)
    # Combinação de: prob_profit, Sharpe, variância do ROI e max_dd aceitável
    s_profit  = min(10, prob_profit / 10)
    s_sharpe  = min(10, max(0, sharpe_mean * 3))
    s_dd      = max(0, 10 - max_dd_p95 / 5)
    s_consist = max(0, 10 - (roi_p95 - roi_p5) / 10)  # quanto menor a variância, melhor
    robustness = round((s_profit * 0.35 + s_sharpe * 0.30 + s_dd * 0.20 + s_consist * 0.15), 2)

    return MonteCarloResult(
        n_simulations    = n_simulations,
        n_trades         = n,
        roi_mean         = round(roi_mean, 2),
        roi_median       = round(roi_median, 2),
        roi_p5           = round(roi_p5, 2),
        roi_p95          = round(roi_p95, 2),
        max_dd_mean      = round(max_dd_mean, 2),
        max_dd_p95       = round(max_dd_p95, 2),
        sharpe_mean      = round(sharpe_mean, 3),
        sortino_mean     = round(sortino_mean, 3),
        prob_profit      = round(prob_profit, 1),
        robustness_score = robustness,
        roi_array        = [round(x, 2) for x in roi_list[:200]],  # primeiras 200 para UI
        max_dd_array     = [round(x, 2) for x in max_dd_list[:200]],
    )


def interpret(result: MonteCarloResult) -> dict:
    """Traduz o resultado em linguagem humana."""
    r = result
    verdict = "APROVADA" if r.robustness_score >= 6 else \
              "MARGINAL"  if r.robustness_score >= 4 else "REPROVADA"

    lines = []
    lines.append(f"Estratégia {verdict} (robustez {r.robustness_score:.1f}/10)")
    lines.append(f"{r.prob_profit:.0f}% das simulações terminaram no lucro")
    lines.append(f"ROI esperado: {r.roi_median:+.1f}% (P5={r.roi_p5:+.1f}% / P95={r.roi_p95:+.1f}%)")
    lines.append(f"Max Drawdown médio: {r.max_dd_mean:.1f}% | pior cenário (P95): {r.max_dd_p95:.1f}%")
    lines.append(f"Sharpe médio: {r.sharpe_mean:.2f} | Sortino médio: {r.sortino_mean:.2f}")

    if r.sharpe_mean < 0.5:
        lines.append("AVISO: Sharpe baixo — retorno insuficiente para o risco assumido")
    if r.max_dd_p95 > 30:
        lines.append("AVISO: Em 5% dos cenários o drawdown ultrapassa 30%")
    if r.prob_profit < 55:
        lines.append("AVISO: Menos de 55% de chance de lucro — estratégia fraca")

    return {
        "verdict":    verdict,
        "score":      r.robustness_score,
        "lines":      lines,
    }
