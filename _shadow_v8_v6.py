"""
Shadow test V8+V6 (jun/2026, a pedido do usuario) -- PROCESSO SEPARADO, NAO toca no
bot 001 que ja esta rodando (PID distinto). So LE dados ao vivo da Binance e usa o
motor de score REAL do bot (signal_engine.analyze_asset) para o regime TRENDING +
uma camada de fade de RSI extremo (V8 RANGING) pro regime de lateralizacao -- a
mesma logica "DNA decide o mecanismo" validada no backtest, agora rodando com o
codigo de producao real em vez da replica simplificada do engine.py da sandbox.

Envia SOMENTE pro chat pessoal (TELEGRAM_CHAT_ID) -- NUNCA pro canal publico nem pro
grupo VIP. Isso e feito sobrescrevendo TELEGRAM_VIP_ID/TELEGRAM_CHANNEL_ID SO na
memoria deste processo (zero efeito no processo real do bot, que tem sua propria
copia separada dessas variaveis).
"""
import asyncio
import json
import time
import datetime as dt
import pandas as pd
import numpy as np

import config
config.TELEGRAM_VIP_ID = ""
config.TELEGRAM_CHANNEL_ID = ""

import notifier
import signal_engine as se
from data_fetcher import get_klines
from models import Direction

UNIVERSE = ["SOLUSDT", "HYPEUSDT", "ZECUSDT", "XRPUSDT", "DOGEUSDT", "NEARUSDT",
            "1000PEPEUSDT", "SUIUSDT", "ADAUSDT", "AVAXUSDT", "TAOUSDT", "ENAUSDT",
            "AAVEUSDT", "XLMUSDT", "ARBUSDT", "LINKUSDT", "WLDUSDT"]
TFS = ["3m", "5m", "15m"]
DNA_THRESHOLD = 0.05
RSI_EXTREME = 20.0
SR_SKIP_EVERY = 4
LOOP_INTERVAL_S = 30

LOG_PATH = "_shadow_v8_v6_log.jsonl"
seen = set()


def dna_regime(close: pd.Series) -> float:
    ret = close.pct_change()
    if len(ret.dropna()) < 101:
        return 0.0
    r0 = ret.iloc[-100:]
    r1 = ret.shift(1).iloc[-100:]
    if r0.std() == 0 or r1.std() == 0:
        return 0.0
    return float(r0.corr(r1))


async def get_btc_rel_strength(symbol_df: pd.DataFrame, tf: str) -> float:
    try:
        btc_df = await get_klines("BTCUSDT", tf, limit=30)
        sym_ret = symbol_df["close"].iloc[-1] / symbol_df["close"].iloc[-15] - 1
        btc_ret = btc_df["close"].iloc[-1] / btc_df["close"].iloc[-15] - 1
        return float(sym_ret - btc_ret)
    except Exception:
        return 0.0


async def try_ranging_fade(symbol: str, tf: str, df: pd.DataFrame, bar_idx: int) -> dict | None:
    """Fade de RSI extremo (V8 RANGING) -- mesma logica validada no backtest,
    usando as funcoes REAIS de indicador do signal_engine (rsi/atr/bollinger_bands)."""
    close = df["close"]
    high = df["high"]
    low = df["low"]
    i = bar_idx
    if i < 30:
        return None
    rsi_s = se.rsi(close)
    atr_s = se.atr(df)
    bb_u, bb_m, bb_l = se.bollinger_bands(close)
    vol = df["volume"]
    vol_ratio = float(vol.iloc[i] / vol.iloc[max(0, i - 20):i].mean()) if vol.iloc[max(0, i - 20):i].mean() else 1.0

    atr_pct = float(atr_s.iloc[i] / close.iloc[i] * 100) if close.iloc[i] else 0.0
    if atr_pct < 0.65:
        return None

    atr_price0 = atr_s.iloc[i]
    support = low.iloc[i - 30:i].min()
    resistance = high.iloc[i - 30:i].max()
    near_support = (close.iloc[i] - support) <= 0.5 * atr_price0
    near_resistance = (resistance - close.iloc[i]) <= 0.5 * atr_price0
    sr_required = (i % SR_SKIP_EVERY) != 0

    rsi_now = rsi_s.iloc[i]
    vol_confirm = vol_ratio >= 1.2
    bb_long_confirm = close.iloc[i] <= bb_l.iloc[i] * 1.002
    bb_short_confirm = close.iloc[i] >= bb_u.iloc[i] * 0.998

    rel_strength = await get_btc_rel_strength(df, tf) if symbol != "BTCUSDT" else 0.0
    rs_not_against_long = rel_strength >= -0.004
    rs_not_against_short = rel_strength <= 0.004

    long_fade = (rsi_now <= RSI_EXTREME and close.iloc[i] > close.iloc[i - 1] and close.iloc[i - 1] <= close.iloc[i - 2]
                 and (near_support or not sr_required) and vol_confirm and bb_long_confirm and rs_not_against_long)
    short_fade = (rsi_now >= (100.0 - RSI_EXTREME) and close.iloc[i] < close.iloc[i - 1] and close.iloc[i - 1] >= close.iloc[i - 2]
                  and (near_resistance or not sr_required) and vol_confirm and bb_short_confirm and rs_not_against_short)

    direction = None
    if long_fade:
        direction = Direction.LONG
    elif short_fade:
        direction = Direction.SHORT
    if direction is None:
        return None

    levels = se.calculate_levels(df.iloc[:i + 1], direction, symbol, tf)
    if not levels or levels.get("rr", 0) < 1.2:
        return None

    return {
        "asset": symbol, "direction": direction.value, "entry": levels["entry"],
        "stop_loss": levels["stop_loss"], "tp1": levels["tp1"], "tp2": levels["tp2"],
        "tp3": levels["tp3"], "rr": levels["rr"], "confidence": 70.0,
        "reason": f"[V8-RANGE|FADE] RSI {rsi_now:.1f} | vol {vol_ratio:.2f}x | "
                  f"S/R {'perto' if (near_support or near_resistance) else 'longe'}",
        "score": se.SignalScore(trend=0, volume=0, momentum=0, market_structure=0,
                                 funding_oi=0, news_context=0, total_override=70.0),
        "timeframe": tf, "trade_type": "SCALP", "body_pct": 0.0, "vol_ratio": vol_ratio,
        "rsi_val": float(rsi_now), "confirmed_signals": ["V8-RANGE-FADE"],
        "perfil": "AGGRESSIVE", "mode": "SINAIS",
    }


async def scan_one(symbol: str, tf: str):
    try:
        df = await get_klines(symbol, tf, limit=300)
    except Exception as e:
        return None, f"erro download: {e}", None
    if df is None or len(df) < 151:
        return None, "poucos dados", None

    # FIX (jun/2026): get_klines traz a vela ATUAL ainda em formacao como ultima
    # linha -- analisar ela faz o sinal "mudar" a cada poucos segundos (preco
    # mexendo) e disparar repetido pro mesmo candle. Descarta a ultima linha,
    # analisa so a ultima vela JA FECHADA (igual ao backtest, sem look-ahead).
    df = df.iloc[:-1]
    bar_ts = df.index[-1]

    ac = dna_regime(df["close"])
    i = len(df) - 1

    if ac > DNA_THRESHOLD:
        try:
            sig = await se.analyze_asset(symbol, tf, direction=None, mode="AGGRESSIVE", df=df)
        except Exception as e:
            return None, f"erro analyze_asset: {e}"
        if sig is None:
            return None, None, bar_ts
        d = sig.model_dump() if hasattr(sig, "model_dump") else sig.dict()
        d["score_total"] = sig.score.total
        d["perfil"] = "AGGRESSIVE"
        d["mode"] = "SINAIS"
        d["reason"] = "[V8-TREND|V6-REAL] " + d.get("reason", "")
        return d, "TRENDING", bar_ts
    elif ac < -DNA_THRESHOLD:
        d = await try_ranging_fade(symbol, tf, df, i)
        return d, ("RANGING" if d else None), bar_ts
    else:
        return None, None, bar_ts


async def main_loop():
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] Shadow V8+V6 iniciado -- "
          f"{len(UNIVERSE)} ativos x {len(TFS)} TFs, perfil AGGRESSIVE, "
          f"envio restrito ao chat pessoal.", flush=True)
    n_loop = 0
    n_sent = 0
    while True:
        n_loop += 1
        t0 = time.time()
        for symbol in UNIVERSE:
            for tf in TFS:
                sig, regime, bar_ts = await scan_one(symbol, tf)
                if sig is None:
                    continue
                dedup_key = (symbol, tf, str(bar_ts), sig["direction"])
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                n_sent += 1
                ts_now = dt.datetime.now().strftime("%H:%M:%S")
                print(f"[{ts_now}] SINAL V8+V6 ({regime}) -> {symbol}/{tf} "
                      f"{sig['direction']} entry={sig['entry']} sl={sig['stop_loss']} "
                      f"tp={sig.get('tp1')} score={sig.get('score_total', sig.get('confidence'))}",
                      flush=True)
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": ts_now, "regime": regime, **{k: v for k, v in sig.items() if k != "score"}},
                                        default=str, ensure_ascii=False) + "\n")
                try:
                    await notifier.send_sinais_alert(sig)
                except Exception as e:
                    print(f"[{ts_now}] erro ao enviar telegram: {e}", flush=True)
        elapsed = time.time() - t0
        print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] loop {n_loop} ok "
              f"({elapsed:.1f}s, {n_sent} sinais enviados ate agora)", flush=True)
        await asyncio.sleep(max(0, LOOP_INTERVAL_S - elapsed))


if __name__ == "__main__":
    asyncio.run(main_loop())
