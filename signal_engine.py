"""
Signal Engine — multi-factor scoring for Long/Short setups.

SCORE WEIGHTS (adaptativo por timeframe):

SCALP (1m/3m/5m):
  EMA Fast Cross   20%   EMA 9/21 crossover + slope
  Volume/CVD       20%   Volume vs avg + buy/sell pressure
  Momentum         20%   Stochastic RSI + MACD aceleração
  VWAP             15%   Distância e lado do VWAP
  Market Structure 15%   S/R + BB squeeze/breakout
  Funding/OI       10%   Funding rate + long/short ratio

DAY/SWING (15m+):
  Trend EMA        25%   EMA 21/55/200
  Volume           20%   Volume spike + direção
  Momentum         15%   RSI + MACD
  Market Structure 15%   Estrutura + BB
  VWAP             10%   Posição vs VWAP
  Funding/OI       15%   Funding + OI + L/S ratio

Candle patterns: bonus adicional (+5~15pts) sobre o score final.
RSI Divergencia: bonus (+5~15pts) — divergencia regular/oculta.
Golden/Death Cross EMA50/200: bonus (+5~12pts).
Macro Cycle: ajuste (-10~+15pts) baseado em tendencia de longo prazo.
"""
import asyncio
import numpy as np
import pandas as pd
from typing import Optional

from models import SignalScore, TradeSignal, Direction
from data_fetcher import (
    get_funding_rate, get_open_interest, get_long_short_ratio,
    get_trending_futures, get_oi_history, get_orderbook_depth,
)
from klines_cache import get_klines_cached as get_klines
from config import WATCHLIST, WATCHLIST_VOLATILE, MODE_SETTINGS, TRADING_MODE


# ── Indicadores Técnicos ──────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    
    # Previne divisão por zero: se loss é zero e gain > 0, rsi é 100. Se ambos são zero, rsi é 50.
    rs = gain / loss.replace(0, np.nan)
    rsi_vals = 100 - 100 / (1 + rs)
    
    # Coerção de NaNs
    rsi_vals = np.where((gain > 0) & (loss == 0), 100.0, rsi_vals)
    rsi_vals = np.where((gain == 0) & (loss == 0), 50.0, rsi_vals)
    
    rsi_series = pd.Series(rsi_vals, index=series.index)
    if len(rsi_series) > 0 and pd.isna(rsi_series.iloc[-1]):
        rsi_series = rsi_series.fillna(50.0)
    return rsi_series


def stoch_rsi(series: pd.Series, rsi_period: int = 14, stoch_period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    """Stochastic RSI — melhor que RSI para scalp (mais sensível)."""
    rsi_vals = rsi(series, rsi_period)
    min_rsi = rsi_vals.rolling(stoch_period).min()
    max_rsi = rsi_vals.rolling(stoch_period).max()
    rng = max_rsi - min_rsi
    
    # Se rng é 0, significa que o RSI não mudou no período. Setamos K em 50.0 para evitar NaN.
    k_vals = np.where(rng == 0, 50.0, 100 * (rsi_vals - min_rsi) / rng.replace(0, np.nan))
    k = pd.Series(k_vals, index=series.index)
    k = k.rolling(smooth_k).mean()
    d = k.rolling(smooth_d).mean()
    
    k = k.fillna(50.0)
    d = d.fillna(50.0)
    return k, d


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def macd(series: pd.Series):
    fast = ema(series, 12)
    slow = ema(series, 26)
    line = fast - slow
    signal_line = ema(line, 9)
    hist = line - signal_line
    return line, signal_line, hist


def bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0):
    mid = series.rolling(period).mean()
    sigma = series.rolling(period).std()
    upper = mid + std * sigma
    lower = mid - std * sigma
    return upper, mid, lower


def vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum()


# Candles por dia em cada timeframe (Binance Futures: mercado 24h)
_CANDLES_PER_DAY: dict[str, float] = {
    "1m": 1440, "3m": 480,  "5m": 288,
    "15m": 96,  "30m": 48,  "1h": 24,  "2h": 12,
    "4h": 6,    "6h": 4,    "8h": 3,   "12h": 2,
    "1d": 1,    "3d": 0.33, "1w": 0.14,
}

# Dias-alvo de lookback por categoria (centro do intervalo do usuário)
#   scalp  (1m/3m/5m)      → 1-2 dias  → usa 2d
#   day    (15m/30m/1h/2h) → 3-5 dias  → usa 4d
#   swing  (4h+)           → 5-15 dias → usa 10d
_LOOKBACK_DAYS: dict[str, float] = {
    "scalp": 2.0,
    "day":   4.0,
    "swing": 10.0,
}

_SCALP_TFS = {"1m", "3m", "5m"}
_DAY_TFS   = {"15m", "30m", "1h", "2h"}


def _structure_lookback(timeframe: str) -> int:
    """
    Retorna o número de candles para análise de estrutura, escalado pelo TF.
    scalp=1-2 dias | day trade=3-5 dias | swing=5-15 dias (em candles).
    Limitado entre 15 e 250 (dentro do DF disponível).
    """
    cpd  = _CANDLES_PER_DAY.get(timeframe, 24)
    if timeframe in _SCALP_TFS:
        days = _LOOKBACK_DAYS["scalp"]   # 2 dias
    elif timeframe in _DAY_TFS:
        days = _LOOKBACK_DAYS["day"]     # 4 dias
    else:
        days = _LOOKBACK_DAYS["swing"]   # 10 dias
    return max(15, min(int(cpd * days), 250))


def identify_structure(df: pd.DataFrame, timeframe: str = "") -> dict:
    """
    Detecta HH/HL (uptrend) ou LH/LL (downtrend) e níveis S/R.
    Lookback escalado pelo timeframe:
      scalp (1-2d) | day trade (3-5d) | swing (5-15d).
    """
    lookback = _structure_lookback(timeframe) if timeframe else 20
    highs = df["high"].values[-lookback:]
    lows  = df["low"].values[-lookback:]

    swing_highs = [highs[i] for i in range(1, len(highs)-1)
                   if highs[i] > highs[i-1] and highs[i] > highs[i+1]]
    swing_lows  = [lows[i]  for i in range(1, len(lows)-1)
                   if lows[i]  < lows[i-1]  and lows[i]  < lows[i+1]]

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1]  > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1]  < swing_lows[-2]
        if hh and hl:
            structure = "UPTREND"
        elif lh and ll:
            structure = "DOWNTREND"
        else:
            structure = "RANGING"
    else:
        structure = "RANGING"

    resistance = max(swing_highs[-3:]) if len(swing_highs) >= 3 else float(df["high"].max())
    support    = min(swing_lows[-3:])  if len(swing_lows)  >= 3 else float(df["low"].min())

    return {"structure": structure, "resistance": resistance, "support": support}


# ── Score EMA Fast Cross (SCALP) ──────────────────────────────────────────────

def score_ema_cross(df: pd.DataFrame, direction: Direction) -> float:
    """
    EMA 9/21 crossover — ideal para scalp em 3m/5m.
    Detecta: cruzamento recente, slope, distância.
    """
    close = df["close"]
    e9 = ema(close, 9)
    e21 = ema(close, 21)
    e55 = ema(close, 55)

    e9_now = e9.iloc[-1]
    e21_now = e21.iloc[-1]
    e9_prev = e9.iloc[-2]
    e21_prev = e21.iloc[-2]
    price = close.iloc[-1]

    score = 0

    if direction == Direction.LONG:
        # EMA 9 acima de EMA 21
        if e9_now > e21_now:
            score += 35
        # Cruzamento recente (golden cross)
        if e9_prev <= e21_prev and e9_now > e21_now:
            score += 25  # bônus cruzamento
        # EMA 21 acima de EMA 55 (tendência maior confirma)
        if e21_now > e55.iloc[-1]:
            score += 20
        # Slope positivo (EMA 9 subindo)
        if e9.iloc[-1] > e9.iloc[-3]:
            score += 20
    else:
        if e9_now < e21_now:
            score += 35
        if e9_prev >= e21_prev and e9_now < e21_now:
            score += 25  # death cross
        if e21_now < e55.iloc[-1]:
            score += 20
        if e9.iloc[-1] < e9.iloc[-3]:
            score += 20

    return max(0, min(100, score))


# ── Score Trend (DAY/SWING) ───────────────────────────────────────────────────

def score_trend(df: pd.DataFrame, direction: Direction) -> float:
    """EMA 21/55/200 para timeframes maiores."""
    close = df["close"]
    e21 = ema(close, 21).iloc[-1]
    e55 = ema(close, 55).iloc[-1]
    e200 = ema(close, 200).iloc[-1]
    price = close.iloc[-1]

    if direction == Direction.LONG:
        score = 0
        if price > e21: score += 30
        if e21 > e55: score += 30
        if price > e200: score += 25
        if e55 > e200: score += 15
        return score
    else:
        score = 0
        if price < e21: score += 30
        if e21 < e55: score += 30
        if price < e200: score += 25
        if e55 < e200: score += 15
        return score


# ── Score Volume / CVD proxy ──────────────────────────────────────────────────

def score_volume(df: pd.DataFrame, direction: Direction) -> float:
    """
    Volume spike + direção do candle + CVD proxy (compra vs venda).
    CVD proxy: soma de (volume × sinal do candle) nas últimas N velas.
    """
    vol = df["volume"]
    close = df["close"]
    open_ = df["open"]

    avg_vol = vol.rolling(20).mean().iloc[-1]
    last_vol = vol.iloc[-1]
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    score = min(vol_ratio * 35, 65)

    # Confirmação de direção no último candle
    last = df.iloc[-1]
    bullish = last["close"] > last["open"]
    if direction == Direction.LONG and bullish:
        score += 20
    elif direction == Direction.SHORT and not bullish:
        score += 20
    else:
        score -= 15

    # CVD proxy: últimas 5 velas
    last5 = df.iloc[-5:]
    bull_vol = last5.loc[last5["close"] > last5["open"], "volume"].sum()
    bear_vol = last5.loc[last5["close"] <= last5["open"], "volume"].sum()
    total_vol = bull_vol + bear_vol
    if total_vol > 0:
        if direction == Direction.LONG:
            cvd_pct = bull_vol / total_vol
        else:
            cvd_pct = bear_vol / total_vol
        # +15 se >65% do volume na direção correta
        if cvd_pct > 0.65:
            score += 15
        elif cvd_pct > 0.55:
            score += 8

    return max(0, min(100, score))


# ── Score Momentum — Stoch RSI + MACD ────────────────────────────────────────

def score_momentum(df: pd.DataFrame, direction: Direction, scalp: bool = False) -> float:
    """
    Para scalp: usa Stoch RSI (mais sensível).
    Para day/swing: usa RSI + MACD.
    """
    close = df["close"]
    _, _, macd_hist = macd(close)
    hist_val = macd_hist.iloc[-1]
    prev_hist = macd_hist.iloc[-2]

    score = 0

    if scalp:
        # Stochastic RSI
        k, d = stoch_rsi(close)
        k_val = k.iloc[-1]
        d_val = d.iloc[-1]
        k_prev = k.iloc[-2]

        if direction == Direction.LONG:
            # Oversold (zona de compra): k < 20
            if k_val < 20:
                score += 40
            elif k_val < 40:
                score += 25
            elif k_val < 60:
                score += 15
            # K cruzou D para cima (sinal de entrada)
            if k_prev <= d.iloc[-2] and k_val > d_val:
                score += 30
            elif k_val > d_val:
                score += 15
            # MACD confirmando
            if hist_val > 0 and hist_val > prev_hist:
                score += 30
            elif hist_val > prev_hist:
                score += 15
        else:
            if k_val > 80:
                score += 40
            elif k_val > 60:
                score += 25
            elif k_val > 40:
                score += 15
            if k_prev >= d.iloc[-2] and k_val < d_val:
                score += 30
            elif k_val < d_val:
                score += 15
            if hist_val < 0 and hist_val < prev_hist:
                score += 30
            elif hist_val < prev_hist:
                score += 15
        # RSI normal para day/swing
        rsi_val = rsi(close).iloc[-1]
        if direction == Direction.LONG:
            # Gradação de RSI para LONG: recompensa momentum sem aceitar sobrecompras próximas a topos
            if 40 < rsi_val <= 55:
                score += 40
            elif 55 < rsi_val < 70:
                score += 20  # Reduz pontuação conforme se aproxima do topo (70)
            elif rsi_val <= 40:
                score += 20
            
            # Corrige bug do MACD que acumulava pontos de forma independente (Double-count)
            if hist_val > 0 and hist_val > prev_hist:
                score += 30  # Altamente bullish e acelerando
            elif hist_val > 0 or hist_val > prev_hist:
                score += 15  # Apenas um dos dois fatores positivos
        else:
            # Gradação de RSI para SHORT
            if 45 <= rsi_val < 60:
                score += 40
            elif 30 < rsi_val < 45:
                score += 20
            elif rsi_val >= 60:
                score += 20
                
            # Corrige bug do MACD para SHORT
            if hist_val < 0 and hist_val < prev_hist:
                score += 30
            elif hist_val < 0 or hist_val < prev_hist:
                score += 15

    return max(0, min(100, score))


# ── Score VWAP ────────────────────────────────────────────────────────────────

def score_vwap(df: pd.DataFrame, direction: Direction) -> float:
    """
    Posição vs VWAP e distância.
    - Preço acima VWAP = bias LONG
    - Preço abaixo VWAP = bias SHORT
    - Próximo ao VWAP (0.1-0.5%): melhor entrada
    - Muito longe do VWAP (>2%): overextended, menos confiável
    """
    vwap_series = vwap(df)
    vwap_val = vwap_series.iloc[-1]
    price = df["close"].iloc[-1]

    if vwap_val <= 0:
        return 50

    dist_pct = (price - vwap_val) / vwap_val * 100  # positivo = acima VWAP

    score = 0
    if direction == Direction.LONG:
        if dist_pct > 0:  # acima do VWAP
            score += 40
            if 0.1 < dist_pct < 0.5:
                score += 30  # pullback ao VWAP = ótima entrada
            elif 0.5 < dist_pct < 1.5:
                score += 15  # breakout válido
            elif dist_pct > 2.5:
                score -= 20  # overextended, risco de reversão
        else:  # abaixo do VWAP
            score += 10  # possível bounce, mas fraco
            if abs(dist_pct) < 0.3:
                score += 20  # muito próximo, pode testar acima
    else:
        if dist_pct < 0:  # abaixo do VWAP
            score += 40
            if -0.5 < dist_pct < -0.1:
                score += 30
            elif -1.5 < dist_pct < -0.5:
                score += 15
            elif dist_pct < -2.5:
                score -= 20
        else:
            score += 10
            if dist_pct < 0.3:
                score += 20

    return max(0, min(100, score))


# ── Score Market Structure + Bollinger Bands ──────────────────────────────────

def score_market_structure(df: pd.DataFrame, direction: Direction,
                           timeframe: str = "") -> float:
    """
    Estrutura de mercado (HH/HL/LH/LL) + Bollinger Band squeeze e breakout.
    Lookback escalado pelo TF: scalp=2d | day=4d | swing=10d.
    """
    struct = identify_structure(df, timeframe)
    s = struct["structure"]
    price = df["close"].iloc[-1]
    support = struct["support"]
    resistance = struct["resistance"]

    score = 0

    # Estrutura
    if direction == Direction.LONG:
        if s == "UPTREND": score += 40
        elif s == "RANGING": score += 20
        dist_from_support = (price - support) / support * 100 if support > 0 else 5
        if 0 < dist_from_support < 2:
            score += 30  # próximo ao suporte = entrada boa
        elif dist_from_support < 6:
            score += 15
        
        # Penaliza proximidade de resistência (evita comprar no topo da resistência)
        if resistance > 0:
            dist_to_resistance = (resistance - price) / resistance * 100
            if dist_to_resistance < 1.0:
                score -= 30  # Muito colado na resistência
            elif dist_to_resistance < 2.0:
                score -= 15
    else:
        if s == "DOWNTREND": score += 40
        elif s == "RANGING": score += 20
        dist_from_resistance = (resistance - price) / resistance * 100 if resistance > 0 else 5
        if 0 < dist_from_resistance < 2:
            score += 30
        elif dist_from_resistance < 6:
            score += 15
            
        # Penaliza proximidade de suporte (evita vender no fundo do suporte)
        if support > 0:
            dist_to_support = (price - support) / support * 100
            if dist_to_support < 1.0:
                score -= 30  # Muito colado no suporte
            elif dist_to_support < 2.0:
                score -= 15

    # Bollinger Bands
    close = df["close"]
    upper, mid, lower = bollinger_bands(close)
    bb_upper = upper.iloc[-1]
    bb_lower = lower.iloc[-1]
    bb_mid = mid.iloc[-1]

    # BB width (squeeze detection)
    bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
    avg_width = ((upper - lower) / mid).rolling(20).mean().iloc[-1]
    squeeze = bb_width < avg_width * 0.7  # largura < 70% da média = squeeze

    if direction == Direction.LONG:
        if price > bb_mid:
            score += 15  # acima da média BB
        if price <= bb_lower * 1.002:
            score += 20  # toque na banda inferior = reversão
        if squeeze and price > bb_mid:
            score += 15  # breakout de squeeze para cima
    else:
        if price < bb_mid:
            score += 15
        if price >= bb_upper * 0.998:
            score += 20  # toque na banda superior
        if squeeze and price < bb_mid:
            score += 15

    return max(0, min(100, score))


# ── Score Funding/OI ──────────────────────────────────────────────────────────

async def score_funding_oi(symbol: str, direction: Direction) -> float:
    """
    Score multi-dimensional:
      Funding Rate  — taxa entre longs/shorts a cada 8h (contrarian ou confirmatório)
      L/S Ratio     — % de contas em long vs short (contrarian)
      OI Momentum   — variação do Open Interest (histórico 15m): novo dinheiro ou liquidação
    """
    try:
        fr      = await get_funding_rate(symbol)
        _       = await get_open_interest(symbol)   # mantido para compatibilidade futura
        ls      = await get_long_short_ratio(symbol)

        score = 50  # baseline

        # ── Funding Rate ─────────────────────────────────────────────────────
        if direction == Direction.LONG:
            if fr < -0.01:    score += 30   # shorts pagando longs — contrarian bullish
            elif fr < 0.01:   score += 15   # neutro — condições normais
            elif fr > 0.05:   score -= 25   # longs overextended
            elif fr > 0.03:   score -= 12
            # L/S contrarian
            if ls["long_pct"] < 40:    score += 25  # maioria short = smart money long
            elif ls["long_pct"] < 45:  score += 15
            elif ls["long_pct"] > 72:  score -= 15  # excesso de longs = armadilha
        else:
            if fr > 0.05:     score += 30
            elif fr > 0.01:   score += 15
            elif fr < -0.01:  score -= 25
            elif fr < -0.03:  score -= 12
            if ls["short_pct"] < 40:   score += 25
            elif ls["short_pct"] < 45: score += 15
            elif ls["short_pct"] > 72: score -= 15

        # ── OI Momentum (histórico 8×15m) ────────────────────────────────────
        # OI crescendo = novo dinheiro entrando no mercado (confirma tendência)
        # OI caindo   = posições sendo fechadas ou liquidadas (enfraquece movimento)
        try:
            oi_hist = await get_oi_history(symbol, period="15m", limit=8)
            if len(oi_hist) >= 4:
                oi_vals   = [x["oi"] for x in oi_hist]
                oi_recent = sum(oi_vals[-3:]) / 3
                oi_base   = sum(oi_vals[:3]) / 3
                if oi_base > 0:
                    oi_chg_pct = (oi_recent - oi_base) / oi_base * 100
                    if direction == Direction.LONG:
                        if oi_chg_pct > 3.0:    score += 15   # acumulação com capital novo
                        elif oi_chg_pct > 1.0:  score += 8
                        elif oi_chg_pct < -3.0: score -= 12  # liquidação em curso
                        elif oi_chg_pct < -1.0: score -= 6
                    else:
                        if oi_chg_pct > 3.0:    score += 8   # novos shorts entrando
                        elif oi_chg_pct > 1.0:  score += 4
                        elif oi_chg_pct < -5.0: score -= 15  # short squeeze iminente
                        elif oi_chg_pct < -3.0: score -= 8
        except Exception:
            pass

        return max(0, min(100, score))
    except Exception:
        return 50


async def score_orderbook_liquidity(symbol: str, direction: Direction,
                                    max_spread_pct: float = 0.5) -> tuple[float, str]:
    """
    Gate ÚNICO de liquidez via profundidade do orderbook (substitui o filtro
    duplicado de spread bid/ask que existia no job_auto_trade).

    Args:
      max_spread_pct: spread máximo aceito (vem do perfil ativo — CONSERVATIVE=0.10,
                      NORMAL=0.25, AGGRESSIVE=0.50).

    Returns:
      (score: 0-100, block_reason: str)
      block_reason != "" → sinal deve ser bloqueado.

    Critérios:
      Spread > max_spread:  BLOQUEIA (slippage severo / baixa liquidez)
      Spread 0.15–max:      penalidade -20pts
      Spread < 0.05%:       bônus +15pts (mercado líquido)
      Imbalance favorável:  +25pts (bids > asks para LONG / asks > bids para SHORT)
      Profundidade < $100K: penalidade -15pts
    """
    try:
        ob         = await get_orderbook_depth(symbol, limit=20)
        spread_pct = ob.get("spread_pct", 0.0)
        imbalance  = ob.get("imbalance", 0.0)    # +1=só bids, -1=só asks
        bid_depth  = ob.get("bid_depth_usdt", 0.0)
        ask_depth  = ob.get("ask_depth_usdt", 0.0)

        if spread_pct > max_spread_pct:
            return 0.0, f"Spread {spread_pct:.2f}% > {max_spread_pct:.2f}% — liquidez insuficiente"

        score = 50

        # Spread
        if spread_pct < 0.05:    score += 15
        elif spread_pct < 0.10:  score += 8
        elif spread_pct > 0.15:  score -= 20

        # Profundidade mínima
        if bid_depth < 100_000 or ask_depth < 100_000:
            score -= 15

        # Imbalance direcional
        if direction == Direction.LONG:
            if imbalance > 0.20:    score += 25
            elif imbalance > 0.10:  score += 15
            elif imbalance < -0.20: score -= 20
            elif imbalance < -0.10: score -= 10
        else:
            if imbalance < -0.20:   score += 25
            elif imbalance < -0.10: score += 15
            elif imbalance > 0.20:  score -= 20
            elif imbalance > 0.10:  score -= 10

        return max(0.0, min(100.0, float(score))), ""
    except Exception:
        return 50.0, ""


# ── Candlestick Pattern Bonus ─────────────────────────────────────────────────

def candle_pattern_bonus(df: pd.DataFrame, direction: Direction) -> float:
    """
    Detecta padrões de candle usando o CandlePatternEngine (41 padrões).
    Retorna bonus 0-20pts para o signal_engine.
    """
    from candle_pattern_engine import detect_patterns, get_pattern_bonus
    if len(df) < 5:
        return 0.0
    patterns = detect_patterns(df)
    return min(get_pattern_bonus(patterns, direction), 20.0)


# ── Fibonacci Retracement ─────────────────────────────────────────────────────

def fibonacci_levels(df: pd.DataFrame, lookback: int = 50) -> dict:
    """Calcula níveis de Fibonacci do swing high/low das últimas N velas."""
    window = df.iloc[-lookback:]
    swing_high = float(window["high"].max())
    swing_low = float(window["low"].min())
    rng = swing_high - swing_low
    if rng <= 0:
        return {}
    return {
        "swing_high": swing_high,
        "swing_low":  swing_low,
        "fib_236":    swing_high - 0.236 * rng,
        "fib_382":    swing_high - 0.382 * rng,
        "fib_500":    swing_high - 0.500 * rng,
        "fib_618":    swing_high - 0.618 * rng,
        "fib_786":    swing_high - 0.786 * rng,
    }


def score_fibonacci_confluence(df: pd.DataFrame, direction: Direction, tol_pct: float = 0.5) -> float:
    """
    Bônus quando preço está numa zona de Fibonacci (golden zones: 0.382/0.5/0.618).
    LONG: suporte fib (retração de alta) | SHORT: resistência fib (retração de baixa).
    Retorna 0, 5 ou 10 pontos.
    """
    try:
        fibs = fibonacci_levels(df)
        if not fibs:
            return 0.0
        price = df["close"].iloc[-1]
        tol = price * tol_pct / 100
        golden = [fibs["fib_382"], fibs["fib_500"], fibs["fib_618"]]
        minor  = [fibs["fib_236"], fibs["fib_786"]]
        for level in golden:
            if abs(price - level) <= tol:
                return 10.0
        for level in minor:
            if abs(price - level) <= tol:
                return 5.0
        return 0.0
    except Exception:
        return 0.0


# ── RSI Divergência (regular + oculta) ───────────────────────────────────────

def detect_rsi_divergence(df: pd.DataFrame, direction: Direction, lookback: int = 30) -> tuple[bool, str]:
    """
    Detecta divergencias RSI.
    Regular: preco faz novo extremo mas RSI nao confirma → reversao.
    Oculta:  preco corrige mas RSI nao confirma a correcao → continuacao.
    Retorna (encontrou, tipo) onde tipo = "regular" | "hidden" | "".
    """
    if len(df) < lookback + 5:
        return False, ""
    try:
        close = df["close"].iloc[-lookback:]
        rsi_s = rsi(df["close"]).iloc[-lookback:]

        prices = close.values
        rsi_v  = rsi_s.values

        # Encontra picos/vales no preco e no RSI
        def local_highs(arr):
            return [i for i in range(1, len(arr)-1) if arr[i] > arr[i-1] and arr[i] > arr[i+1]]
        def local_lows(arr):
            return [i for i in range(1, len(arr)-1) if arr[i] < arr[i-1] and arr[i] < arr[i+1]]

        if direction == Direction.LONG:
            # Divergencia regular bullish: preco faz LL, RSI faz HL
            lows_p = local_lows(prices)
            lows_r = local_lows(rsi_v)
            if len(lows_p) >= 2 and len(lows_r) >= 2:
                p1, p2 = lows_p[-2], lows_p[-1]
                r1, r2 = lows_r[-2], lows_r[-1]
                if abs(p1 - r1) <= 3 and abs(p2 - r2) <= 3:
                    if prices[p2] < prices[p1] and rsi_v[r2] > rsi_v[r1]:
                        return True, "regular"
            # Divergencia oculta bullish: preco faz HL, RSI faz LL
            if len(lows_p) >= 2 and len(lows_r) >= 2:
                p1, p2 = lows_p[-2], lows_p[-1]
                r1, r2 = lows_r[-2], lows_r[-1]
                if abs(p1 - r1) <= 3 and abs(p2 - r2) <= 3:
                    if prices[p2] > prices[p1] and rsi_v[r2] < rsi_v[r1]:
                        return True, "hidden"
        else:
            # Divergencia regular bearish: preco faz HH, RSI faz LH
            highs_p = local_highs(prices)
            highs_r = local_highs(rsi_v)
            if len(highs_p) >= 2 and len(highs_r) >= 2:
                p1, p2 = highs_p[-2], highs_p[-1]
                r1, r2 = highs_r[-2], highs_r[-1]
                if abs(p1 - r1) <= 3 and abs(p2 - r2) <= 3:
                    if prices[p2] > prices[p1] and rsi_v[r2] < rsi_v[r1]:
                        return True, "regular"
            # Divergencia oculta bearish: preco faz LH, RSI faz HH
            if len(highs_p) >= 2 and len(highs_r) >= 2:
                p1, p2 = highs_p[-2], highs_p[-1]
                r1, r2 = highs_r[-2], highs_r[-1]
                if abs(p1 - r1) <= 3 and abs(p2 - r2) <= 3:
                    if prices[p2] < prices[p1] and rsi_v[r2] > rsi_v[r1]:
                        return True, "hidden"
    except Exception:
        pass
    return False, ""


def score_rsi_divergence(df: pd.DataFrame, direction: Direction) -> float:
    """
    Bonus: divergencia regular = 15pts (reversao de alta confiabilidade),
           divergencia oculta   = 8pts (continuacao).
    """
    found, div_type = detect_rsi_divergence(df, direction)
    if not found:
        return 0.0
    return 15.0 if div_type == "regular" else 8.0


# ── CVD Real & Divergências de Microestrutura ───────────────────────────────

def calculate_real_cvd(df: pd.DataFrame) -> pd.Series:
    """Calcula CVD real usando taker buy volume e volume total de futuros."""
    if "taker_buy_base" not in df.columns or "volume" not in df.columns:
        return pd.Series(0.0, index=df.index)
    tb = df["taker_buy_base"].astype(float)
    v = df["volume"].astype(float)
    delta = 2 * tb - v
    return delta.cumsum()


def detect_cvd_divergence(df: pd.DataFrame, direction: Direction, lookback: int = 20) -> tuple[bool, str]:
    """
    Detecta divergências entre o preço e o CVD real.
    Bullish: Preço faz mínima menor, mas CVD faz mínima maior (absorção).
    Bearish: Preço faz máxima maior, mas CVD faz máxima menor (exaustão).
    """
    if len(df) < lookback + 5:
        return False, ""
    try:
        closes = df["close"].iloc[-lookback:].values
        cvd_vals = calculate_real_cvd(df).iloc[-lookback:].values

        def local_highs(arr):
            return [i for i in range(1, len(arr)-1) if arr[i] > arr[i-1] and arr[i] > arr[i+1]]
        def local_lows(arr):
            return [i for i in range(1, len(arr)-1) if arr[i] < arr[i-1] and arr[i] < arr[i+1]]

        if direction == Direction.LONG:
            lows_p = local_lows(closes)
            lows_c = local_lows(cvd_vals)
            if len(lows_p) >= 2 and len(lows_c) >= 2:
                p1, p2 = lows_p[-2], lows_p[-1]
                c1, c2 = lows_c[-2], lows_c[-1]
                if abs(p1 - c1) <= 3 and abs(p2 - c2) <= 3:
                    if closes[p2] < closes[p1] and cvd_vals[c2] > cvd_vals[c1]:
                        return True, "regular"
                    if closes[p2] > closes[p1] and cvd_vals[c2] < cvd_vals[c1]:
                        return True, "hidden"
        else:
            highs_p = local_highs(closes)
            highs_c = local_highs(cvd_vals)
            if len(highs_p) >= 2 and len(highs_c) >= 2:
                p1, p2 = highs_p[-2], highs_p[-1]
                c1, c2 = highs_c[-2], highs_c[-1]
                if abs(p1 - c1) <= 3 and abs(p2 - c2) <= 3:
                    if closes[p2] > closes[p1] and cvd_vals[c2] < cvd_vals[c1]:
                        return True, "regular"
                    if closes[p2] < closes[p1] and cvd_vals[c2] > cvd_vals[c1]:
                        return True, "hidden"
    except Exception:
        pass
    return False, ""


def score_cvd_divergence(df: pd.DataFrame, direction: Direction) -> float:
    """Retorna bônus de score se houver divergência no CVD real."""
    found, div_type = detect_cvd_divergence(df, direction)
    if not found:
        return 0.0
    return 15.0 if div_type == "regular" else 8.0


# ── Smart Money Concepts: Liquidity Sweep Score ───────────────────────────────

def score_liquidity_sweep(df: pd.DataFrame, direction: Direction) -> float:
    """Retorna bônus de score se houver varredura de liquidez (SMC)."""
    try:
        import supply_demand as sd
        res = sd.detect_liquidity_sweep(df)
        if not res.get("sweep"):
            return 0.0
        sweep_type = res["type"]
        if direction == Direction.LONG and sweep_type == "BULLISH_SWEEP":
            return 18.0
        if direction == Direction.SHORT and sweep_type == "BEARISH_SWEEP":
            return 18.0
    except Exception:
        pass
    return 0.0


# ── Golden Cross / Death Cross EMA 50/200 ────────────────────────────────────

def score_golden_death_cross(df: pd.DataFrame, direction: Direction) -> float:
    """
    Detecta cruzamentos EMA 50/200 e alinhamento de longo prazo.
    Golden Cross (50 cruza acima 200): bonus LONG 12pts, penalidade SHORT -8pts.
    Death Cross  (50 cruza abaixo 200): bonus SHORT 12pts, penalidade LONG -8pts.
    Sem cruzamento recente: bonus menor pelo alinhamento.
    """
    if len(df) < 205:
        return 0.0
    try:
        close  = df["close"]
        e50    = ema(close, 50)
        e200   = ema(close, 200)

        e50_now  = e50.iloc[-1]
        e50_prev = e50.iloc[-2]
        e200_now = e200.iloc[-1]
        e200_prev= e200.iloc[-2]

        golden_cross = e50_prev <= e200_prev and e50_now > e200_now
        death_cross  = e50_prev >= e200_prev and e50_now < e200_now

        if direction == Direction.LONG:
            if golden_cross:
                return 12.0
            if death_cross:
                return -8.0
            if e50_now > e200_now:
                return 6.0   # tendencia bull confirmada
            return -3.0      # tendencia bear de longo prazo
        else:
            if death_cross:
                return 12.0
            if golden_cross:
                return -8.0
            if e50_now < e200_now:
                return 6.0
            return -3.0
    except Exception:
        return 0.0


# ── Liquidation Cascade Score ─────────────────────────────────────────────────

def score_liquidation_cascade(symbol: str, direction: Direction) -> tuple[float, str]:
    """
    Bônus quando há cascata de liquidações a favor da direção (reversão).
    Lê o feed de liquidações em tempo real do ws_feed (não bloqueia, não faz I/O).

    LONG  + shorts liquidados em massa (short squeeze) = +bônus
    SHORT + longs liquidados em massa (long flush)      = +bônus

    Retorna (bonus 0-15, tag).
    """
    try:
        import ws_feed
        casc = ws_feed.liquidation_cascade(symbol, window_s=300, min_usdt=1_000_000)
        if not casc["detected"]:
            return 0.0, ""
        bias = casc["bias"]
        total = casc["total_usdt"]
        # Escala do bônus pelo volume liquidado
        mag = 15.0 if total >= 5_000_000 else 10.0 if total >= 2_000_000 else 6.0
        if direction == Direction.LONG and bias == "BULLISH":
            return mag, f"SHORT-SQUEEZE ${total/1e6:.1f}M"
        if direction == Direction.SHORT and bias == "BEARISH":
            return mag, f"LONG-FLUSH ${total/1e6:.1f}M"
        # Cascata contra a direção = penalidade leve (não entrar contra fluxo)
        if direction == Direction.LONG and bias == "BEARISH":
            return -6.0, "LIQ-CONTRA"
        if direction == Direction.SHORT and bias == "BULLISH":
            return -6.0, "LIQ-CONTRA"
        return 0.0, ""
    except Exception:
        return 0.0, ""


# ── Macro Cycle Score ─────────────────────────────────────────────────────────

def score_macro_cycle(df: pd.DataFrame, direction: Direction) -> float:
    """
    Avalia o ciclo macro baseado na posicao do preco vs EMA50 e EMA200.
    Bonus quando operando a favor do ciclo, penalidade contra.
    """
    if len(df) < 205:
        return 0.0
    try:
        close = df["close"]
        price = close.iloc[-1]
        e50   = ema(close, 50).iloc[-1]
        e200  = ema(close, 200).iloc[-1]

        # Distancia % do preco vs EMAs longas
        dist_e50  = (price - e50)  / e50  * 100
        dist_e200 = (price - e200) / e200 * 100

        if direction == Direction.LONG:
            if price > e50 and e50 > e200:
                # Ciclo bull perfeito
                if dist_e50 < 3.0:
                    return 15.0   # pullback ao suporte macro = ótima entrada
                elif dist_e50 < 8.0:
                    return 10.0   # ainda dentro do range saudavel
                else:
                    return 3.0    # overextended, mas bull
            elif price > e200 and price < e50:
                return 5.0        # abaixo da EMA50 mas acima da 200 — indecisao
            elif price < e200:
                return -10.0      # abaixo da EMA200 = contra tendencia macro
        else:
            if price < e50 and e50 < e200:
                dist_neg = abs(dist_e50)
                if dist_neg < 3.0:
                    return 15.0
                elif dist_neg < 8.0:
                    return 10.0
                else:
                    return 3.0
            elif price < e200 and price > e50:
                return 5.0
            elif price > e200:
                return -10.0
    except Exception:
        pass
    return 0.0


# ── R/R Dinâmico ─────────────────────────────────────────────────────────────

def dynamic_rr_multipliers(df: pd.DataFrame) -> tuple[float, list]:
    """
    Calcula multiplicadores de SL e TP dinamicamente.
    Em tendencia forte: sl menor, alvos maiores (captura mais do movimento).
    Em mercado lateral: sl menor, alvos mais conservadores (evita reversao).
    Retorna (sl_mult, [tp1_mult, tp2_mult, tp3_mult]).
    """
    if len(df) < 60:
        return 1.5, [2.0, 3.5, 5.5]
    try:
        close = df["close"]
        e21  = ema(close, 21).iloc[-1]
        e55  = ema(close, 55).iloc[-1]
        e200_s = ema(close, 200)
        e200   = e200_s.iloc[-1] if len(df) >= 200 else None

        # Slope da EMA21 — medida de forcda tendencia
        slope = (ema(close, 21).iloc[-1] - ema(close, 21).iloc[-5]) / ema(close, 21).iloc[-5] * 100

        if e200 and e21 > e55 > e200 and slope > 0.3:
            # Tendencia forte de alta: alvos generosos
            return 1.3, [2.0, 4.0, 7.0]
        elif e200 and e21 < e55 < e200 and slope < -0.3:
            # Tendencia forte de baixa
            return 1.3, [2.0, 4.0, 7.0]
        elif abs(slope) > 0.2:
            # Tendencia moderada
            return 1.5, [2.0, 3.5, 5.5]
        else:
            # Mercado lateral: SL menor, alvos mais conservadores
            return 1.2, [1.8, 3.0, 4.5]
    except Exception:
        return 1.5, [2.0, 3.5, 5.5]


# ── News Score ────────────────────────────────────────────────────────────────

def score_news(news_data: list, direction: Direction) -> float:
    if not news_data:
        return 50
    sentiments = [n.get("sentiment", "NEUTRAL") for n in news_data[:5]]
    bullish = sentiments.count("BULLISH")
    bearish = sentiments.count("BEARISH")
    total = len(sentiments)
    if direction == Direction.LONG:
        return min(100, 50 + (bullish - bearish) / total * 50)
    else:
        return min(100, 50 + (bearish - bullish) / total * 50)


# ── Risk/Reward & TP/SL ───────────────────────────────────────────────────────

def calculate_levels(df: pd.DataFrame, direction: Direction, symbol: str,
                     timeframe: str = "") -> dict:
    atr_val = atr(df).iloc[-1]
    price   = df["close"].iloc[-1]
    struct  = identify_structure(df, timeframe)

    sl_multiplier, tp_multipliers = dynamic_rr_multipliers(df)

    if direction == Direction.LONG:
        stop = max(
            price - atr_val * sl_multiplier,
            struct["support"] * 0.998,
        )
        tp1 = price + atr_val * tp_multipliers[0]
        tp2 = price + atr_val * tp_multipliers[1]
        tp3 = price + atr_val * tp_multipliers[2]
    else:
        stop = min(
            price + atr_val * sl_multiplier,
            struct["resistance"] * 1.002,
        )
        tp1 = price - atr_val * tp_multipliers[0]
        tp2 = price - atr_val * tp_multipliers[1]
        tp3 = price - atr_val * tp_multipliers[2]

    risk = abs(price - stop)
    reward = abs(tp2 - price)
    rr = reward / risk if risk > 0 else 0

    return {
        "entry": round(price, 6),
        "stop_loss": round(stop, 6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "tp3": round(tp3, 6),
        "rr": round(rr, 2),
        "atr": round(atr_val, 6),
    }


def classify_trade_type(timeframe: str) -> str:
    scalp_tfs = {"1m", "3m", "5m"}
    swing_tfs = {"4h", "6h", "12h", "1d", "3d", "1w"}
    if timeframe in scalp_tfs:
        return "SCALP"
    if timeframe in swing_tfs:
        return "SWING"
    return "DAY_TRADE"


def detect_anomaly(df: pd.DataFrame) -> str:
    """Detecta volume spike, candle explosivo, rápida direcional."""
    anomalies = []
    vol = df["volume"]
    close = df["close"]
    avg_vol = vol.rolling(20).mean().iloc[-1]
    last_vol = vol.iloc[-1]
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    if vol_ratio >= 3.0:
        anomalies.append(f"VOLUME SPIKE {vol_ratio:.1f}x")
    elif vol_ratio >= 2.0:
        anomalies.append(f"Volume alto {vol_ratio:.1f}x")

    atr_val = atr(df).iloc[-1]
    last = df.iloc[-1]
    body = abs(last["close"] - last["open"])
    if atr_val > 0 and body > atr_val * 2.5:
        pct = (body / last["open"]) * 100
        anomalies.append(f"Candle explosivo +{pct:.1f}%")

    returns = close.pct_change().iloc[-3:]
    if all(r > 0.005 for r in returns):
        total_move = (close.iloc[-1] / close.iloc[-4] - 1) * 100
        anomalies.append(f"Alta rapida +{total_move:.1f}% (3 velas)")
    elif all(r < -0.005 for r in returns):
        total_move = (close.iloc[-1] / close.iloc[-4] - 1) * 100
        anomalies.append(f"Queda rapida {total_move:.1f}% (3 velas)")

    return " | ".join(anomalies) if anomalies else ""


def build_reason(score_obj, direction: Direction, df: pd.DataFrame,
                 scalp: bool = False, timeframe: str = "") -> str:
    """Reason string rica com indicadores chave."""
    close  = df["close"]
    price  = close.iloc[-1]
    struct = identify_structure(df, timeframe)["structure"]

    if scalp:
        k, d = stoch_rsi(close)
        k_val = round(k.iloc[-1], 1)
        e9_val = round(ema(close, 9).iloc[-1], 6)
        e21_val = round(ema(close, 21).iloc[-1], 6)
        cross = "9>21" if e9_val > e21_val else "9<21"
        vwap_val = round(vwap(df).iloc[-1], 6)
        vwap_side = "acima" if price > vwap_val else "abaixo"
        parts = [
            f"Struct: {struct}",
            f"StochRSI K: {k_val}",
            f"EMA {cross}",
            f"VWAP {vwap_side}",
            f"Score: {round(score_obj.total, 1)}/100",
            f"Dir: {direction.value}",
        ]
    else:
        rsi_val = round(rsi(close).iloc[-1], 1)
        e21  = ema(close, 21).iloc[-1]
        e200 = ema(close, 200).iloc[-1] if len(close) >= 200 else None
        trend_word = "acima" if price > e21 else "abaixo"
        _, div_type = detect_rsi_divergence(df, direction)
        div_str = f" | RSI-Div:{div_type}" if div_type else ""
        e200_str = f" | EMA200:{'acima' if e200 and price > e200 else 'abaixo'}" if e200 else ""
        parts = [
            f"Struct: {struct}",
            f"EMA21 {trend_word}{e200_str}",
            f"RSI: {rsi_val}{div_str}",
            f"Score: {round(score_obj.total, 1)}/100",
            f"Dir: {direction.value}",
        ]
    return " | ".join(parts)


# ── Score Composto com Pesos Adaptativos ──────────────────────────────────────

def _composite_score(
    ema_cross_or_trend: float,
    volume: float,
    momentum: float,
    vwap_score: float,
    market_structure: float,
    funding_oi: float,
    scalp: bool,
) -> float:
    """Calcula score ponderado adaptado ao tipo de operação com coerção contra NaNs."""
    ema_val = float(ema_cross_or_trend) if not np.isnan(ema_cross_or_trend) else 50.0
    vol_val = float(volume) if not np.isnan(volume) else 0.0
    mom_val = float(momentum) if not np.isnan(momentum) else 50.0
    vw_val  = float(vwap_score) if not np.isnan(vwap_score) else 50.0
    ms_val  = float(market_structure) if not np.isnan(market_structure) else 50.0
    foi_val = float(funding_oi) if not np.isnan(funding_oi) else 50.0

    if scalp:
        # SCALP weights
        return (
            ema_val * 0.20 +
            vol_val * 0.20 +
            mom_val * 0.20 +
            vw_val * 0.15 +
            ms_val * 0.15 +
            foi_val * 0.10
        )
    else:
        # DAY/SWING weights
        return (
            ema_val * 0.25 +
            vol_val * 0.20 +
            mom_val * 0.15 +
            ms_val * 0.15 +
            vw_val * 0.10 +
            foi_val * 0.15
        )


# ── Main Scanner ──────────────────────────────────────────────────────────────

async def analyze_asset(
    symbol: str,
    timeframe: str = "15m",
    direction: Optional[Direction] = None,
    news_data: list = None,
    mode: str = None,
    df: Optional[pd.DataFrame] = None,
    route: bool = True,
) -> Optional[TradeSignal]:
    try:
        # Micro-caps voláteis usam estratégia momentum-only (volatile_engine)
        from config import WATCHLIST_VOLATILE
        if symbol in WATCHLIST_VOLATILE:
            from volatile_engine import analyze_volatile
            return await analyze_volatile(symbol, timeframe, direction, df=df)

        is_backtest = df is not None
        if df is None:
            df = await get_klines(symbol, timeframe, limit=300)
        if df is None or len(df) < 100:
            return None

        # Roteamento de confluência de engines (BUG-003 fix)
        if route:
            import engine_router
            sig = await engine_router.route(symbol, timeframe, df, news_data, mode)
            if sig and (direction is None or sig.direction == direction):
                return sig
            return None

        if direction is None:
            long_sig = await _score_direction(symbol, df, Direction.LONG, timeframe, news_data, mode, is_backtest)
            short_sig = await _score_direction(symbol, df, Direction.SHORT, timeframe, news_data, mode, is_backtest)
            if long_sig and short_sig:
                return long_sig if long_sig.score.total >= short_sig.score.total else short_sig
            return long_sig or short_sig
        else:
            return await _score_direction(symbol, df, direction, timeframe, news_data, mode, is_backtest)
    except Exception:
        return None


async def _score_direction(
    symbol: str, df: pd.DataFrame, direction: Direction, timeframe: str,
    news_data: list = None, mode: str = None, is_backtest: bool = False
) -> Optional[TradeSignal]:

    # Restringe o bot a timeframes de scalp (1m, 3m, 5m, 15m)
    if timeframe not in {"1m", "3m", "5m", "15m"}:
        return None

    scalp = timeframe in {"1m", "3m", "5m", "15m"}
    active_mode = mode or TRADING_MODE   # disponível em toda a função
    settings = MODE_SETTINGS.get(active_mode, MODE_SETTINGS["NORMAL"])  # perfil ativo

    # ── Filtro "Down/Up Too Much" ──────────────────────────────────────────────
    # Bloqueia SHORT em tokens que já caíram > 60% (risco de bounce)
    # Bloqueia LONG em tokens que subiram > 300% (risco de correção violenta)
    try:
        close_7d_ago = df["close"].iloc[-int(7*24*60/{"1m":1,"3m":3,"5m":5,"15m":15,"1h":60,"4h":240}.get(timeframe,60))]
        current_close = df["close"].iloc[-1]
        chg_7d = (current_close - close_7d_ago) / close_7d_ago * 100
        if direction == Direction.SHORT and chg_7d < -60:
            return None   # Não shortar coisa que já tombou 60%+ — bounce esperado
        if direction == Direction.LONG and chg_7d > 300:
            return None   # Não comprar pump de 300%+ — correção esperada
    except Exception:
        pass

    if is_backtest:
        funding_score = 50.0
    else:
        funding_score = await score_funding_oi(symbol, direction)

        # Fear & Greed — direction-aware (+/-10pts) + Funding direction-aware
        try:
            from fear_greed import fg_score_adjustment, get_funding_adj
            _fg_adj, _fg_tag = await fg_score_adjustment(direction.value)
            if _fg_adj != 0:
                funding_score = min(100, max(0, funding_score + _fg_adj * 0.5))
            _fund_adj, _fund_tag = get_funding_adj(symbol, direction.value)
            if _fund_adj != 0:
                funding_score = min(100, max(0, funding_score + _fund_adj * 0.5))
        except Exception:
            pass

    # Calcula todos os scores
    if scalp:
        trend_or_cross = score_ema_cross(df, direction)
    else:
        trend_or_cross = score_trend(df, direction)

    vol_score    = score_volume(df, direction)
    mom_score    = score_momentum(df, direction, scalp=scalp)
    vwap_score   = score_vwap(df, direction)
    struct_score = score_market_structure(df, direction, timeframe)

    raw_total = _composite_score(
        trend_or_cross, vol_score, mom_score, vwap_score, struct_score, funding_score, scalp
    )

    # Penalidade por mercado lateral (RANGING = setup fraco)
    try:
        _struct_label = identify_structure(df, timeframe)["structure"]
        if _struct_label == "RANGING":
            raw_total = max(0.0, raw_total - 12.0)
    except Exception:
        pass

    # Penalidade por volume fraco (< 0.6x média = liquidez insuficiente)
    try:
        _vol_avg = float(df["volume"].rolling(20).mean().iloc[-1]) or 1.0
        _vol_cur = float(df["volume"].iloc[-1])
        if _vol_cur / _vol_avg < 0.6:
            raw_total = max(0.0, raw_total - 10.0)
    except Exception:
        pass

    # ── V6 Structural Engine ────────────────────────────────────────────────────
    # NORMAL:     bloqueia RSI > 76 OU vol > 6.5x
    # AGGRESSIVE: bloqueia RSI > 82 E vol > 8x (ambos simultâneos)
    # Em ambos os modos: is_trend_continuation() pode salvar o sinal se for
    # tendência real (EMAs alinhadas + slope gradual + estrutura + volume saudável).
    _trend_cont_tag   = False
    _trend_cont_conf  = 0.0
    if not scalp:
        _is_aggressive   = (active_mode == "AGGRESSIVE")
        _is_conservative = (active_mode == "CONSERVATIVE")
        # CONSERVATIVE: thresholds mais rígidos (bloqueia pump mais cedo)
        if _is_conservative:
            _rsi_th, _vol_th, _req_both = 76, 5.5, False
        elif _is_aggressive:
            _rsi_th, _vol_th, _req_both = 82, 8.0, True
        else:
            _rsi_th, _vol_th, _req_both = 80, 6.5, False
        _pump = check_pump_dump(
            df, rsi_th=_rsi_th, vol_th=_vol_th, require_both=_req_both,
        )
        if _pump:
            _is_trend, _trend_conf = is_trend_continuation(df, direction)
            if _is_trend:
                # Tendência real confirmada — não bloqueia, registra para bônus
                _trend_cont_tag  = True
                _trend_cont_conf = _trend_conf
                _rsi_log = round(rsi(df["close"]).iloc[-1], 1)
                _vol_log = round(df["volume"].iloc[-1] / (df["volume"].rolling(20).mean().iloc[-1] or 1), 2)
                print(
                    f"[TREND-CONT] {symbol} {timeframe} {direction.value} "
                    f"| RSI={_rsi_log} vol={_vol_log}x conf={_trend_conf:.0f}/100 "
                    f"| modo={active_mode} — sinal SALVO do pump_dump"
                )
            else:
                _rsi_log = round(rsi(df["close"]).iloc[-1], 1)
                _vol_log = round(df["volume"].iloc[-1] / (df["volume"].rolling(20).mean().iloc[-1] or 1), 2)
                print(
                    f"[TREND-CONT] {symbol} {timeframe} {direction.value} "
                    f"| RSI={_rsi_log} vol={_vol_log}x "
                    f"| modo={active_mode} — BLOQUEADO (pump confirmado)"
                )
                return None

    # Coleta todos os bônus antes de aplicar (para controlar inflação)
    # ── Padrões de candlestick (novo engine — 41 padrões) ────────────────────
    from candle_pattern_engine import detect_patterns, get_pattern_bonus
    _sig_patterns = detect_patterns(df) if len(df) >= 5 else []
    candle_bonus   = min(get_pattern_bonus(_sig_patterns, direction), 20.0)

    # MTF rápido: verifica 1h, 4h, 1d usando cache (sem chamadas extras à API)
    _MTF_CHECK = ["1h", "4h", "1d"]
    _mtf_pat: dict = {timeframe: _sig_patterns}
    for _mtf_tf in _MTF_CHECK:
        if _mtf_tf != timeframe:
            try:
                _mdf = await get_klines(symbol, _mtf_tf, limit=20)
                if _mdf is not None and len(_mdf) >= 5:
                    _mtf_pat[_mtf_tf] = detect_patterns(_mdf)
            except Exception:
                pass

    def _pat_to_dict(p):
        return {"name_pt": p.name_pt, "signal": p.signal, "strength": p.strength}

    _sig_pats_dict = [_pat_to_dict(p) for p in _sig_patterns]
    _mtf_pats_dict = {tf: [_pat_to_dict(p) for p in pats] for tf, pats in _mtf_pat.items()}
    # ─────────────────────────────────────────────────────────────────────────

    v6_bonus = 0.0
    v6_tags  = []
    if not scalp:
        v6_bonus   = score_v6_structure(df, direction)
        bos_bonus  = score_breakout_bos(df, direction)
        if bos_bonus > 0:
            v6_bonus = min(20.0, v6_bonus + bos_bonus * 0.5)
            v6_tags.append("BOS")
        if v6_bonus >= 8.0:
            v6_tags.append("OB/FVG")
        elif v6_bonus >= 4.0:
            v6_tags.append("sweep")
        elif v6_bonus >= 2.0:
            v6_tags.append("struct")

    fib_bonus = 0.0
    if not scalp:
        fib_bonus = score_fibonacci_confluence(df, direction)
        if fib_bonus >= 10.0:
            v6_tags.append("FIB618")
        elif fib_bonus >= 5.0:
            v6_tags.append("FIB")

    rsi_div_bonus = score_rsi_divergence(df, direction)
    if rsi_div_bonus > 0:
        _, div_type = detect_rsi_divergence(df, direction)
        v6_tags.append(f"DIV-{div_type[:3].upper()}")

    cross_bonus = score_golden_death_cross(df, direction)
    if cross_bonus >= 12.0:
        v6_tags.append("GOLDEN-X" if direction == Direction.LONG else "DEATH-X")
    elif cross_bonus >= 6.0:
        v6_tags.append("BULL-TREND" if direction == Direction.LONG else "BEAR-TREND")

    macro_bonus = score_macro_cycle(df, direction)
    if macro_bonus >= 10.0:
        v6_tags.append("MACRO-BULL" if direction == Direction.LONG else "MACRO-BEAR")
    elif macro_bonus <= -8.0:
        v6_tags.append("MACRO-CONTRA")

    # Liquidation cascade (tempo real via ws_feed) — reversão por short-squeeze/long-flush
    liq_bonus, liq_tag = score_liquidation_cascade(symbol, direction)
    if liq_tag:
        v6_tags.append(liq_tag)

    # Trend Continuation — bônus quando sinal foi salvo do pump_dump por tendência real
    trend_cont_bonus = 0.0
    if _trend_cont_tag:
        # Bônus proporcional à confidence (máx 15pts dentro do cap global)
        trend_cont_bonus = round(_trend_cont_conf / 100 * 15, 1)
        v6_tags.append("TREND-CONT")

    regime_adj  = 0.0
    regime_cap  = None
    try:
        import regime_detector as _rd
        _regime_data = _rd.detect(df, direction.value)
        regime_adj   = _regime_data.get("score_adj", 0.0)
        regime_cap   = _regime_data.get("score_cap")
        if regime_adj != 0:
            v6_tags.append(f"RGM-{_regime_data.get('regime','')[:3]}")
        _rd.update_cache(symbol, _regime_data)
    except Exception:
        pass

    # CVD Divergência & SMC Liquidity Sweep
    cvd_div_bonus = score_cvd_divergence(df, direction)
    if cvd_div_bonus > 0:
        _, cvd_div_type = detect_cvd_divergence(df, direction)
        v6_tags.append(f"CVD-{cvd_div_type[:3].upper()}")

    sweep_bonus = score_liquidity_sweep(df, direction)
    if sweep_bonus > 0:
        v6_tags.append("sweep")

    # ── Teto global de bônus ──────────────────────────────────────────────────────
    # Bônus positivos somados limitados a 20pts (AGGRESSIVE) / 17pts (NORMAL).
    # Penalidades negativas (cross, macro, regime) aplicam integralmente.
    _BONUS_CAP = settings.get("bonus_cap", 17.0)
    positive_bonus = (
        candle_bonus + v6_bonus + fib_bonus + rsi_div_bonus
        + cvd_div_bonus + sweep_bonus
        + max(0.0, cross_bonus) + max(0.0, macro_bonus) + max(0.0, regime_adj)
        + trend_cont_bonus + max(0.0, liq_bonus)
    )
    negative_adj = (
        min(0.0, cross_bonus) + min(0.0, macro_bonus) + min(0.0, regime_adj)
        + min(0.0, liq_bonus)
    )

    total = raw_total + min(positive_bonus, _BONUS_CAP) + negative_adj

    if regime_cap is not None:
        total = min(total, regime_cap)

    total = min(100.0, max(0.0, total))

    # Usa thresholds do modo atual (settings já definido no início da função)
    min_score = settings["min_score"]
    min_rr = settings["min_rr"]

    if total < min_score:
        return None

    levels = calculate_levels(df, direction, symbol, timeframe)
    if levels["rr"] < min_rr:
        return None

    # ── Orderbook Liquidity Gate ──────────────────────────────────────────────
    # Chamado apenas para sinais que passaram score + RR (evita calls desnecessárias)
    if is_backtest:
        _ob_score, _ob_block = 100.0, ""
    else:
        _ob_score, _ob_block = await score_orderbook_liquidity(
            symbol, direction, max_spread_pct=settings.get("max_spread_pct", 0.5)
        )
    if _ob_block:
        print(f"[OB-GATE] {symbol} {timeframe} {direction.value} BLOQUEADO: {_ob_block}")
        return None
    if _ob_score < 25:
        print(f"[OB-GATE] {symbol} {timeframe} {direction.value} BLOQUEADO: liquidez muito baixa (ob={_ob_score:.0f})")
        return None

    # SignalScore compatível com o modelo existente
    score_obj = SignalScore(
        trend=trend_or_cross,
        volume=vol_score,
        momentum=mom_score,
        market_structure=struct_score,
        funding_oi=funding_score,
        news_context=score_news(news_data or [], direction),
    )

    reason = build_reason(score_obj, direction, df, scalp=scalp, timeframe=timeframe)
    if v6_tags:
        reason = f"[V6:{'+'.join(v6_tags)} +{v6_bonus:.0f}pt] " + reason
    trade_type = classify_trade_type(timeframe)
    anomaly = detect_anomaly(df)

    # Atualiza score com total correto (pesos adaptativos + bônus candle)
    score_obj = score_obj.model_copy(update={"total_override": round(total, 1)})

    # ── Campos de qualidade para exibição no Telegram ───────────────────────
    _last = df.iloc[-1]
    _prev = df.iloc[-2] if len(df) > 1 else _last

    # Corpo/range da última vela (0-1)
    _rng = float(_last["high"] - _last["low"])
    _body = abs(float(_last["close"]) - float(_last["open"]))
    _body_pct = round(_body / _rng, 3) if _rng > 0 else 0.0

    # Volume ratio vs média 20 velas
    _vol_avg = float(df["volume"].rolling(20).mean().iloc[-1]) or 1.0
    _vol_ratio = round(float(_last["volume"]) / _vol_avg, 2)

    # RSI atual
    _rsi_series = rsi(df["close"], 14)
    _rsi_val = round(float(_rsi_series.iloc[-1]), 1)

    # Variação da última vela e das últimas 3
    _chg1 = (float(_last["close"]) - float(_last["open"])) / float(_last["open"]) * 100 if float(_last["open"]) > 0 else 0.0
    _close3 = float(df["close"].iloc[-1])
    _open3  = float(df["close"].iloc[-4]) if len(df) >= 4 else float(df["open"].iloc[0])
    _chg3   = (_close3 - _open3) / _open3 * 100 if _open3 > 0 else 0.0

    # Aceleração de volume vs vela anterior
    _vol_prev = float(_prev["volume"]) or 1.0
    _vol_accel = round(float(_last["volume"]) / _vol_prev, 2)

    # Lista de sinais confirmados
    _dir_word = "alta" if direction == Direction.LONG else "baixa"
    _confirmed: list[str] = []
    if _vol_ratio >= 2.0:
        _confirmed.append(f"Volume forte {_vol_ratio:.1f}x acima da média")
    if abs(_chg1) >= 2.0:
        _sign = "+" if _chg1 > 0 else ""
        _confirmed.append(f"Vela {_sign}{_chg1:.1f}% — {'subida' if _chg1 > 0 else 'queda'} {'forte' if abs(_chg1) >= 5 else 'relevante'}")
    if _body_pct >= 0.70:
        _confirmed.append(f"Vela cheia {_body_pct*100:.0f}% corpo/range — movimento limpo")
    if _vol_accel >= 2.0:
        _confirmed.append(f"Volume acelerou {_vol_accel:.1f}x vs vela anterior")
    if abs(_chg3) >= 5.0:
        _sign3 = "+" if _chg3 > 0 else ""
        _confirmed.append(f"{'Alta' if _chg3 > 0 else 'Queda'} {_sign3}{_chg3:.1f}% nas últimas 3 velas")
    if v6_tags:
        for tag in v6_tags[:2]:
            _confirmed.append(f"Padrão estrutural: {tag}")
    if not _confirmed:
        _confirmed.append(f"Score técnico {round(total,0):.0f}/100 — múltiplos fatores alinhados")

    # Recomendação baseada no contexto
    _rec = _build_recommendation(direction, _rsi_val, _vol_ratio, total, levels, _chg3, v6_tags)

    # Alavancagem sugerida
    _sl_pct = abs(levels["entry"] - levels["stop_loss"]) / levels["entry"] * 100 if levels["entry"] > 0 else 3.0
    try:
        from risk_manager import suggest_leverage as _suggest_lev
        _lev_info = _suggest_lev(
            symbol=symbol, score=total, body_pct=_body_pct,
            vol_ratio=_vol_ratio, rsi_val=_rsi_val, sl_pct=_sl_pct,
            rr=levels["rr"], trade_type=classify_trade_type(timeframe),
            v6_tags=v6_tags,
        )
        _sug_lev = _lev_info["leverage"]
        _lev_reason = _lev_info["reason"]
    except Exception:
        _sug_lev = 0
        _lev_reason = ""
    # ────────────────────────────────────────────────────────────────────────

    return TradeSignal(
        asset=symbol,
        direction=direction,
        entry=levels["entry"],
        stop_loss=levels["stop_loss"],
        tp1=levels["tp1"],
        tp2=levels["tp2"],
        tp3=levels["tp3"],
        rr=levels["rr"],
        confidence=round(total, 1),
        reason=reason,
        score=score_obj,
        timeframe=timeframe,
        trade_type=trade_type,
        anomaly=anomaly,
        body_pct=_body_pct,
        vol_ratio=_vol_ratio,
        rsi_val=_rsi_val,
        confirmed_signals=_confirmed,
        recommendation=_rec,
        suggested_leverage=_sug_lev,
        leverage_reason=_lev_reason,
        patterns_detected=_sig_pats_dict,
        patterns_mtf=_mtf_pats_dict,
    )


def _build_recommendation(direction: Direction, rsi_val: float, vol_ratio: float,
                           score: float, levels: dict, chg3: float, tags: list) -> str:
    """Gera recomendação textual baseada no contexto do sinal."""
    is_long = direction == Direction.LONG
    rr = levels.get("rr", 0)

    if is_long:
        if rsi_val > 75:
            return (f"LONG em sobrecompra (RSI {rsi_val:.0f}) — operar apenas com confirmação de continuação. "
                    f"Reduzir tamanho para 50%. Aguardar pullback antes de entrar se possível.")
        if chg3 < -8:
            return (f"Possível reversão após queda forte ({chg3:.1f}%). "
                    f"Aguardar vela de confirmação (engulf/martelo) antes de entrar. SL abaixo do fundo.")
        if vol_ratio >= 3.0:
            return (f"Volume {vol_ratio:.1f}x acima da média confirma força. "
                    f"Entrada na zona atual válida. TP1 conservador como primeiro alvo. Mover SL para BE após TP1.")
        if score >= 80:
            return (f"Sinal de alta qualidade (score {score:.0f}). "
                    f"Entrada confirmada. R:R {rr:.1f}:1 favorável. Operar tamanho normal.")
        return (f"LONG com confirmação parcial. "
                f"Aguardar fechamento de vela {'acima' if is_long else 'abaixo'} da entrada antes de confirmar. "
                f"Manter SL rígido.")
    else:
        if rsi_val < 25:
            return (f"SHORT em sobrevenda (RSI {rsi_val:.0f}) — risco de reversão. "
                    f"Operar apenas abaixo do suporte confirmado. Reduzir tamanho para 50%.")
        if chg3 > 8:
            return (f"Possível exaustão após alta forte (+{chg3:.1f}%). "
                    f"SHORT válido apenas com rompimento do suporte. Aguardar confirmação de topo.")
        if vol_ratio >= 3.0:
            return (f"Volume de distribuição {vol_ratio:.1f}x confirma pressão vendedora. "
                    f"SHORT válido na zona atual. TP1 como alvo conservador. SL acima da resistência.")
        if score >= 80:
            return (f"Sinal SHORT de alta qualidade (score {score:.0f}). "
                    f"Entrada confirmada. R:R {rr:.1f}:1 favorável. Operar tamanho normal.")
        return (f"SHORT com confirmação parcial. "
                f"Aguardar fechamento de vela abaixo do suporte antes de confirmar. Manter SL rígido.")


_SCAN_SEMAPHORE: asyncio.Semaphore = None


def _get_scan_semaphore() -> asyncio.Semaphore:
    """Semáforo para limitar concorrência de requests — máx 40 simultâneos."""
    global _SCAN_SEMAPHORE
    if _SCAN_SEMAPHORE is None:
        _SCAN_SEMAPHORE = asyncio.Semaphore(40)
    return _SCAN_SEMAPHORE


async def _analyze_with_limit(symbol: str, tf: str, **kwargs) -> Optional[TradeSignal]:
    async with _get_scan_semaphore():
        return await analyze_asset(symbol, tf, **kwargs)


async def scan_watchlist(news_data: list = None, mode: str = None, trending: list = None,
                         dynamic_universe: list = None) -> list[TradeSignal]:
    """
    Scan watchlist + top trending da Binance.
    Em modo AGGRESSIVE com dynamic_universe preenchido, usa o universo dinâmico
    no lugar da watchlist fixa — mas mantém WATCHLIST + WATCHLIST_VOLATILE como base.
    Concorrência limitada a 40 requests simultâneos para não causar ban de IP.
    """
    active_mode = mode or TRADING_MODE
    settings = MODE_SETTINGS.get(active_mode, MODE_SETTINGS["NORMAL"])
    timeframes = settings.get("timeframes", ["5m", "15m", "1h", "4h"])

    if active_mode == "AGGRESSIVE" and dynamic_universe:
        # Universo dinâmico: combina base fixa + universo gerado pelo universe_builder
        full_list = list(dict.fromkeys(WATCHLIST + WATCHLIST_VOLATILE + dynamic_universe))
        source = f"dinâmico:{len(dynamic_universe)}"
    else:
        if trending is None:
            trending = await get_trending_futures(top_n=10)
        full_list = list(dict.fromkeys(WATCHLIST + WATCHLIST_VOLATILE + trending))
        source = f"trending:{len(trending)}"

    # Restrição de universo por perfil (CONSERVATIVE = só majors)
    _allowed = settings.get("allowed_assets")
    if _allowed:
        _allowed_set = {a.upper() for a in _allowed}
        full_list = [s for s in full_list if s.upper() in _allowed_set]
        source = f"restrito:{len(full_list)}"

    print(f"[SCAN] Modo: {active_mode} | {len(full_list)} ativos ({source}) | TFs: {timeframes}")

    tasks = []
    for symbol in full_list:
        for tf in timeframes:
            tasks.append(_analyze_with_limit(symbol, tf, news_data=news_data, mode=active_mode))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    signals = [r for r in results if isinstance(r, TradeSignal)]
    signals.sort(key=lambda s: s.confidence, reverse=True)
    return signals


def analyze_smart_flow(df: pd.DataFrame) -> dict:
    """
    Smart Flow Analysis — buy/sell volume imbalance + momentum classification.
    Returns: phase (Accumulation/Distribution/Breakout/Exhaustion), buy_pct, sell_pct, imbalance.
    Additive — does not modify existing signal logic.
    """
    if df is None or len(df) < 20:
        return {"phase": "Unknown", "buy_pct": 50.0, "sell_pct": 50.0, "imbalance": 0.0}

    close  = df["close"]
    open_  = df["open"]
    volume = df["volume"]

    # Proxy: candles where close > open = buy vol; close < open = sell vol
    buy_mask  = close > open_
    sell_mask = close < open_

    buy_vol  = (volume * buy_mask).iloc[-20:].sum()
    sell_vol = (volume * sell_mask).iloc[-20:].sum()
    total    = buy_vol + sell_vol + 1e-9

    buy_pct  = round(buy_vol / total * 100, 1)
    sell_pct = round(sell_vol / total * 100, 1)
    imbalance = round(buy_pct - sell_pct, 1)

    # Price trend over last 20 candles
    price_trend = (close.iloc[-1] / close.iloc[-20] - 1) * 100 if len(df) >= 20 else 0.0

    # Momentum shift: compare last 5 imbalance vs prior 15
    recent_buy  = (volume * buy_mask).iloc[-5:].sum()
    recent_sell = (volume * sell_mask).iloc[-5:].sum()
    recent_imb  = (recent_buy - recent_sell) / (recent_buy + recent_sell + 1e-9) * 100

    # Classify phase
    if buy_pct >= 60 and price_trend > 1.0:
        phase = "Breakout"
    elif buy_pct >= 55 and price_trend <= 0:
        phase = "Accumulation"
    elif sell_pct >= 60 and price_trend < -1.0:
        phase = "Distribution"
    elif sell_pct >= 55 and abs(recent_imb) < 10:
        phase = "Exhaustion"
    elif abs(imbalance) < 10:
        phase = "Consolidation"
    else:
        phase = "Accumulation" if imbalance > 0 else "Distribution"

    return {
        "phase":     phase,
        "buy_pct":   buy_pct,
        "sell_pct":  sell_pct,
        "imbalance": imbalance,
        "momentum_shift": round(recent_imb, 1),
    }


# ══════════════════════════════════════════════════════════════════════════════
# V6 STRUCTURAL ENGINE — OB + FVG + Pivots + Sweeps + Breakout BOS
# Funciona como camada de bônus sobre o score V4:
#   score_final = min(100, v4_score + v6_bonus)
# V6 bonus: 0–20 pts. Quando ambos concordam, sinal ultrapassa threshold.
# ══════════════════════════════════════════════════════════════════════════════

def _v6_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    hl = h - l
    hc = (h - c.shift()).abs()
    lc = (l - c.shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def _v6_detect_pivots(df: pd.DataFrame, lb: int = 5):
    h = df["high"].values
    l = df["low"].values
    n = len(h)
    ph, pl = [], []
    for i in range(lb, n - lb):
        if h[i] >= h[i-lb:i+lb+1].max():
            ph.append((i, float(h[i])))
        if l[i] <= l[i-lb:i+lb+1].min():
            pl.append((i, float(l[i])))
    return ph, pl


def _v6_structure(ph, pl) -> str:
    if len(ph) < 2 or len(pl) < 2:
        return "ranging"
    hh = ph[-1][1] > ph[-2][1]
    hl = pl[-1][1] > pl[-2][1]
    lh = ph[-1][1] < ph[-2][1]
    ll = pl[-1][1] < pl[-2][1]
    if hh and hl:
        return "up"
    if lh and ll:
        return "down"
    return "ranging"


def _v6_find_obs(df: pd.DataFrame, atr_s: pd.Series, impulse: float = 1.8) -> list:
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    atr_v = atr_s.values
    obs = []
    n = len(c)
    for i in range(5, n - 3):
        av = atr_v[i]
        if av <= 0:
            continue
        if c[i] < o[i]:  # bearish → bullish OB
            fwd_h = max(h[i+1], h[i+2], h[i+3])
            if (fwd_h - c[i]) >= impulse * av:
                obs.append({"idx": i, "dir": 1, "top": float(h[i]),
                             "bot": float(l[i]), "valid": True})
        elif c[i] > o[i]:  # bullish → bearish OB
            fwd_l = min(l[i+1], l[i+2], l[i+3])
            if (c[i] - fwd_l) >= impulse * av:
                obs.append({"idx": i, "dir": -1, "top": float(h[i]),
                             "bot": float(l[i]), "valid": True})
    return obs


def _v6_find_fvg(df: pd.DataFrame, atr_s: pd.Series, min_frac: float = 0.25) -> list:
    h = df["high"].values
    l = df["low"].values
    atr_v = atr_s.values
    fvgs = []
    n = len(h)
    for i in range(1, n - 1):
        av = atr_v[i]
        if av <= 0:
            continue
        gap_up = l[i+1] - h[i-1]
        if gap_up >= min_frac * av:
            fvgs.append({"idx": i, "dir": 1, "top": float(l[i+1]),
                          "bot": float(h[i-1]), "valid": True})
        gap_dn = l[i-1] - h[i+1]
        if gap_dn >= min_frac * av:
            fvgs.append({"idx": i, "dir": -1, "top": float(l[i-1]),
                          "bot": float(h[i+1]), "valid": True})
    return fvgs


def _v6_sweep_recent(df: pd.DataFrame, lookback: int = 20, tol: float = 0.003) -> dict:
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(c)
    if n < lookback + 2:
        return {"bull_sweep": False, "bear_sweep": False}
    win_h = h[n-lookback-2:n-2]
    win_l = l[n-lookback-2:n-2]
    mh = win_h.max() if len(win_h) else 0
    ml = win_l.min() if len(win_l) else 0
    last_h = h[-1]
    last_l = l[-1]
    last_c = c[-1]
    bear_sweep = bool(last_h > mh * (1 + tol) and last_c < mh)
    bull_sweep = bool(last_l < ml * (1 - tol) and last_c > ml)
    return {"bull_sweep": bull_sweep, "bear_sweep": bear_sweep}


def check_pump_dump(
    df: pd.DataFrame,
    rsi_th: float = 76,
    vol_th: float = 6.5,
    require_both: bool = False,
) -> bool:
    """
    Retorna True se condições de pump/dump extremo detectadas.
    NORMAL:     RSI > 76 OU vol > 6.5x  (OR — mais conservador)
    AGGRESSIVE: RSI > 82 E  vol > 8x    (AND — só bloqueia se ambos extremos)
    """
    try:
        close = df["close"]
        rsi_val = rsi(close).iloc[-1]
        vol = df["volume"]
        avg_vol = vol.rolling(20).mean().iloc[-1]
        vol_ratio = vol.iloc[-1] / avg_vol if avg_vol > 0 else 1.0
        if require_both:
            return bool(rsi_val > rsi_th and vol_ratio > vol_th)
        return bool(rsi_val > rsi_th or vol_ratio > vol_th)
    except Exception:
        return False


def is_trend_continuation(df: pd.DataFrame, direction: Direction = Direction.LONG) -> tuple[bool, float]:
    """
    Detecta se o ativo está em tendência real (não pump) mesmo com RSI alto.

    Critérios LONG (todos devem passar):
      1. EMAs alinhadas: EMA9 > EMA21 > EMA55
      2. Slope gradual: EMA21 subiu de forma consistente (não spike)
      3. Estrutura HH/HL intacta nas últimas 20 velas
      4. Volume saudável: média das últimas 5 velas > média geral (crescimento),
         mas sem spike único > 8x (seria pump)
      5. RSI < 88 (RSI > 88 = exaustão real mesmo em tendência)

    Retorna (is_trend: bool, confidence: float 0-100).
    confidence é usado como bônus de score (máx 15pts via tag TREND-CONT).
    """
    if df is None or len(df) < 60:
        return False, 0.0
    try:
        close  = df["close"]
        volume = df["volume"]

        e9  = ema(close, 9)
        e21 = ema(close, 21)
        e55 = ema(close, 55)

        e9_now  = e9.iloc[-1]
        e21_now = e21.iloc[-1]
        e55_now = e55.iloc[-1]
        price   = close.iloc[-1]
        rsi_val = rsi(close).iloc[-1]

        if direction == Direction.SHORT:
            emas_aligned = e9_now < e21_now < e55_now
        else:
            emas_aligned = e9_now > e21_now > e55_now

        if not emas_aligned:
            return False, 0.0

        # RSI extremo real → não é tendência, é exaustão
        if rsi_val > 88:
            return False, 0.0

        # Slope da EMA21 nas últimas 8 velas — deve ser positivo e gradual
        e21_8ago = e21.iloc[-8]
        slope_pct = (e21_now - e21_8ago) / e21_8ago * 100 if e21_8ago > 0 else 0.0
        if direction == Direction.LONG:
            slope_ok = 0.1 <= slope_pct <= 15.0   # subindo, mas não em spike
        else:
            slope_ok = -15.0 <= slope_pct <= -0.1

        if not slope_ok:
            return False, 0.0

        # Estrutura HH/HL (LONG) ou LH/LL (SHORT)
        struct = identify_structure(df)["structure"]
        struct_ok = (struct == "UPTREND" if direction == Direction.LONG else struct == "DOWNTREND")

        # Volume: média das últimas 5 velas vs média geral — crescimento gradual
        avg_vol_20 = volume.rolling(20).mean().iloc[-1]
        avg_vol_5  = volume.iloc[-5:].mean()
        max_vol_5  = volume.iloc[-5:].max()
        vol_growing   = avg_vol_5 > avg_vol_20 * 1.1    # pelo menos 10% acima da média
        vol_no_spike  = max_vol_5 < avg_vol_20 * 8.0    # sem spike isolado de pump

        # Calcula confidence baseado em quantos critérios passaram
        points = 0.0
        if emas_aligned:   points += 35.0
        if slope_ok:       points += 25.0
        if struct_ok:      points += 25.0
        if vol_growing:    points += 10.0
        if vol_no_spike:   points += 5.0

        # Exige EMAs + slope + estrutura (70pts — EMAs+slope já são 60pts)
        is_trend = points >= 70.0
        return is_trend, round(points, 1)

    except Exception:
        return False, 0.0


def score_breakout_bos(df: pd.DataFrame, direction: Direction) -> float:
    """
    Breakout de estrutura (BOS): preco quebra HH/LL com volume >=2x.
    Retorna 0–20 pontos de bonus.
    """
    try:
        ph, pl = _v6_detect_pivots(df, lb=5)
        if len(ph) < 2 or len(pl) < 2:
            return 0.0
        close = df["close"]
        vol = df["volume"]
        avg_vol = vol.rolling(20).mean().iloc[-1]
        vol_ratio = vol.iloc[-1] / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio < 2.0:
            return 0.0
        price = close.iloc[-1]
        prev_price = close.iloc[-2]
        last_hh = ph[-1][1]
        last_ll = pl[-1][1]
        if direction == Direction.LONG and price > last_hh and prev_price <= last_hh:
            return 18.0
        if direction == Direction.SHORT and price < last_ll and prev_price >= last_ll:
            return 18.0
        return 0.0
    except Exception:
        return 0.0


def score_v6_structure(df: pd.DataFrame, direction: Direction) -> float:
    """
    Score estrutural V6 (bonus 0–20 pts sobre score V4).
    Componentes:
      OB tap + confirmacao:  +8
      FVG tap + confirmacao: +5
      Sweep recente:         +4
      Breakout BOS:          via score_breakout_bos()
      H1 estrutura alinhada: +3
    Retorna valor a ser somado ao score V4 (capped em 20).
    """
    try:
        if df is None or len(df) < 50:
            return 0.0

        atr_s   = _v6_atr(df)
        obs     = _v6_find_obs(df, atr_s)
        fvgs    = _v6_find_fvg(df, atr_s)
        sweeps  = _v6_sweep_recent(df)
        ph, pl  = _v6_detect_pivots(df)
        struct  = _v6_structure(ph, pl)

        price   = float(df["close"].iloc[-1])
        low_i   = float(df["low"].iloc[-1])
        high_i  = float(df["high"].iloc[-1])
        open_i  = float(df["open"].iloc[-1])
        atr_val = float(atr_s.iloc[-1]) if not np.isnan(atr_s.iloc[-1]) else 0.0

        # Candle body quality
        rng  = high_i - low_i
        body = abs(price - open_i)
        body_ok = (body / rng >= 0.40) if rng > 0 else False

        bonus = 0.0

        if direction == Direction.LONG:
            # Estrutura alinhada
            if struct == "up":
                bonus += 3.0
            elif struct == "ranging":
                bonus += 1.0

            # OB bullish tap: low tocou zona, close acima do topo
            if body_ok and price > open_i:
                n = len(df)
                recent_obs = [ob for ob in obs if ob["dir"] == 1
                              and n - ob["idx"] <= 250
                              and ob.get("valid", True)]
                for ob in reversed(recent_obs[-15:]):
                    if atr_val > 0 and low_i <= ob["top"] and price > ob["top"]:
                        bonus += 8.0
                        break

                # FVG bullish tap
                if bonus < 5.0:
                    recent_fvg = [f for f in fvgs if f["dir"] == 1
                                  and n - f["idx"] <= 150
                                  and f.get("valid", True)]
                    for f in reversed(recent_fvg[-10:]):
                        if low_i <= f["top"] and price > f["top"]:
                            bonus += 5.0
                            break

            # Sweep de low recente
            if sweeps["bull_sweep"]:
                bonus += 4.0

        else:  # SHORT
            if struct == "down":
                bonus += 3.0
            elif struct == "ranging":
                bonus += 1.0

            if body_ok and price < open_i:
                n = len(df)
                recent_obs = [ob for ob in obs if ob["dir"] == -1
                              and n - ob["idx"] <= 250
                              and ob.get("valid", True)]
                for ob in reversed(recent_obs[-15:]):
                    if atr_val > 0 and high_i >= ob["bot"] and price < ob["bot"]:
                        bonus += 8.0
                        break

                if bonus < 5.0:
                    recent_fvg = [f for f in fvgs if f["dir"] == -1
                                  and n - f["idx"] <= 150
                                  and f.get("valid", True)]
                    for f in reversed(recent_fvg[-10:]):
                        if high_i >= f["bot"] and price < f["bot"]:
                            bonus += 5.0
                            break

            if sweeps["bear_sweep"]:
                bonus += 4.0

        return min(20.0, bonus)

    except Exception:
        return 0.0


def v6_grid_zones(df: pd.DataFrame, current_price: float) -> dict:
    """
    Identifica zonas OB/FVG para definir range estrutural do grid.
    Suporte  = OB/FVG bullish mais próximo ABAIXO do preço.
    Resistência = OB/FVG bearish mais próximo ACIMA do preço.
    Fallback: ±2×ATR se nenhuma zona encontrada.
    """
    try:
        atr_s   = _v6_atr(df)
        atr_val = float(atr_s.iloc[-1]) if not np.isnan(atr_s.iloc[-1]) else 0.0
        obs     = _v6_find_obs(df, atr_s)
        fvgs    = _v6_find_fvg(df, atr_s)
        n       = len(df)

        # Suporte estrutural (bullish OB/FVG abaixo)
        candidates_lo = []
        for ob in obs:
            if ob["dir"] == 1 and (n - ob["idx"]) <= 200 and ob["top"] < current_price:
                candidates_lo.append(("OB", ob))
        for fvg in fvgs:
            if fvg["dir"] == 1 and (n - fvg["idx"]) <= 150 and fvg["top"] < current_price:
                candidates_lo.append(("FVG", fvg))
        candidates_lo.sort(key=lambda x: x[1]["top"], reverse=True)  # mais próximo = maior top

        # Resistência estrutural (bearish OB/FVG acima)
        candidates_hi = []
        for ob in obs:
            if ob["dir"] == -1 and (n - ob["idx"]) <= 200 and ob["bot"] > current_price:
                candidates_hi.append(("OB", ob))
        for fvg in fvgs:
            if fvg["dir"] == -1 and (n - fvg["idx"]) <= 150 and fvg["bot"] > current_price:
                candidates_hi.append(("FVG", fvg))
        candidates_hi.sort(key=lambda x: x[1]["bot"])  # mais próximo = menor bot

        lower_type  = candidates_lo[0][0] if candidates_lo else "ATR"
        lower_zone  = candidates_lo[0][1] if candidates_lo else None
        upper_type  = candidates_hi[0][0] if candidates_hi else "ATR"
        upper_zone  = candidates_hi[0][1] if candidates_hi else None

        lower = lower_zone["bot"] if lower_zone else round(current_price - 2.0 * atr_val, 6)
        upper = upper_zone["top"] if upper_zone else round(current_price + 2.0 * atr_val, 6)

        # Distância entre zona e preço em %
        dist_lo_pct = round((current_price - lower) / current_price * 100, 3) if current_price else 0
        dist_hi_pct = round((upper - current_price) / current_price * 100, 3) if current_price else 0

        return {
            "lower":       round(lower, 6),
            "upper":       round(upper, 6),
            "lower_type":  lower_type,
            "upper_type":  upper_type,
            "lower_zone":  lower_zone,
            "upper_zone":  upper_zone,
            "dist_lo_pct": dist_lo_pct,
            "dist_hi_pct": dist_hi_pct,
            "atr":         round(atr_val, 6),
            "found":       bool(candidates_lo or candidates_hi),
        }
    except Exception as e:
        print(f"[V6_GRID_ZONES] erro: {e}")
        return {"lower": 0.0, "upper": 0.0, "lower_type": "ATR", "upper_type": "ATR",
                "dist_lo_pct": 0, "dist_hi_pct": 0, "atr": 0.0, "found": False}


async def scan_anomalies() -> list[dict]:
    """Varre watchlist em timeframes curtos buscando movimentos atípicos."""
    quick_tfs = ["3m", "5m", "15m"]
    found = []
    for symbol in WATCHLIST:
        for tf in quick_tfs:
            try:
                df = await get_klines(symbol, tf, limit=50)
                if df is None or len(df) < 25:
                    continue
                anomaly = detect_anomaly(df)
                if anomaly:
                    vol = df["volume"]
                    avg_vol = vol.rolling(20).mean().iloc[-1]
                    vol_ratio = vol.iloc[-1] / avg_vol if avg_vol > 0 else 1
                    found.append({
                        "symbol": symbol,
                        "timeframe": tf,
                        "anomaly": anomaly,
                        "price": round(df["close"].iloc[-1], 6),
                        "vol_ratio": round(vol_ratio, 2),
                    })
            except Exception:
                pass
    return found
