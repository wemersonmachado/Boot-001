#!/usr/bin/env python3
"""
MICRO-CAP BACKTEST V7  -  V6 + Fibonacci Confluence | Multi-TF 3m/5m/15m
VERSAO OTIMIZADA: numpy vetorizado, cache em disco, computacao compartilhada.

GRUPOS:
  A = V6 puro (OB/FVG + Score numerico, baseline)
  B = V6 + requer zona Fibonacci (0.382/0.500/0.618)
  C = Fibonacci isolado - apenas retracao + confirmacao, sem OB/FVG

WATCHLIST: Top 15 Binance Futures 24h (volume + trades + valorizacao)
  #1 BTCUSDT  #2 ETHUSDT  #3 SOLUSDT  #4 BEATUSDT  #5 STGUSDT
  #6 SOXLUSDT #7 MUUSDT   #8 WLDUSDT  #9 AIOUSDT   #10 MRVLUSDT
  #11 CLUSDT  #12 ALLOUSDT #13 SUIUSDT #14 DOGEUSDT #15 LUMIAUSDT
"""

import asyncio
import bisect
import json
import time
import numpy as np
import aiohttp
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

BASE      = Path(__file__).parent
CACHE_DIR = BASE / ".klines_cache"

# ── Watchlist ───────────────────────────────────────────────────────────────
WATCHLIST = [
    "BTCUSDT",  "ETHUSDT",  "SOLUSDT",  "BEATUSDT", "STGUSDT",
    "SOXLUSDT", "MUUSDT",   "WLDUSDT",  "AIOUSDT",  "MRVLUSDT",
    "CLUSDT",   "ALLOUSDT", "SUIUSDT",  "DOGEUSDT", "LUMIAUSDT",
]

# ── Config por timeframe ─────────────────────────────────────────────────────
TF_CONFIGS = {
    '3m':  {'struct_tf':'15m','htf_tf':'1h',
             'struct_ms':15*60_000,'htf_ms':60*60_000,
             'exit_bars':60,'min_conf_ob':4,'min_conf_fvg':5,
             'ob_max_bars':750,'fvg_max_bars':450},
    '5m':  {'struct_tf':'15m','htf_tf':'1h',
             'struct_ms':15*60_000,'htf_ms':60*60_000,
             'exit_bars':60,'min_conf_ob':4,'min_conf_fvg':5,
             'ob_max_bars':450,'fvg_max_bars':270},
    '15m': {'struct_tf':'1h','htf_tf':'4h',
             'struct_ms':60*60_000,'htf_ms':240*60_000,
             'exit_bars':60,'min_conf_ob':5,'min_conf_fvg':6,
             'ob_max_bars':250,'fvg_max_bars':150},
}

CAPITAL_INIT = 1000.0
LEVERAGE     = 10
MARGIN_FRAC  = 0.04
FEE_TAKER    = 0.0005

SL_ATR_MULT  = 1.0
TP1_RR       = 1.5
TP2_RR       = 3.0
TRAIL_ATR    = 1.0

PIVOT_LB     = 5
OB_IMPULSE   = 1.8
FVG_MIN      = 0.25
SWEEP_LB     = 25
SWEEP_TOL    = 0.003
COOLDOWN     = 5

FIB_LOOKBACK = 50
FIB_TOL_PCT  = 0.5

FAPI         = "https://fapi.binance.com"
CACHE_TTL    = 4 * 3600  # 4 horas


# ── Indicadores (vetorizados) ────────────────────────────────────────────────

def _atr_arr(h, l, c, period=14):
    """ATR vetorizado - TR com np.maximum, EMA sequencial."""
    h = np.asarray(h, dtype=np.float64)
    l = np.asarray(l, dtype=np.float64)
    c = np.asarray(c, dtype=np.float64)
    n = len(c)
    tr = np.zeros(n)
    if n > 1:
        tr[1:] = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]),
                               np.abs(l[1:] - c[:-1])))
    atr = np.zeros(n)
    if n <= period:
        return atr
    atr[period] = tr[1:period + 1].mean()
    a = 1.0 / period
    for i in range(period + 1, n):
        atr[i] = atr[i-1] + a * (tr[i] - atr[i-1])
    return atr


def _sma(arr, p):
    cs  = np.cumsum(arr.astype(float))
    out = np.full(len(arr), np.nan)
    out[p-1:] = (cs[p-1:] - np.concatenate([[0.0], cs[:-p]])) / p
    return out


def _ema_arr(arr, p):
    out    = np.empty(len(arr))
    out[0] = arr[0]
    a = 2.0 / (p + 1)
    for i in range(1, len(arr)):
        out[i] = arr[i] * a + out[i-1] * (1 - a)
    return out


def _rsi_arr(c, p=14):
    d    = np.diff(c, prepend=c[0])
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag   = _sma(gain, p)
    al   = _sma(loss, p)
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(al > 0, ag / al, 100.0)
    return 100.0 - 100.0 / (1.0 + rs)


# ── Fibonacci ────────────────────────────────────────────────────────────────

def _fib_levels(h_arr, l_arr, up_to, lookback=FIB_LOOKBACK):
    start = max(0, up_to - lookback)
    if up_to - start < 10:
        return (None,) * 7
    sw_h = float(h_arr[start:up_to].max())
    sw_l = float(l_arr[start:up_to].min())
    rng  = sw_h - sw_l
    if rng <= 0:
        return (None,) * 7
    return (sw_h, sw_l,
            sw_h - 0.236 * rng, sw_h - 0.382 * rng,
            sw_h - 0.500 * rng, sw_h - 0.618 * rng,
            sw_h - 0.786 * rng)


def _fib_confluence(price, h_arr, l_arr, up_to):
    sw_h, sw_l, f236, f382, f500, f618, f786 = _fib_levels(h_arr, l_arr, up_to)
    if sw_h is None:
        return 0, '', 0.0
    tol = price * FIB_TOL_PCT / 100
    for level, name in [(f382, '0.382'), (f500, '0.500'), (f618, '0.618')]:
        if abs(price - level) <= tol:
            return 10, name, level
    for level, name in [(f236, '0.236'), (f786, '0.786')]:
        if abs(price - level) <= tol:
            return 5, name, level
    return 0, '', 0.0


# ── Estrutura de mercado ─────────────────────────────────────────────────────

def detect_pivots(h, l, lb=PIVOT_LB):
    ph, pl = [], []
    n = len(h)
    for i in range(lb, n - lb):
        if h[i] >= h[i-lb:i+lb+1].max():
            ph.append((i, float(h[i])))
        if l[i] <= l[i-lb:i+lb+1].min():
            pl.append((i, float(l[i])))
    return ph, pl


def detect_structure(ph, pl):
    if len(ph) < 2 or len(pl) < 2:
        return 'ranging'
    if ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]:
        return 'up'
    if ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]:
        return 'down'
    return 'ranging'


def _htf_trend_map(h, l, c, ts_arr, step_ms):
    """O(n log n) com bisect - era O(n^2) antes."""
    if c is None or len(c) < 20:
        return {}
    ph, pl   = detect_pivots(h, l)
    ph_idxs  = [p[0] for p in ph]
    pl_idxs  = [p[0] for p in pl]
    result   = {}
    for idx, ts_val in enumerate(ts_arr):
        n_ph = bisect.bisect_right(ph_idxs, idx)
        n_pl = bisect.bisect_right(pl_idxs, idx)
        result[int(ts_val)] = detect_structure(ph[:n_ph], pl[:n_pl])
    return result


def _htf_at(trend_map, ts_ms, step_ms):
    if not trend_map:
        return 'ranging'
    bar_ms = (ts_ms // step_ms) * step_ms
    return trend_map.get(bar_ms, trend_map.get(bar_ms - step_ms, 'ranging'))


# ── Order Blocks ─────────────────────────────────────────────────────────────

def find_order_blocks(o, h, l, c, atr, n):
    obs   = []
    limit = min(n - 3, len(c) - 3)
    for i in range(PIVOT_LB, limit):
        av = atr[i]
        if av <= 0:
            continue
        if c[i] < o[i]:
            if (max(h[i+1], h[i+2], h[i+3]) - c[i]) >= OB_IMPULSE * av:
                obs.append({'idx': i, 'dir': 1,
                             'top': float(h[i]), 'bot': float(l[i])})
        if c[i] > o[i]:
            if (c[i] - min(l[i+1], l[i+2], l[i+3])) >= OB_IMPULSE * av:
                obs.append({'idx': i, 'dir': -1,
                             'top': float(h[i]), 'bot': float(l[i])})
    return obs


def find_fvg(h, l, atr, n):
    fvgs  = []
    limit = min(n - 1, len(h) - 1)
    for i in range(1, limit):
        av = atr[i]
        if av <= 0:
            continue
        gap_up = l[i+1] - h[i-1]
        if gap_up >= FVG_MIN * av:
            fvgs.append({'idx': i, 'dir': 1,
                          'top': float(l[i+1]), 'bot': float(h[i-1])})
        gap_dn = l[i-1] - h[i+1]
        if gap_dn >= FVG_MIN * av:
            fvgs.append({'idx': i, 'dir': -1,
                          'top': float(l[i-1]), 'bot': float(h[i+1])})
    return fvgs


# ── Zonas → numpy (para invalidacao vetorizada) ──────────────────────────────

def _zones_to_numpy(all_obs, all_fvgs):
    """
    Converte listas de OBs/FVGs para arrays numpy.
    Colunas: [idx, top, bot, dir]
    """
    if all_obs:
        ob_arr = np.array([[z['idx'], z['top'], z['bot'], z['dir']]
                           for z in all_obs], dtype=np.float64)
    else:
        ob_arr = np.empty((0, 4), dtype=np.float64)

    if all_fvgs:
        fvg_arr = np.array([[z['idx'], z['top'], z['bot'], z['dir']]
                             for z in all_fvgs], dtype=np.float64)
    else:
        fvg_arr = np.empty((0, 4), dtype=np.float64)

    return ob_arr, fvg_arr


# ── Backtest V7 (otimizado) ──────────────────────────────────────────────────

def run_backtest_v7(o, h, l, c, v, ts_arr,
                    atr_a, vol_ma, rsi_a,
                    ob_arr, ob_idxs,
                    fvg_arr, fvg_idxs,
                    struct_map, htf_map,
                    struct_ms, htf_ms,
                    leverage, btc_regime,
                    symbol, group,
                    exit_bars, min_conf_ob, min_conf_fvg,
                    ob_max_bars, fvg_max_bars):
    """
    Motor de backtest com invalidacao numpy vetorizada.
    Recebe dados pre-computados (OBs/FVGs/ATR) compartilhados entre grupos.
    """
    n = len(c)
    if n < 200:
        return None

    # Copias frescas de valid por grupo
    n_obs  = len(ob_arr)
    n_fvgs = len(fvg_arr)
    ob_valid  = np.ones(n_obs,  dtype=bool)
    fvg_valid = np.ones(n_fvgs, dtype=bool)

    # Pre-extrai colunas para acesso rapido
    if n_obs:
        ob_idx_col = ob_arr[:, 0]
        ob_top_col = ob_arr[:, 1]
        ob_bot_col = ob_arr[:, 2]
        ob_dir_col = ob_arr[:, 3]
    if n_fvgs:
        fvg_idx_col = fvg_arr[:, 0]
        fvg_top_col = fvg_arr[:, 1]
        fvg_bot_col = fvg_arr[:, 2]
        fvg_dir_col = fvg_arr[:, 3]

    capital       = CAPITAL_INIT
    equity        = [CAPITAL_INIT]
    trades        = []
    in_trade      = False
    trade_dir     = 0
    entry_px      = 0.0
    sl_px         = 0.0
    tp1_px        = 0.0
    tp2_px        = 0.0
    trail_ref     = 0.0
    partial       = False
    entry_bar     = 0
    margin_used   = 0.0
    last_exit     = -999
    trade_pnl_acc = 0.0
    exit_counts   = defaultdict(int)
    fib_hits      = defaultdict(int)

    for i in range(200, n):
        av  = atr_a[i]
        vma = vol_ma[i]
        if np.isnan(av) or av <= 0 or np.isnan(vma) or vma <= 0:
            continue

        c_i = float(c[i]); h_i = float(h[i])
        l_i = float(l[i]); o_i = float(o[i])
        v_i = float(v[i]); ts_i = int(ts_arr[i])

        # ── Invalida zonas mitigadas (numpy vetorizado - era O(n^2)) ──────────
        if n_obs:
            alive = ob_valid & (ob_idx_col < i)
            ob_valid[alive & (ob_dir_col == 1)  & (c_i < ob_bot_col)] = False
            ob_valid[alive & (ob_dir_col == -1) & (c_i > ob_top_col)] = False

        if n_fvgs:
            alive = fvg_valid & (fvg_idx_col < i)
            fvg_valid[alive & (fvg_dir_col == 1)  & (c_i < fvg_bot_col)] = False
            fvg_valid[alive & (fvg_dir_col == -1) & (c_i > fvg_top_col)] = False

        # ── Saida ─────────────────────────────────────────────────────────────
        if in_trade:
            fee = margin_used * FEE_TAKER * leverage

            if not partial:
                hit_sl = (trade_dir ==  1 and l_i <= sl_px) or \
                         (trade_dir == -1 and h_i >= sl_px)
                if hit_sl:
                    raw = (sl_px - entry_px) / entry_px * leverage * margin_used \
                          if trade_dir == 1 else \
                          (entry_px - sl_px) / entry_px * leverage * margin_used
                    pnl = raw - fee * 2
                    capital += pnl
                    trades.append({'pnl': pnl, 'exit': 'sl'})
                    exit_counts['sl'] += 1
                    equity.append(capital)
                    in_trade = False; last_exit = i; trade_pnl_acc = 0.0
                    continue

                hit_tp1 = (trade_dir ==  1 and h_i >= tp1_px) or \
                           (trade_dir == -1 and l_i <= tp1_px)
                if hit_tp1:
                    raw     = abs(tp1_px - entry_px) / entry_px * leverage * margin_used * 0.5
                    pnl_tp1 = raw - fee * 0.5 * 2
                    capital += pnl_tp1; trade_pnl_acc += pnl_tp1
                    equity.append(capital)
                    partial     = True
                    margin_used *= 0.5
                    trail_ref   = c_i
                    sl_px       = entry_px
                    exit_counts['tp1'] += 1
                    continue
            else:
                if trade_dir == 1:
                    trail_ref  = max(trail_ref, c_i)
                    trail_stop = trail_ref - TRAIL_ATR * av
                else:
                    trail_ref  = min(trail_ref, c_i)
                    trail_stop = trail_ref + TRAIL_ATR * av

                hit_tp2 = (trade_dir ==  1 and h_i >= tp2_px) or \
                           (trade_dir == -1 and l_i <= tp2_px)
                if hit_tp2:
                    raw  = abs(tp2_px - entry_px) / entry_px * leverage * margin_used
                    pnl  = raw - fee * 2; capital += pnl
                    pnl_full = pnl + trade_pnl_acc
                    trades.append({'pnl': pnl_full, 'exit': 'tp2'})
                    exit_counts['tp2'] += 1
                    equity.append(capital)
                    in_trade = False; last_exit = i; trade_pnl_acc = 0.0
                    continue

                timeout   = (i - entry_bar) >= exit_bars
                hit_trail = (trade_dir ==  1 and l_i < trail_stop) or \
                             (trade_dir == -1 and h_i > trail_stop)
                if hit_trail or timeout:
                    exit_px = trail_stop if hit_trail else c_i
                    raw = (exit_px - entry_px) / entry_px * leverage * margin_used \
                          if trade_dir == 1 else \
                          (entry_px - exit_px) / entry_px * leverage * margin_used
                    pnl      = raw - fee * 2; capital += pnl
                    pnl_full = pnl + trade_pnl_acc
                    reason   = 'trail' if hit_trail else 'timeout'
                    trades.append({'pnl': pnl_full, 'exit': reason})
                    exit_counts[reason] += 1
                    equity.append(capital)
                    in_trade = False; last_exit = i; trade_pnl_acc = 0.0
                    continue
            continue

        # ── Entrada ───────────────────────────────────────────────────────────
        if capital <= 50:
            break
        if i - last_exit < COOLDOWN:
            continue

        htf_s = _htf_at(struct_map, ts_i, struct_ms)
        htf_h = _htf_at(htf_map,    ts_i, htf_ms)

        can_long  = btc_regime and htf_s == 'up'  and htf_h in ('up', 'ranging')
        can_short =               htf_s == 'down' and htf_h in ('down', 'ranging')
        if not can_long and not can_short:
            continue

        rsi_v = float(rsi_a[i])
        if v_i > vma * 5.0:
            continue
        if can_long  and rsi_v > 72:
            continue
        if can_short and rsi_v < 28:
            continue

        rng_i  = h_i - l_i
        body_i = abs(c_i - o_i)
        body_q = body_i / rng_i if rng_i > 0 else 0.0

        fib_pts, fib_name, fib_level = _fib_confluence(c_i, h, l, i)

        def _extras(direction):
            score    = 0
            low_ref  = float(l[max(0, i - SWEEP_LB):i - 1].min())
            high_ref = float(h[max(0, i - SWEEP_LB):i - 1].max())
            if direction == 1:
                for k in range(max(0, i - 8), i):
                    if l[k] < low_ref * (1 - SWEEP_TOL) and c[k] > low_ref:
                        score += 2; break
            else:
                for k in range(max(0, i - 8), i):
                    if h[k] > high_ref * (1 + SWEEP_TOL) and c[k] < high_ref:
                        score += 2; break
            if v_i >= vma * 2.0:   score += 2
            elif v_i >= vma * 1.2: score += 1
            if direction ==  1 and htf_h == 'up':   score += 2
            if direction == -1 and htf_h == 'down':  score += 2
            if body_q >= 0.70: score += 1
            return score

        entry_found = False

        # ── Grupos A e B — OB/FVG ─────────────────────────────────────────────
        if group in ('A', 'B'):

            if can_long and 38 <= rsi_v <= 68 and c_i > o_i and body_q >= 0.50:
                # OB long
                ob_cut = bisect.bisect_left(ob_idxs, i - 3)
                for j in range(ob_cut - 1, max(-1, ob_cut - 15), -1):
                    if not ob_valid[j] or ob_dir_col[j] != 1 or (i - ob_idx_col[j]) > ob_max_bars:
                        continue
                    if l_i <= ob_top_col[j] and c_i > ob_top_col[j]:
                        if group == 'B' and fib_pts == 0:
                            break
                        conf = 3 + _extras(1)
                        if conf >= min_conf_ob:
                            sl_px  = ob_bot_col[j] - SL_ATR_MULT * av
                            risk_r = c_i - sl_px
                            if 0 < risk_r < 4 * av:
                                tp1_px = c_i + risk_r * TP1_RR
                                tp2_px = c_i + risk_r * TP2_RR
                                entry_px = c_i; trade_dir = 1
                                entry_found = True
                                if fib_name: fib_hits[fib_name] += 1
                        break

                # FVG long
                if not entry_found:
                    fvg_cut = bisect.bisect_left(fvg_idxs, i - 3)
                    for j in range(fvg_cut - 1, max(-1, fvg_cut - 10), -1):
                        if not fvg_valid[j] or fvg_dir_col[j] != 1 or (i - fvg_idx_col[j]) > fvg_max_bars:
                            continue
                        if l_i <= fvg_top_col[j] and c_i > fvg_top_col[j]:
                            if group == 'B' and fib_pts == 0:
                                break
                            conf = 2 + _extras(1)
                            if conf >= min_conf_fvg:
                                sl_px  = fvg_bot_col[j] - SL_ATR_MULT * av
                                risk_r = c_i - sl_px
                                if 0 < risk_r < 4 * av:
                                    tp1_px = c_i + risk_r * TP1_RR
                                    tp2_px = c_i + risk_r * TP2_RR
                                    entry_px = c_i; trade_dir = 1
                                    entry_found = True
                                    if fib_name: fib_hits[fib_name] += 1
                            break

            elif can_short and 32 <= rsi_v <= 62 and c_i < o_i and body_q >= 0.50:
                # OB short
                ob_cut = bisect.bisect_left(ob_idxs, i - 3)
                for j in range(ob_cut - 1, max(-1, ob_cut - 15), -1):
                    if not ob_valid[j] or ob_dir_col[j] != -1 or (i - ob_idx_col[j]) > ob_max_bars:
                        continue
                    if h_i >= ob_bot_col[j] and c_i < ob_bot_col[j]:
                        if group == 'B' and fib_pts == 0:
                            break
                        conf = 3 + _extras(-1)
                        if conf >= min_conf_ob:
                            sl_px  = ob_top_col[j] + SL_ATR_MULT * av
                            risk_r = sl_px - c_i
                            if 0 < risk_r < 4 * av:
                                tp1_px = c_i - risk_r * TP1_RR
                                tp2_px = c_i - risk_r * TP2_RR
                                entry_px = c_i; trade_dir = -1
                                entry_found = True
                                if fib_name: fib_hits[fib_name] += 1
                        break

                # FVG short
                if not entry_found:
                    fvg_cut = bisect.bisect_left(fvg_idxs, i - 3)
                    for j in range(fvg_cut - 1, max(-1, fvg_cut - 10), -1):
                        if not fvg_valid[j] or fvg_dir_col[j] != -1 or (i - fvg_idx_col[j]) > fvg_max_bars:
                            continue
                        if h_i >= fvg_bot_col[j] and c_i < fvg_bot_col[j]:
                            if group == 'B' and fib_pts == 0:
                                break
                            conf = 2 + _extras(-1)
                            if conf >= min_conf_fvg:
                                sl_px  = fvg_top_col[j] + SL_ATR_MULT * av
                                risk_r = sl_px - c_i
                                if 0 < risk_r < 4 * av:
                                    tp1_px = c_i - risk_r * TP1_RR
                                    tp2_px = c_i - risk_r * TP2_RR
                                    entry_px = c_i; trade_dir = -1
                                    entry_found = True
                                    if fib_name: fib_hits[fib_name] += 1
                            break

        # ── Grupo C — Fibonacci isolado ───────────────────────────────────────
        elif group == 'C' and fib_pts >= 10 and fib_level > 0:

            if can_long and 40 <= rsi_v <= 65 and c_i > o_i and body_q >= 0.45:
                if l_i <= fib_level * (1 + FIB_TOL_PCT / 100) and c_i > fib_level:
                    sl_px  = fib_level - SL_ATR_MULT * av
                    risk_r = c_i - sl_px
                    if 0 < risk_r < 4 * av:
                        tp1_px = c_i + risk_r * TP1_RR
                        tp2_px = c_i + risk_r * TP2_RR
                        entry_px = c_i; trade_dir = 1
                        entry_found = True
                        fib_hits[fib_name] += 1

            elif can_short and 35 <= rsi_v <= 60 and c_i < o_i and body_q >= 0.45:
                if h_i >= fib_level * (1 - FIB_TOL_PCT / 100) and c_i < fib_level:
                    sl_px  = fib_level + SL_ATR_MULT * av
                    risk_r = sl_px - c_i
                    if 0 < risk_r < 4 * av:
                        tp1_px = c_i - risk_r * TP1_RR
                        tp2_px = c_i - risk_r * TP2_RR
                        entry_px = c_i; trade_dir = -1
                        entry_found = True
                        fib_hits[fib_name] += 1

        if entry_found:
            margin_used   = min(capital * MARGIN_FRAC, capital * 0.15)
            in_trade      = True
            partial       = False
            trail_ref     = entry_px
            entry_bar     = i
            trade_pnl_acc = 0.0

    # ── Metricas ──────────────────────────────────────────────────────────────
    total = len(trades)
    if total < 3:
        return None

    wins  = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr    = len(wins) / total * 100
    gp    = sum(t['pnl'] for t in wins)
    gl    = abs(sum(t['pnl'] for t in losses))
    pf    = round(gp / gl, 3) if gl > 0 else round(gp, 3)

    eq   = np.array([CAPITAL_INIT] + equity)
    peak = np.maximum.accumulate(eq)
    dd   = float(((peak - eq) / peak * 100).max())

    return {
        'symbol':        symbol,
        'group':         group,
        'total_trades':  total,
        'win_rate':      round(wr, 2),
        'profit_factor': pf,
        'final_capital': round(capital, 2),
        'pct_return':    round((capital / CAPITAL_INIT - 1) * 100, 2),
        'max_drawdown':  round(dd, 2),
        'exits':         dict(exit_counts),
        'pnl_total':     round(capital - CAPITAL_INIT, 2),
        'fib_hits':      dict(fib_hits),
    }


# ── Download com cache em disco ──────────────────────────────────────────────

async def fetch_klines(session, symbol, interval, days=180):
    mins_per = {'1m':1,'3m':3,'5m':5,'15m':15,'30m':30,'1h':60,'4h':240}
    step_ms  = mins_per.get(interval, 15) * 60_000
    needed   = days * 24 * 60 // mins_per.get(interval, 15)
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - needed * step_ms
    rows = []
    cur  = start_ms
    while cur < end_ms and len(rows) < needed:
        try:
            async with session.get(
                f"{FAPI}/fapi/v1/klines",
                params={'symbol': symbol, 'interval': interval,
                        'startTime': cur, 'limit': 1500},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    break
                data = await r.json()
                if not data:
                    break
                rows.extend(data)
                cur = int(data[-1][0]) + step_ms
                if len(data) < 1500:
                    break
        except Exception:
            break
    if not rows:
        return None
    return np.array([[float(r[1]), float(r[2]), float(r[3]),
                      float(r[4]), float(r[5]), float(r[0])] for r in rows])


async def fetch_klines_cached(session, symbol, interval, days=180):
    """Usa cache em disco - evita re-downloads repetidos."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path = CACHE_DIR / f"{symbol}_{interval}_{days}d.npy"

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            try:
                return np.load(str(cache_path))
            except Exception:
                pass

    arr = await fetch_klines(session, symbol, interval, days)
    if arr is not None:
        np.save(str(cache_path), arr)
    return arr


async def get_btc_regime(session):
    try:
        arr = await fetch_klines_cached(session, 'BTCUSDT', '4h', days=45)
        if arr is not None and len(arr) >= 25:
            return bool(arr[-1, 3] > _ema_arr(arr[:, 3], 21)[-1])
    except Exception:
        pass
    return True


# ── Agregacao ────────────────────────────────────────────────────────────────

def _aggregate(results_list):
    if not results_list:
        return None
    all_trades = sum(r['total_trades'] for r in results_list)
    if all_trades == 0:
        return None
    gp  = sum(r['pnl_total'] for r in results_list if r['pnl_total'] > 0)
    gl  = abs(sum(r['pnl_total'] for r in results_list if r['pnl_total'] <= 0))
    pf  = round(gp / gl, 3) if gl > 0 else round(gp, 3)
    all_cap = CAPITAL_INIT + sum(r['pnl_total'] for r in results_list)
    pct = round((all_cap / CAPITAL_INIT - 1) * 100, 2)
    max_dd = max(r['max_drawdown'] for r in results_list)
    ex = defaultdict(int)
    for r in results_list:
        for k, vv in r['exits'].items():
            ex[k] += vv
    tot_ex = sum(ex.values())
    tp_ex  = ex.get('tp1', 0) + ex.get('tp2', 0) + ex.get('trail', 0)
    wr     = round(tp_ex / tot_ex * 100, 1) if tot_ex > 0 else 0.0
    fib_all = defaultdict(int)
    for r in results_list:
        for k, vv in r.get('fib_hits', {}).items():
            fib_all[k] += vv
    by_sym = sorted(results_list, key=lambda x: x['profit_factor'], reverse=True)
    return {
        'total_trades': all_trades,
        'win_rate':     wr,
        'profit_factor': pf,
        'pct_return':   pct,
        'max_drawdown': round(max_dd, 1),
        'exits':        dict(ex),
        'fib_hits':     dict(fib_all),
        'top5':   [(r['symbol'], r['profit_factor'], r['win_rate']) for r in by_sym[:5]],
        'worst3': [(r['symbol'], r['profit_factor'], r['win_rate']) for r in by_sym[-3:]],
    }


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(days=180, capital=1000.0, lev=10):
    global CAPITAL_INIT, LEVERAGE
    CAPITAL_INIT = capital
    LEVERAGE     = lev

    print(f"""
{'='*78}
  MICRO-CAP BACKTEST V7 (OTIMIZADO)  --  V6 + Fibonacci | Multi-TF 3m/5m/15m
  Capital: ${capital:.0f} | Lev: {lev}x | {days} dias | Taxas reais | {len(WATCHLIST)} simbolos
  Grupos: A=V6 puro | B=V6+Fib | C=Fib isolado
  Otimizacoes: numpy vetorizado | cache disco | computacao compartilhada
{'='*78}
""")

    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(),
                                     limit=10, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as sess:
        print("  BTC regime (4h EMA21)...")
        btc_regime = await get_btc_regime(sess)
        print(f"  BTC: {'UP -- LONGs permitidos' if btc_regime else 'DOWN -- somente SHORTs'}\n")

        all_results = {tf: {'A': [], 'B': [], 'C': []} for tf in TF_CONFIGS}

        for sym_idx, sym in enumerate(WATCHLIST, 1):
            t0 = time.time()
            print(f"\n  [{sym_idx:>2}/{len(WATCHLIST)}] {sym} {'-'*(38-len(sym))}")

            # TFs necessarios sem duplicata
            tfs_needed = set()
            for cfg in TF_CONFIGS.values():
                tfs_needed.update([cfg['struct_tf'], cfg['htf_tf']])
            tfs_needed.update(TF_CONFIGS.keys())

            klines = {}
            for tf in sorted(tfs_needed,
                             key=lambda x: {'1h':1,'4h':2,'15m':3,'5m':4,'3m':5}.get(x, 9)):
                print(f"     DL {tf}...", end='', flush=True)
                klines[tf] = await fetch_klines_cached(sess, sym, tf, days)
                await asyncio.sleep(0.2)
            print()

            for tf, cfg in TF_CONFIGS.items():
                arr_e = klines.get(tf)
                if arr_e is None or len(arr_e) < 200:
                    print(f"     {tf}: dados insuficientes")
                    continue

                arr_s = klines.get(cfg['struct_tf'])
                arr_h = klines.get(cfg['htf_tf'])

                map_s = _htf_trend_map(arr_s[:,1], arr_s[:,2], arr_s[:,3],
                                        arr_s[:,5], cfg['struct_ms']) \
                        if arr_s is not None and len(arr_s) >= 20 else {}
                map_h = _htf_trend_map(arr_h[:,1], arr_h[:,2], arr_h[:,3],
                                        arr_h[:,5], cfg['htf_ms']) \
                        if arr_h is not None and len(arr_h) >= 15 else {}

                o_e, h_e, l_e = arr_e[:,0], arr_e[:,1], arr_e[:,2]
                c_e, v_e, ts_e = arr_e[:,3], arr_e[:,4], arr_e[:,5]
                n_e = len(c_e)

                # ── Computacao compartilhada entre grupos ─────────────────────
                atr_a  = _atr_arr(h_e, l_e, c_e, 14)
                vol_ma = _sma(v_e.astype(float), 20)
                rsi_a  = _rsi_arr(c_e, 14)

                all_obs  = find_order_blocks(o_e, h_e, l_e, c_e, atr_a, n_e)
                all_fvgs = find_fvg(h_e, l_e, atr_a, n_e)

                ob_idxs  = [z['idx'] for z in all_obs]
                fvg_idxs = [z['idx'] for z in all_fvgs]
                ob_arr, fvg_arr = _zones_to_numpy(all_obs, all_fvgs)

                # Pre-extrai colunas (mesmo array compartilhado, valid e por grupo)
                ob_idx_col  = ob_arr[:, 0]  if len(ob_arr)  else np.array([])
                fvg_idx_col = fvg_arr[:, 0] if len(fvg_arr) else np.array([])

                line_parts = [f"     {tf}:"]
                for grp in ('A', 'B', 'C'):
                    res = run_backtest_v7(
                        o_e, h_e, l_e, c_e, v_e, ts_e,
                        atr_a, vol_ma, rsi_a,
                        ob_arr, ob_idxs,
                        fvg_arr, fvg_idxs,
                        map_s, map_h,
                        cfg['struct_ms'], cfg['htf_ms'],
                        lev, btc_regime, sym, grp,
                        cfg['exit_bars'], cfg['min_conf_ob'], cfg['min_conf_fvg'],
                        cfg['ob_max_bars'], cfg['fvg_max_bars'],
                    )
                    if res:
                        all_results[tf][grp].append(res)
                        tag = "**" if res['profit_factor'] >= 1.5 \
                              else "OK" if res['profit_factor'] >= 1.0 else "--"
                        line_parts.append(
                            f"[{grp}] T:{res['total_trades']:>3}  "
                            f"WR:{res['win_rate']:.0f}%  "
                            f"PF:{res['profit_factor']:.2f}{tag}"
                        )
                print("  |  ".join(line_parts))

            elapsed = time.time() - t0
            print(f"     tempo: {elapsed:.1f}s")

        # ── Relatorio final ───────────────────────────────────────────────────
        print(f"\n\n{'='*78}")
        print("  COMPARACAO A vs B vs C  x  TIMEFRAME")
        print(f"{'='*78}")
        print(f"  {'TF':<5} {'Grp':<4} {'Trades':>7} {'WR':>7} {'PF':>7} {'Retorno':>9} {'DD':>7}")
        print(f"  {'-'*50}")

        output = {'version': 'v7', 'days': days, 'by_tf': {}, 'best_combos': []}

        for tf in ('3m', '5m', '15m'):
            tf_out = {}
            for grp in ('A', 'B', 'C'):
                agg = _aggregate(all_results[tf][grp])
                tf_out[grp] = agg
                if agg:
                    print(f"  {tf:<5} {grp:<4} "
                          f"{agg['total_trades']:>7}  "
                          f"{agg['win_rate']:>5.1f}%  "
                          f"{agg['profit_factor']:>5.3f}  "
                          f"{agg['pct_return']:>+7.1f}%  "
                          f"{agg['max_drawdown']:>5.1f}%")
                else:
                    print(f"  {tf:<5} {grp:<4}  (sem dados)")
            output['by_tf'][tf] = tf_out
            print()

        best = []
        for tf, grp_data in output['by_tf'].items():
            for grp, agg in grp_data.items():
                if agg and agg['total_trades'] >= 10:
                    best.append({
                        'tf': tf, 'group': grp,
                        'profit_factor': agg['profit_factor'],
                        'win_rate':      agg['win_rate'],
                        'pct_return':    agg['pct_return'],
                        'total_trades':  agg['total_trades'],
                        'max_drawdown':  agg['max_drawdown'],
                    })
        best.sort(key=lambda x: x['profit_factor'], reverse=True)
        output['best_combos'] = best

        print(f"\n  TOP 5 COMBINACOES (PF):")
        print(f"  {'Rank':<5} {'TF':<5} {'Grp':<4} {'PF':>7} {'WR':>7} {'Ret':>9} {'DD':>7} {'T':>6}")
        print(f"  {'-'*55}")
        for rank, b in enumerate(best[:5], 1):
            print(f"  {rank:<5} {b['tf']:<5} {b['group']:<4} "
                  f"{b['profit_factor']:>5.3f}  "
                  f"{b['win_rate']:>5.1f}%  "
                  f"{b['pct_return']:>+7.1f}%  "
                  f"{b['max_drawdown']:>5.1f}%  "
                  f"{b['total_trades']:>5}")

        print(f"\n  FIBONACCI -- Niveis mais acionados (Grupos B+C):")
        fib_total = defaultdict(int)
        for tf in output['by_tf'].values():
            for grp in ('B', 'C'):
                agg = tf.get(grp)
                if agg and agg.get('fib_hits'):
                    for k, vv in agg['fib_hits'].items():
                        fib_total[k] += vv
        for level, count in sorted(fib_total.items(), key=lambda x: x[1], reverse=True):
            print(f"    Fib {level}: {count} entradas")

        print(f"\n{'='*78}")

        out_path = BASE / "micro_cap_results_v7.json"
        enc = __import__('codecs').lookup('utf-8').incrementalencoder
        out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False),
                            encoding='utf-8')
        print(f"  Resultados: {out_path}")
        return output


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days",    type=int,   default=180)
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--lev",     type=int,   default=10)
    args = p.parse_args()
    asyncio.run(main(args.days, args.capital, args.lev))
