"""
Gerador de PDF — Backtest V7: V6 + Fibonacci Confluence | Multi-TF 3m/5m/15m
Funciona antes e apos rodar o backtest.
  - Sem resultados: mostra estrategia, watchlist e metodologia.
  - Com resultados:  adiciona comparacao A vs B vs C e analise por TF.
"""

import json
from pathlib import Path
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY

BASE = Path(__file__).parent

# ── Cores ────────────────────────────────────────────────────────────────────────
DARK_BG  = colors.HexColor("#0D1117")
ACCENT   = colors.HexColor("#00B4D8")
GREEN    = colors.HexColor("#2ECC71")
RED      = colors.HexColor("#E74C3C")
YELLOW   = colors.HexColor("#F39C12")
ORANGE   = colors.HexColor("#E67E22")
WHITE    = colors.HexColor("#FFFFFF")
LIGHT_BG = colors.HexColor("#161B22")
GRAY     = colors.HexColor("#8B949E")
BORDER   = colors.HexColor("#30363D")
PURPLE   = colors.HexColor("#9B59B6")
TEAL     = colors.HexColor("#1ABC9C")
INDIGO   = colors.HexColor("#6C5CE7")

styles = getSampleStyleSheet()

def _sty(name, **kw):
    return ParagraphStyle(name + "_v7", parent=styles[name], **kw)

TITLE  = _sty("Title",    fontSize=26, textColor=WHITE,  fontName="Helvetica-Bold", spaceAfter=6,  alignment=TA_CENTER)
H1     = _sty("Heading1", fontSize=20, textColor=WHITE,  fontName="Helvetica-Bold", spaceAfter=8,  alignment=TA_CENTER)
H2     = _sty("Heading2", fontSize=14, textColor=ACCENT, fontName="Helvetica-Bold", spaceAfter=6)
H3     = _sty("Heading3", fontSize=11, textColor=YELLOW, fontName="Helvetica-Bold", spaceAfter=4)
H4     = _sty("Heading3", fontSize=10, textColor=TEAL,   fontName="Helvetica-Bold", spaceAfter=3)
BODY   = _sty("Normal",   fontSize=9,  textColor=WHITE,  spaceAfter=4,  leading=14, alignment=TA_JUSTIFY)
BODYS  = _sty("Normal",   fontSize=8,  textColor=GRAY,   spaceAfter=3,  leading=12)
SUBT   = _sty("Normal",   fontSize=11, textColor=GRAY,   spaceAfter=4,  alignment=TA_CENTER)
CENTER = _sty("Normal",   fontSize=9,  textColor=WHITE,  spaceAfter=4,  alignment=TA_CENTER)

def tbl(hdr=ACCENT, row_bg=LIGHT_BG):
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  hdr),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  DARK_BG),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),  9),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND",    (0, 1), (-1, -1), row_bg),
        ("TEXTCOLOR",     (0, 1), (-1, -1), WHITE),
        ("FONTSIZE",      (0, 1), (-1, -1), 8.5),
        ("GRID",          (0, 0), (-1, -1), 0.4, BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ])

def hr():
    return HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=8, spaceBefore=4)

def sp(h=0.3):
    return Spacer(1, h * cm)

def _v(d, *keys, default=0):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d if d is not None else default

def _fmt_pf(v): return f"{v:.3f}" if isinstance(v, (int, float)) else "—"
def _fmt_wr(v): return f"{v:.1f}%" if isinstance(v, (int, float)) else "—"
def _fmt_ret(v): return f"{v:+.1f}%" if isinstance(v, (int, float)) else "—"
def _fmt_dd(v): return f"{v:.1f}%" if isinstance(v, (int, float)) else "—"

def _color_pf(ts, col, row, pf):
    c = GREEN if pf >= 1.5 else YELLOW if pf >= 1.0 else RED
    ts.add("TEXTCOLOR", (col, row), (col, row), c)

def _color_ret(ts, col, row, ret):
    if ret is None:
        return
    c = GREEN if ret > 0 else RED
    ts.add("TEXTCOLOR", (col, row), (col, row), c)


# ── Watchlist ────────────────────────────────────────────────────────────────────
WATCHLIST_INFO = [
    ("BTCUSDT",   "Vol #1",  "$11.9B", "4.3M",  "+2.5%",  "Maior volume global"),
    ("ETHUSDT",   "Vol #2",  "$ 8.4B", "6.5M",  "+2.6%",  "2o volume + 2o trades"),
    ("SOLUSDT",   "Vol #3",  "$ 1.5B", "1.5M",  "+4.3%",  "Alta liquidez + volatilidade"),
    ("BEATUSDT",  "Trades#1","$ 0.8B", "11.4M", "+22.2%", "Mais trades do dia (11.4M)"),
    ("STGUSDT",   "Apr #1",  "$ 0.3B", "4.7M",  "+42.9%", "Maior valorizacao 24h"),
    ("SOXLUSDT",  "Apr #2",  "$ 0.8B", "2.4M",  "+25.3%", "2a maior valorizacao"),
    ("MUUSDT",    "Vol+Apr", "$ 0.7B", "2.0M",  "+10.4%", "Alto volume e apreciacao"),
    ("WLDUSDT",   "Apr+Trd", "$ 0.5B", "2.5M",  "+11.3%", "Trades elevados + alta"),
    ("AIOUSDT",   "Trades#3","$ 0.3B", "5.2M",  "-6.2%",  "3o maior numero de trades"),
    ("MRVLUSDT",  "Apr #4",  "$ 0.5B", "1.7M",  "+11.8%", "Valorizacao + volume solido"),
    ("CLUSDT",    "Vol #4",  "$ 0.9B", "1.0M",  "-5.5%",  "4o maior volume 24h"),
    ("ALLOUSDT",  "Trades#6","$ 0.1B", "2.9M",  "+6.4%",  "Muitos trades, ativo liquido"),
    ("SUIUSDT",   "Balanced","$ 0.1B", "0.6M",  "+2.7%",  "Equilibrado vol+trades"),
    ("DOGEUSDT",  "Popular", "$ 0.3B", "0.8M",  "+3.2%",  "Alta liquidez + popularidade"),
    ("LUMIAUSDT", "Apr #5",  "$ 0.02B","0.4M",  "+36.2%", "5a maior valorizacao 24h"),
]


# ══════════════════════════════════════════════════════════════════════════════════
# PAGINAS
# ══════════════════════════════════════════════════════════════════════════════════

def page_cover(story, v7):
    story.append(sp(1.5))
    story.append(Paragraph("TRADER 001", TITLE))
    story.append(Paragraph("BACKTEST V7 — FIBONACCI CONFLUENCE | MULTI-TF", H1))
    story.append(sp(0.2))
    story.append(Paragraph(
        "V6 (OB + FVG + Score) vs V6+Fibonacci vs Fibonacci Isolado  |  3m / 5m / 15m  |  Top 15 Binance Futures",
        SUBT))
    story.append(hr())
    story.append(sp(0.4))

    # KPIs se resultados existem
    if v7:
        best = v7.get("best_combos", [])
        if best:
            b = best[0]
            story.append(Paragraph("MELHOR RESULTADO ENCONTRADO", H3))
            kpi = [
                ["TF", "Grupo", "Profit Factor", "Win Rate", "Retorno", "Max DD", "Trades"],
                [
                    b.get("tf", "—"),
                    b.get("group", "—"),
                    _fmt_pf(b.get("profit_factor", 0)),
                    _fmt_wr(b.get("win_rate", 0)),
                    _fmt_ret(b.get("pct_return", 0)),
                    _fmt_dd(b.get("max_drawdown", 0)),
                    str(b.get("total_trades", 0)),
                ],
            ]
            t = Table(kpi, colWidths=[2*cm, 2.5*cm, 3.5*cm, 3*cm, 3*cm, 2.5*cm, 2.5*cm])
            ts = tbl(TEAL)
            ts.add("TEXTCOLOR", (2, 1), (2, 1), GREEN)
            ts.add("FONTSIZE",  (0, 1), (-1, 1), 10)
            ts.add("FONTNAME",  (0, 1), (-1, 1), "Helvetica-Bold")
            t.setStyle(ts)
            story.append(t)
            story.append(sp(0.4))
    else:
        story.append(Paragraph(
            "Backtest ainda nao executado. Execute micro_cap_backtest_v7.py para "
            "popular os resultados. Este PDF documenta a metodologia e estrategia completa.",
            BODY))
        story.append(sp(0.4))

    # Info box
    info = [
        ["Parametro", "Valor"],
        ["Periodo", "180 dias"],
        ["Timeframes", "3m  |  5m  |  15m"],
        ["Capital inicial", "$1.000 USDT"],
        ["Alavancagem", "10x"],
        ["Taxas (taker)", "0.05% por lado"],
        ["Sizing por trade", "4% do capital (max 15%)"],
        ["Simbolos testados", "15 (Top Binance Futures 24h)"],
        ["Grupos", "A = V6 puro  |  B = V6+Fib  |  C = Fib isolado"],
    ]
    t2 = Table(info, colWidths=[5*cm, 13*cm])
    t2.setStyle(tbl(ACCENT))
    story.append(t2)
    story.append(sp(0.5))

    story.append(Paragraph(
        f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}  |  "
        "Binance USDT-M Futures  |  Taxas reais incluidas",
        BODYS))
    story.append(PageBreak())


def page_hipotese(story):
    story.append(Paragraph("HIPOTESE E METODOLOGIA", H2))
    story.append(hr())

    story.append(Paragraph(
        "O backtest V7 testa se a confluencia entre Order Blocks/FVG (estrategia V6) "
        "e niveis de retracaoo de Fibonacci (0.382, 0.500, 0.618) produz resultados "
        "superiores ao V6 puro — e se o Fibonacci sozinho ja tem edge operavel.",
        BODY))
    story.append(sp(0.3))

    story.append(Paragraph("TRES GRUPOS DE TESTE", H3))
    grupos = [
        ["Grupo", "Nome", "Condicao de Entrada", "Hipotese"],
        ["A", "V6 Puro (baseline)",
         "OB/FVG tap + Score >= 5/6 + HTF estrutura",
         "Referencia — igual ao melhor resultado anterior (PF 3.525 no V6)"],
        ["B", "V6 + Fibonacci",
         "Mesmas regras do grupo A + preco em zona fib 0.382/0.5/0.618",
         "Menos trades, mas WR e PF maiores por alta confluencia"],
        ["C", "Fibonacci Isolado",
         "Retração em golden zone (fib 0.382/0.5/0.618) + vela confirmacao + HTF",
         "Fibonacci sozinho tem edge? Se PF < 1.0, nao tem. Se >= 1.0, vale como sistema."],
    ]
    t = Table(grupos, colWidths=[1.5*cm, 3.5*cm, 6*cm, 7*cm])
    ts = tbl(INDIGO)
    ts.add("TEXTCOLOR", (0, 1), (0, 3), YELLOW)
    ts.add("FONTNAME",  (0, 1), (0, 3), "Helvetica-Bold")
    t.setStyle(ts)
    story.append(t)
    story.append(sp(0.4))

    story.append(Paragraph("CONFIGURACAO POR TIMEFRAME", H3))
    tf_cfg = [
        ["TF Entrada", "Estrutura HTF", "HTF Superior", "Exit Bars", "Min Conf OB", "Min Conf FVG", "OB Max Bars"],
        ["3m",  "15m", "1h",  "60 (~3h)",  "4", "5", "750"],
        ["5m",  "15m", "1h",  "60 (~5h)",  "4", "5", "450"],
        ["15m", "1h",  "4h",  "60 (~15h)", "5", "6", "250"],
    ]
    t2 = Table(tf_cfg, colWidths=[2.5*cm, 2.5*cm, 2.5*cm, 2.8*cm, 2.8*cm, 2.8*cm, 2.8*cm])
    t2.setStyle(tbl(ORANGE))
    story.append(t2)
    story.append(sp(0.3))

    story.append(Paragraph(
        "Para scalp (3m e 5m) o threshold de score e reduzido em 1 ponto (4/5 vs 5/6) "
        "para compensar que timeframes curtos tem menos barras para acumular OBs. "
        "O lookback de zonas e proporcionalmente maior para cobrir o mesmo periodo em horas.",
        BODYS))
    story.append(PageBreak())


def page_fibonacci(story):
    story.append(Paragraph("FIBONACCI — IMPLEMENTACAO E LOGICA", H2))
    story.append(hr())

    story.append(Paragraph(
        "A retracaoo de Fibonacci calcula os niveis de suporte/resistencia esperados "
        "apos um movimento impulsivo. O calculo usa o swing high e swing low das "
        "ultimas 50 velas, gerando 5 niveis de retracaoo.",
        BODY))
    story.append(sp(0.3))

    story.append(Paragraph("NIVEIS E PONTUACAO", H3))
    fib_niveis = [
        ["Nivel Fib", "Formula", "Bonus no Score", "Categoria", "Interpretacao"],
        ["0.236", "HH - 23.6% do range", "+5 pts", "Minor", "Retracaoo rasa — mercado forte"],
        ["0.382", "HH - 38.2% do range", "+10 pts", "Golden Zone", "Primeira zona de suporte relevante"],
        ["0.500", "HH - 50.0% do range", "+10 pts", "Golden Zone", "Zona de equilibrio — alta reversao"],
        ["0.618", "HH - 61.8% do range", "+10 pts", "Golden Zone", "Razao aurea — zona de maior probabilidade"],
        ["0.786", "HH - 78.6% do range", "+5 pts", "Minor", "Ultima zona antes de invalidar o swing"],
    ]
    t = Table(fib_niveis, colWidths=[2*cm, 4*cm, 2.5*cm, 2.5*cm, 7*cm])
    ts = tbl(TEAL)
    for row in [2, 3, 4]:
        ts.add("BACKGROUND", (0, row), (-1, row), colors.HexColor("#1a3d2e"))
        ts.add("TEXTCOLOR", (3, row), (3, row), GREEN)
    t.setStyle(ts)
    story.append(t)
    story.append(sp(0.4))

    story.append(Paragraph("CONDICAO DE ENTRADA — GRUPO B (V6 + FIB)", H3))
    story.append(Paragraph(
        "Todas as condicoes do V6 (OB/FVG tap, estrutura HTF, RSI neutro, "
        "corpo da vela >=50%) <b>mais</b> o preco dentro de ±0.5% de uma golden zone "
        "(0.382 / 0.500 / 0.618). Se nenhuma golden zone esta ativa, o trade nao abre.",
        BODY))
    story.append(sp(0.3))

    story.append(Paragraph("CONDICAO DE ENTRADA — GRUPO C (FIB ISOLADO)", H3))
    fib_c = [
        ["Regra", "Long", "Short"],
        ["Zona Fibonacci", "Low da vela toca golden zone AND close > nivel", "High toca zona AND close < nivel"],
        ["Tolerancia", "±0.5% do preco para considerar a zona ativa", "Igual"],
        ["Vela", "Bullish (close > open), corpo >= 45%", "Bearish (close < open), corpo >= 45%"],
        ["RSI", "Entre 40 e 65", "Entre 35 e 60"],
        ["HTF", "Estrutura 15m/1h em alta + regime BTC", "Estrutura em baixa"],
        ["Stop Loss", "Nivel fib - 1.0 ATR", "Nivel fib + 1.0 ATR"],
        ["TP1", "+1.5R (50% da posicao)", "-1.5R (50% da posicao)"],
        ["TP2", "+3.0R (restante)", "-3.0R (restante)"],
    ]
    t2 = Table(fib_c, colWidths=[4*cm, 7.5*cm, 6.5*cm])
    t2.setStyle(tbl(YELLOW))
    story.append(t2)
    story.append(sp(0.3))

    story.append(Paragraph(
        "Diferenca chave: no Grupo C nao ha verificacao de OB ou FVG. "
        "A logica e puramente: preco retrai para nivel Fibonacci + confirmacao direcional + tendencia. "
        "Se este grupo tiver PF > 1.0, Fibonacci sozinho tem edge. "
        "Se PF < 1.0, a confluencia com OB/FVG (Grupo B) e necessaria.",
        BODYS))
    story.append(PageBreak())


def page_watchlist(story):
    story.append(Paragraph("WATCHLIST — TOP 15 BINANCE FUTURES", H2))
    story.append(hr())
    story.append(Paragraph(
        "Criterios: maior volume 24h + maior numero de trades + maior valorizacao. "
        "Dados coletados em 12/06/2026. Inclui grandes (BTC/ETH) e ativos em alta "
        "para capturar comportamentos em diferentes regimes de liquidez.",
        BODY))
    story.append(sp(0.3))

    data = [["Simbolo", "Criterio", "Vol 24h", "Trades 24h", "Var % 24h", "Justificativa"]]
    for row in WATCHLIST_INFO:
        data.append(list(row))

    t = Table(data, colWidths=[2.8*cm, 2.5*cm, 2.2*cm, 2.2*cm, 2.2*cm, 6.1*cm])
    ts = tbl(ACCENT)
    for i, (_, _, _, _, var, _) in enumerate(WATCHLIST_INFO, start=1):
        pct = float(var.replace("%", "").replace("+", ""))
        col = GREEN if pct > 5 else YELLOW if pct > 0 else RED
        ts.add("TEXTCOLOR", (4, i), (4, i), col)
    t.setStyle(ts)
    story.append(t)
    story.append(sp(0.3))

    story.append(Paragraph(
        "Observacao: AIOUSDT (-6.2%) e CLUSDT (-5.5%) foram incluidos por volume e numero "
        "de trades, nao por valorizacao. Em condicoes de queda, a estrategia opera SHORT — "
        "estes ativos podem gerar sinais SHORT de alta qualidade.",
        BODYS))
    story.append(PageBreak())


def page_resultados_por_tf(story, v7):
    story.append(Paragraph("RESULTADOS — COMPARACAO A vs B vs C", H2))
    story.append(hr())

    if not v7 or not v7.get("by_tf"):
        story.append(Paragraph(
            "Resultados nao disponiveis. Execute: python micro_cap_backtest_v7.py",
            BODY))
        story.append(PageBreak())
        return

    by_tf = v7["by_tf"]
    days  = v7.get("days", 180)

    story.append(Paragraph(
        f"Periodo: {days} dias | Capital: $1.000 | Leverage: 10x | Taxas reais 0.05%",
        BODYS))
    story.append(sp(0.3))

    for tf_name in ("3m", "5m", "15m"):
        tf_data = by_tf.get(tf_name, {})
        story.append(Paragraph(f"TIMEFRAME: {tf_name}", H3))

        rows = [["Grupo", "Trades", "Win Rate", "Profit Factor", "Retorno", "Max DD", "Capital Final"]]
        for grp in ("A", "B", "C"):
            agg = tf_data.get(grp)
            if agg:
                ret = agg.get("pct_return") or 0
                fc = 1000 + ret / 100 * 1000
                rows.append([
                    f"{grp} — {'V6 Puro' if grp=='A' else 'V6+Fib' if grp=='B' else 'Fib Iso'}",
                    str(agg.get("total_trades", 0)),
                    _fmt_wr(agg.get("win_rate", 0)),
                    _fmt_pf(agg.get("profit_factor", 0)),
                    _fmt_ret(agg.get("pct_return", 0)),
                    _fmt_dd(agg.get("max_drawdown", 0)),
                    f"${fc:,.2f}",
                ])
            else:
                rows.append([f"{grp}", "—", "—", "—", "—", "—", "—"])

        t = Table(rows, colWidths=[4*cm, 2*cm, 2.5*cm, 3*cm, 2.5*cm, 2.5*cm, 3*cm])
        ts = tbl(ACCENT if tf_name == "15m" else ORANGE if tf_name == "5m" else PURPLE)
        for i in range(1, len(rows)):
            agg_ref = tf_data.get(("A", "B", "C")[i-1])
            if agg_ref:
                pf_v = agg_ref.get("profit_factor", 0)
                ret_v = agg_ref.get("pct_return", 0)
                _color_pf(ts, 3, i, pf_v)
                _color_ret(ts, 4, i, ret_v)
        t.setStyle(ts)
        story.append(t)

        # Exits por grupo
        story.append(sp(0.2))
        exit_rows = [["Saida"] + [f"[{g}]" for g in ("A", "B", "C")]]
        for exit_key in ("tp1", "tp2", "trail", "sl", "timeout"):
            row = [exit_key.upper()]
            for grp in ("A", "B", "C"):
                agg = tf_data.get(grp, {})
                ex  = agg.get("exits", {}) if agg else {}
                row.append(str(ex.get(exit_key, 0)))
            exit_rows.append(row)
        t2 = Table(exit_rows, colWidths=[3*cm, 4.5*cm, 4.5*cm, 4.5*cm])
        t2.setStyle(tbl(GRAY))
        story.append(t2)
        story.append(sp(0.4))

    story.append(PageBreak())


def page_fibonacci_analise(story, v7):
    story.append(Paragraph("ANALISE FIBONACCI — NIVEIS MAIS ACIONADOS", H2))
    story.append(hr())

    if not v7 or not v7.get("by_tf"):
        story.append(Paragraph("Resultados nao disponiveis.", BODY))
        story.append(PageBreak())
        return

    story.append(Paragraph(
        "Contagem de quantas vezes cada nivel de Fibonacci foi atingido como zona de "
        "confluencia nos Grupos B e C. Mostra quais retracooes sao mais relevantes "
        "para os ativos da watchlist.",
        BODY))
    story.append(sp(0.3))

    from collections import defaultdict
    fib_by_tf = {}
    fib_total = defaultdict(int)

    for tf_name in ("3m", "5m", "15m"):
        tf_data = v7["by_tf"].get(tf_name, {})
        fib_tf  = defaultdict(int)
        for grp in ("B", "C"):
            agg = tf_data.get(grp)
            if agg and agg.get("fib_hits"):
                for k, vv in agg["fib_hits"].items():
                    fib_tf[k]    += vv
                    fib_total[k] += vv
        fib_by_tf[tf_name] = dict(fib_tf)

    # Tabela por TF
    all_levels = ["0.236", "0.382", "0.500", "0.618", "0.786"]
    header = ["Nivel Fib", "Categoria"] + [f"3m", "5m", "15m", "Total"]
    rows = [header]
    for level in all_levels:
        cat = "Golden Zone" if level in ("0.382", "0.500", "0.618") else "Minor"
        row = [level, cat]
        for tf_name in ("3m", "5m", "15m"):
            row.append(str(fib_by_tf.get(tf_name, {}).get(level, 0)))
        row.append(str(fib_total.get(level, 0)))
        rows.append(row)

    t = Table(rows, colWidths=[2.5*cm, 3*cm, 2.5*cm, 2.5*cm, 2.5*cm, 2.5*cm])
    ts = tbl(TEAL)
    for i, level in enumerate(all_levels, start=1):
        if level in ("0.382", "0.500", "0.618"):
            ts.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#0d2b1e"))
            ts.add("TEXTCOLOR", (1, i), (1, i), GREEN)
    t.setStyle(ts)
    story.append(t)
    story.append(sp(0.3))

    story.append(Paragraph(
        "Interpretacao: nivel com mais acionamentos indica onde o mercado "
        "mais frequentemente para e reverte. Se 0.618 dominar, os ativos "
        "tendem a retrair profundamente antes de retomar a tendencia.",
        BODYS))
    story.append(PageBreak())


def page_per_symbol(story, v7):
    per = v7.get("per_symbol")
    if not per:
        return

    def _ps(d, k, fmt):
        v = d.get(k, 0) if d else 0
        if fmt == "pf":  return f"{v:.2f}"
        if fmt == "wr":  return f"{v}%"
        return str(v)

    def _pfc(ts_s, col, row, d, k="pf"):
        pf = d.get(k, 0) if d else 0
        c = GREEN if pf >= 1.5 else YELLOW if pf >= 1.0 else RED
        ts_s.add("TEXTCOLOR", (col, row), (col, row), c)

    story.append(Paragraph("RESULTADOS POR SIMBOLO", H2))
    story.append(hr())

    n_done = v7.get("symbols_completed", len(per))
    pending = ", ".join(v7.get("symbols_pending", []))
    story.append(Paragraph(
        f"Simbolos processados: {n_done}/15.  Pendentes: {pending if pending else 'nenhum'}.",
        BODYS))
    story.append(sp(0.3))

    # 15m: Grupo A vs B
    story.append(Paragraph("15m — por Simbolo (A = V6 puro  |  B = V6+Fibonacci)", H3))
    hdr = ["Simbolo", "A Trades", "A WR", "A PF", "B Trades", "B WR", "B PF"]
    rows = [hdr]
    for s in per:
        a = s.get("15m_A", {})
        b = s.get("15m_B", {})
        rows.append([s["symbol"],
                     _ps(a,"trades","t"), _ps(a,"wr","wr"), _ps(a,"pf","pf"),
                     _ps(b,"trades","t"), _ps(b,"wr","wr"), _ps(b,"pf","pf")])
    t = Table(rows, colWidths=[3.2*cm, 2.2*cm, 1.8*cm, 1.8*cm, 2.2*cm, 1.8*cm, 1.8*cm])
    ts2 = tbl(ACCENT)
    for i, s in enumerate(per, start=1):
        _pfc(ts2, 3, i, s.get("15m_A", {}))
        _pfc(ts2, 6, i, s.get("15m_B", {}))
    t.setStyle(ts2)
    story.append(t)
    story.append(sp(0.4))

    # 5m: Grupo A vs B
    story.append(Paragraph("5m — por Simbolo (A = V6 puro  |  B = V6+Fibonacci)", H3))
    hdr5 = ["Simbolo", "A Trades", "A WR", "A PF", "B Trades", "B WR", "B PF"]
    rows5 = [hdr5]
    for s in per:
        a = s.get("5m_A", {})
        b = s.get("5m_B", {})
        rows5.append([s["symbol"],
                      _ps(a,"trades","t"), _ps(a,"wr","wr"), _ps(a,"pf","pf"),
                      _ps(b,"trades","t"), _ps(b,"wr","wr"), _ps(b,"pf","pf")])
    t5 = Table(rows5, colWidths=[3.2*cm, 2.2*cm, 1.8*cm, 1.8*cm, 2.2*cm, 1.8*cm, 1.8*cm])
    ts5 = tbl(ORANGE)
    for i, s in enumerate(per, start=1):
        _pfc(ts5, 3, i, s.get("5m_A", {}))
        _pfc(ts5, 6, i, s.get("5m_B", {}))
    t5.setStyle(ts5)
    story.append(t5)
    story.append(sp(0.4))

    # 3m: Grupo A
    story.append(Paragraph("3m — por Simbolo (Grupo A = V6 puro)", H3))
    hdr3 = ["Simbolo", "Trades", "Win Rate", "Profit Factor"]
    rows3 = [hdr3]
    for s in per:
        a = s.get("3m_A", {})
        rows3.append([s["symbol"], _ps(a,"trades","t"), _ps(a,"wr","wr"), _ps(a,"pf","pf")])
    t3 = Table(rows3, colWidths=[3.2*cm, 3*cm, 3*cm, 3*cm])
    ts3 = tbl(PURPLE)
    for i, s in enumerate(per, start=1):
        _pfc(ts3, 3, i, s.get("3m_A", {}))
    t3.setStyle(ts3)
    story.append(t3)
    story.append(PageBreak())


def page_top_combos(story, v7):
    story.append(Paragraph("MELHORES COMBINACOES — RANKING GLOBAL", H2))
    story.append(hr())

    if not v7 or not v7.get("best_combos"):
        story.append(Paragraph("Resultados nao disponiveis.", BODY))
        story.append(PageBreak())
        return

    best = v7["best_combos"]

    story.append(Paragraph(
        "Todas as combinacoes TF x Grupo ordenadas por Profit Factor. "
        "Somente combinacoes com pelo menos 10 trades sao incluidas.",
        BODY))
    story.append(sp(0.3))

    rows = [["Rank", "TF", "Grupo", "PF", "Win Rate", "Retorno", "Max DD", "Trades"]]
    for i, b in enumerate(best, start=1):
        rows.append([
            str(i),
            b.get("tf", "—"),
            b.get("group", "—"),
            _fmt_pf(b.get("profit_factor", 0)),
            _fmt_wr(b.get("win_rate", 0)),
            _fmt_ret(b.get("pct_return", 0)),
            _fmt_dd(b.get("max_drawdown", 0)),
            str(b.get("total_trades", 0)),
        ])

    t = Table(rows, colWidths=[1.5*cm, 2*cm, 3*cm, 3*cm, 3*cm, 3*cm, 2.5*cm, 2.5*cm])
    ts = tbl(PURPLE)
    for i, b in enumerate(best, start=1):
        pf_v  = b.get("profit_factor", 0)
        ret_v = b.get("pct_return", 0)
        _color_pf(ts, 3, i, pf_v)
        _color_ret(ts, 5, i, ret_v)
        if i == 1:
            ts.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#1a2e1a"))
    t.setStyle(ts)
    story.append(t)
    story.append(sp(0.4))

    # Analise comparativa V6 vs V7
    story.append(Paragraph("ANALISE: V6 vs V7 — O QUE MUDOU", H3))
    comp_data = [
        ["Aspecto", "V6", "V7 (melhor combo)", "Impacto esperado"],
        ["Timeframes", "15m apenas", "3m / 5m / 15m", "Descobre melhor janela operacional"],
        ["Fibonacci", "Nao existia", "Golden zones como bonus/filtro", "Reduz trades, aumenta qualidade"],
        ["Watchlist", "24 micro-caps", "15 top Binance Futures", "Maior liquidez, menor spread"],
        ["Periodo", "90 dias", "180 dias", "Cobre mais ciclos de mercado"],
        ["Grupos", "Unico", "A / B / C comparados", "Prova ou refuta hipotese Fib"],
    ]
    t2 = Table(comp_data, colWidths=[3.5*cm, 3.5*cm, 4.5*cm, 6.5*cm])
    t2.setStyle(tbl(TEAL))
    story.append(t2)
    story.append(PageBreak())


def page_roadmap(story, v7):
    story.append(Paragraph("CONCLUSOES E PROXIMOS PASSOS", H2))
    story.append(hr())

    story.append(Paragraph("INTERPRETACAO DOS RESULTADOS", H3))
    story.append(Paragraph(
        "Se Grupo B (V6+Fib) tiver PF e WR maiores que Grupo A (V6 puro): "
        "confirma que Fibonacci adiciona valor como filtro de qualidade. "
        "Implementar no bot live como condicao opcional (+10 pts no score).",
        BODY))
    story.append(Paragraph(
        "Se Grupo C (Fib isolado) tiver PF > 1.0: Fibonacci sozinho tem edge. "
        "Vale criar um modo de operacao dedicado apenas a retracooes.",
        BODY))
    story.append(Paragraph(
        "Se Grupo B == Grupo A: a filtragem por Fibonacci nao adiciona valor "
        "alem do que o V6 ja seleciona — manter como bonus de score sem obrigatoriedade.",
        BODY))
    story.append(sp(0.3))

    story.append(Paragraph("ROTEIRO V8 (BASEADO NOS RESULTADOS DO V7)", H3))
    roadmap = [
        ["Prioridade", "Acao", "Condicao"],
        ["ALTA",
         "Integrar melhor TF+Grupo no signal_engine.py do bot live",
         "TF com maior PF no V7 passa a ser o timeframe padrao do modo autonomo"],
        ["ALTA",
         "Extensoes Fibonacci como alvos de TP",
         "Se 0.618 for o nivel mais acionado, TP2 = extensao 1.618 do swing"],
        ["MEDIA",
         "CVD (Volume Delta) como filtro adicional no Grupo B",
         "Exigir pressao compradora confirmada no reteste da zona fib"],
        ["MEDIA",
         "Multi-TF Fibonacci: nivell 4h como zona principal",
         "Zona fib calculada no 4h tem mais peso que a do 15m"],
        ["BAIXA",
         "Otimizar FIB_TOL_PCT por simbolo",
         "BTC tolerancia 0.3%, micro-caps 0.8% — adaptar ao ATR medio"],
    ]
    t = Table(roadmap, colWidths=[2.5*cm, 7*cm, 8.5*cm])
    t.setStyle(tbl(ORANGE))
    story.append(t)
    story.append(sp(0.4))

    story.append(Paragraph("COMO RODAR O BACKTEST", H3))
    cmds = [
        ["Comando", "Descricao"],
        ["python micro_cap_backtest_v7.py", "180 dias, $1.000, 10x (padrao)"],
        ["python micro_cap_backtest_v7.py --days 90", "Teste rapido 90 dias"],
        ["python micro_cap_backtest_v7.py --capital 5000 --lev 5", "Capital maior, leverage menor"],
        ["python generate_micro_cap_pdf_v7.py", "Regerar este PDF com resultados atualizados"],
    ]
    t2 = Table(cmds, colWidths=[8*cm, 10*cm])
    t2.setStyle(tbl(GRAY))
    story.append(t2)
    story.append(sp(0.3))

    story.append(Paragraph(
        "Tempo estimado de execucao: 15–25 minutos (180 dias x 15 simbolos x 3 TFs = ~75 downloads). "
        "Resultados salvos em micro_cap_results_v7.json.",
        BODYS))


# ══════════════════════════════════════════════════════════════════════════════════
# CONSTRUCAO DO PDF
# ══════════════════════════════════════════════════════════════════════════════════

def build_pdf():
    # Carrega resultados se existirem
    results_path = BASE / "micro_cap_results_v7.json"
    v7 = None
    if results_path.exists():
        try:
            v7 = json.loads(results_path.read_text(encoding="utf-8"))
            print(f"  Resultados V7 carregados: {results_path.name}")
        except Exception as e:
            print(f"  Aviso: erro ao ler resultados ({e}) — gerando PDF sem dados.")
    else:
        print("  micro_cap_results_v7.json nao encontrado — PDF sem resultados.")

    ts   = datetime.now().strftime("%Y%m%d_%H%M")
    name = f"backtest_v7_report_{ts}.pdf"
    path = BASE / name

    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
    )

    def bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(DARK_BG)
        canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
        canvas.setFillColor(GRAY)
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(A4[0] - 1.8 * cm, 0.8 * cm, f"Trader 001 V7 | {ts}")
        canvas.drawString(1.8 * cm, 0.8 * cm, f"Pag. {doc.page}")
        canvas.restoreState()

    story = []
    page_cover(story, v7)
    page_hipotese(story)
    page_fibonacci(story)
    page_watchlist(story)

    if v7:
        page_resultados_por_tf(story, v7)
        page_per_symbol(story, v7)
        page_fibonacci_analise(story, v7)
        page_top_combos(story, v7)

    page_roadmap(story, v7)

    doc.build(story, onFirstPage=bg, onLaterPages=bg)
    size_kb = path.stat().st_size / 1024
    print(f"\nPDF gerado: {name}  ({size_kb:.1f} KB)")
    print(f"Caminho:    {path}")
    return path


if __name__ == "__main__":
    print("Gerando PDF V7...")
    build_pdf()
