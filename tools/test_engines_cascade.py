"""
test_engines_cascade.py -- Teste das 4 engines em modo cascata.

Roda offline (sem servidor FastAPI), busca klines reais da Binance
e mostra qual engine vence em cada ativo/timeframe.

Uso:
    cd trader_001
    python test_engines_cascade.py

Parametros (topo do arquivo):
    SYMBOLS    -- ativos a testar
    TIMEFRAMES -- timeframes a testar
    MODE       -- "NORMAL" | "AGGRESSIVE"
    MIN_CONF   -- confidence minima para considerar sinal valido
"""
import asyncio
import sys
import os
import time

# Garante saida UTF-8 no Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Parâmetros do teste ───────────────────────────────────────────────────────
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
    "1000PEPEUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT", "WIFUSDT",
    "ARBUSDT",  "OPUSDT",  "FETUSDT",  "JUPUSDT",  "TIAUSDT",
]
TIMEFRAMES = ["15m", "1h"]
MODE       = "NORMAL"
MIN_CONF   = 55.0

# ── Cores ANSI para terminal ──────────────────────────────────────────────────
GREEN  = ""
YELLOW = ""
RED    = ""
BLUE   = ""
CYAN   = ""
BOLD   = ""
DIM    = ""
RESET  = ""

ENGINE_COLOR = {
    "TREND":    GREEN,
    "RANGE":    BLUE,
    "BREAKOUT": YELLOW,
    "FADE":     RED,
}

REGIME_COLOR = {
    "TRENDING": GREEN,
    "RANGING":  BLUE,
    "VOLATILE": YELLOW,
    "NEUTRAL":  DIM,
}


def _fmt_bar(val: float, max_val: float = 100.0, width: int = 12) -> str:
    """Barra de progresso ASCII."""
    filled = int(val / max_val * width)
    bar = "#" * filled + "." * (width - filled)
    return bar


def _fmt_score_row(scores: dict) -> str:
    parts = []
    mx = max(scores.values()) if scores else 0
    for eng, s in scores.items():
        marker = "*" if s == mx and s > 0 else " "
        parts.append(f"{eng[:5]:<5}{marker}{s:5.1f}")
    return " | ".join(parts)


async def main():
    print(f"\n{'='*72}")
    print(f"  ENGINE CASCADE TEST -- Trader 001")
    print(f"  Modo: {MODE}  |  Min conf: {MIN_CONF}  |  "
          f"Ativos: {len(SYMBOLS)}  |  TFs: {TIMEFRAMES}")
    print(f"{'='*72}\n")

    # Importa engine_router após path setup
    sys.path.insert(0, os.path.dirname(__file__))
    import engine_router
    from klines_cache import get_klines_cached

    t0 = time.time()

    # ── Busca klines em paralelo (pré-cache) ─────────────────────────────────
    print(f"{DIM}[1/3] Pré-carregando klines...{RESET}")
    cache_tasks = [get_klines_cached(sym, tf, limit=200)
                   for sym in SYMBOLS for tf in TIMEFRAMES]
    await asyncio.gather(*cache_tasks, return_exceptions=True)
    print(f"{DIM}      {len(cache_tasks)} klines carregados em {time.time()-t0:.1f}s{RESET}\n")

    # ── Scan cascata ─────────────────────────────────────────────────────────
    print(f"{DIM}[2/3] Rodando 4 engines em cada ativo...{RESET}\n")

    results = await engine_router.scan_cascade(
        symbols=SYMBOLS,
        timeframes=TIMEFRAMES,
        mode=MODE,
        min_confidence=MIN_CONF,
        max_concurrent=15,
    )

    # ── Resultado de todos os ativos (incluindo sem sinal) ───────────────────
    print(f"{DIM}[3/3] Coletando resultados de todos os pares...{RESET}\n")

    # Para ativos sem sinal, ainda mostra os scores de cada engine
    all_tasks = {}
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                df = await get_klines_cached(sym, tf, limit=200)
                all_tasks[(sym, tf)] = df
            except Exception:
                all_tasks[(sym, tf)] = None

    full_results = []
    for (sym, tf), df in all_tasks.items():
        try:
            r = await engine_router.cascade_with_regime_scores(sym, tf, df, mode=MODE, min_confidence=MIN_CONF)
            full_results.append(r)
        except Exception as e:
            print(f"  [skip] {sym}/{tf}: {e}")
            continue

    full_results.sort(key=lambda x: max(x["all_scores"].values()), reverse=True)

    # ── Tabela principal ──────────────────────────────────────────────────────
    print(f"{BOLD}{'─'*72}{RESET}")
    print(f"{BOLD}{'ATIVO':<10} {'TF':<4} {'REGIME':<10} {'ADX':<6} "
          f"{'WINNER':<9} {'CONF':<7} {'DIR':<6} {'ADEQUAÇÃO REGIME (sem sinal=~)  TREND/RANGE/BREAK/FADE'}{RESET}")
    print(f"{'─'*72}")

    winners   = []
    no_signal = []

    for r in full_results:
        sym    = r.get("symbol", "?")
        regime = r.get("regime", "?")
        adx    = r.get("adx", 0.0)
        scores = r.get("all_scores", {})
        winner = r.get("winner")
        engine = r.get("engine", "—")
        tf     = "?"

        if winner:
            tf    = getattr(winner, "timeframe", "?")
            conf  = winner.confidence
            dirn  = getattr(winner, "direction", "?")
            dirn_val = dirn.value if hasattr(dirn, "value") else str(dirn)
            dir_color = GREEN if "LONG" in dirn_val.upper() else RED
            ec    = ENGINE_COLOR.get(engine, "")
            rc    = REGIME_COLOR.get(regime, "")
            bar   = _fmt_bar(conf)

            print(f"{BOLD}{sym:<10}{RESET} {tf:<4} "
                  f"{rc}{regime:<10}{RESET} {adx:<6.1f} "
                  f"{ec}{engine:<9}{RESET} {bar} {conf:<5.1f} "
                  f"{dir_color}{dirn_val[:5]:<6}{RESET} "
                  f"{_fmt_score_row(scores)}")
            winners.append(r)
        else:
            rc = REGIME_COLOR.get(regime, "")
            mode_tag = "~" if r.get("regime_match_mode") else " "
            print(f"{DIM}{sym:<10}  ?   {rc}{regime:<10}{RESET} {adx:<6.1f} "
                  f"{'—':<9} {'░'*12}  —     —     {mode_tag}"
                  f"{_fmt_score_row(scores)}{RESET}")
            no_signal.append(r)

    # ── Sumário por engine ────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*72}{RESET}")
    print(f"{BOLD}  SUMÁRIO POR ENGINE{RESET}")
    print(f"{'─'*72}")

    engine_stats: dict = {}
    for r in winners:
        e = r.get("engine", "?")
        w = r["winner"]
        if e not in engine_stats:
            engine_stats[e] = {"count": 0, "avg_conf": 0, "long": 0, "short": 0}
        engine_stats[e]["count"] += 1
        engine_stats[e]["avg_conf"] += w.confidence
        dirn = getattr(w, "direction", None)
        dirn_val = dirn.value if hasattr(dirn, "value") else str(dirn)
        if "LONG" in dirn_val.upper():
            engine_stats[e]["long"] += 1
        else:
            engine_stats[e]["short"] += 1

    for e, st in sorted(engine_stats.items(), key=lambda x: -x[1]["count"]):
        avg = st["avg_conf"] / st["count"] if st["count"] > 0 else 0
        ec  = ENGINE_COLOR.get(e, "")
        print(f"  {ec}{e:<10}{RESET} {st['count']:2d} sinais  "
              f"conf média {avg:.1f}  "
              f"{GREEN}L:{st['long']}{RESET}  {RED}S:{st['short']}{RESET}")

    # ── Sumário por regime ────────────────────────────────────────────────────
    print(f"\n{BOLD}  SUMÁRIO POR REGIME{RESET}")
    regime_cnt: dict = {}
    for r in full_results:
        reg = r.get("regime", "?")
        regime_cnt[reg] = regime_cnt.get(reg, 0) + 1
    for reg, cnt in sorted(regime_cnt.items(), key=lambda x: -x[1]):
        rc = REGIME_COLOR.get(reg, "")
        pct = cnt / len(full_results) * 100
        bar = "█" * int(pct / 5)
        print(f"  {rc}{reg:<12}{RESET} {bar:<20} {cnt:2d} pares ({pct:.0f}%)")

    # ── Top 5 sinais ──────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*72}{RESET}")
    print(f"{BOLD}  TOP 5 SINAIS MAIS FORTES{RESET}")
    print(f"{'─'*72}")

    top5 = sorted(winners, key=lambda x: x["winner"].confidence, reverse=True)[:5]
    for i, r in enumerate(top5, 1):
        w  = r["winner"]
        e  = r.get("engine", "?")
        ec = ENGINE_COLOR.get(e, "")
        dirn = getattr(w, "direction", None)
        dirn_val = dirn.value if hasattr(dirn, "value") else str(dirn)
        dir_color = GREEN if "LONG" in dirn_val.upper() else RED
        print(f"  {BOLD}{i}.{RESET} {BOLD}{w.asset:<10}{RESET} "
              f"{w.timeframe:<4} "
              f"{ec}{e:<9}{RESET} "
              f"conf {BOLD}{w.confidence:.1f}{RESET}  "
              f"{dir_color}{dirn_val}{RESET}  "
              f"entry {w.entry:.4f}  "
              f"RR {getattr(w, 'rr', getattr(w, 'risk_reward', 0)):.2f}x")
        reason_short = str(getattr(w, "reason", ""))[:60]
        print(f"     {DIM}{reason_short}{RESET}")

    # ── Rodapé ────────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    print(f"\n{BOLD}{'═'*72}{RESET}")
    print(f"  {GREEN}{len(winners)}{RESET} sinais válidos  |  "
          f"{DIM}{len(no_signal)}{RESET} sem sinal  |  "
          f"Tempo total: {elapsed:.1f}s")
    print(f"{BOLD}{'═'*72}{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
