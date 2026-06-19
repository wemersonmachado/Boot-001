"""
Candlestick Pattern Engine — 41 padrões de candlestick.
Referência: blog.toroinvestimentos.com.br/trading/padroes-de-candlestick

Padrões implementados (41):
  Single   (13): Doji, Dragonfly Doji, Gravestone Doji, Hammer, Inverted Hammer,
                 Shooting Star, Hanging Man, Marubozu, Spinning Top, Long Day,
                 Short Day, Force Candle, Long Shadows
  Double   (12): Bullish/Bearish Engulfing, Piercing Line, Dark Cloud Cover,
                 Bullish/Bearish Kicker, Bullish/Bearish Harami,
                 Top/Bottom Tweezers, Bullish/Bearish Tasuki Gap
  Triple   (12): Three White Soldiers, Three Black Crows, Three Inside Up,
                 Three Outside Down, Bullish/Bearish Abandoned Baby,
                 Morning/Evening Star, Rising/Falling Three Methods,
                 Bullish/Bearish Strike
  Outros    (4): High Gap Two Crows, Bullish/Bearish Reversal Island, Stick Sandwich

Análise Multi-Timeframe: 3m, 5m, 15m, 1h, 2h, 4h, 12h, 1d, 1w
"""
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Optional

import pandas as pd

from klines_cache import get_klines_cached as _get_klines

# ── Timeframes MTF ────────────────────────────────────────────────────────────

MTF_TIMEFRAMES = ["3m", "5m", "15m", "1h", "2h", "4h", "12h", "1d", "1w"]

# Rótulos de exibição (PT-BR)
MTF_LABELS = {
    "3m": "3m", "5m": "5m", "15m": "15m", "1h": "1h",
    "2h": "2h", "4h": "4h", "12h": "12h", "1d": "1D", "1w": "1S",
}

# Pesos para cálculo de viés ponderado (TF maior = mais peso)
_TF_WEIGHTS = {
    "3m": 0.5, "5m": 0.5, "15m": 1.0, "1h": 1.5,
    "2h": 2.0, "4h": 2.5, "12h": 3.0, "1d": 4.0, "1w": 5.0,
}

# Ícones e estrelas para formatação
SIGNAL_ICONS  = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
STRENGTH_LABEL = {1: "★", 2: "★★", 3: "★★★"}

# Bonus de pontuação por padrão (integração com signal_engine)
_BONUS: Dict[str, float] = {
    "Doji":                      5,
    "Dragonfly Doji":           10,
    "Gravestone Doji":          10,
    "Hammer":                   12,
    "Inverted Hammer":          10,
    "Shooting Star":            12,
    "Hanging Man":              10,
    "Marubozu":                 10,
    "Spinning Top":              4,
    "Long Day":                  8,
    "Short Day":                 3,
    "Force Candle":             10,
    "Long Shadows":              6,
    "Bullish Engulfing":        15,
    "Bearish Engulfing":        15,
    "Piercing Line":            10,
    "Dark Cloud Cover":         10,
    "Bullish Kicker":           18,
    "Bearish Kicker":           18,
    "Bullish Harami":           10,
    "Bearish Harami":           10,
    "Bottom Tweezers":          10,
    "Top Tweezers":             10,
    "Bullish Tasuki Gap":        8,
    "Bearish Tasuki Gap":        8,
    "Three White Soldiers":     20,
    "Three Black Crows":        20,
    "Three Inside Up":          18,
    "Three Outside Down":       18,
    "Bullish Abandoned Baby":   20,
    "Bearish Abandoned Baby":   20,
    "Morning Star":             18,
    "Evening Star":             18,
    "Rising Three Methods":     12,
    "Falling Three Methods":    12,
    "Bullish Strike":           12,
    "Bearish Strike":           12,
    "High Gap Two Crows":       12,
    "Bullish Reversal Island":  12,
    "Bearish Reversal Island":  12,
    "Stick Sandwich":           10,
}


@dataclass
class CandlePattern:
    name:     str    # nome em inglês
    name_pt:  str    # nome em português
    signal:   str    # "bullish" | "bearish" | "neutral"
    strength: int    # 1=fraco | 2=moderado | 3=forte
    bonus:    float  # pontos de bonus para signal_engine


# ── Detecção de padrões ───────────────────────────────────────────────────────

def detect_patterns(df: pd.DataFrame) -> List[CandlePattern]:
    """
    Detecta todos os 41 padrões no DataFrame OHLCV.
    Analisa as 5 últimas velas (c1=mais recente).
    Retorna lista de padrões encontrados, sem duplicatas.
    """
    if len(df) < 5:
        return []

    def _v(c):
        o  = float(c["open"])
        cl = float(c["close"])
        h  = float(c["high"])
        l  = float(c["low"])
        rng  = max(h - l, 1e-10)
        body = abs(cl - o)
        uw   = h - max(o, cl)
        lw   = min(o, cl) - l
        bull = cl > o
        bear = cl < o
        doji = body / rng < 0.08
        return o, cl, h, l, rng, body, uw, lw, bull, bear, doji

    c1, c2, c3, c4, c5 = (df.iloc[-i] for i in range(1, 6))
    o1,cl1,h1,l1,rng1,body1,uw1,lw1,bull1,bear1,doji1 = _v(c1)
    o2,cl2,h2,l2,rng2,body2,uw2,lw2,bull2,bear2,doji2 = _v(c2)
    o3,cl3,h3,l3,rng3,body3,uw3,lw3,bull3,bear3,doji3 = _v(c3)
    o4,cl4,h4,l4,rng4,body4,uw4,lw4,bull4,bear4,doji4 = _v(c4)
    o5,cl5,h5,l5,rng5,body5,uw5,lw5,bull5,bear5,doji5 = _v(c5)

    avg_body = (body1 + body2 + body3 + body4 + body5) / 5 or 1e-10
    avg_rng  = (rng1 + rng2 + rng3 + rng4 + rng5) / 5 or 1e-10

    # Contexto de tendência simples (últimas 3 velas)
    trend_up   = cl3 < cl2 < cl1
    trend_down = cl3 > cl2 > cl1

    found: List[CandlePattern] = []

    def add(name, name_pt, signal, strength):
        found.append(CandlePattern(
            name=name, name_pt=name_pt, signal=signal,
            strength=strength, bonus=_BONUS.get(name, 5),
        ))

    # ── SINGLE CANDLE (13 padrões) ────────────────────────────────────────────

    # 1. Doji — corpo < 8% do range
    if doji1:
        add("Doji", "Doji", "neutral", 1)

    # 2. Dragonfly Doji — corpo no topo, sombra inferior longa
    if doji1 and uw1 < rng1 * 0.05 and lw1 > rng1 * 0.60:
        add("Dragonfly Doji", "Doji Libélula", "bullish", 2)

    # 3. Gravestone Doji — corpo na base, sombra superior longa
    if doji1 and lw1 < rng1 * 0.05 and uw1 > rng1 * 0.60:
        add("Gravestone Doji", "Doji Lápide", "bearish", 2)

    # 4. Hammer — downtrend, sombra inferior longa, corpo pequeno no topo
    if (trend_down and body1 / rng1 < 0.35
            and lw1 > body1 * 1.8 and uw1 < body1 * 0.5):
        add("Hammer", "Martelo", "bullish", 2)

    # 5. Inverted Hammer — downtrend, sombra superior longa, corpo pequeno na base
    if (trend_down and body1 / rng1 < 0.35
            and uw1 > body1 * 1.8 and lw1 < body1 * 0.5):
        add("Inverted Hammer", "Martelo Invertido", "bullish", 2)

    # 6. Shooting Star — uptrend, sombra superior longa, corpo pequeno na base
    if (trend_up and body1 / rng1 < 0.35
            and uw1 > body1 * 1.8 and lw1 < body1 * 0.5):
        add("Shooting Star", "Estrela Cadente", "bearish", 2)

    # 7. Hanging Man — uptrend, sombra inferior longa, corpo pequeno no topo
    if (trend_up and body1 / rng1 < 0.35
            and lw1 > body1 * 1.8 and uw1 < body1 * 0.5):
        add("Hanging Man", "Homem Enforcado", "bearish", 2)

    # 8. Marubozu — sem sombras, corpo ≥ 90% do range
    if body1 / rng1 > 0.90 and body1 > avg_body * 1.1:
        sig = "bullish" if bull1 else "bearish"
        add("Marubozu", "Marubozu (Vela Careca)", sig, 2)

    # 9. Spinning Top — corpo pequeno, wicks dos dois lados
    if (body1 / rng1 < 0.30
            and uw1 > rng1 * 0.18 and lw1 > rng1 * 0.18):
        add("Spinning Top", "Peão", "neutral", 1)

    # 10. Long Day — corpo ≥ 2× média
    if body1 > avg_body * 2.0:
        sig = "bullish" if bull1 else "bearish"
        add("Long Day", "Dia Longo", sig, 2)

    # 11. Short Day — corpo < 50% da média, wicks mínimas
    if (body1 < avg_body * 0.50
            and uw1 < rng1 * 0.20 and lw1 < rng1 * 0.20):
        add("Short Day", "Dia Curto", "neutral", 1)

    # 12. Force Candle — corpo ≥ 3× média
    if body1 > avg_body * 3.0:
        sig = "bullish" if bull1 else "bearish"
        add("Force Candle", "Candle de Força", sig, 2)

    # 13. Long Shadows — sombras longas dos dois lados
    if uw1 > body1 * 2.0 and lw1 > body1 * 2.0 and not doji1:
        add("Long Shadows", "Sombras Longas", "neutral", 1)

    # ── TWO CANDLE (12 padrões) ───────────────────────────────────────────────

    # 14. Bullish Engulfing — c2 bearish, c1 bullish envolve completamente
    if bear2 and bull1 and o1 <= cl2 and cl1 >= o2 and body1 > body2:
        add("Bullish Engulfing", "Engolfo de Alta", "bullish", 3)

    # 15. Bearish Engulfing — c2 bullish, c1 bearish envolve completamente
    if bull2 and bear1 and o1 >= cl2 and cl1 <= o2 and body1 > body2:
        add("Bearish Engulfing", "Engolfo de Baixa", "bearish", 3)

    # 16. Piercing Line — c2 bearish, c1 bullish fecha acima do meio do c2
    if bear2 and bull1 and o1 < cl2:
        mid2 = (o2 + cl2) / 2
        if cl1 > mid2 and cl1 < o2:
            add("Piercing Line", "Padrão Perfurante", "bullish", 2)

    # 17. Dark Cloud Cover — c2 bullish, c1 bearish fecha abaixo do meio do c2
    if bull2 and bear1 and o1 > cl2:
        mid2 = (o2 + cl2) / 2
        if cl1 < mid2 and cl1 > cl2:
            add("Dark Cloud Cover", "Nuvem Negra", "bearish", 2)

    # 18. Bullish Kicker — gap de alta entre velas opostas
    if bear2 and bull1 and o1 > o2 and body1 > avg_body * 0.8:
        add("Bullish Kicker", "Chute de Alta", "bullish", 3)

    # 19. Bearish Kicker — gap de baixa entre velas opostas
    if bull2 and bear1 and o1 < o2 and body1 > avg_body * 0.8:
        add("Bearish Kicker", "Chute de Queda", "bearish", 3)

    # 20. Bullish Harami — c2 bearish grande, c1 pequeno contido dentro
    if (bear2 and body2 > avg_body
            and h1 < h2 and l1 > l2 and body1 < body2 * 0.5):
        add("Bullish Harami", "Harami de Alta (Mulher Grávida)", "bullish", 2)

    # 21. Bearish Harami — c2 bullish grande, c1 pequeno contido dentro
    if (bull2 and body2 > avg_body
            and h1 < h2 and l1 > l2 and body1 < body2 * 0.5):
        add("Bearish Harami", "Harami de Baixa (Mulher Grávida)", "bearish", 2)

    # 22. Top Tweezers — highs quase iguais após uptrend
    if trend_up and bear1 and abs(h1 - h2) / avg_rng < 0.04:
        add("Top Tweezers", "Pinça de Topo", "bearish", 2)

    # 23. Bottom Tweezers — lows quase iguais após downtrend
    if trend_down and bull1 and abs(l1 - l2) / avg_rng < 0.04:
        add("Bottom Tweezers", "Pinça de Fundo", "bullish", 2)

    # 24. Bullish Tasuki Gap — gap de alta que não é fechado pelo c1 bearish
    if bull3 and bull2 and bear1 and l2 > h3 and cl1 > h3:
        add("Bullish Tasuki Gap", "Gap de Alta Tasuki", "bullish", 2)

    # 25. Bearish Tasuki Gap — gap de baixa que não é fechado pelo c1 bullish
    if bear3 and bear2 and bull1 and h2 < l3 and cl1 < l3:
        add("Bearish Tasuki Gap", "Gap de Baixa Tasuki", "bearish", 2)

    # ── THREE CANDLE (12 padrões) ─────────────────────────────────────────────

    # 26. Three White Soldiers — 3 velas verdes crescentes consecutivas
    if (bull1 and bull2 and bull3
            and cl1 > cl2 > cl3 and o1 > o2 > o3
            and body1 > avg_body * 0.7 and body2 > avg_body * 0.7):
        add("Three White Soldiers", "3 Soldados Brancos", "bullish", 3)

    # 27. Three Black Crows — 3 velas vermelhas decrescentes consecutivas
    if (bear1 and bear2 and bear3
            and cl1 < cl2 < cl3 and o1 < o2 < o3
            and body1 > avg_body * 0.7 and body2 > avg_body * 0.7):
        add("Three Black Crows", "3 Corvos Pretos", "bearish", 3)

    # 28. Three Inside Up — Harami + confirmação bullish que supera o c3
    if (bear3 and h2 < h3 and l2 > l3
            and bull2 and bull1 and cl1 > h3):
        add("Three Inside Up", "3 Por Dentro de Alta", "bullish", 3)

    # 29. Three Outside Down — Engolfo bearish + confirmação abaixo do engolfo
    if (bull3 and bear2 and o2 >= cl3 and cl2 <= o3
            and bear1 and cl1 < cl2):
        add("Three Outside Down", "3 Por Fora de Baixa", "bearish", 3)

    # 30. Bullish Abandoned Baby — c3 bearish, c2 doji com gap abaixo, c1 bullish com gap acima
    if (bear3 and (doji2 or body2 / rng2 < 0.15)
            and bull1 and l2 > h3 and l2 > l1):
        add("Bullish Abandoned Baby", "Bebê Abandonado de Alta", "bullish", 3)

    # 31. Bearish Abandoned Baby — c3 bullish, c2 doji com gap acima, c1 bearish com gap abaixo
    if (bull3 and (doji2 or body2 / rng2 < 0.15)
            and bear1 and h2 < l3 and h2 < h1):
        add("Bearish Abandoned Baby", "Bebê Abandonado de Baixa", "bearish", 3)

    # 32. Morning Star — c3 bearish grande + c2 doji/pequeno + c1 bullish fecha acima do meio de c3
    if (bear3 and body3 > avg_body * 0.7
            and (doji2 or body2 / rng2 < 0.35)
            and bull1 and body1 > avg_body * 0.7
            and cl1 > (o3 + cl3) / 2):
        add("Morning Star", "Estrela da Manhã", "bullish", 3)

    # 33. Evening Star — c3 bullish grande + c2 doji/pequeno + c1 bearish fecha abaixo do meio de c3
    if (bull3 and body3 > avg_body * 0.7
            and (doji2 or body2 / rng2 < 0.35)
            and bear1 and body1 > avg_body * 0.7
            and cl1 < (o3 + cl3) / 2):
        add("Evening Star", "Estrela da Noite", "bearish", 3)

    # 34. Rising Three Methods — 5 velas: c5 bull grande, c4/c3/c2 pequenas contidas, c1 bull acima
    if (bull5 and body5 > avg_body * 1.4
            and bear4 and bear3 and bear2
            and h4 < h5 and l4 > l5
            and bull1 and cl1 > cl5):
        add("Rising Three Methods", "Padrão de Alta de 3 Dias", "bullish", 2)

    # 35. Falling Three Methods
    if (bear5 and body5 > avg_body * 1.4
            and bull4 and bull3 and bull2
            and h4 < h5 and l4 > l5
            and bear1 and cl1 < cl5):
        add("Falling Three Methods", "Padrão de Baixa de 3 Dias", "bearish", 2)

    # 36. Bullish Strike — 3 velas de baixa + c1 bullish gigante que engolfa tudo
    if (bear2 and bear3 and bear4
            and bull1 and body1 > (body2 + body3 + body4) * 0.75):
        add("Bullish Strike", "Strike de Alta", "bullish", 2)

    # 37. Bearish Strike — 3 velas de alta + c1 bearish gigante que engolfa tudo
    if (bull2 and bull3 and bull4
            and bear1 and body1 > (body2 + body3 + body4) * 0.75):
        add("Bearish Strike", "Strike de Baixa", "bearish", 2)

    # ── GAP / OUTROS (4 padrões) ──────────────────────────────────────────────

    # 38. High Gap with Two Crows — c3 bullish, gap, c2 bearish, c1 bearish maior
    if bull3 and l2 > h3 and bear2 and bear1 and body1 > body2:
        add("High Gap Two Crows", "Gap com 2 Corvos", "bearish", 2)

    # 39. Bullish Reversal Island — gap abaixo de c3, c2 isolado, gap acima em c1
    if (h2 < l3 and l1 > h2 and bull1):
        add("Bullish Reversal Island", "Ilha de Reversão Ascendente", "bullish", 2)

    # 40. Bearish Reversal Island — gap acima de c3, c2 isolado, gap abaixo em c1
    if (l2 > h3 and h1 < l2 and bear1):
        add("Bearish Reversal Island", "Ilha de Reversão Descendente", "bearish", 2)

    # 41. Stick Sandwich — c3 bearish, c2 bullish, c1 bearish com close ≈ c3
    if (bear3 and bull2 and bear1
            and abs(cl1 - cl3) / avg_rng < 0.03):
        add("Stick Sandwich", "Vela Prensada", "bullish", 2)

    # Remove duplicatas e padrões subsumidos — prioriza o mais específico (maior strength)
    seen: set = set()
    unique: List[CandlePattern] = []
    for p in sorted(found, key=lambda x: -x.strength):
        if p.name not in seen:
            seen.add(p.name)
            unique.append(p)

    return unique


# ── Integração com signal_engine ──────────────────────────────────────────────

def get_pattern_bonus(patterns: List[CandlePattern], direction) -> float:
    """
    Retorna o maior bonus de padrão compatível com a direção.
    Substitui a antiga candle_pattern_bonus() no signal_engine.
    direction: Direction enum ou string "LONG"/"SHORT"
    """
    dir_str = str(direction).upper().split(".")[-1]
    target  = "bullish" if dir_str == "LONG" else "bearish"

    bonus = 0.0
    for p in patterns:
        if p.signal in (target, "neutral"):
            bonus = max(bonus, p.bonus)
    return bonus


# ── Análise Multi-Timeframe ───────────────────────────────────────────────────

async def analyze_asset_mtf(symbol: str) -> Dict[str, List[CandlePattern]]:
    """
    Analisa padrões nos 9 timeframes (3m → 1S).
    Retorna dict: timeframe → lista de padrões.
    Executa todas as fetches em paralelo.
    """
    results: Dict[str, List[CandlePattern]] = {}

    async def _fetch(tf: str):
        try:
            df = await _get_klines(symbol, tf, limit=50)
            results[tf] = detect_patterns(df) if df is not None and len(df) >= 5 else []
        except Exception as e:
            print(f"[PATTERN_MTF] {symbol} {tf} erro: {e}")
            results[tf] = []

    await asyncio.gather(*(_fetch(tf) for tf in MTF_TIMEFRAMES))
    return results


def summarize_bias(mtf_results: Dict[str, List[CandlePattern]]) -> dict:
    """
    Calcula viés geral ponderado por timeframe e força do padrão.
    Retorna: {"bias": "ALTA"|"BAIXA"|"NEUTRO"|"INDEFINIDO",
              "bull_score": float, "bear_score": float,
              "bull_count": int, "bear_count": int}
    """
    total_bull = 0.0
    total_bear = 0.0
    cnt_bull = cnt_bear = 0

    for tf, patterns in mtf_results.items():
        w = _TF_WEIGHTS.get(tf, 1.0)
        for p in patterns:
            if p.signal == "bullish":
                total_bull += w * p.strength
                cnt_bull   += 1
            elif p.signal == "bearish":
                total_bear += w * p.strength
                cnt_bear   += 1

    if total_bull == 0 and total_bear == 0:
        bias = "NEUTRO"
    elif total_bull > total_bear * 1.30:
        bias = "ALTA"
    elif total_bear > total_bull * 1.30:
        bias = "BAIXA"
    else:
        bias = "INDEFINIDO"

    return {
        "bias":       bias,
        "bull_score": round(total_bull, 1),
        "bear_score": round(total_bear, 1),
        "bull_count": cnt_bull,
        "bear_count": cnt_bear,
    }


def format_mtf_telegram(symbol: str, mtf_results: Dict[str, List[CandlePattern]],
                        bias_info: dict) -> str:
    """Formata análise MTF para envio no Telegram (MarkdownV2 compatible)."""
    from datetime import datetime

    bias    = bias_info["bias"]
    bull_sc = bias_info["bull_score"]
    bear_sc = bias_info["bear_score"]

    bias_emoji = {"ALTA": "🟢", "BAIXA": "🔴", "NEUTRO": "⚪", "INDEFINIDO": "🟡"}.get(bias, "⚪")
    now = datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")

    lines = [f"📊 *PADRÕES GRÁFICOS MTF — {symbol}*\n"]

    for tf in MTF_TIMEFRAMES:
        label    = MTF_LABELS.get(tf, tf)
        patterns = mtf_results.get(tf, [])

        if not patterns:
            lines.append(f"  `{label:>3}` ⚫ _sem padrão_")
            continue

        # Ordena por força decrescente, mostra até 3
        top = sorted(patterns, key=lambda x: -x.strength)[:3]
        parts = []
        for p in top:
            icon  = SIGNAL_ICONS.get(p.signal, "⚪")
            stars = STRENGTH_LABEL.get(p.strength, "★")
            parts.append(f"{icon} {p.name_pt} {stars}")

        lines.append(f"  `{label:>3}` {'  |  '.join(parts)}")

    bull_n = bias_info["bull_count"]
    bear_n = bias_info["bear_count"]
    total  = bull_n + bear_n

    lines.append(f"\n{bias_emoji} *Viés Geral: {bias}*")
    lines.append(f"   🟢 Alta: {bull_n} padrões (score {bull_sc})")
    lines.append(f"   🔴 Baixa: {bear_n} padrões (score {bear_sc})")
    if total:
        lines.append(f"   📈 Dominância: {'Alta' if bull_sc > bear_sc else 'Baixa'} "
                     f"({max(bull_sc, bear_sc)/(bull_sc+bear_sc)*100:.0f}%)")
    lines.append(f"\n_🕐 {now}_")

    return "\n".join(lines)
