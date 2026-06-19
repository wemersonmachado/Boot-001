"""
Engine Router — Trader 001
===========================
Roteador inteligente que seleciona a estratégia correta para cada ativo
baseado no regime de mercado detectado em tempo real.

Regimes → Engines:
  TRENDING  → signal_engine   (EMA/MACD/RSI trend following — o engine atual)
  RANGING   → mean_rev_engine (RSI extremos + Bollinger Bands mean reversion)
  VOLATILE  → volatile_engine (breakout de volume + momentum — já existia)
  FADE      → fade_engine     (contrarian em pump/dump detectado)
  NEUTRAL   → signal_engine   (fallback — engine padrão)

Sinais adicionais aplicados pelo router sobre qualquer engine:
  - Volume Profile (POC/VAH/VAL) bonus/penalidade
  - Asset Memory (WR histórico por ativo) ajuste de score
  - RS Score (força relativa vs BTC)
  - Session threshold (SCORE_THRESH dinâmico por sessão)
"""
import asyncio
import time
from typing import Optional

import numpy as np
import pandas as pd

from models import TradeSignal, SignalScore, Direction
from klines_cache import get_klines_cached
import regime_detector
import volume_profile as vp_module
import asset_memory


# ── Cache de regime por ativo (evita recalcular a cada call) ─────────────────
_regime_ts: dict = {}   # symbol+tf → timestamp
_REGIME_TTL = 300       # 5 min


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 2 — MEAN REVERSION (mercado lateral)
# ═══════════════════════════════════════════════════════════════════════════════

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def _bb(s: pd.Series, p: int = 20, std: float = 2.0):
    mid = s.rolling(p).mean()
    dev = s.rolling(p).std()
    return mid - std * dev, mid, mid + std * dev


async def _mean_rev_signal(
    symbol: str, tf: str, df: pd.DataFrame,
    news_data: list = None, mode: str = "NORMAL"
) -> Optional[TradeSignal]:
    """
    Mean Reversion Engine — para mercados laterais (ADX < 20).

    Entry criteria:
      LONG  — RSI < 32 AND preço toca BB inferior AND EMA 50 próxima (±1.5%)
      SHORT — RSI > 68 AND preço toca BB superior AND EMA 50 próxima (±1.5%)

    SL: breakout da BB oposta
    TP1: linha média BB (50%)
    TP2: BB oposta (35%)
    TP3: extensão 1.5× BB width (15%)
    """
    if df is None or len(df) < 50:
        return None
    try:
        # Filtro ADX para evitar entrar contra tendência forte
        regime_data = regime_detector.detect(df, direction="LONG")
        adx_val = regime_data.get("adx", 20.0)
        if adx_val >= 25.0:
            return None

        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        price = float(close.iloc[-1])

        rsi_val  = float(_rsi(close).iloc[-1])
        bb_lo, bb_mid, bb_hi = _bb(close)
        bb_lo_v  = float(bb_lo.iloc[-1])
        bb_hi_v  = float(bb_hi.iloc[-1])
        bb_mid_v = float(bb_mid.iloc[-1])
        bb_width = bb_hi_v - bb_lo_v
        e50      = float(_ema(close, 50).iloc[-1])

        near_e50_pct = abs(price - e50) / price * 100

        # Volume confirmação
        vol     = df["volume"]
        avg_vol = float(vol.rolling(20).mean().iloc[-1]) or 1
        vol_ratio = float(vol.iloc[-1]) / avg_vol

        score = 0.0
        direction = None

        # LONG setup
        if rsi_val < 32 and price <= bb_lo_v * 1.002 and near_e50_pct < 4.0:
            score = 0.0
            score += 30.0                                    # RSI oversold
            score += min(20.0, (32 - rsi_val) * 2)          # quanto mais baixo melhor
            score += 20.0 if price <= bb_lo_v else 10.0     # toca BB
            score += 15.0 if vol_ratio >= 1.3 else 0.0      # volume confirma
            score += 15.0 if near_e50_pct < 2.0 else 8.0   # próximo da EMA50
            # Candle reversal bonus
            body  = abs(float(close.iloc[-1]) - float(df["open"].iloc[-1]))
            wick_lo = float(close.iloc[-1]) - float(low.iloc[-1])
            if wick_lo > body * 1.5:
                score += 10.0   # hammer / pin bar
            direction = Direction.LONG

        # SHORT setup
        elif rsi_val > 68 and price >= bb_hi_v * 0.998 and near_e50_pct < 4.0:
            score = 0.0
            score += 30.0
            score += min(20.0, (rsi_val - 68) * 2)
            score += 20.0 if price >= bb_hi_v else 10.0
            score += 15.0 if vol_ratio >= 1.3 else 0.0
            score += 15.0 if near_e50_pct < 2.0 else 8.0
            body    = abs(float(close.iloc[-1]) - float(df["open"].iloc[-1]))
            wick_hi = float(high.iloc[-1]) - float(close.iloc[-1])
            if wick_hi > body * 1.5:
                score += 10.0   # shooting star / pin bar
            direction = Direction.SHORT

        if direction is None or score < 55:
            return None

        # Níveis (SL/TP baseados na BB)
        from signal_engine import atr as _atr_fn
        atr_val = float(_atr_fn(df).iloc[-1]) or (price * 0.01)

        if direction == Direction.LONG:
            entry    = price
            sl       = bb_lo_v - atr_val * 0.5
            tp1      = bb_mid_v
            tp2      = bb_hi_v
            tp3      = bb_hi_v + bb_width * 0.5
        else:
            entry    = price
            sl       = bb_hi_v + atr_val * 0.5
            tp1      = bb_mid_v
            tp2      = bb_lo_v
            tp3      = bb_lo_v - bb_width * 0.5

        rr = abs(tp2 - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 1.0
        if rr < 1.3:
            return None

        sc = SignalScore(
            total=round(score, 1),
            trend=0.0, volume=round(vol_ratio * 20, 1),
            momentum=30.0 if rsi_val < 32 or rsi_val > 68 else 10.0,
            structure=20.0, funding=0.0
        )

        return TradeSignal(
            asset=symbol, direction=direction, timeframe=tf,
            confidence=round(score, 1),
            score=sc,
            entry=round(entry, 8), stop_loss=round(sl, 8),
            tp1=round(tp1, 8), tp2=round(tp2, 8), tp3=round(tp3, 8),
            rr=round(rr, 2),
            reason=f"RANGE|RSI{rsi_val:.0f}|BB|E2-MEANREV",
            confirmed_signals=["RGM-RNG", "BB", "RSI-EXTREME"],
        )
    except Exception as e:
        print(f"[MEAN_REV] {symbol}/{tf} erro: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 5 — VDLS (Volume Delta & Liquidity Sweeps)
# ═══════════════════════════════════════════════════════════════════════════════

async def _vdls_signal(
    symbol: str, tf: str, df: pd.DataFrame, news_data: list = None, mode: str = "NORMAL"
) -> Optional[TradeSignal]:
    """
    VDLS Engine — opera capturas de liquidez com divergência de volume delta.
    Otimizado para scalp (1m, 3m, 5m, 15m).
    """
    if df is None or len(df) < 30:
        return None
    try:
        if tf not in {"1m", "3m", "5m", "15m"}:
            return None

        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        open_ = df["open"]
        vol   = df["volume"]
        price = float(close.iloc[-1])

        # Calcula ATR
        hl = high - low
        hc = (high - close.shift()).abs()
        lc = (low  - close.shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr_val = float(tr.rolling(14).mean().iloc[-1]) or (price * 0.01)

        # Mínimas e Máximas locais excluindo a vela atual (i-1)
        lookback = 20
        local_low  = float(low.shift(1).rolling(lookback).min().iloc[-1])
        local_high = float(high.shift(1).rolling(lookback).max().iloc[-1])

        # CVD Proxy
        delta_vol = vol * np.sign(close - open_)
        cvd_series = delta_vol.rolling(8).sum()
        cvd_now = float(cvd_series.iloc[-1])
        cvd_prev = float(cvd_series.iloc[-2])

        direction = None
        score = 0.0
        
        # Filtro de volatilidade mínima
        atr_filter_mult = 0.5 if tf in ["1m", "3m"] else 0.8
        if atr_val < (price * 0.0005 * atr_filter_mult):
            return None

        # Gatilho LONG: rompimento falso do suporte local + CVD subindo
        if float(low.iloc[-1]) < local_low and price >= local_low:
            if cvd_now > cvd_prev:
                direction = Direction.LONG
                score = 65.0
                score += min(15.0, (price - local_low) / (atr_val or 1.0) * 10.0) # bônus força
                score += 15.0 if cvd_now > 0 else 0.0

        # Gatilho SHORT: rompimento falso da resistência local + CVD caindo
        elif float(high.iloc[-1]) > local_high and price <= local_high:
            if cvd_now < cvd_prev:
                direction = Direction.SHORT
                score = 65.0
                score += min(15.0, (local_high - price) / (atr_val or 1.0) * 10.0)
                score += 15.0 if cvd_now < 0 else 0.0

        if direction is None or score < 60:
            return None

        # Níveis de SL/TP estritos da estratégia VDLS (Stop colado no pavio, R:R fixo de 2x)
        if direction == Direction.LONG:
            sl = float(low.iloc[-1]) - (atr_val * 0.15)
            if sl >= price:
                sl = price - (atr_val * 1.2)
            risk = price - sl
            tp1 = price + risk * 1.2
            tp2 = price + risk * 2.0
            tp3 = price + risk * 3.0
        else:
            sl = float(high.iloc[-1]) + (atr_val * 0.15)
            if sl <= price:
                sl = price + (atr_val * 1.2)
            risk = sl - price
            tp1 = price - risk * 1.2
            tp2 = price - risk * 2.0
            tp3 = price - risk * 3.0

        rr = abs(tp2 - price) / risk if risk > 0 else 2.0

        sc = SignalScore(
            total=round(score, 1),
            trend=10.0,
            volume=min(100.0, float(vol.iloc[-1] / (vol.rolling(20).mean().iloc[-1] or 1.0)) * 15.0),
            momentum=40.0,
            structure=30.0, funding=0.0
        )

        return TradeSignal(
            asset=symbol, direction=direction, timeframe=tf,
            confidence=round(score, 1), score=sc,
            entry=round(price, 8), stop_loss=round(sl, 8),
            tp1=round(tp1, 8), tp2=round(tp2, 8), tp3=round(tp3, 8),
            rr=round(rr, 2),
            reason=f"VDLS|SWEEP|CVD{cvd_now:.0f}|SL-PAVIO",
            confirmed_signals=["VDLS-SWEEP", "CVD-DIV", "LQ-SWEEP"],
        )
    except Exception as e:
        print(f"[VDLS] {symbol}/{tf} erro: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 4 — FADE (contrarian em pump/dump detectado)
# ═══════════════════════════════════════════════════════════════════════════════

async def _fade_signal(
    symbol: str, tf: str, df: pd.DataFrame
) -> Optional[TradeSignal]:
    """
    Fade Engine — opera contra movimentos excessivos (pump/dump exausto).

    Entry criteria para SHORT (fade de pump):
      RSI > 78 AND vol_ratio > 5× AND ATR% > 3× média histórica
      → Preço sobreextendido, exaustão iminente

    Entry criteria para LONG (fade de dump):
      RSI < 22 AND vol_ratio > 5× AND ATR% > 3× média histórica
    """
    if df is None or len(df) < 30:
        return None
    try:
        # Filtro ADX para evitar entrar antes da exaustão em tendência hiper-forte
        regime_data = regime_detector.detect(df, direction="LONG")
        adx_val = regime_data.get("adx", 20.0)
        if adx_val >= 35.0:
            return None

        close   = df["close"]
        price   = float(close.iloc[-1])
        rsi_val = float(_rsi(close).iloc[-1])
        vol     = df["volume"]
        avg_vol = float(vol.rolling(20).mean().iloc[-1]) or 1
        vol_ratio = float(vol.iloc[-1]) / avg_vol

        hl = df["high"] - df["low"]
        hc = (df["high"] - close.shift()).abs()
        lc = (df["low"]  - close.shift()).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr_cur  = float(tr.rolling(14).mean().iloc[-1])
        atr_hist = float(tr.rolling(50).mean().iloc[-1]) or atr_cur
        atr_ratio = atr_cur / atr_hist if atr_hist > 0 else 1.0

        direction = None
        score     = 0.0

        if rsi_val > 78 and vol_ratio > 4.5 and atr_ratio > 2.5:
            direction = Direction.SHORT
            score = 55.0
            score += min(20, (rsi_val - 78) * 2)       # quanto mais extremo
            score += min(15, (vol_ratio - 4.5) * 3)     # pico de volume
            score += min(10, (atr_ratio - 2.5) * 5)     # volatilidade anormal

        elif rsi_val < 22 and vol_ratio > 4.5 and atr_ratio > 2.5:
            direction = Direction.LONG
            score = 55.0
            score += min(20, (22 - rsi_val) * 2)
            score += min(15, (vol_ratio - 4.5) * 3)
            score += min(10, (atr_ratio - 2.5) * 5)

        if direction is None or score < 60:
            return None

        # Validação por Order Blocks (OB) institucionais recentes (últimos 100 candles)
        from signal_engine import _v6_find_obs
        atr_s = tr.rolling(14).mean().ffill().bfill()
        obs = _v6_find_obs(df, atr_s, impulse=1.8)
        recent_obs = [ob for ob in obs if (len(df) - ob["idx"]) <= 100]
        
        ob_confirmed = False
        ob_info = "NO-OB"
        
        if direction == Direction.SHORT:
            for ob in recent_obs:
                if ob["dir"] == -1:
                    # Preço atual está testando a zona do Bearish OB com tolerância
                    if (ob["bot"] - 0.25 * atr_cur) <= price <= (ob["top"] + 0.25 * atr_cur):
                        ob_confirmed = True
                        ob_info = f"OB-Supply-idx{ob['idx']}"
                        break
        else:
            for ob in recent_obs:
                if ob["dir"] == 1:
                    # Preço atual está testando a zona do Bullish OB com tolerância
                    if (ob["bot"] - 0.25 * atr_cur) <= price <= (ob["top"] + 0.25 * atr_cur):
                        ob_confirmed = True
                        ob_info = f"OB-Demand-idx{ob['idx']}"
                        break
        
        if not ob_confirmed:
            # Rejeita o sinal FADE se não colidir com OB institucional para evitar perdas
            return None

        atr_val = atr_cur or price * 0.015
        is_scalp_tf = tf in {"1m", "3m", "5m", "15m"}
        sl_mult = 1.0 if is_scalp_tf else 1.5
        tp1_mult = 1.2 if is_scalp_tf else 2.0
        tp2_mult = 2.0 if is_scalp_tf else 3.5
        tp3_mult = 3.0 if is_scalp_tf else 5.0

        if direction == Direction.SHORT:
            entry = price
            sl    = price + atr_val * sl_mult
            tp1   = price - atr_val * tp1_mult
            tp2   = price - atr_val * tp2_mult
            tp3   = price - atr_val * tp3_mult
        else:
            entry = price
            sl    = price - atr_val * sl_mult
            tp1   = price + atr_val * tp1_mult
            tp2   = price + atr_val * tp2_mult
            tp3   = price + atr_val * tp3_mult

        rr = abs(tp2 - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 1.0

        sc = SignalScore(
            total=round(score, 1),
            trend=0.0, volume=min(100, vol_ratio * 15),
            momentum=min(100, abs(rsi_val - 50) * 2),
            structure=30.0, funding=0.0  # Estrutura ganha importância com OB
        )

        return TradeSignal(
            asset=symbol, direction=direction, timeframe=tf,
            confidence=round(score, 1), score=sc,
            entry=round(entry, 8), stop_loss=round(sl, 8),
            tp1=round(tp1, 8), tp2=round(tp2, 8), tp3=round(tp3, 8),
            rr=round(rr, 2),
            reason=f"FADE|RSI{rsi_val:.0f}|VOL{vol_ratio:.1f}x|{ob_info}",
            confirmed_signals=["FADE", "RSI-EXTREME", "VOL-SPIKE", "OB-BLOCK"],
        )
    except Exception as e:
        print(f"[FADE] {symbol}/{tf} erro: {e}")
        return None


def _post_process_signal(
    signal: Optional[TradeSignal],
    df: pd.DataFrame,
    symbol: str,
    tf: str,
    engine_used: str,
    regime: str,
    adx_val: float,
    mode: str,
) -> Optional[TradeSignal]:
    if signal is None:
        return None

    # 1. Garante que o tipo de trade seja SCALP se for um timeframe de scalp
    if tf in {"1m", "3m", "5m", "15m"}:
        object.__setattr__(signal, "trade_type", "SCALP")

    # 2. Calcula as métricas da última vela caso ainda não estejam calculadas
    price = float(df["close"].iloc[-1])
    last = df.iloc[-1]
    
    # Body pct
    if getattr(signal, "body_pct", 0.0) == 0.0:
        rng = float(last["high"] - last["low"])
        body = abs(float(last["close"]) - float(last["open"]))
        body_pct = round(body / rng, 3) if rng > 0 else 0.0
        object.__setattr__(signal, "body_pct", body_pct)
    else:
        body_pct = signal.body_pct

    # Volume ratio
    if getattr(signal, "vol_ratio", 1.0) == 1.0:
        vol_avg = float(df["volume"].rolling(20).mean().iloc[-1]) or 1.0
        vol_ratio = round(float(last["volume"]) / vol_avg, 2)
        object.__setattr__(signal, "vol_ratio", vol_ratio)
    else:
        vol_ratio = signal.vol_ratio

    # RSI val
    if getattr(signal, "rsi_val", 50.0) == 50.0:
        rsi_series = _rsi(df["close"], 14)
        rsi_val = round(float(rsi_series.iloc[-1]), 1)
        object.__setattr__(signal, "rsi_val", rsi_val)
    else:
        rsi_val = signal.rsi_val

    # 3. Calcula suggested_leverage e leverage_reason caso ainda não estejam calculados
    if getattr(signal, "suggested_leverage", 0) <= 0:
        sl_pct = abs(float(signal.entry) - float(signal.stop_loss)) / float(signal.entry) * 100 if float(signal.entry) > 0 else 3.0
        try:
            from risk_manager import suggest_leverage as _suggest_lev
            v6_tags = []
            if "BOS" in signal.reason: v6_tags.append("BOS")
            if "OB/FVG" in signal.reason: v6_tags.append("OB/FVG")
            if "GOLDEN-X" in signal.reason or "DEATH-X" in signal.reason: v6_tags.append("GOLDEN-X")
            
            _lev_info = _suggest_lev(
                symbol=symbol, score=signal.confidence, body_pct=body_pct,
                vol_ratio=vol_ratio, rsi_val=rsi_val, sl_pct=sl_pct,
                rr=signal.rr, trade_type=signal.trade_type,
                v6_tags=v6_tags,
            )
            object.__setattr__(signal, "suggested_leverage", _lev_info["leverage"])
            object.__setattr__(signal, "leverage_reason", _lev_info["reason"])
        except Exception as _le:
            print(f"[ROUTER] Erro ao sugerir alavancagem pos-processamento: {_le}")

    return signal


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTER PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

async def route(
    symbol: str,
    tf: str,
    df: pd.DataFrame,
    news_data: list = None,
    mode: str = "NORMAL",
    rs_scores: dict = None,
    force_engine: str = None,   # "TREND"|"RANGE"|"BREAKOUT"|"FADE" para override
) -> Optional[TradeSignal]:
    """
    Ponto de entrada principal do roteador.

    1. Detecta regime do ativo (ADX + ATR + EMA alignment)
    2. Verifica se há pump/dump → engine FADE
    3. Roteia para o engine certo baseado no regime
    4. Aplica Volume Profile (POC/VAH/VAL) como ajuste adicional
    5. Aplica Asset Memory (WR histórico) como ajuste final
    6. Retorna o melhor sinal encontrado (ou None)
    """
    if df is None or len(df) < 40:
        return None

    rs_scores  = rs_scores or {}

    # ── 1. Detecta regime ────────────────────────────────────────────────────
    regime_data = regime_detector.detect(df, direction="LONG")
    regime      = regime_data.get("regime", "NEUTRAL")
    adx_val     = regime_data.get("adx", 20.0)
    regime_detector.update_cache(symbol, regime_data)

    # ── 2. Verifica pump/dump primeiro (override de regime) ──────────────────
    is_fade = False
    if not force_engine:
        try:
            from signal_engine import check_pump_dump
            is_fade = check_pump_dump(df)
        except Exception:
            pass

    # ── 3. Seleciona engine ──────────────────────────────────────────────────
    engine_used = force_engine or ("FADE" if is_fade else _select_engine(regime, tf))

    signal = None

    if engine_used == "FADE":
        signal = await _fade_signal(symbol, tf, df)

    elif engine_used == "VDLS":
        signal = await _vdls_signal(symbol, tf, df, news_data, mode)

    elif engine_used == "RANGE":
        signal = await _mean_rev_signal(symbol, tf, df, news_data, mode)

    elif engine_used == "BREAKOUT":
        try:
            from volatile_engine import analyze_volatile as _vol_analyze
            signal = await _vol_analyze(symbol, tf, df=df)
        except Exception as e:
            print(f"[ROUTER] volatile_engine erro: {e}")
            signal = None
        if signal is None:
            from signal_engine import analyze_asset
            signal = await analyze_asset(symbol, tf, news_data=news_data, mode=mode, df=df, route=False)

    else:  # TREND / NEUTRAL
        from signal_engine import analyze_asset
        signal = await analyze_asset(symbol, tf, news_data=news_data, mode=mode, df=df, route=False)

    if signal is None:
        return None

    # ── 4. Aplica regime score adjustment ────────────────────────────────────
    regime_adj = regime_data.get("score_adj", 0.0)
    sl_mult    = regime_data.get("sl_mult_adj", 1.0)
    score_cap  = regime_data.get("score_cap")

    new_conf = signal.confidence + regime_adj
    if score_cap:
        new_conf = min(new_conf, score_cap)

    # ── 5. Aplica Volume Profile ──────────────────────────────────────────────
    try:
        vp_data  = vp_module.compute(df, bins=50)
        price    = float(df["close"].iloc[-1])
        hl       = df["high"] - df["low"]
        hc       = (df["high"] - df["close"].shift()).abs()
        lc       = (df["low"]  - df["close"].shift()).abs()
        atr_val  = float(pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean().iloc[-1])
        vp_adj   = vp_module.score_confluence(vp_data, price, signal.direction.value, atr_val)
        new_conf += vp_adj
        if abs(vp_adj) > 0:
            print(f"[ROUTER] {symbol}/{tf} VP adj: {vp_adj:+.0f}pts (POC={vp_data.get('poc',0):.4f})")
    except Exception as e:
        vp_data = {}
        print(f"[ROUTER] VP erro: {e}")

    # ── 6. Aplica RS Score ────────────────────────────────────────────────────
    if rs_scores:
        try:
            from signal_filters import rs_score_adj
            rs_adj   = rs_score_adj(symbol, signal.direction.value, rs_scores)
            new_conf += rs_adj
        except Exception:
            rs_adj = 0.0

    # ── 7. Aplica Asset Memory ────────────────────────────────────────────────
    mem_stats = asset_memory.get_stats(symbol)
    if mem_stats.get("paused"):
        print(f"[ROUTER] {symbol} pausado ({mem_stats.get('paused_minutes')}min restantes)")
        return None

    new_conf += mem_stats.get("score_adj", 0.0)

    # ── 8. Ajusta SL pelo multiplicador do regime ────────────────────────────
    if sl_mult != 1.0 and signal.stop_loss:
        dist    = abs(float(signal.entry) - float(signal.stop_loss))
        new_sl  = (float(signal.entry) - dist * sl_mult
                   if signal.direction == Direction.LONG
                   else float(signal.entry) + dist * sl_mult)
        object.__setattr__(signal, "stop_loss", round(new_sl, 8))

    # ── 9. Atualiza confidence final e tag de regime ─────────────────────────
    new_conf = max(0.0, min(100.0, new_conf))
    object.__setattr__(signal, "confidence", round(new_conf, 1))

    regime_tag = f"[{engine_used}|{regime}|ADX{adx_val:.0f}]"
    new_reason = f"{regime_tag} {signal.reason}"
    object.__setattr__(signal, "reason", new_reason)

    # Pós-processamento scalp e alavancagem
    signal = _post_process_signal(signal, df, symbol, tf, engine_used, regime, adx_val, mode)

    return signal


def _select_engine(regime: str, tf: str) -> str:
    """Mapeia regime detectado para nome do engine, ativando VDLS para scalp lateral/neutro."""
    if tf in {"1m", "3m", "5m"}:
        if regime in {"RANGING", "NEUTRAL"}:
            return "VDLS"
    return {
        "TRENDING": "TREND",
        "RANGING":  "RANGE",
        "VOLATILE": "BREAKOUT",
        "NEUTRAL":  "TREND",
    }.get(regime, "TREND")


# ═══════════════════════════════════════════════════════════════════════════════
# CASCADE MODE — testa TODAS as engines e retorna a de maior confidence
# ═══════════════════════════════════════════════════════════════════════════════

async def cascade(
    symbol: str,
    tf: str,
    df: pd.DataFrame,
    news_data: list = None,
    mode: str = "NORMAL",
    rs_scores: dict = None,
    min_confidence: float = 55.0,
) -> dict:
    """
    Modo cascata: testa todos os 4 engines no mesmo ativo e retorna o melhor sinal.

    Returns:
        {
            "winner":    TradeSignal | None,
            "engine":    "TREND" | "RANGE" | "BREAKOUT" | "FADE" | None,
            "regime":    str,
            "adx":       float,
            "all_scores": {"TREND": float, "RANGE": float, "BREAKOUT": float, "FADE": float},
            "tried":     int,
        }
    """
    if df is None or len(df) < 40:
        return _cascade_empty(symbol)

    # Detecta regime para contexto (mas NÃO bloqueia nenhuma engine)
    regime_data = regime_detector.detect(df, direction="LONG")
    regime      = regime_data.get("regime", "NEUTRAL")
    adx_val     = regime_data.get("adx", 20.0)
    regime_detector.update_cache(symbol, regime_data)

    # Roda todas as 5 engines em paralelo
    results = await asyncio.gather(
        _run_engine("TREND",    symbol, tf, df, news_data, mode),
        _run_engine("RANGE",    symbol, tf, df, news_data, mode),
        _run_engine("BREAKOUT", symbol, tf, df, news_data, mode),
        _run_engine("FADE",     symbol, tf, df, news_data, mode),
        _run_engine("VDLS",     symbol, tf, df, news_data, mode),
        return_exceptions=True,
    )

    engine_names = ["TREND", "RANGE", "BREAKOUT", "FADE", "VDLS"]
    all_scores   = {}
    candidates   = []

    for name, result in zip(engine_names, results):
        if isinstance(result, Exception) or result is None:
            all_scores[name] = 0.0
            continue
        all_scores[name] = result.confidence
        if result.confidence >= min_confidence:
            candidates.append((name, result))

    if not candidates:
        return {
            "winner": None, "engine": None, "regime": regime, "adx": adx_val,
            "all_scores": all_scores, "tried": 5, "symbol": symbol,
        }

    # Ganhador = maior confidence
    winner_name, winner_sig = max(candidates, key=lambda x: x[1].confidence)

    # Aplica Volume Profile e Asset Memory no vencedor
    try:
        vp_data  = vp_module.compute(df)
        price    = float(df["close"].iloc[-1])
        hl       = df["high"] - df["low"]
        hc       = (df["high"] - df["close"].shift()).abs()
        lc       = (df["low"]  - df["close"].shift()).abs()
        atr_val  = float(pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean().iloc[-1])
        vp_adj   = vp_module.score_confluence(vp_data, price, winner_sig.direction.value, atr_val)
        new_conf = min(100.0, winner_sig.confidence + vp_adj)
        object.__setattr__(winner_sig, "confidence", round(new_conf, 1))
        all_scores[winner_name] = round(new_conf, 1)
    except Exception:
        vp_data = {}

    mem = asset_memory.get_stats(symbol)
    if mem.get("paused"):
        return {
            "winner": None, "engine": None, "regime": regime, "adx": adx_val,
            "all_scores": all_scores, "tried": 5, "symbol": symbol, "paused": True,
        }

    # Tag cascade na razão do sinal
    tag = f"[CASCADE:{winner_name}|{regime}|ADX{adx_val:.0f}]"
    object.__setattr__(winner_sig, "reason", f"{tag} {winner_sig.reason}")

    # Pós-processamento scalp e alavancagem
    winner_sig = _post_process_signal(winner_sig, df, symbol, tf, winner_name, regime, adx_val, mode)

    return {
        "winner":    winner_sig,
        "engine":    winner_name,
        "regime":    regime,
        "adx":       adx_val,
        "all_scores": all_scores,
        "tried":     4,
        "symbol":    symbol,
    }


async def _run_engine(
    engine: str, symbol: str, tf: str, df: pd.DataFrame,
    news_data: list, mode: str
) -> "Optional[TradeSignal]":
    """Roda um engine específico de forma isolada (sem ajustes de regime)."""
    try:
        if engine == "TREND":
            from signal_engine import analyze_asset
            return await analyze_asset(symbol, tf, news_data=news_data, mode=mode, df=df, route=False)
        elif engine == "RANGE":
            return await _mean_rev_signal(symbol, tf, df, news_data, mode)
        elif engine == "VDLS":
            return await _vdls_signal(symbol, tf, df, news_data, mode)
        elif engine == "BREAKOUT":
            from volatile_engine import analyze_volatile
            sig = await analyze_volatile(symbol, tf, df=df)
            if sig is None:
                from signal_engine import analyze_asset
                sig = await analyze_asset(symbol, tf, news_data=news_data, mode=mode, df=df, route=False)
            return sig
        elif engine == "FADE":
            return await _fade_signal(symbol, tf, df)
    except Exception as e:
        print(f"[CASCADE] {engine}/{symbol}/{tf} erro: {e}")
    return None


def _cascade_empty(symbol: str) -> dict:
    return {
        "winner": None, "engine": None, "regime": "NEUTRAL", "adx": 0.0,
        "all_scores": {"TREND": 0.0, "RANGE": 0.0, "BREAKOUT": 0.0, "FADE": 0.0},
        "tried": 0, "symbol": symbol,
    }


async def scan_cascade(
    symbols: list,
    timeframes: list,
    news_data: list = None,
    mode: str = "NORMAL",
    min_confidence: float = 55.0,
    max_concurrent: int = 20,
) -> list[dict]:
    """
    Scan completo em modo cascata — todas as engines testadas em cada ativo.
    Retorna lista de resultados ordenada por confidence descendente.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _single(sym: str, tf: str) -> dict:
        async with semaphore:
            try:
                df = await get_klines_cached(sym, tf, limit=200)
                return await cascade(sym, tf, df, news_data=news_data,
                                     mode=mode, min_confidence=min_confidence)
            except Exception as e:
                print(f"[CASCADE SCAN] {sym}/{tf}: {e}")
                return _cascade_empty(sym)

    tasks   = [_single(sym, tf) for sym in symbols for tf in timeframes]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid   = [r for r in results if isinstance(r, dict) and r.get("winner")]
    valid.sort(key=lambda x: x["winner"].confidence, reverse=True)

    # Estatísticas de engine
    engine_count: dict = {}
    regime_count: dict = {}
    for r in valid:
        e = r.get("engine", "?")
        reg = r.get("regime", "?")
        engine_count[e]   = engine_count.get(e, 0) + 1
        regime_count[reg] = regime_count.get(reg, 0) + 1

    print(f"[CASCADE] {len(valid)} sinais | engines: {engine_count} | regimes: {regime_count}")
    return valid


# ═══════════════════════════════════════════════════════════════════════════════
# SCAN COM ROUTER (substitui scan_watchlist para modos AUTÔNOMO e GRID)
# ═══════════════════════════════════════════════════════════════════════════════

async def scan_with_router(
    symbols: list,
    timeframes: list,
    news_data: list = None,
    mode: str = "NORMAL",
    rs_scores: dict = None,
    max_concurrent: int = 30,
) -> list[TradeSignal]:
    """
    Scan completo usando o engine router.
    Paralelo com semáforo para não causar ban de IP.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _single(sym: str, tf: str) -> Optional[TradeSignal]:
        async with semaphore:
            try:
                df = await get_klines_cached(sym, tf, limit=200)
                return await route(sym, tf, df, news_data=news_data,
                                   mode=mode, rs_scores=rs_scores)
            except Exception as e:
                print(f"[ROUTER] {sym}/{tf} erro: {e}")
                return None

    tasks   = [_single(sym, tf) for sym in symbols for tf in timeframes]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    signals = [r for r in results if isinstance(r, TradeSignal)]
    signals.sort(key=lambda s: s.confidence, reverse=True)

    # Log de distribuição de engines
    engine_counts: dict = {}
    for sig in signals:
        tag = sig.reason.split("|")[0].lstrip("[")
        engine_counts[tag] = engine_counts.get(tag, 0) + 1
    print(f"[ROUTER] Scan: {len(signals)} sinais | engines: {engine_counts}")

    return signals


# ═══════════════════════════════════════════════════════════════════════════════
# VOLATILE ENGINE BRIDGE — adapta interface para o router
# ═══════════════════════════════════════════════════════════════════════════════

async def _volatile_bridge(symbol: str, tf: str) -> Optional[TradeSignal]:
    """Chama volatile_engine.analyze_volatile() com interface normalizada."""
    try:
        from volatile_engine import analyze_volatile
        return await analyze_volatile(symbol, tf)
    except Exception as e:
        print(f"[ROUTER] volatile_engine bridge erro: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# SCORE REGIME MATCH — pontuação de adequação de engine sem gate de threshold
# ═══════════════════════════════════════════════════════════════════════════════

def score_regime_match(df: pd.DataFrame) -> dict:
    """
    Retorna scores de 0-100 indicando o quanto cada engine é adequado
    para o ativo atual, independente de haver sinal válido.

    Útil para diagnóstico e para o teste de cascade.
    Scores:
      TREND    — ADX alto + EMAs alinhadas + momentum direcional
      RANGE    — ADX baixo + BB estreita + RSI perto do extremo
      BREAKOUT — Volume spike + ATR expansão + candle breakout
      FADE     — RSI sobrecomprado/sobrevendido + volume spike
    """
    if df is None or len(df) < 30:
        return {"TREND": 0.0, "RANGE": 0.0, "BREAKOUT": 0.0, "FADE": 0.0}

    try:
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]
        price  = float(close.iloc[-1])

        # RSI
        d = close.diff()
        g = d.clip(lower=0).rolling(14).mean()
        l = (-d.clip(upper=0)).rolling(14).mean()
        rsi_val = float(100 - 100 / (1 + g / l.replace(0, np.nan)).iloc[-1])

        # ADX
        hl = high - low
        hc = (high - close.shift()).abs()
        lc = (low  - close.shift()).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr14 = float(tr.rolling(14).mean().iloc[-1]) or 1.0

        up   = high.diff().clip(lower=0)
        down = (-low.diff()).clip(lower=0)
        dmp  = (up.rolling(14).mean() / atr14 * 100)
        dmn  = (down.rolling(14).mean() / atr14 * 100)
        dx   = ((dmp - dmn).abs() / (dmp + dmn).replace(0, np.nan) * 100)
        adx_val = float(dx.rolling(14).mean().iloc[-1]) if not dx.isna().all() else 15.0

        # EMAs
        e21  = float(close.ewm(span=21,  adjust=False).mean().iloc[-1])
        e55  = float(close.ewm(span=55,  adjust=False).mean().iloc[-1])
        e200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        emas_up   = price > e21 > e55 > e200
        emas_down = price < e21 < e55 < e200
        ema_align = emas_up or emas_down

        # Volume ratio
        avg_vol   = float(volume.rolling(20).mean().iloc[-1]) or 1.0
        vol_ratio = float(volume.iloc[-1]) / avg_vol

        # ATR ratio (current vs historical)
        atr50 = float(tr.rolling(50).mean().iloc[-1]) or atr14
        atr_ratio = atr14 / atr50 if atr50 > 0 else 1.0

        # BB width (narrowness = range)
        bb_mid = float(close.rolling(20).mean().iloc[-1])
        bb_std = float(close.rolling(20).std().iloc[-1]) or 1.0
        bb_width_pct = (bb_std * 4) / bb_mid * 100  # BB width as % of price

        # ── TREND score ───────────────────────────────────────────────────────
        trend_score = 0.0
        trend_score += min(40.0, adx_val * 1.6)          # ADX 25 → 40pts
        trend_score += 30.0 if ema_align else 0.0         # EMAs alinhadas
        trend_score += 20.0 if vol_ratio >= 0.8 else 10.0 # volume saudável
        trend_score += 10.0 if 40 < rsi_val < 75 else 0.0 # RSI não extremo

        # ── RANGE score ───────────────────────────────────────────────────────
        range_score = 0.0
        range_score += max(0.0, (25.0 - adx_val) * 2.0)  # ADX < 20 → até 40pts
        range_score += 25.0 if rsi_val < 35 or rsi_val > 65 else 10.0  # RSI extremo
        range_score += max(0.0, 20.0 - bb_width_pct * 2)  # BB estreita
        range_score += 15.0 if not ema_align else 0.0      # EMAs não alinhadas

        # ── BREAKOUT score ────────────────────────────────────────────────────
        brkout_score = 0.0
        brkout_score += min(40.0, vol_ratio * 10.0)       # volume spike
        brkout_score += min(30.0, (atr_ratio - 1.0) * 30) # ATR expansão
        brkout_score += 20.0 if adx_val > 20 else 0.0     # alguma direcionalidade
        brkout_score += 10.0 if rsi_val > 60 or rsi_val < 40 else 0.0  # momentum

        # ── FADE score ────────────────────────────────────────────────────────
        fade_score = 0.0
        rsi_extreme = max(0.0, rsi_val - 70) + max(0.0, 30 - rsi_val)
        fade_score += min(50.0, rsi_extreme * 3.0)         # RSI sobreextendido
        fade_score += min(30.0, (vol_ratio - 1.0) * 10)   # volume spike
        fade_score += min(20.0, (atr_ratio - 1.0) * 20)   # volatilidade anormal

        return {
            "TREND":    round(min(100.0, max(0.0, trend_score)),  1),
            "RANGE":    round(min(100.0, max(0.0, range_score)),  1),
            "BREAKOUT": round(min(100.0, max(0.0, brkout_score)), 1),
            "FADE":     round(min(100.0, max(0.0, fade_score)),   1),
        }
    except Exception as e:
        print(f"[SCORE_REGIME] erro: {e}")
        return {"TREND": 0.0, "RANGE": 0.0, "BREAKOUT": 0.0, "FADE": 0.0}


async def cascade_with_regime_scores(
    symbol: str,
    tf: str,
    df: pd.DataFrame,
    news_data: list = None,
    mode: str = "NORMAL",
    min_confidence: float = 55.0,
) -> dict:
    """
    Cascade + regime match scores. Retorna all_scores com scores de adequação de engine
    mesmo quando nenhum sinal é gerado (útil para diagnóstico).
    """
    # Primeiro tenta gerar sinal real
    result = await cascade(symbol, tf, df, news_data=news_data, mode=mode,
                           min_confidence=min_confidence)

    # Se não gerou sinal (all_scores zerados), usa regime match scores como proxy
    if not result.get("winner") and all(v == 0.0 for v in result.get("all_scores", {}).values()):
        regime_scores = score_regime_match(df)
        result = {**result, "all_scores": regime_scores, "regime_match_mode": True}

    return result
