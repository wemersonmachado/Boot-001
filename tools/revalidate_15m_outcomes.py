# -*- coding: utf-8 -*-
"""
Revalida os outcomes TIMEOUT do 15m com a janela nova de 24h (2026-07-02).

Contexto: a janela antiga (8h) fechava sinais de 15m cedo demais — muitos ainda
iam bater TP1 e eram gravados como TIMEOUT (pnl do momento). Isso rebaixava o
WR medido e fazia o auto-tune apertar o corte sem motivo real.

O que faz:
  1. Pega signal_outcomes com timeframe='15m' e outcome='TIMEOUT'.
  2. Busca klines históricos 15m na Binance (públicos) a partir do timestamp
     do sinal e rejulga com 24h: SL tocado → LOSS (prioridade conservadora),
     TP tocado → WIN, nada em 24h → TIMEOUT com pnl no fechamento das 24h.
  3. Atualiza a linha (outcome, exit_price, pnl_pct).
  4. Imprime WR do 15m antes/depois (mesma métrica do auto-tune:
     WIN ou TIMEOUT com pnl>0 conta como acerto).

Rate limit: 1 request por sinal + sleep 0.25s — bem abaixo do limite da Binance.
"""
import sqlite3
import time
import sys
import os
from datetime import datetime, timezone

import requests

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader_001.db")
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
WINDOW_H = 24


def wr_15m(rows):
    """Mesma métrica do get_recent_signal_stats: WIN ou TIMEOUT pnl>0 = acerto."""
    n = len(rows)
    if not n:
        return 0.0, 0, 0
    wins = sum(1 for o, p in rows if o == "WIN" or (o == "TIMEOUT" and (p or 0) > 0))
    return round(wins / n * 100.0, 1), wins, n


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    before = con.execute(
        "SELECT outcome, pnl_pct FROM signal_outcomes WHERE timeframe='15m'"
    ).fetchall()
    wr_b, wins_b, n_b = wr_15m([(r[0], r[1]) for r in before])
    print(f"ANTES : WR 15m = {wr_b}% ({wins_b}/{n_b})")

    pend = con.execute(
        """SELECT o.id AS oid, o.asset, o.direction, o.entry, o.pnl_pct,
                  s.stop_loss AS sl, s.tp1 AS tp, s.timestamp AS ts
           FROM signal_outcomes o
           JOIN signals s ON s.id = o.signal_db_id
           WHERE o.timeframe='15m' AND o.outcome='TIMEOUT'
             AND s.stop_loss > 0 AND s.tp1 > 0"""
    ).fetchall()
    print(f"Revalidando {len(pend)} TIMEOUTs de 15m com janela de {WINDOW_H}h...")

    changed = {"WIN": 0, "LOSS": 0, "TIMEOUT": 0}
    errors = 0
    for r in pend:
        try:
            ts = datetime.fromisoformat(r["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            start_ms = int(ts.timestamp() * 1000)
            resp = requests.get(KLINES_URL, params={
                "symbol": r["asset"], "interval": "15m",
                "startTime": start_ms, "limit": 100,  # 100 velas = 25h
            }, timeout=10)
            resp.raise_for_status()
            kl = resp.json()
            if not isinstance(kl, list) or not kl:
                errors += 1
                continue

            entry, sl, tp = float(r["entry"]), float(r["sl"]), float(r["tp"])
            is_long = "LONG" in (r["direction"] or "").upper()
            limit_ms = start_ms + WINDOW_H * 3600 * 1000

            outcome, exit_px = None, entry
            last_close = entry
            for k in kl:
                open_ms, hi, lo, close = int(k[0]), float(k[2]), float(k[3]), float(k[4])
                if open_ms > limit_ms:
                    break
                last_close = close
                hit_tp = (hi >= tp) if is_long else (lo <= tp)
                hit_sl = (lo <= sl) if is_long else (hi >= sl)
                if hit_sl:                       # conservador: SL primeiro
                    outcome, exit_px = "LOSS", sl
                    break
                if hit_tp:
                    outcome, exit_px = "WIN", tp
                    break
            if outcome is None:
                outcome, exit_px = "TIMEOUT", last_close

            pnl = ((exit_px - entry) / entry * 100.0) if is_long else ((entry - exit_px) / entry * 100.0)
            con.execute(
                "UPDATE signal_outcomes SET outcome=?, exit_price=?, pnl_pct=? WHERE id=?",
                (outcome, exit_px, round(pnl, 3), r["oid"]),
            )
            changed[outcome] += 1
            time.sleep(0.25)
        except Exception as e:
            errors += 1
            print(f"  [skip] {r['asset']}: {e}")

    con.commit()
    after = con.execute(
        "SELECT outcome, pnl_pct FROM signal_outcomes WHERE timeframe='15m'"
    ).fetchall()
    wr_a, wins_a, n_a = wr_15m([(r[0], r[1]) for r in after])
    con.close()

    print(f"Rejulgados: WIN={changed['WIN']} LOSS={changed['LOSS']} "
          f"TIMEOUT(24h)={changed['TIMEOUT']} | erros/skips={errors}")
    print(f"DEPOIS: WR 15m = {wr_a}% ({wins_a}/{n_a})")


if __name__ == "__main__":
    sys.exit(main())
