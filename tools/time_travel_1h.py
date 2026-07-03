"""
Viagem no tempo: reconstroi quais sinais de 1h o motor REAL (engine_router + V6)
teria gerado ontem, caso o timeframe 1h ja estivesse ativo no perfil NORMAL
(so foi adicionado hoje). Roda walk-forward candle a candle sobre dados
historicos reais da Binance, aplica os mesmos thresholds do perfil NORMAL
(score>=72, RR>=2.0, tag estrutural V6 obrigatoria), e depois verifica o que
realmente aconteceu com o preco (TP1 ou SL primeiro) pra dizer WIN/LOSS.

Script standalone — nao toca no processo do bot rodando, so le dados publicos
da Binance e usa as MESMAS funcoes de analise (sem duplicar logica).
"""
import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from data_fetcher import get_klines
from engine_router import route
from signal_filters import has_structural_tag
from models import Direction
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

from config import WATCHLIST as _WL, WATCHLIST_VOLATILE as _WLV
WATCHLIST = list(dict.fromkeys(_WL + _WLV))

MIN_SCORE = 72.0
MIN_RR    = 2.0

YESTERDAY_UTC = (datetime.now(timezone.utc) - timedelta(days=1)).date()


def _make_session() -> aiohttp.ClientSession:
    # aiodns (c-ares) falha intermitentemente no DNS deste host Windows —
    # forca o resolver thread-based padrao do Python (getaddrinfo), que e
    # o mesmo usado pelo curl (que funciona).
    from aiohttp.resolver import ThreadedResolver
    connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
    return aiohttp.ClientSession(connector=connector)


async def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERR] TELEGRAM_TOKEN/CHAT_ID ausente", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            async with _make_session() as s:
                async with s.post(url, data={
                    "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"
                }) as r:
                    res = await r.json()
                    if not res.get("ok"):
                        print(f"[ERR] Telegram: {res}", flush=True)
                    return
        except Exception as e:
            print(f"[ERR] Telegram tentativa {attempt+1}: {e}", flush=True)
            await asyncio.sleep(2)


async def analyze_symbol(symbol: str, sem: asyncio.Semaphore):
    async with sem:
        try:
            df = await get_klines(symbol, "1h", limit=500)
        except Exception as e:
            print(f"[{symbol}] erro ao buscar klines: {e}", flush=True)
            return []

        if df is None or len(df) < 210:
            print(f"[{symbol}] candles insuficientes: {0 if df is None else len(df)}", flush=True)
            return []

        # indices cujo candle pertence a "ontem" (UTC)
        day_idx = [i for i in range(len(df)) if df.index[i].date() == YESTERDAY_UTC]
        print(f"[{symbol}] {len(df)} candles 1h | {len(day_idx)} de ontem ({YESTERDAY_UTC})", flush=True)
        if not day_idx:
            return []

        found = []
        last_dir_hour = {}  # evita 2 sinais da mesma direcao em horas vizinhas
        for i in day_idx:
            if i < 200:
                continue
            df_slice = df.iloc[: i + 1]
            try:
                sig = await route(symbol, "1h", df_slice, mode="NORMAL")
            except Exception as e:
                print(f"[{symbol}] erro route @ {df.index[i]}: {type(e).__name__}: {e}")
                continue
            if sig is None:
                continue

            score = float(sig.confidence)
            rr    = float(sig.rr)
            sig_dict = {"reason": sig.reason}
            has_tag = has_structural_tag(sig_dict)
            print(f"[{symbol}] RAW @ {df.index[i]} dir={sig.direction} score={score:.0f} "
                  f"rr={rr:.2f} tag={has_tag} reason={sig.reason[:60]}", flush=True)
            if score < MIN_SCORE or rr < MIN_RR or not has_tag:
                continue

            direction = sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)
            key = f"{direction}"
            if key in last_dir_hour and (i - last_dir_hour[key]) < 3:
                continue  # mesmo tipo de sinal em horas muito proximas — ignora duplicata
            last_dir_hour[key] = i

            entry_ts = df.index[i]
            entry    = float(sig.entry)
            sl       = float(sig.stop_loss)
            tp1      = float(sig.tp1)
            is_long  = direction == "LONG" or "LONG" in direction

            # Verifica o que aconteceu DEPOIS (candles seguintes, dados reais)
            outcome = "EM ANDAMENTO"
            outcome_pct = 0.0
            outcome_ts  = None
            for j in range(i + 1, len(df)):
                hi = float(df["high"].iloc[j])
                lo = float(df["low"].iloc[j])
                if is_long:
                    hit_tp = hi >= tp1
                    hit_sl = lo <= sl
                else:
                    hit_tp = lo <= tp1
                    hit_sl = hi >= sl
                if hit_tp and hit_sl:
                    # mesma vela tocou os dois — assume SL primeiro (conservador)
                    outcome, outcome_ts = "LOSS", df.index[j]
                    outcome_pct = (sl - entry) / entry * 100 if is_long else (entry - sl) / entry * 100
                    break
                if hit_tp:
                    outcome, outcome_ts = "WIN", df.index[j]
                    outcome_pct = (tp1 - entry) / entry * 100 if is_long else (entry - tp1) / entry * 100
                    break
                if hit_sl:
                    outcome, outcome_ts = "LOSS", df.index[j]
                    outcome_pct = (sl - entry) / entry * 100 if is_long else (entry - sl) / entry * 100
                    break

            if outcome == "EM ANDAMENTO":
                last_close = float(df["close"].iloc[-1])
                outcome_pct = (last_close - entry) / entry * 100 if is_long else (entry - last_close) / entry * 100

            found.append({
                "symbol": symbol, "direction": "LONG" if is_long else "SHORT",
                "entry_ts": entry_ts, "entry": entry, "sl": sl, "tp1": tp1,
                "score": score, "rr": rr, "reason": sig.reason,
                "outcome": outcome, "outcome_pct": outcome_pct, "outcome_ts": outcome_ts,
            })
        return found


async def main():
    sem = asyncio.Semaphore(4)
    results = await asyncio.gather(*[analyze_symbol(s, sem) for s in WATCHLIST])
    all_signals = [sig for sub in results for sig in sub]
    all_signals.sort(key=lambda s: s["entry_ts"])

    print(f"Total de sinais reconstruidos: {len(all_signals)}")
    for s in all_signals:
        print(s)

    header = (
        f"🕐 *VIAGEM NO TEMPO*\n"
        f"Reconstrução dos sinais de *1h* que o motor teria gerado em "
        f"*{YESTERDAY_UTC.strftime('%d/%m/%Y')}* (perfil NORMAL — score≥72, RR≥2.0, "
        f"tag estrutural V6 obrigatória)\n"
        f"_Esse timeframe só foi ativado hoje — isso é uma simulação retroativa "
        f"com dados reais da Binance, não um sinal que foi de fato enviado._"
    )
    await send_telegram(header)
    await asyncio.sleep(1)

    if not all_signals:
        await send_telegram(
            "❌ Nenhum sinal de 1h teria passado pelo filtro do perfil NORMAL ontem "
            "(score≥72 + RR≥2.0 + tag estrutural V6) nos 9 ativos da watchlist."
        )
        return

    wins   = [s for s in all_signals if s["outcome"] == "WIN"]
    losses = [s for s in all_signals if s["outcome"] == "LOSS"]
    open_  = [s for s in all_signals if s["outcome"] == "EM ANDAMENTO"]

    for s in all_signals:
        emoji = "🟢" if s["direction"] == "LONG" else "🔴"
        res_emoji = "✅ WIN" if s["outcome"] == "WIN" else ("❌ LOSS" if s["outcome"] == "LOSS" else "🟡 EM ANDAMENTO")
        msg = (
            f"{emoji} *{s['symbol']}* {s['direction']} | 1H\n"
            f"Horário (UTC): `{s['entry_ts'].strftime('%d/%m %H:%M')}`\n"
            f"Score: `{s['score']:.0f}` | R:R: `{s['rr']:.1f}`\n"
            f"Entrada: `${s['entry']:,.4f}` | Stop: `${s['sl']:,.4f}` | TP1: `${s['tp1']:,.4f}`\n"
            f"Resultado: {res_emoji} ({s['outcome_pct']:+.2f}%)"
        )
        await send_telegram(msg)
        await asyncio.sleep(1)

    total_pct = sum(s["outcome_pct"] for s in all_signals if s["outcome"] != "EM ANDAMENTO")
    wr = (len(wins) / (len(wins) + len(losses)) * 100) if (wins or losses) else 0.0
    summary = (
        f"📊 *RESUMO DA VIAGEM NO TEMPO — {YESTERDAY_UTC.strftime('%d/%m/%Y')}*\n"
        f"Sinais reconstruídos: `{len(all_signals)}`\n"
        f"✅ Wins: `{len(wins)}`   ❌ Losses: `{len(losses)}`   🟡 Em andamento: `{len(open_)}`\n"
        f"Win rate (decididos): `{wr:.1f}%`\n"
        f"PnL acumulado (sem alavancagem): `{total_pct:+.2f}%`"
    )
    await send_telegram(summary)


if __name__ == "__main__":
    asyncio.run(main())
