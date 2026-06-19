"""
Correlation Engine — matriz de correlação DINÂMICA entre ativos.

Substitui os grupos estáticos (CORRELATION_GROUPS) por correlação real calculada
a partir dos retornos de 1h dos últimos N candles. Evita abrir posições na mesma
direção em ativos que estão se movendo juntos (risco de portfólio concentrado).

Cache de 30 min — recalcula em background, leitura instantânea pelo auto-trade.
"""
import time
import asyncio
import numpy as np
import pandas as pd
from typing import Optional

# Cache: {(asset_a, asset_b): correlation}  +  timestamp do último cálculo
_corr_matrix: dict = {}
_last_calc: float = 0.0
_CALC_INTERVAL = 1800   # recalcula a cada 30 min
_CORR_TF = "1h"
_CORR_LOOKBACK = 100    # 100 velas de 1h ≈ 4 dias
HIGH_CORR_THRESHOLD = 0.75   # acima disso = "muito correlacionado"


async def refresh_correlation_matrix(assets: list) -> dict:
    """
    Calcula a matriz de correlação dos retornos de 1h dos ativos fornecidos.
    Chamado em background pelo scheduler. Não bloqueia o auto-trade.
    """
    global _corr_matrix, _last_calc
    from klines_cache import get_klines_cached as get_klines

    returns: dict = {}
    for asset in assets:
        try:
            df = await get_klines(asset, _CORR_TF, _CORR_LOOKBACK)
            if df is not None and len(df) >= 30:
                returns[asset] = df["close"].pct_change().dropna().reset_index(drop=True)
        except Exception:
            pass

    new_matrix: dict = {}
    asset_list = list(returns.keys())
    for i, a in enumerate(asset_list):
        for b in asset_list[i + 1:]:
            ra, rb = returns[a], returns[b]
            n = min(len(ra), len(rb))
            if n < 30:
                continue
            try:
                corr = float(np.corrcoef(ra.iloc[-n:], rb.iloc[-n:])[0, 1])
                if not np.isnan(corr):
                    new_matrix[(a, b)] = round(corr, 3)
                    new_matrix[(b, a)] = round(corr, 3)
            except Exception:
                pass

    _corr_matrix = new_matrix
    _last_calc = time.time()
    print(f"[CORR] Matriz atualizada — {len(asset_list)} ativos, {len(new_matrix)//2} pares")
    return _corr_matrix


def get_correlation(asset_a: str, asset_b: str) -> Optional[float]:
    """Correlação entre dois ativos (None se não calculada)."""
    if asset_a == asset_b:
        return 1.0
    return _corr_matrix.get((asset_a, asset_b))


def is_highly_correlated(asset: str, open_assets, direction: str,
                         open_trades: list = None,
                         threshold: float = HIGH_CORR_THRESHOLD) -> tuple[bool, str]:
    """
    Retorna (True, motivo) se 'asset' está altamente correlacionado (>threshold) a
    algum ativo JÁ ABERTO na MESMA direção. Usado como gate dinâmico no auto-trade.

    Funciona como complemento aos CORRELATION_GROUPS estáticos: pega correlações
    que surgem dinamicamente (ex: duas alts de IA andando juntas numa semana).
    """
    if not open_trades:
        return False, ""
    dir_up = direction.upper()
    for t in open_trades:
        other = t.get("asset", "")
        if other == asset:
            continue
        t_dir = str(t.get("direction", "")).upper()
        same_dir = ("LONG" in dir_up and "LONG" in t_dir) or ("SHORT" in dir_up and "SHORT" in t_dir)
        if not same_dir:
            continue
        corr = get_correlation(asset, other)
        if corr is not None and corr >= threshold:
            return True, f"{asset}~{other} corr={corr:.2f} (mesma direção {dir_up})"
    return False, ""


def get_correlation_status() -> dict:
    """Resumo para dashboard/diagnóstico."""
    pairs = [(k[0], k[1], v) for k, v in _corr_matrix.items()
             if k[0] < k[1]]   # dedup
    pairs.sort(key=lambda x: -abs(x[2]))
    return {
        "last_calc_ago_s": round(time.time() - _last_calc, 0) if _last_calc else None,
        "pairs_cached": len(pairs),
        "top_correlated": [
            {"a": a, "b": b, "corr": c} for a, b, c in pairs[:8]
        ],
        "threshold": HIGH_CORR_THRESHOLD,
    }
