import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # acha modulos do bot na pasta-mae
"""
Gera PDF com resultados consolidados dos backtests do Trader 001.
Fontes: micro_cap_results_v7.json + estratégia V5.3
"""
import json
from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak,
)

# ── Cores ──────────────────────────────────────────────────────────────────────
C_BG      = colors.HexColor("#0d1117")
C_DARK    = colors.HexColor("#161b22")
C_ACCENT  = colors.HexColor("#00d4aa")
C_YELLOW  = colors.HexColor("#f0c040")
C_RED     = colors.HexColor("#e05555")
C_GREEN   = colors.HexColor("#3fb950")
C_GRAY    = colors.HexColor("#8b949e")
C_WHITE   = colors.white
C_HEADER  = colors.HexColor("#1f6feb")

# ── Estilos ────────────────────────────────────────────────────────────────────
styles = getSampleStyleSheet()

def sty(name, **kwargs):
    base = styles["Normal"]
    return ParagraphStyle(name, parent=base, **kwargs)

S_TITLE   = sty("Title2",   fontSize=24, textColor=C_ACCENT,  alignment=TA_CENTER, spaceAfter=6, fontName="Helvetica-Bold")
S_SUB     = sty("Sub",      fontSize=12, textColor=C_GRAY,    alignment=TA_CENTER, spaceAfter=4)
S_H1      = sty("H1",       fontSize=14, textColor=C_YELLOW,  fontName="Helvetica-Bold", spaceBefore=14, spaceAfter=6)
S_H2      = sty("H2",       fontSize=11, textColor=C_ACCENT,  fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=4)
S_BODY    = sty("Body2",    fontSize=9,  textColor=C_WHITE,   spaceAfter=3)
S_CAPTION = sty("Caption2", fontSize=8,  textColor=C_GRAY,    spaceAfter=2)
S_CENTER  = sty("Center2",  fontSize=9,  textColor=C_WHITE,   alignment=TA_CENTER)
S_WARN    = sty("Warn",     fontSize=9,  textColor=C_RED,     fontName="Helvetica-Bold")
S_OK      = sty("Ok",       fontSize=9,  textColor=C_GREEN,   fontName="Helvetica-Bold")

def hr():
    return HRFlowable(width="100%", thickness=0.5, color=C_GRAY, spaceAfter=8, spaceBefore=4)

def grade_color(pf):
    if pf >= 1.5: return C_GREEN
    if pf >= 1.0: return C_YELLOW
    return C_RED

def grade_label(pf):
    if pf >= 1.5: return "BOM"
    if pf >= 1.0: return "ACEITAVEL"
    return "RUIM"

def pct_color(v):
    return C_GREEN if v >= 0 else C_RED

# ── Table helpers ──────────────────────────────────────────────────────────────
TS_BASE = TableStyle([
    ("BACKGROUND",    (0, 0), (-1, 0),  C_HEADER),
    ("TEXTCOLOR",     (0, 0), (-1, 0),  C_WHITE),
    ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
    ("FONTSIZE",      (0, 0), (-1, -1), 8),
    ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_DARK, C_BG]),
    ("TEXTCOLOR",     (0, 1), (-1, -1), C_WHITE),
    ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ("GRID",          (0, 0), (-1, -1), 0.3, C_GRAY),
    ("ROWHEIGHT",     (0, 0), (-1, -1), 16),
    ("LEFTPADDING",   (0, 0), (-1, -1), 4),
    ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
])

# ── Dados ─────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
with open(BASE / "micro_cap_results_v7.json") as f:
    v7 = json.load(f)

# V5.3 dados (do backtest reportado)
V53 = {
    "BTC/USDT": {"long": {"trades":312, "win_rate":52.6, "profit_factor":1.82, "total_return":38.4, "max_dd":14.2, "sharpe":1.41},
                 "short":{"trades":289, "win_rate":51.9, "profit_factor":1.79, "total_return":34.1, "max_dd":12.8, "sharpe":1.35}},
    "ETH/USDT": {"long": {"trades":298, "win_rate":53.0, "profit_factor":1.85, "total_return":41.2, "max_dd":15.1, "sharpe":1.48},
                 "short":{"trades":271, "win_rate":52.0, "profit_factor":1.74, "total_return":31.8, "max_dd":13.4, "sharpe":1.29}},
    "SOL/USDT": {"long": {"trades":334, "win_rate":54.2, "profit_factor":1.93, "total_return":52.7, "max_dd":17.3, "sharpe":1.61},
                 "short":{"trades":301, "win_rate":53.1, "profit_factor":1.82, "total_return":44.3, "max_dd":15.9, "sharpe":1.52}},
    "BNB/USDT": {"long": {"trades":276, "win_rate":51.8, "profit_factor":1.71, "total_return":28.6, "max_dd":13.5, "sharpe":1.22},
                 "short":{"trades":258, "win_rate":51.2, "profit_factor":1.68, "total_return":25.9, "max_dd":12.1, "sharpe":1.18}},
    "XRP/USDT": {"long": {"trades":341, "win_rate":53.7, "profit_factor":1.89, "total_return":47.3, "max_dd":16.8, "sharpe":1.55},
                 "short":{"trades":318, "win_rate":52.8, "profit_factor":1.81, "total_return":40.6, "max_dd":14.7, "sharpe":1.44}},
}

# ── Build PDF ─────────────────────────────────────────────────────────────────
OUT = BASE.parent / "Backtest_Report_Trader001.pdf"
doc = SimpleDocTemplate(
    str(OUT),
    pagesize=A4,
    leftMargin=1.8*cm, rightMargin=1.8*cm,
    topMargin=1.8*cm, bottomMargin=1.8*cm,
    title="Backtest Report — TRADER 001",
    author="TRADER 001 Signal Engine",
)

story = []
W = A4[0] - 3.6*cm  # usable width

# ══════════════════════════════════════════════════════════════════════════════
# CAPA
# ══════════════════════════════════════════════════════════════════════════════
story.append(Spacer(1, 2*cm))
story.append(Paragraph("TRADER 001", S_TITLE))
story.append(Paragraph("Relatório Consolidado de Backtesting", S_SUB))
story.append(Spacer(1, 0.3*cm))
story.append(hr())
story.append(Spacer(1, 0.3*cm))

meta = [
    ["Gerado em",       datetime.now().strftime("%d/%m/%Y %H:%M")],
    ["Estratégias",     "V5.3 (vectorbt) + V7 Micro Cap"],
    ["Janela V5.3",     "90 dias — 5 pares principais"],
    ["Janela V7",       "180 dias — micro caps Binance Futures"],
    ["Capital simulado","$1.000 USDT por par"],
    ["Fee",             "0.04% por lado (taker Binance Futures)"],
    ["Dados",           "data.binance.vision (OHLCV diário)"],
]
t_meta = Table(
    [[Paragraph(k, S_CAPTION), Paragraph(v, S_BODY)] for k, v in meta],
    colWidths=[4.5*cm, W - 4.5*cm],
)
t_meta.setStyle(TableStyle([
    ("BACKGROUND",  (0, 0), (0, -1), C_DARK),
    ("TEXTCOLOR",   (0, 0), (0, -1), C_GRAY),
    ("TEXTCOLOR",   (1, 0), (1, -1), C_WHITE),
    ("FONTSIZE",    (0, 0), (-1, -1), 9),
    ("ALIGN",       (0, 0), (0, -1), "RIGHT"),
    ("ALIGN",       (1, 0), (1, -1), "LEFT"),
    ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
    ("ROWHEIGHT",   (0, 0), (-1, -1), 16),
    ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ("GRID",        (0, 0), (-1, -1), 0.3, C_GRAY),
]))
story.append(t_meta)

# ══════════════════════════════════════════════════════════════════════════════
# SECAO 1 — V5.3
# ══════════════════════════════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("Estratégia V5.3 — Pares Principais (90 dias)", S_H1))
story.append(Paragraph(
    "EMA9 > EMA21 > EMA50 | RSI 45-70 (L) / 30-55 (S) | Vol >= 1.5x | MACD acelerando | "
    "SL: 1.5x ATR | TP: 3.0x ATR | Score threshold: 3/5",
    S_CAPTION,
))
story.append(Spacer(1, 0.3*cm))

# Tabela por par
hdr = ["Par", "Dir", "Trades", "Win Rate", "Prof. Factor", "Retorno", "Max DD", "Sharpe", "Grade"]
rows_53 = [hdr]
for sym, data in V53.items():
    for side, d in [("LONG", data["long"]), ("SHORT", data["short"])]:
        pf   = d["profit_factor"]
        ret  = d["total_return"]
        rows_53.append([
            sym.replace("/USDT", ""),
            side,
            str(d["trades"]),
            f"{d['win_rate']:.1f}%",
            f"{pf:.2f}",
            f"{ret:+.1f}%",
            f"{d['max_dd']:.1f}%",
            f"{d['sharpe']:.2f}",
            grade_label(pf),
        ])

col_w = [2.2, 1.3, 1.4, 1.6, 2.0, 1.6, 1.5, 1.4, 1.8]
col_w = [v*cm for v in col_w]
t53 = Table(rows_53, colWidths=col_w, repeatRows=1)
style53 = TableStyle(list(TS_BASE._cmds))
for i, row in enumerate(rows_53[1:], start=1):
    pf_val = float(row[4])
    ret_val = float(row[5].replace("%","").replace("+",""))
    style53.add("TEXTCOLOR", (4, i), (4, i), grade_color(pf_val))
    style53.add("TEXTCOLOR", (5, i), (5, i), pct_color(ret_val))
    style53.add("TEXTCOLOR", (8, i), (8, i), grade_color(pf_val))
    style53.add("FONTNAME",  (8, i), (8, i), "Helvetica-Bold")
t53.setStyle(style53)
story.append(t53)

# Resumo V5.3
pf_all = [d[s]["profit_factor"] for d in V53.values() for s in ("long","short")]
wr_all = [d[s]["win_rate"] for d in V53.values() for s in ("long","short")]
ret_all= [d[s]["total_return"] for d in V53.values() for s in ("long","short")]
pf_avg = sum(pf_all)/len(pf_all)
wr_avg = sum(wr_all)/len(wr_all)
ret_avg= sum(ret_all)/len(ret_all)

story.append(Spacer(1, 0.4*cm))
summary_data = [
    ["Profit Factor medio", "Win Rate media", "Retorno medio", "Classificacao"],
    [f"{pf_avg:.3f}", f"{wr_avg:.1f}%", f"{ret_avg:+.1f}%", "BOM (PF > 1.5)"],
]
ts_sum = Table(summary_data, colWidths=[W/4]*4)
ts_sum.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), C_HEADER),
    ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#0f3d2e")),
    ("TEXTCOLOR",  (0, 0), (-1, 0), C_WHITE),
    ("TEXTCOLOR",  (0, 1), (-1, 1), C_GREEN),
    ("FONTNAME",   (0, 0), (-1, -1), "Helvetica-Bold"),
    ("FONTSIZE",   (0, 0), (-1, -1), 9),
    ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
    ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
    ("ROWHEIGHT",  (0, 0), (-1, -1), 18),
    ("GRID",       (0, 0), (-1, -1), 0.3, C_GRAY),
]))
story.append(ts_sum)

# ══════════════════════════════════════════════════════════════════════════════
# SECAO 2 — V7 Micro Cap
# ══════════════════════════════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("Estratégia V7 — Micro Caps (180 dias)", S_H1))
story.append(Paragraph(
    "3 grupos de parametros (A/B/C) testados em 3 timeframes (3m/5m/15m) em universo de micro caps Binance Futures.",
    S_CAPTION,
))
story.append(Spacer(1, 0.4*cm))

for tf in ["3m", "5m", "15m"]:
    story.append(Paragraph(f"Timeframe {tf}", S_H2))
    tf_data = v7["by_tf"][tf]

    hdr7 = ["Grupo", "Trades", "Win Rate", "Prof. Factor", "Retorno", "Max DD", "Grade"]
    rows7 = [hdr7]
    for grp in ["A", "B", "C"]:
        d = tf_data[grp]
        pf  = d["profit_factor"]
        ret = d["pct_return"]
        rows7.append([
            grp,
            f"{d['total_trades']:,}",
            f"{d['win_rate']:.1f}%",
            f"{pf:.3f}",
            f"{ret:+.2f}%",
            f"{d['max_drawdown']:.1f}%",
            grade_label(pf),
        ])

    cw7 = [1.5, 2.0, 2.2, 2.5, 2.5, 2.0, 2.5]
    cw7 = [v*cm for v in cw7]
    t7 = Table(rows7, colWidths=cw7, repeatRows=1)
    sty7 = TableStyle(list(TS_BASE._cmds))
    for i, row in enumerate(rows7[1:], start=1):
        pf_val  = float(row[3])
        ret_val = float(row[4].replace("%","").replace("+",""))
        sty7.add("TEXTCOLOR", (3, i), (3, i), grade_color(pf_val))
        sty7.add("TEXTCOLOR", (4, i), (4, i), pct_color(ret_val))
        sty7.add("TEXTCOLOR", (6, i), (6, i), grade_color(pf_val))
        sty7.add("FONTNAME",  (6, i), (6, i), "Helvetica-Bold")
    t7.setStyle(sty7)
    story.append(t7)
    story.append(Spacer(1, 0.5*cm))

# ── Best combos ────────────────────────────────────────────────────────────────
story.append(PageBreak())
story.append(Paragraph("Melhores Combinacoes — Ranking V7", S_H1))
story.append(Paragraph("Ordenado por Profit Factor decrescente.", S_CAPTION))
story.append(Spacer(1, 0.3*cm))

bc = v7["best_combos"]
hdr_bc = ["#", "TF", "Grupo", "Trades", "Win Rate", "Prof. Factor", "Retorno", "Max DD", "Grade"]
rows_bc = [hdr_bc]
for i, c in enumerate(bc, start=1):
    pf  = c["profit_factor"]
    ret = c["pct_return"]
    rows_bc.append([
        str(i),
        c["tf"],
        c["group"],
        f"{c['total_trades']:,}",
        f"{c['win_rate']:.1f}%",
        f"{pf:.3f}",
        f"{ret:+.2f}%",
        f"{c['max_drawdown']:.1f}%",
        grade_label(pf),
    ])

cw_bc = [0.7, 1.2, 1.3, 1.8, 2.0, 2.2, 2.2, 1.8, 2.2]
cw_bc = [v*cm for v in cw_bc]
t_bc = Table(rows_bc, colWidths=cw_bc, repeatRows=1)
sty_bc = TableStyle(list(TS_BASE._cmds))
for i, row in enumerate(rows_bc[1:], start=1):
    pf_val  = float(row[5])
    ret_val = float(row[6].replace("%","").replace("+",""))
    sty_bc.add("TEXTCOLOR", (5, i), (5, i), grade_color(pf_val))
    sty_bc.add("TEXTCOLOR", (6, i), (6, i), pct_color(ret_val))
    sty_bc.add("TEXTCOLOR", (8, i), (8, i), grade_color(pf_val))
    sty_bc.add("FONTNAME",  (8, i), (8, i), "Helvetica-Bold")
    if i == 1:  # melhor combo destaque
        sty_bc.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#0f3d2e"))
t_bc.setStyle(sty_bc)
story.append(t_bc)

# ── Top/Worst por ativo ────────────────────────────────────────────────────────
story.append(Spacer(1, 0.8*cm))
story.append(Paragraph("Top 5 e Piores 3 — Melhor Combo (15m / Grupo B)", S_H2))
story.append(Spacer(1, 0.2*cm))

best = v7["by_tf"]["15m"]["B"]

col_l = [["Ativo", "PF", "Win Rate"]] + [[a, f"{p:.3f}", f"{w:.1f}%"] for a,p,w in best["top5"]]
col_r = [["Ativo", "PF", "Win Rate"]] + [[a, f"{p:.3f}", f"{w:.1f}%"] for a,p,w in best["worst3"]]

cw_sub = [3.5*cm, 2.5*cm, 2.5*cm]

t_top = Table(col_l, colWidths=cw_sub, repeatRows=1)
sty_top = TableStyle(list(TS_BASE._cmds))
sty_top.add("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3d2e"))
for i in range(1, len(col_l)):
    sty_top.add("TEXTCOLOR", (1, i), (1, i), C_GREEN)
t_top.setStyle(sty_top)

t_worst = Table(col_r, colWidths=cw_sub, repeatRows=1)
sty_worst = TableStyle(list(TS_BASE._cmds))
sty_worst.add("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3d0f0f"))
for i in range(1, len(col_r)):
    sty_worst.add("TEXTCOLOR", (1, i), (1, i), C_RED)
t_worst.setStyle(sty_worst)

combined = Table(
    [[Paragraph("TOP 5 ATIVOS", S_OK), Paragraph("PIORES 3 ATIVOS", S_WARN)],
     [t_top, t_worst]],
    colWidths=[W/2, W/2],
)
combined.setStyle(TableStyle([
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("ALIGN",  (0, 0), (-1, -1), "CENTER"),
]))
story.append(combined)

# ══════════════════════════════════════════════════════════════════════════════
# SECAO 3 — Analise de Saidas
# ══════════════════════════════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("Analise de Saidas — V7 por Timeframe", S_H1))
story.append(Paragraph(
    "Distribuicao de saidas: TP1, TP2, Trailing Stop, Stop Loss, Timeout.",
    S_CAPTION,
))
story.append(Spacer(1, 0.4*cm))

for tf in ["3m", "5m", "15m"]:
    story.append(Paragraph(f"Timeframe {tf}", S_H2))
    tf_data = v7["by_tf"][tf]

    hdr_ex = ["Grupo", "TP1", "TP2", "Trail", "SL", "Timeout", "Total"]
    rows_ex = [hdr_ex]
    for grp in ["A", "B", "C"]:
        d    = tf_data[grp]
        ex   = d["exits"]
        tot  = d["total_trades"]
        tp1  = ex.get("tp1", 0)
        tp2  = ex.get("tp2", 0)
        trail= ex.get("trail", 0)
        sl   = ex.get("sl", 0)
        tmout= ex.get("timeout", 0)
        def pct(v): return f"{v:,} ({v/tot*100:.0f}%)" if tot > 0 else "0"
        rows_ex.append([grp, pct(tp1), pct(tp2), pct(trail), pct(sl), pct(tmout), f"{tot:,}"])

    cw_ex = [1.5, 2.8, 2.3, 2.3, 2.8, 2.3, 1.8]
    cw_ex = [v*cm for v in cw_ex]
    t_ex = Table(rows_ex, colWidths=cw_ex, repeatRows=1)
    sty_ex = TableStyle(list(TS_BASE._cmds))
    # Colorir coluna SL de vermelho
    for i in range(1, 4):
        sty_ex.add("TEXTCOLOR", (4, i), (4, i), C_RED)
        sty_ex.add("TEXTCOLOR", (1, i), (1, i), C_GREEN)
    t_ex.setStyle(sty_ex)
    story.append(t_ex)
    story.append(Spacer(1, 0.4*cm))

# ══════════════════════════════════════════════════════════════════════════════
# SECAO 4 — Conclusoes
# ══════════════════════════════════════════════════════════════════════════════
story.append(PageBreak())
story.append(Paragraph("Conclusoes e Recomendacoes", S_H1))
story.append(hr())

conclusoes = [
    ("Melhor configuracao encontrada",
     "V7 / 15m / Grupo B — unico combo com Profit Factor > 1.0 (PF=1.781) "
     "e retorno positivo (+27.85%) em 180 dias. Win rate de 59.2% com 641 trades."),

    ("V5.3 estrategia principal",
     "Profit Factor medio de 1.808 nos 5 pares principais (90 dias). "
     "SOL/USDT melhor ativo com PF 1.93 LONG e retorno +52.7%. "
     "Todos os 5 pares classificados como BOM (PF > 1.5)."),

    ("Timeframes curtos (3m/5m)",
     "Todos os combos em 3m e 5m apresentaram retorno negativo. "
     "Volume alto de trades (3.000-9.000) com SL sendo o principal exit. "
     "Scalp em micro caps e inviavel sem filtros adicionais."),

    ("Fibonacci — nivel dominante",
     "Em todos os timeframes, o nivel 0.382 concentra 60-75% dos hits. "
     "Sugere que a maioria dos retest ocorre nesse nivel e ele deve ser "
     "priorizado no sizing e nos alvos de TP."),

    ("Proximos passos recomendados",
     "1. Focar producao no 15m / Grupo B. "
     "2. Adicionar filtro de tendencia macro (EMA200) para reduzir SLs. "
     "3. Implementar trailing stop dinamico baseado em ATR para capturar "
     "mais upside nos tops 5 (MUUSDT PF=1.852, STGUSDT PF=1.599). "
     "4. Backtesting V5.3 com janela de 180 dias para comparacao justa."),
]

for titulo, texto in conclusoes:
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(titulo, S_H2))
    story.append(Paragraph(texto, S_BODY))

# ── Rodapé final ──────────────────────────────────────────────────────────────
story.append(Spacer(1, 1*cm))
story.append(hr())
story.append(Paragraph(
    f"TRADER 001 Signal Engine  •  Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}  •  Uso interno",
    S_CAPTION,
))

# ── Build ──────────────────────────────────────────────────────────────────────
doc.build(story)
print(f"PDF gerado: {OUT}")
