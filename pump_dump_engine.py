"""
Pump & Dump Detection Engine v4
Multi-timeframe scanner: 3m, 5m, 15m.
MTF confluence: mesmo ativo em 2+ TFs = bonus de confiança.
Sustentação: FRESH | CONTINUATION | DISTRIBUTION.

Gates obrigatórios (por ativo, por TF):
  1. Volume >= 3x média (ajustado por sessão)
  2. Variação >= 1x ATR% OU vela ≥ 5% absoluto
  3. Fechamento coerente com direção (sem rejeição dominante)
  4. Fora da janela de funding settlement (00/08/16h UTC ±3min)

Cobertura ampliada:
  - TOP_N por volume + big movers ±5% com volume mínimo $300k

OI enrichment:
  - Alertas ≥50 conf recebem direção de OI (ORGANIC/SQUEEZE/EXHAUSTION)

Thresholds finais:
  MODERADO : 35–49
  FORTE    : 50–74
  EXTREMO  : >= 75 (exige MTF confluence em 2+ TFs)
"""
import asyncio
import time
import datetime
import aiohttp
import pandas as pd
from typing import Optional

from data_fetcher import get_all_tickers
from klines_cache import get_klines_cached as get_klines

# ── Config ────────────────────────────────────────────────────────────────────

SCAN_TIMEFRAMES = [
    ("3m",  0.80, "3 min"),
    ("5m",  0.90, "5 min"),
    ("15m", 1.00, "15 min"),
]

TOP_N                = 100     # top por volume (liquidez)
BIG_MOVER_CHANGE_PCT = 5.0     # inclui movers ±5%+ mesmo fora do top-volume
MIN_DAILY_VOL_USD    = 300_000 # piso mínimo $300k/dia para big movers
_PD_CACHE_TTL        = 60      # 60s — 3m exige frescor

_pd_cache:    list  = []
_pd_cache_ts: float = 0.0
_oi_cache:    dict  = {}       # symbol → último OI conhecido

_pd_alert_cooldown: dict = {}
PD_ALERT_COOLDOWN_S = 300  # 5 min — permite re-alertar continuações


# ── Session context ───────────────────────────────────────────────────────────

def _session_context() -> dict:
    h = datetime.datetime.utcnow().hour
    if 13 <= h < 17:
        return {"name": "NY",   "vol_gate_mult": 1.0, "score_bonus":  5}
    elif 7 <= h < 13:
        return {"name": "EU",   "vol_gate_mult": 1.0, "score_bonus":  2}
    elif 1 <= h < 7:
        return {"name": "ASIA", "vol_gate_mult": 1.1, "score_bonus":  0}
    else:
        # NY Close + noite: reduzido de 1.6 → 1.2 (ainda ativo, não morto)
        return {"name": "DEAD", "vol_gate_mult": 1.2, "score_bonus": -3}


# ── Funding settlement window ─────────────────────────────────────────────────

def _is_funding_window() -> bool:
    """
    Retorna True se estamos dentro da janela de funding settlement.
    Funding ocorre a cada 8h: 00:00, 08:00, 16:00 UTC.
    Nos ±3 min dessas janelas, spikes de volume são artefatos, não pumps reais.
    """
    now = datetime.datetime.utcnow()
    m   = now.hour * 60 + now.minute
    return any(abs(m - fm) <= 3 for fm in (0, 480, 960))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain.iloc[-1] / (loss.iloc[-1] or 1e-9)
    return round(100 - 100 / (1 + rs), 1)


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]


def _pre_accum_score(vol: pd.Series, avg_vol: float) -> tuple[float, bool]:
    """
    Detecção de acumulação silenciosa nas 4 velas ANTES do spike.
    Padrão clássico: volume cresce progressivamente antes do explosão.
    Retorna (score 0-1, confirmado: bool).
    """
    if len(vol) < 6:
        return 0.0, False
    pre = [float(vol.iloc[i]) for i in range(-5, -1)]
    above_avg = sum(1 for v in pre if v > avg_vol * 1.2)
    ascending  = sum(1 for i in range(1, len(pre)) if pre[i] > pre[i - 1] * 1.05)
    score      = ascending / (len(pre) - 1)
    confirmed  = above_avg >= 2 and ascending >= 2
    return round(score, 2), confirmed


def _close_position(last: pd.Series) -> float:
    """Onde o preço fechou dentro do range (0=mínima, 1=máxima)."""
    high  = float(last["high"])
    low   = float(last["low"])
    close = float(last["close"])
    rng   = high - low
    if rng <= 0:
        return 0.5
    return round((close - low) / rng, 2)


def _sustentation(df: pd.DataFrame, price_gate: float, direction: float) -> str:
    """
    Classifica o que aconteceu COM o movimento atual nas velas seguintes.

    FRESH        — spike acontecendo agora (candle atual)
    CONTINUATION — vela anterior também subiu/caiu na mesma direção (momentum)
    DISTRIBUTION — vela anterior reverteu mais de 50% do spike anterior (falso pump)

    'direction' > 0 → pump sendo analisado, < 0 → dump.
    """
    if len(df) < 3:
        return "FRESH"

    # Candle -2: o anterior ao spike atual (-1)
    prev       = df.iloc[-2]
    prev_move  = (float(prev["close"]) / float(prev["open"]) - 1) * 100 \
                  if float(prev["open"]) > 0 else 0.0

    same_dir   = (direction > 0 and prev_move > 0) or (direction < 0 and prev_move < 0)
    oppos_dir  = (direction > 0 and prev_move < 0) or (direction < 0 and prev_move > 0)

    # Candle anterior foi um spike relevante na mesma direção → estamos em continuação
    if abs(prev_move) >= price_gate * 0.7 and same_dir:
        return "CONTINUATION"

    # Procura spike 2 candles atrás que possa ter sido distribuído
    if len(df) >= 4:
        prev2      = df.iloc[-3]
        prev2_move = (float(prev2["close"]) / float(prev2["open"]) - 1) * 100 \
                      if float(prev2["open"]) > 0 else 0.0

        if abs(prev2_move) >= price_gate:
            spike_open  = float(prev2["open"])
            spike_close = float(prev2["close"])
            current     = float(df.iloc[-1]["close"])
            midpoint    = (spike_open + spike_close) / 2

            if prev2_move > 0:   # era pump
                return "CONTINUATION" if current > midpoint else "DISTRIBUTION"
            else:                # era dump
                return "CONTINUATION" if current < midpoint else "DISTRIBUTION"

    return "FRESH"


# ── Core analyzer ─────────────────────────────────────────────────────────────

def analyze_symbol(
    df: pd.DataFrame,
    symbol: str,
    tf: str = "15m",
    tf_weight: float = 1.0,
) -> Optional[dict]:
    """
    Analisa um DataFrame de OHLCV e retorna alerta ou None.

    GATE 1: volume >= 3x média ajustado por sessão
    GATE 2: variação >= 1x ATR% do ativo (relativo, não 1.5% fixo)
    GATE 3: fechamento coerente com a direção (sem rejeição dominante)
    """
    if df is None or len(df) < 50:
        return None

    close  = df["close"]
    vol    = df["volume"]
    open_  = df["open"]
    price  = float(close.iloc[-1])
    last   = df.iloc[-1]

    session = _session_context()

    # ── GATE 4: Funding settlement window — spikes artificiais ────────────────
    if _is_funding_window():
        return None

    # ── GATE 1: Volume ────────────────────────────────────────────────────────
    avg_vol = vol.rolling(20).mean().iloc[-1]
    cur_vol = float(vol.iloc[-1])
    rel_vol = cur_vol / avg_vol if avg_vol > 0 else 0.0

    vol_min = 3.0 * session["vol_gate_mult"]
    if rel_vol < vol_min:
        return None

    # ── GATE 2: Variação relativa ao ATR ─────────────────────────────────────
    atr_val  = _atr(df, 14)
    atr_pct  = (atr_val / price * 100) if price > 0 else 2.0
    price_gate = max(1.0, min(4.0, atr_pct * 1.0))

    price_1c = (float(last["close"]) / float(last["open"]) - 1) * 100 \
               if float(last["open"]) > 0 else 0.0

    # Vela ≥ 5% passa automaticamente (override absoluto do gate relativo)
    big_candle = abs(price_1c) >= 5.0

    if not big_candle and abs(price_1c) < price_gate:
        return None

    # ── GATE 3: Posição do fechamento ─────────────────────────────────────────
    # Vela ≥ 5%: aceita fechamento mais amplo (min 30% do range)
    close_pos  = _close_position(last)
    _close_min = 0.30 if big_candle else 0.40
    _close_max = 0.70 if big_candle else 0.60
    if price_1c > 0 and close_pos < _close_min:
        return None   # pump mas fechou no fundo — rejeição
    if price_1c < 0 and close_pos > _close_max:
        return None   # dump mas fechou no topo — recuperação

    # ── Métricas adicionais ───────────────────────────────────────────────────
    ret_recent    = (close.iloc[-1] / close.iloc[-4] - 1) * 100 if len(df) >= 4 else 0.0
    rsi_val       = _rsi(close, 14)
    rsi_prev_val  = _rsi(close.iloc[:-1], 14) if len(close) > 15 else rsi_val
    rsi_delta     = abs(rsi_val - rsi_prev_val)
    # Volume sustentado: vela anterior também acima de 1.5x média
    vol_sustained = (float(vol.iloc[-2]) >= avg_vol * 1.5) if len(vol) >= 2 else False
    body          = abs(float(last["close"]) - float(last["open"]))
    rng_         = float(last["high"]) - float(last["low"])
    body_pct_rng = body / rng_ if rng_ > 0 else 0.0
    prev_vol     = float(vol.iloc[-2]) if len(vol) >= 2 else cur_vol
    vol_accel    = cur_vol / prev_vol if prev_vol > 0 else 1.0
    move_in_atrs = abs(price_1c) / atr_pct if atr_pct > 0 else 1.0

    avg_body  = (close - open_).abs().rolling(20).mean().iloc[-1]
    consec_up = consec_down = 0
    for i in range(-1, -6, -1):
        row = df.iloc[i]
        b   = abs(row["close"] - row["open"])
        v   = row["volume"]
        if row["close"] > row["open"] and b > avg_body * 1.1 and v > avg_vol * 1.5:
            consec_up += 1
        else:
            break
    for i in range(-1, -6, -1):
        row = df.iloc[i]
        b   = abs(row["close"] - row["open"])
        v   = row["volume"]
        if row["close"] < row["open"] and b > avg_body * 1.1 and v > avg_vol * 1.5:
            consec_down += 1
        else:
            break

    atr_recent    = _atr(df.iloc[-14:], 5) if len(df) >= 19 else atr_val
    atr_hist      = _atr(df.iloc[-50:-14], 14) if len(df) >= 64 else atr_val
    vol_expansion = atr_recent / atr_hist if atr_hist > 0 else 1.0

    ema9  = close.ewm(span=9,  adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    trend_up   = ema9.iloc[-1] > ema21.iloc[-1] and ema9.iloc[-1] > ema9.iloc[-3]
    trend_down = ema9.iloc[-1] < ema21.iloc[-1] and ema9.iloc[-1] < ema9.iloc[-3]

    pre_score, pre_confirmed = _pre_accum_score(vol, avg_vol)
    sust = _sustentation(df, price_gate, price_1c)

    # ── Scoring ───────────────────────────────────────────────────────────────
    pump_score = 0
    dump_score = 0
    signals: list[str] = []

    # 0. Bônus especial: vela ≥ 5% (movimento absoluto significativo)
    if big_candle:
        bonus_5pct = min(25, int(abs(price_1c) * 3))  # 5%→15pts, 8%→24pts, 9%+→25pts
        if price_1c > 0:
            pump_score += bonus_5pct
            signals.append(f"🕯️ VELA +{price_1c:.1f}% — valorização absoluta expressiva")
        else:
            dump_score += bonus_5pct
            signals.append(f"🕯️ VELA {price_1c:.1f}% — queda absoluta expressiva")

    # 1. Volume magnitude
    if rel_vol >= 10.0:
        pump_score += 35; dump_score += 35
        signals.append(f"Volume EXPLOSIVO {rel_vol:.1f}x acima da media")
    elif rel_vol >= 6.0:
        pump_score += 28; dump_score += 28
        signals.append(f"Volume EXTREMO {rel_vol:.1f}x acima da media")
    elif rel_vol >= 4.0:
        pump_score += 20; dump_score += 20
        signals.append(f"Volume forte {rel_vol:.1f}x acima da media")
    else:
        pump_score += 12; dump_score += 12
        signals.append(f"Volume elevado {rel_vol:.1f}x acima da media")

    # 2. Variação normalizada por ATR (movimento real para ESTE ativo)
    if move_in_atrs >= 3.0:
        if price_1c > 0: pump_score += 30
        else:            dump_score += 30
        signals.append(f"Vela {price_1c:+.1f}% = {move_in_atrs:.1f}x ATR — extremo")
    elif move_in_atrs >= 2.0:
        if price_1c > 0: pump_score += 22
        else:            dump_score += 22
        signals.append(f"Vela {price_1c:+.1f}% = {move_in_atrs:.1f}x ATR — forte")
    elif move_in_atrs >= 1.5:
        if price_1c > 0: pump_score += 14
        else:            dump_score += 14
        signals.append(f"Vela {price_1c:+.1f}% = {move_in_atrs:.1f}x ATR")
    else:
        if price_1c > 0: pump_score += 7
        else:            dump_score += 7
        signals.append(f"Vela {price_1c:+.1f}% = {move_in_atrs:.1f}x ATR")

    # 3. Posição do fechamento (gates já eliminaram rejeições extremas)
    if price_1c > 0:
        if close_pos >= 0.85:
            pump_score += 15
            signals.append(f"Fechamento no topo do range ({close_pos*100:.0f}%) — demanda pura")
        elif close_pos >= 0.70:
            pump_score += 10
            signals.append(f"Fechamento alto ({close_pos*100:.0f}% do range)")
        elif close_pos >= 0.55:
            pump_score += 5
    else:
        if close_pos <= 0.15:
            dump_score += 15
            signals.append(f"Fechamento no fundo do range ({close_pos*100:.0f}%) — pressao pura")
        elif close_pos <= 0.30:
            dump_score += 10
            signals.append(f"Fechamento baixo ({close_pos*100:.0f}% do range)")
        elif close_pos <= 0.45:
            dump_score += 5

    # 4. Acumulação pré-spike (intencionalidade)
    if pre_confirmed:
        pump_score += 18; dump_score += 18
        signals.append(f"Acumulacao pre-spike confirmada ({pre_score*100:.0f}%)")
    elif pre_score >= 0.5:
        pump_score += 8; dump_score += 8
        signals.append(f"Acumulacao parcial antes do spike ({pre_score*100:.0f}%)")

    # 5. Sustentação (penaliza distribuição, bonifica continuação)
    if sust == "CONTINUATION":
        pump_score += 12; dump_score += 12
        signals.append("Continuacao — vela anterior confirmou mesma direcao")
    elif sust == "DISTRIBUTION":
        pump_score -= 15; dump_score -= 15
        signals.append("Atencao: distribuicao detectada — movimento anterior reverteu")

    # 6. Corpo da vela
    if body_pct_rng >= 0.75:
        if price_1c > 0: pump_score += 12
        else:            dump_score += 12
        signals.append(f"Vela cheia {body_pct_rng*100:.0f}% corpo/range")
    elif body_pct_rng >= 0.60:
        if price_1c > 0: pump_score += 7
        else:            dump_score += 7

    # 7. Aceleração de volume
    if vol_accel >= 3.0:
        pump_score += 10; dump_score += 10
        signals.append(f"Volume acelerou {vol_accel:.1f}x vs vela anterior")
    elif vol_accel >= 2.0:
        pump_score += 5; dump_score += 5
        signals.append(f"Volume acelerou {vol_accel:.1f}x vs vela anterior")

    # 8. Aceleração de preço em 3 velas
    if ret_recent >= 5.0:
        pump_score += 15
        signals.append(f"Movimento +{ret_recent:.1f}% nas ultimas 3 velas")
    elif ret_recent >= 3.0:
        pump_score += 8
        signals.append(f"Movimento +{ret_recent:.1f}% em 3 velas")
    elif ret_recent <= -5.0:
        dump_score += 15
        signals.append(f"Queda {ret_recent:.1f}% nas ultimas 3 velas")
    elif ret_recent <= -3.0:
        dump_score += 8
        signals.append(f"Queda {ret_recent:.1f}% em 3 velas")

    # 9. RSI extremo
    if rsi_val >= 82:
        pump_score += 10
        signals.append(f"RSI extremo {rsi_val:.0f} — sobrecomprado")
    elif rsi_val >= 74:
        pump_score += 5
        signals.append(f"RSI elevado {rsi_val:.0f}")
    elif rsi_val <= 18:
        dump_score += 10
        signals.append(f"RSI extremo {rsi_val:.0f} — sobrevendido")
    elif rsi_val <= 26:
        dump_score += 5
        signals.append(f"RSI baixo {rsi_val:.0f}")

    # 10b. Delta RSI — aceleração de momentum (novo)
    if rsi_delta >= 20:
        pump_score += 12; dump_score += 12
        signals.append(f"RSI acelerou {rsi_delta:.0f}pts em 1 vela — momentum real")
    elif rsi_delta >= 12:
        pump_score += 7; dump_score += 7
        signals.append(f"RSI acelerou {rsi_delta:.0f}pts — impulso forte")

    # 10c. Volume sustentado — 2ª vela também acima da média (novo)
    if vol_sustained:
        pump_score += 8; dump_score += 8
        signals.append("Volume sustentado — vela anterior também elevada")

    # 10. Sequência de velas consecutivas
    if consec_up >= 3:
        pump_score += 12
        signals.append(f"{consec_up} velas altas seguidas com volume acima da media")
    elif consec_up == 2:
        pump_score += 5
    if consec_down >= 3:
        dump_score += 12
        signals.append(f"{consec_down} velas baixas seguidas com volume acima da media")
    elif consec_down == 2:
        dump_score += 5

    # 11. Expansão de volatilidade histórica
    if vol_expansion >= 2.5:
        pump_score += 8; dump_score += 8
        signals.append(f"Volatilidade {vol_expansion:.1f}x acima da media historica")

    # 12. Tendência EMA
    if trend_up:   pump_score += 5
    if trend_down: dump_score += 5

    # 13. Contexto de sessão
    bonus = session["score_bonus"]
    if bonus != 0:
        pump_score += bonus; dump_score += bonus

    # 14. Peso do timeframe (TFs menores têm base ligeiramente menor)
    pump_score = pump_score * tf_weight
    dump_score = dump_score * tf_weight

    best_score = max(pump_score, dump_score)
    if best_score < 35:
        return None

    alert_type = "PUMP" if pump_score >= dump_score else "DUMP"
    confidence = min(100, int(best_score))

    if confidence >= 75:
        intensity = "EXTREMO"
    elif confidence >= 50:
        intensity = "FORTE"
    else:
        intensity = "MODERADO"

    rec = _build_recommendation(alert_type, intensity, rsi_val, price_1c, sust)

    return {
        "symbol":        symbol,
        "tf":            tf,
        "type":          alert_type,
        "intensity":     intensity,
        "confidence":    confidence,
        "price":         round(price, 6),
        "rel_volume":    round(rel_vol, 2),
        "rsi":           round(rsi_val, 1),
        "price_acc":     round(ret_recent, 2),
        "price_1c":      round(price_1c, 2),
        "move_in_atrs":  round(move_in_atrs, 2),
        "close_pos":     round(close_pos, 2),
        "body_pct_rng":  round(body_pct_rng, 2),
        "vol_accel":     round(vol_accel, 2),
        "pre_accum":     pre_confirmed,
        "pre_score":     pre_score,
        "sustentation":  sust,
        "consec_up":     consec_up,
        "consec_down":   consec_down,
        "vol_expansion": round(vol_expansion, 2),
        "session":       session["name"],
        "big_candle":    big_candle,
        "rsi_delta":     round(rsi_delta, 1),
        "vol_sustained": vol_sustained,
        "oi_signal":     "PENDING",   # preenchido por _enrich_oi
        "oi_change_pct": 0.0,
        "signals":       signals,
        "recommendation": rec,
        "ts":            int(time.time()),
    }


def _build_recommendation(
    alert_type: str, intensity: str, rsi: float, price_1c: float, sust: str
) -> str:
    dist_note = " ⚠️ Distribuicao detectada — movimento pode estar exausto." if sust == "DISTRIBUTION" else ""
    cont_note = " Continuacao confirmada — momentum ativo." if sust == "CONTINUATION" else ""

    if alert_type == "PUMP":
        if intensity == "EXTREMO":
            return (
                "NAO entrar agora — vela explosiva no topo."
                + dist_note + cont_note
                + " Aguardar reteste da zona (candle anterior). "
                "Short viavel apenas apos reversao confirmada + volume caindo."
            )
        elif intensity == "FORTE":
            if rsi >= 78:
                return (
                    "RSI sobrecomprado com volume forte — risco de exaustao."
                    + dist_note
                    + " Aguardar pullback ate suporte ou OB. Nao perseguir preco."
                )
            return (
                "Pump com momentum real." + cont_note
                + " Aguardar consolidacao de 2-3 velas. "
                "LONG apenas no reteste com volume menor."
            )
        else:
            return (
                "Volume elevado, movimento moderado." + dist_note
                + " Monitorar proximas velas para confirmar direcao."
            )
    else:
        if intensity == "EXTREMO":
            return (
                "Dump agressivo — NAO long agora." + dist_note + cont_note
                + " Aguardar RSI < 28 e fundo (vela de reversao + volume caindo). "
                "Short so abaixo do suporte confirmado."
            )
        elif intensity == "FORTE":
            return (
                "Queda forte com volume real." + cont_note
                + " Short valido abaixo do suporte imediato. "
                "Aguardar estabilizacao antes de LONG."
            )
        else:
            return (
                "Pressao vendedora com volume acima da media." + dist_note
                + " Observar se volume arrefece nas proximas velas."
            )


# ── Multi-TF scanner ──────────────────────────────────────────────────────────

async def _scan_tf(symbols: list, tf: str, tf_weight: float) -> dict[str, dict]:
    """Escaneia todos os símbolos em um único timeframe. Retorna {symbol: alert}."""
    results: dict[str, dict] = {}
    for i in range(0, len(symbols), 10):
        batch = symbols[i:i + 10]
        tasks = [get_klines(s, tf, limit=100) for s in batch]
        dfs   = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, df in zip(batch, dfs):
            if isinstance(df, Exception) or df is None:
                continue
            alert = analyze_symbol(df, sym, tf=tf, tf_weight=tf_weight)
            if alert:
                results[sym] = alert
        await asyncio.sleep(0.05)
    return results


async def _enrich_oi(alerts: list[dict]) -> list[dict]:
    """
    Enriquece alertas com confidence >= 50 com direção do OI.
    ORGANIC   : OI subiu junto com preço → dinheiro novo, pump sustentável
    SQUEEZE   : OI caiu com preço subindo → short squeeze, pump vai exaurir
    EXHAUSTION: OI caiu com preço caindo → longs liquidando, pode reverter
    """
    strong = [a for a in alerts if a.get("confidence", 0) >= 50]
    if not strong:
        return alerts

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as sess:
            for alert in strong:
                sym = alert["symbol"]
                try:
                    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}"
                    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                        if r.status != 200:
                            continue
                        data  = await r.json(content_type=None)
                        oi_now  = float(data.get("openInterest", 0))
                        oi_prev = _oi_cache.get(sym, oi_now)
                        oi_chg  = (oi_now - oi_prev) / oi_prev * 100 if oi_prev > 0 else 0.0
                        _oi_cache[sym] = oi_now

                        alert["oi_change_pct"] = round(oi_chg, 2)

                        pd_type = alert.get("type", "PUMP")
                        if pd_type == "PUMP":
                            if oi_chg > 2:
                                signal = "ORGANIC"    # dinheiro novo entrando long
                                alert["confidence"] = min(100, alert["confidence"] + 8)
                            elif oi_chg < -2:
                                signal = "SQUEEZE"    # shorts fechando — pump efêmero
                            else:
                                signal = "NEUTRAL"
                        else:  # DUMP
                            if oi_chg > 2:
                                signal = "ORGANIC"    # dinheiro novo entrando short
                                alert["confidence"] = min(100, alert["confidence"] + 8)
                            elif oi_chg < -2:
                                signal = "EXHAUSTION" # longs liquidando — pode reverter
                            else:
                                signal = "NEUTRAL"

                        alert["oi_signal"] = signal

                        # Recalcula intensidade se OI boosteou a confiança
                        conf = alert["confidence"]
                        if conf >= 75:
                            alert["intensity"] = "EXTREMO"
                        elif conf >= 50:
                            alert["intensity"] = "FORTE"

                except Exception:
                    pass
    except Exception:
        pass

    return alerts


async def scan_pump_dump(force: bool = False) -> list:
    """
    Escaneia 3m, 5m e 15m em paralelo.
    Se o mesmo ativo aparece em múltiplos TFs → MTF confluence bonus (+15 pts por TF extra).
    """
    global _pd_cache, _pd_cache_ts

    now = time.time()
    if not force and (now - _pd_cache_ts) < _PD_CACHE_TTL:
        return _pd_cache

    try:
        tickers = await get_all_tickers()
    except Exception:
        return _pd_cache

    all_usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
    top_by_vol = sorted(
        all_usdt,
        key=lambda x: float(x.get("quoteVolume", x.get("volume", 0)) or 0),
        reverse=True,
    )[:TOP_N]
    # Big movers: qualquer ticker com |change%| >= 5% e volume >= $300k (pega AGT-style pumps)
    big_movers = [
        t for t in all_usdt
        if abs(float(t.get("priceChangePercent", 0) or 0)) >= BIG_MOVER_CHANGE_PCT
        and float(t.get("quoteVolume", t.get("volume", 0)) or 0) >= MIN_DAILY_VOL_USD
    ]
    symbols = list(dict.fromkeys(
        [t["symbol"] for t in top_by_vol] + [t["symbol"] for t in big_movers]
    ))

    # Escaneia todos os TFs em paralelo
    tf_tasks = [_scan_tf(symbols, tf, w) for tf, w, _ in SCAN_TIMEFRAMES]
    tf_results: list[dict] = list(await asyncio.gather(*tf_tasks))

    # Merge por símbolo — guarda o alerta de maior confiança e acumula TFs
    merged: dict[str, dict] = {}
    for (tf, _, tf_label), sym_map in zip(SCAN_TIMEFRAMES, tf_results):
        for sym, alert in sym_map.items():
            if sym not in merged:
                merged[sym] = dict(alert)
                merged[sym]["timeframes"]  = [tf]
                merged[sym]["tf_labels"]   = [tf_label]
                merged[sym]["mtf_count"]   = 1
            else:
                merged[sym]["timeframes"].append(tf)
                merged[sym]["tf_labels"].append(tf_label)
                merged[sym]["mtf_count"] += 1
                # MTF confluence: adiciona 15 pts por TF extra (máx 100)
                new_conf = min(100, merged[sym]["confidence"] + 15)
                merged[sym]["confidence"] = new_conf
                # Atualiza intensidade se subiu
                if new_conf >= 75:
                    merged[sym]["intensity"] = "EXTREMO"
                elif new_conf >= 50:
                    merged[sym]["intensity"] = "FORTE"
                # Usa o maior TF como referência principal (mais confiável)
                if alert["confidence"] > merged[sym].get("_base_conf", 0):
                    merged[sym].update({k: v for k, v in alert.items()
                                       if k not in ("timeframes", "tf_labels", "mtf_count", "confidence")})
                    merged[sym]["_base_conf"] = alert["confidence"]

    # Remove campo interno auxiliar
    for v in merged.values():
        v.pop("_base_conf", None)

    # EXTREMO exige MTF >= 2 TFs confirmados; downgrade para FORTE se single-TF
    for v in merged.values():
        if v.get("intensity") == "EXTREMO" and v.get("mtf_count", 1) < 2:
            v["intensity"] = "FORTE"
            v["confidence"] = min(v["confidence"], 74)

    results = sorted(merged.values(), key=lambda x: x["confidence"], reverse=True)

    # Enriquece alertas relevantes com direção do OI (fire-and-forget se falhar)
    try:
        results = await _enrich_oi(results)
    except Exception:
        pass

    # Mapeia a intensidade para a chave 'priority' exigida pelo endpoint e frontend
    for v in results:
        intensity = v.get("intensity", "MODERADO")
        if intensity == "EXTREMO":
            v["priority"] = "HIGH"
        elif intensity == "FORTE":
            v["priority"] = "MEDIUM"
        else:
            v["priority"] = "LOW"

    _pd_cache    = results
    _pd_cache_ts = now
    return results


def get_cached() -> list:
    return _pd_cache


def get_new_alerts(min_confidence: int = 40) -> list[dict]:
    now = time.time()
    return [
        a for a in _pd_cache
        if a.get("confidence", 0) >= min_confidence
        and now - _pd_alert_cooldown.get(a["symbol"], 0) >= PD_ALERT_COOLDOWN_S
    ]


def mark_alert_sent(symbol: str):
    _pd_alert_cooldown[symbol] = time.time()


def prune_alert_cooldowns():
    cutoff = time.time() - 3600
    stale  = [k for k, ts in _pd_alert_cooldown.items() if ts < cutoff]
    for k in stale:
        del _pd_alert_cooldown[k]
