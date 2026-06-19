"""
Asset Memory — Memória de performance por ativo.

Rastreia WR, PnL e sequências de ganho/perda por ativo.
Ajusta SCORE_THRESH e tamanho de posição automaticamente.

Regras:
  WR >= 60% (últimos 8 trades) → bonus +3pts, size 1.1×
  WR 40–60%                    → neutro
  WR < 40%  (5+ trades)        → penaliza -5pts, score mínimo +1, size 0.8×
  WR < 25%  (5+ trades)        → pausa 24h no ativo
"""
import json
import os
import time
from collections import deque
from typing import Optional

_MEMORY_FILE = os.path.join(os.path.dirname(__file__), "asset_memory.json")
_WINDOW       = 8     # últimos N trades para calcular WR
_MIN_TRADES   = 5     # mínimo de trades antes de aplicar penalidade
_PAUSE_HOURS  = 24

# {symbol: {"trades": deque[{pnl, ts}], "paused_until": float}}
_memory: dict = {}


def _load():
    global _memory
    try:
        if os.path.exists(_MEMORY_FILE):
            with open(_MEMORY_FILE, "r") as f:
                raw = json.load(f)
            for sym, data in raw.items():
                _memory[sym] = {
                    "trades":       deque(data.get("trades", []), maxlen=_WINDOW),
                    "paused_until": data.get("paused_until", 0.0),
                }
    except Exception as e:
        print(f"[ASSET_MEM] Erro ao carregar: {e}")


def _save():
    try:
        out = {}
        for sym, data in _memory.items():
            out[sym] = {
                "trades":       list(data["trades"]),
                "paused_until": data.get("paused_until", 0.0),
            }
        with open(_MEMORY_FILE, "w") as f:
            json.dump(out, f, indent=2)
    except Exception as e:
        print(f"[ASSET_MEM] Erro ao salvar: {e}")


def _ensure(symbol: str):
    sym = symbol.upper()
    if sym not in _memory:
        _memory[sym] = {"trades": deque(maxlen=_WINDOW), "paused_until": 0.0}
    return sym


def record_trade(symbol: str, pnl_usdt: float):
    """Registra resultado de um trade fechado."""
    sym = _ensure(symbol)
    _memory[sym]["trades"].append({"pnl": pnl_usdt, "ts": time.time()})

    # Verifica se deve pausar
    trades = list(_memory[sym]["trades"])
    if len(trades) >= _MIN_TRADES:
        wins = sum(1 for t in trades if t["pnl"] > 0)
        wr   = wins / len(trades)
        if wr < 0.25:
            _memory[sym]["paused_until"] = time.time() + _PAUSE_HOURS * 3600
            print(f"[ASSET_MEM] {sym} pausado 24h (WR {wr*100:.0f}% — abaixo de 25%)")

    _save()


def get_stats(symbol: str) -> dict:
    """Retorna estatísticas atuais do ativo."""
    sym    = _ensure(symbol)
    trades = list(_memory[sym]["trades"])
    paused = _memory[sym].get("paused_until", 0.0)
    now    = time.time()

    if paused > now:
        return {
            "symbol":         sym,
            "paused":         True,
            "paused_minutes": round((paused - now) / 60),
            "n_trades":       len(trades),
            "win_rate":       0.0,
            "score_adj":      -99,   # bloqueia sinal
            "size_mult":      0.0,
            "score_thresh_delta": 0,
        }

    if len(trades) == 0:
        return _default(sym)

    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr   = wins / len(trades)
    n    = len(trades)

    if wr >= 0.60:
        score_adj, size_mult, thresh_delta = +3.0, 1.1, 0
    elif wr >= 0.40 or n < _MIN_TRADES:
        score_adj, size_mult, thresh_delta =  0.0, 1.0, 0
    else:
        score_adj, size_mult, thresh_delta = -5.0, 0.8, +1

    return {
        "symbol":             sym,
        "paused":             False,
        "paused_minutes":     0,
        "n_trades":           n,
        "win_rate":           round(wr * 100, 1),
        "score_adj":          score_adj,
        "size_mult":          size_mult,
        "score_thresh_delta": thresh_delta,
    }


def is_paused(symbol: str) -> bool:
    sym = _ensure(symbol)
    return _memory[sym].get("paused_until", 0.0) > time.time()


def get_all_stats() -> dict:
    return {sym: get_stats(sym) for sym in _memory}


def _default(sym: str) -> dict:
    return {
        "symbol": sym, "paused": False, "paused_minutes": 0,
        "n_trades": 0, "win_rate": 0.0,
        "score_adj": 0.0, "size_mult": 1.0, "score_thresh_delta": 0,
    }


_load()
