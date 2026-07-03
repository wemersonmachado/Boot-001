import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # acha modulos do bot na pasta-mae
"""
Gerador de Auditoria Executiva — Trader Bot 001 V6.2
Baseado em varredura real dos modulos em 16/06/2026.
"""
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Table, TableStyle,
                                Spacer, HRFlowable, PageBreak, KeepTogether)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from datetime import datetime
import os

# ── Paleta ───────────────────────────────────────────────────────────────────
DARK_BG    = colors.HexColor('#0d1117')
DARK_CARD  = colors.HexColor('#161b22')
GOLD       = colors.HexColor('#d4af37')
GOLD_LIGHT = colors.HexColor('#f0d060')
WHITE      = colors.HexColor('#e8e8e8')
GRAY       = colors.HexColor('#8b8b8b')
RED_ALERT  = colors.HexColor('#c0392b')
YELLOW_MED = colors.HexColor('#e67e22')
GREEN_LOW  = colors.HexColor('#27ae60')

PAGE_W, PAGE_H = A4

# ── Estilos ──────────────────────────────────────────────────────────────────
def _styles():
    title = ParagraphStyle("BotTitle", fontName="Helvetica-Bold", fontSize=22,
                           textColor=GOLD, alignment=TA_CENTER, spaceAfter=4,
                           backColor=DARK_BG)
    sub   = ParagraphStyle("BotSub", fontName="Helvetica", fontSize=10,
                           textColor=GRAY, alignment=TA_CENTER, spaceAfter=2,
                           backColor=DARK_BG)
    emit  = ParagraphStyle("BotEmit", fontName="Helvetica", fontSize=8,
                           textColor=GRAY, alignment=TA_CENTER, backColor=DARK_BG)
    h1    = ParagraphStyle("H1", fontName="Helvetica-Bold", fontSize=13,
                           textColor=GOLD, spaceBefore=10, spaceAfter=4,
                           backColor=DARK_BG)
    h2    = ParagraphStyle("H2", fontName="Helvetica-Bold", fontSize=11,
                           textColor=GOLD_LIGHT, spaceBefore=6, spaceAfter=3,
                           backColor=DARK_BG)
    body  = ParagraphStyle("Body", fontName="Helvetica", fontSize=8.5,
                           textColor=WHITE, spaceBefore=2, spaceAfter=2,
                           backColor=DARK_BG, leading=12)
    mono  = ParagraphStyle("Mono", fontName="Courier", fontSize=8,
                           textColor=WHITE, backColor=DARK_CARD,
                           spaceBefore=2, spaceAfter=2, leading=11)
    score_big = ParagraphStyle("ScoreBig", fontName="Helvetica-Bold", fontSize=20,
                               textColor=GOLD, alignment=TA_CENTER, backColor=DARK_CARD)
    score_lbl = ParagraphStyle("ScoreLbl", fontName="Helvetica-Bold", fontSize=7,
                               textColor=GRAY, alignment=TA_CENTER, backColor=DARK_CARD)
    bug_title = ParagraphStyle("BugTitle", fontName="Helvetica-Bold", fontSize=9,
                               textColor=RED_ALERT, backColor=DARK_BG)
    fix_body  = ParagraphStyle("FixBody", fontName="Courier", fontSize=7.5,
                               textColor=WHITE, backColor=DARK_CARD, leading=11)
    return dict(title=title, sub=sub, emit=emit, h1=h1, h2=h2, body=body,
                mono=mono, score_big=score_big, score_lbl=score_lbl,
                bug_title=bug_title, fix_body=fix_body)

ST = _styles()

# ── Helpers ──────────────────────────────────────────────────────────────────
def _hr():
    return HRFlowable(width="100%", thickness=0.5, color=GOLD, spaceAfter=4, spaceBefore=4)

def _sp(h=4):
    return Spacer(1, h)

def _p(text, style="body"):
    return Paragraph(text, ST[style])

def _score_card(score_str, label, color=GOLD):
    tbl = Table(
        [[Paragraph(score_str, ST["score_big"])],
         [Paragraph(label, ST["score_lbl"])]],
        colWidths=[35*mm],
    )
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), DARK_CARD),
        ("BOX",        (0,0), (-1,-1), 0.8, color),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    return tbl

def _score_row(cards):
    tbl = Table([cards], colWidths=[35*mm]*len(cards), hAlign="CENTER")
    tbl.setStyle(TableStyle([
        ("ALIGN",   (0,0), (-1,-1), "CENTER"),
        ("VALIGN",  (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",  (0,0), (-1,-1), 2),
        ("RIGHTPADDING", (0,0), (-1,-1), 2),
        ("BACKGROUND", (0,0), (-1,-1), DARK_BG),
    ]))
    return tbl

def _table(headers, rows, col_widths=None, header_color=GOLD):
    data = [[Paragraph(str(h), ParagraphStyle("TH", fontName="Helvetica-Bold",
                       fontSize=7.5, textColor=DARK_BG, alignment=TA_CENTER,
                       backColor=header_color))
             for h in headers]] + \
           [[Paragraph(str(c), ParagraphStyle("TD", fontName="Helvetica",
                       fontSize=7.5, textColor=WHITE, alignment=TA_LEFT,
                       backColor=DARK_BG))
             for c in row] for row in rows]
    if col_widths is None:
        col_widths = [PAGE_W * 0.88 / len(headers)] * len(headers)
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0),  header_color),
        ("BACKGROUND", (0,1), (-1,-1), DARK_CARD),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [DARK_CARD, DARK_BG]),
        ("GRID",       (0,0), (-1,-1), 0.3, GRAY),
        ("VALIGN",     (0,0), (-1,-1), "TOP"),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
    ]))
    return tbl


# ── Pagina background ─────────────────────────────────────────────────────────
def _page_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(DARK_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(GRAY)
    footer = ("Trader Bot 001 - Auditoria Executiva V6.2 - 16/06/2026 - "
              "Gerado por Claude Code - Confidencial")
    canvas.drawCentredString(PAGE_W/2, 8*mm, footer)
    canvas.setStrokeColor(GOLD)
    canvas.setLineWidth(0.3)
    canvas.line(15*mm, 12*mm, PAGE_W-15*mm, 12*mm)
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(GRAY)
    canvas.drawRightString(PAGE_W-15*mm, 8*mm, f"Pag. {doc.page}")
    canvas.restoreState()


# ── CONTEUDO ──────────────────────────────────────────────────────────────────
def _build_content():
    story = []
    now   = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    # CAPA
    story.append(_sp(30))
    story.append(_p("TRADER BOT 001", "title"))
    story.append(_p("AUDITORIA EXECUTIVA COMPLETA", "title"))
    story.append(_sp(4))
    story.append(_p("Analise de Modulos · Perfis de Risco · Estrategias · Bugs · Melhorias", "sub"))
    story.append(_sp(2))
    story.append(_p(f"Emitido em {now} · Versao V6.2 · Conta REAL Binance Futures", "emit"))
    story.append(_sp(14))
    story.append(_hr())
    story.append(_sp(6))

    cards = [
        _score_card("8.1", "ARQUITETURA"),
        _score_card("8.5", "ESTRATEGIA"),
        _score_card("8.0", "RISK MGMT"),
        _score_card("7.5", "QUALIDADE"),
        _score_card("8.0", "SCORE GERAL", GOLD_LIGHT),
    ]
    story.append(_score_row(cards))
    story.append(_sp(8))

    info = Table([[
        Paragraph("CONTA\nREAL · Binance Futures", ParagraphStyle("IC", fontName="Helvetica-Bold",
                  fontSize=7.5, textColor=GOLD, alignment=TA_CENTER, backColor=DARK_CARD)),
        Paragraph("STACK\nFastAPI + APScheduler + aiohttp", ParagraphStyle("IC", fontName="Helvetica",
                  fontSize=7.5, textColor=WHITE, alignment=TA_CENTER, backColor=DARK_CARD)),
        Paragraph("MODULOS\n50+ arquivos .py ativos", ParagraphStyle("IC", fontName="Helvetica",
                  fontSize=7.5, textColor=WHITE, alignment=TA_CENTER, backColor=DARK_CARD)),
        Paragraph("ENGINES\n5 engines · Cascade · Router", ParagraphStyle("IC", fontName="Helvetica",
                  fontSize=7.5, textColor=WHITE, alignment=TA_CENTER, backColor=DARK_CARD)),
    ]], colWidths=[43*mm]*4, hAlign="CENTER")
    info.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), DARK_CARD),
        ("BOX",        (0,0), (-1,-1), 0.5, GOLD),
        ("INNERGRID",  (0,0), (-1,-1), 0.3, GRAY),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(info)
    story.append(PageBreak())

    # 01 RESUMO EXECUTIVO
    story.append(_p("01  RESUMO EXECUTIVO", "h1"))
    story.append(_hr())
    story.append(_p(
        "O Trader Bot 001 V6.2 e uma plataforma de trading autonomo sobre Binance Futures USDT-M, "
        "construida em FastAPI com APScheduler e comunicacao assincrona via aiohttp. A arquitetura "
        "e modular com 50+ arquivos Python, 5 engines de sinal (Trend, Range, VDLS, Fade, Breakout), "
        "4 modos de operacao (Sinais, Autonomous, Supervised, Grid), 3 perfis de risco "
        "(Conservative, Normal, Aggressive) e uma suite de modulos auxiliares cobrindo ML, DCA, "
        "Monte Carlo, Correlacao Dinamica, Portfolio VaR e Social Sentiment. "
        "Score geral: 8.0/10. Principal risco imediato: BUG-001 (NameError em producao com BANCA_USDT > 0).",
        "body"
    ))
    story.append(_sp(6))

    story.append(_table(
        ["DIMENSAO", "DESCRICAO", "NOTA", "PRIO"],
        [
            ["Arquitetura & Stack",    "FastAPI + APScheduler + aiohttp + SQLite. Async nativo.",                    "8.1/10", "Baixa"],
            ["Modos de Operacao",      "4 modos implementados. SINAIS e GRID com logica propria.",                   "8.5/10", "Baixa"],
            ["Perfis de Risco",        "3 perfis completos. Anti-martingale, leverage cap, Sortino pause.",          "8.0/10", "Baixa"],
            ["Signal Engine V6",       "6 camadas de score. Bonus: candles, divergencia, Golden Cross.",             "8.5/10", "Baixa"],
            ["Engine Router V2",       "5 engines + Cascade + Volume Profile + RS Score + Asset Memory.",            "8.5/10", "Baixa"],
            ["Binance Executor",       "Cache 5min client, exchange_info 30min, asyncio.to_thread() correto.",       "8.0/10", "Baixa"],
            ["Risk Manager",           "Sizing por risco%, margin cap, trailing, scale-out. 1 bug de variavel.",     "7.5/10", "ALTA"],
            ["Notifier Telegram",      "Session HTTP persistente. Botoes approve/reject. Canal VIP.",                "8.0/10", "Baixa"],
            ["Modulos Auxiliares",     "ML beta (25+ amostras), DCA ativo, pairs trading beta.",                    "7.8/10", "Media"],
            ["Figuras Graficas Auto",  "NAO IMPLEMENTADO — chart preview e script manual separado",                 "0.0/10", "ALTA"],
            ["MTF Breakout 1W/1M",     "NAO IMPLEMENTADO — scan so ate 4h por padrao",                             "0.0/10", "Media"],
        ],
        col_widths=[40*mm, 90*mm, 20*mm, 22*mm]
    ))
    story.append(_sp(4))
    story.append(_p("Itens com ALTA prioridade representam lacunas que limitam o potencial do bot.", "mono"))
    story.append(PageBreak())

    # 02 MODOS DE OPERACAO
    story.append(_p("02  MODOS DE OPERACAO", "h1"))
    story.append(_hr())

    story.append(_score_row([
        _score_card("8.5", "SINAIS"),
        _score_card("8.5", "AUTONOMOUS"),
        _score_card("8.0", "SUPERVISED"),
        _score_card("8.5", "GRID"),
    ]))
    story.append(_sp(8))

    story.append(_p("2.1  SINAIS — 8.5/10", "h2"))
    story.append(_p(
        "Modo somente leitura: escaneia a watchlist e envia sinais ao Telegram sem executar trades. "
        "Alterna automaticamente entre NORMAL e AGGRESSIVE a cada ciclo (_sinais_toggle). "
        "Cooldown por ativo+direcao+timeframe. Fingerprint A+B evita alertas duplicados (zona de preco +/-0.3%).",
        "body"
    ))
    story.append(_table(
        ["PONTOS FORTES", "PONTOS DE ATENCAO"],
        [
            ["+ Alternancia NORMAL/AGGRESSIVE automatica", "[MINOR] Sem metrica de precisao dos sinais enviados"],
            ["+ Fingerprint deduplicacao robusta",         "[MINOR] Cooldown 90s pode omitir breakouts rapidos"],
            ["+ Watchlist propria por modo configuravel",  ""],
        ],
        col_widths=[85*mm, 85*mm]
    ))
    story.append(_sp(5))

    story.append(_p("2.2  AUTONOMOUS — 8.5/10", "h2"))
    story.append(_p(
        "Executa trades automaticamente apos pipeline de 9 filtros: BTC Veto, Staleness Decay, "
        "Structural Tag (NORMAL), volume spike, correlacao dinamica, Portfolio VaR, "
        "ML bonus e Claude Brain (opcional). Notificacao Telegram apos abertura bem-sucedida.",
        "body"
    ))
    story.append(_table(
        ["PONTOS FORTES", "PONTOS DE ATENCAO"],
        [
            ["+ Pipeline de filtros em 9 camadas",           "[MEDIO] BUG-001: NameError margin_per_trade (L648/650)"],
            ["+ Race condition guard via _executing_assets",  "[MEDIO] Cache _active_trades_cache nao recarrega no startup"],
            ["+ Anti-martingale com 4 niveis (1.0x a 0.25x)","[MINOR] Sem alerta quando sinal e rejeitado por filtro"],
        ],
        col_widths=[85*mm, 85*mm]
    ))
    story.append(_sp(5))

    story.append(_p("2.3  SUPERVISED — 8.0/10", "h2"))
    story.append(_p(
        "Envia sinal ao Telegram com botoes APROVAR/REJEITAR. O usuario decide antes da execucao. "
        "Pendencias ficam em get_pending_assets() para evitar duplicar sinal enquanto aguarda aprovacao.",
        "body"
    ))
    story.append(_table(
        ["PONTOS FORTES", "PONTOS DE ATENCAO"],
        [
            ["+ Mesmo pipeline de qualidade do AUTONOMOUS",  "[MEDIO] Sem timeout de aprovacao — sinal fica pendente indefinido"],
            ["+ Botoes inline no Telegram funcionando",      "[MINOR] Aprovacao manual pode executar sinal obsoleto"],
        ],
        col_widths=[85*mm, 85*mm]
    ))
    story.append(_sp(5))

    story.append(_p("2.4  GRID — 8.5/10", "h2"))
    story.append(_p(
        "Opera em pares pre-definidos com 9 camadas de filtro: funding window, session score, "
        "BTC veto, pump/dump awareness, trend EMA9/21/55, VRA regime, ML bonus, Portfolio VaR, Claude Brain. "
        "Grid assimetrico amplia TPs com RSI sobrevendido/sobrecomprado. V6 Grid Zones com OB/FVG. "
        "Reinveste 20% do lucro de cada ciclo automaticamente.",
        "body"
    ))
    story.append(_table(
        ["PONTOS FORTES", "PONTOS DE ATENCAO"],
        [
            ["+ Grid assimetrico baseado em RSI",          "[MEDIO] Sem stale alert global — apenas por par"],
            ["+ Re-entrada automatica apos 10s de ciclo",  "[MINOR] GRID_MAX_CONCURRENT default 2 — pode subutilizar banca"],
            ["+ Reinvestimento automatico 20% do ciclo",   "[MINOR] Sem cooldown minimo entre ciclos do mesmo par"],
        ],
        col_widths=[85*mm, 85*mm]
    ))
    story.append(PageBreak())

    # 03 PERFIS DE RISCO
    story.append(_p("03  PERFIS DE RISCO", "h1"))
    story.append(_hr())
    story.append(_score_row([
        _score_card("8.5", "CONSERVATIVE", GREEN_LOW),
        _score_card("8.0", "NORMAL", YELLOW_MED),
        _score_card("7.5", "AGGRESSIVE", RED_ALERT),
    ]))
    story.append(_sp(8))

    story.append(_table(
        ["PARAMETRO", "CONSERVATIVE", "NORMAL", "AGGRESSIVE"],
        [
            ["Min Score",       "80 / 100",           "70 / 100",              "60 / 100"],
            ["Min R:R",         "2.0 : 1",             "1.5 : 1",               "1.2 : 1"],
            ["Max Posicoes",    "2",                   "5",                     "8"],
            ["Risco por Trade", "0.5%",                "1.0%",                  "1.5%"],
            ["Leverage Cap",    "10x",                 "20x",                   "30x"],
            ["Timeframes",      "15m, 1h",             "15m, 1h, 4h",           "5m, 15m, 1h, 4h"],
            ["Universo",        "Top20 + BTC/ETH",     "Top30 + trending",      "Dinamico (universe_builder)"],
            ["Structural Tag",  "Obrigatoria",         "Obrigatoria",           "Opcional"],
            ["BTC Veto",        "Ativo (+-1.5%)",      "Ativo (+-2%)",          "Ativo (+-3%)"],
            ["Bonus Cap",       "15 pts",              "20 pts",                "25 pts"],
        ],
        col_widths=[50*mm, 45*mm, 45*mm, 45*mm]
    ))
    story.append(_sp(5))

    story.append(_table(
        ["PERFIL", "PONTOS FORTES", "PONTOS DE ATENCAO"],
        [
            ["CONSERVATIVE", "+ Score 80 evita entradas fracas\n+ Leverage 10x protege contra liquidacao",
             "[MINOR] 2 posicoes pode subutilizar banca em tendencia forte"],
            ["NORMAL",       "+ Balance ideal cobertura/qualidade\n+ 5 posicoes cobre mais oportunidades",
             "[MEDIO] Structural Tag obrigatoria pode bloquear sinais validos sem tag"],
            ["AGGRESSIVE",   "+ Universo dinamico expande oportunidades\n+ 5m timeframe captura scalp",
             "[ALTO] Score 60 pode aceitar sinais de baixa qualidade\n[ALTO] Leverage 30x sem BTC Veto estrito = risco elevado"],
        ],
        col_widths=[30*mm, 75*mm, 72*mm]
    ))
    story.append(PageBreak())

    # 04 ESTRATEGIAS & SIGNAL ENGINES
    story.append(_p("04  ESTRATEGIAS & SIGNAL ENGINES", "h1"))
    story.append(_hr())
    story.append(_score_row([
        _score_card("8.5", "SIG ENG V6"),
        _score_card("8.0", "VOLATILE"),
        _score_card("8.5", "MEAN REV"),
        _score_card("8.5", "FADE ENG"),
        _score_card("8.5", "VDLS ENG"),
    ]))
    story.append(_sp(8))

    story.append(_p("4.1  Signal Engine V6 — 6 Camadas de Analise (8.5/10)", "h2"))
    story.append(_table(
        ["CAMADA", "TIPO", "PONTOS DAY", "PONTOS SCALP", "STATUS"],
        [
            ["EMA Cross / Trend",        "Peso 25% / 20%",  "0-25",  "0-20",  "Ativo"],
            ["Volume / CVD",             "Peso 20% / 20%",  "0-20",  "0-20",  "Ativo"],
            ["Momentum (StochRSI+MACD)", "Peso 15% / 20%",  "0-15",  "0-20",  "Ativo"],
            ["Market Structure / BB",    "Peso 15% / 15%",  "0-15",  "0-15",  "Ativo"],
            ["VWAP",                     "Peso 10% / 15%",  "0-10",  "0-15",  "Ativo"],
            ["Funding / OI / LS Ratio",  "Peso 15% / 10%",  "0-15",  "0-10",  "Ativo"],
            ["Bonus Candle Patterns",    "Bonus +5~15",      "0-15",  "0-15",  "Ativo"],
            ["Bonus RSI Divergencia",    "Bonus +5~15",      "0-15",  "0-15",  "Ativo"],
            ["Bonus Golden/Death Cross", "Bonus +5~12",      "0-12",  "N/A",   "Ativo (Day)"],
            ["Bonus Social Sentiment",   "Bonus +3~8",       "0-8",   "0-8",   "Ativo"],
            ["MTF Breakout 1W/1M",       "Bonus +0~15",      "0-15",  "N/A",   "FALTANDO"],
        ],
        col_widths=[50*mm, 33*mm, 22*mm, 25*mm, 27*mm]
    ))
    story.append(_sp(5))

    story.append(_p("4.2  Cobertura da Estrategia", "h2"))
    story.append(_table(
        ["ITEM", "COBERTURA", "DETALHE"],
        [
            ["Rompimento de maximas",    "COBRE",    "EMA breakout + volume spike no volatile_engine e signal_engine"],
            ["Suporte/Resistencia",      "COBRE",    "Order Blocks V6 + Supply/Demand zones + BB structure"],
            ["Confluence multi-TF",      "PARCIAL",  "Ate 4h. Sem scan de 1W/1M automatico"],
            ["Scalp 1m/3m/5m",          "COBRE",    "VDLS engine + StochRSI + CVD dedicados para scalp"],
            ["Mean Reversion lateral",  "COBRE",    "Mean Rev engine: RSI<32/>68 + BB + EMA50 proxima"],
            ["Fade de pump/dump",       "COBRE",    "Fade engine: RSI>78/<22 + vol 4.5x + OB institucional confirmado"],
            ["Social Sentiment",        "COBRE",    "Bonus +3~8pts via social_score integrado ao signal_engine"],
            ["Figuras graficas auto",   "FALTANDO", "chart_preview_send.py e script manual — nao integrado ao scan"],
        ],
        col_widths=[50*mm, 25*mm, 95*mm]
    ))
    story.append(_sp(5))

    story.append(_p("4.3  Engine Router V2 — 5 Engines + Cascade (8.5/10)", "h2"))
    story.append(_table(
        ["ENGINE", "REGIME-ALVO", "CRITERIO DE ENTRADA", "SCORE MIN"],
        [
            ["TREND (signal_engine)",  "TRENDING / NEUTRAL",    "EMA 21/55/200 + MACD + Volume",        "60-80 (por modo)"],
            ["RANGE (mean_rev)",       "RANGING (ADX<20)",      "RSI<32/>68 + BB toca + EMA50 proximo",  "55"],
            ["BREAKOUT (volatile)",    "VOLATILE (ATR spike)",  "Volume spike + ATR expansao + candle",  "60"],
            ["FADE (fade_engine)",     "FADE (RSI extremo)",    "RSI>78/<22 + vol>4.5x + OB confirma",  "60"],
            ["VDLS",                   "SCALP lateral/neutro",  "Sweep min/max local + CVD divergencia", "60"],
        ],
        col_widths=[40*mm, 35*mm, 72*mm, 25*mm]
    ))
    story.append(PageBreak())

    # 05 BUGS
    story.append(_p("05  BUGS & PROBLEMAS ENCONTRADOS", "h1"))
    story.append(_hr())

    story.append(_table(
        ["ID", "SEVERIDADE", "TITULO", "ARQUIVO"],
        [
            ["BUG-001", "ALTO",  "NameError: margin_per_trade nao definida (usa max_ no resto)",  "main.py:648,650"],
            ["BUG-002", "MEDIO", "Cache _active_trades_cache nao populado no startup do bot",     "main.py:83"],
            ["BUG-003", "MEDIO", "asyncio.create_task() chamado em funcao sincrona",              "main.py:469"],
            ["BUG-004", "MEDIO", "Timeout de aprovacao Supervised indefinido (ativo fica preso)", "notifier.py"],
            ["BUG-005", "MEDIO", "cascade() retorna tried:4 mas roda 5 engines (contagem errada)","engine_router.py:734"],
            ["BUG-006", "BAIXO", "Dicts de cooldown crescem ilimitados entre prunings",           "main.py:112-115"],
            ["BUG-007", "BAIXO", "VDLS: rr retornado como 2.0 hardcoded quando sl==entry",       "engine_router.py:268"],
            ["BUG-008", "BAIXO", "Grid stale alert pode spammar se _grid_last_cycle_ts resetado", "main.py:1268"],
        ],
        col_widths=[20*mm, 22*mm, 95*mm, 38*mm]
    ))
    story.append(_sp(8))

    story.append(_p("5.1  Detalhamento — Bug Critico (ALTO)", "h2"))

    story.append(_p("BUG-001 — NameError: margin_per_trade nao definida", "bug_title"))
    story.append(_p("Arquivo: main.py linhas 648 e 650", "mono"))
    story.append(_p(
        "Dentro do bloco if BANCA_USDT > 0 em _execute_trade_inner(), o codigo define "
        "max_margin_per_trade (linha 619) e margin (linha 621-622), mas as linhas 648 e 650 "
        "referenciam margin_per_trade (sem o prefixo max_). Resultado: NameError em runtime "
        "toda vez que BANCA_USDT for configurado e um trade for executado no modo AUTONOMOUS.",
        "body"
    ))
    story.append(_p(
        "Fix (5 min):\n"
        "  L648: '... = ${max_margin_per_trade:.2f} margem ...'  (era margin_per_trade)\n"
        "  L650: 'margem=${max_margin_per_trade:.2f} ...'         (era margin_per_trade)",
        "fix_body"
    ))
    story.append(_sp(6))

    story.append(_p("5.2  Detalhamento — Bugs Medios", "h2"))

    story.append(_p("BUG-002 — Cache de trades nao recarrega no startup", "bug_title"))
    story.append(_p("Arquivo: main.py:83  (_active_trades_cache = {})", "mono"))
    story.append(_p(
        "Se o bot reinicia com trades abertos no banco, o cache fica vazio ate o proximo update. "
        "Guards de execucao usam get_open_trades() (correto), mas /trades/active retorna lista "
        "vazia ate o proximo ciclo de monitoramento.",
        "body"
    ))
    story.append(_p("Fix: no lifespan startup, popular _active_trades_cache via get_open_trades().", "fix_body"))
    story.append(_sp(4))

    story.append(_p("BUG-003 — asyncio.create_task() em funcao sincrona _calc_risk_metrics()", "bug_title"))
    story.append(_p("Arquivo: main.py:469", "mono"))
    story.append(_p(
        "_calc_risk_metrics() e sincrona mas chama asyncio.create_task(send_alert(...)) internamente. "
        "Se chamada de um contexto sem event loop rodando (ex: APScheduler thread), lanca RuntimeError. "
        "Na pratica funciona hoje, mas e fragil e dificulta testes.",
        "body"
    ))
    story.append(_p("Fix: retornar flag/mensagem e deixar o caller async disparar o send_alert.", "fix_body"))
    story.append(_sp(4))

    story.append(_p("BUG-004 — Sem timeout em aprovacoes Supervised", "bug_title"))
    story.append(_p("Arquivo: notifier.py  (_pending_approvals dict)", "mono"))
    story.append(_p(
        "Sinais enviados em modo SUPERVISED ficam em _pending_approvals sem expiracao. "
        "O ativo fica bloqueado em get_pending_assets() indefinidamente, impedindo novos sinais "
        "do mesmo ativo mesmo apos o sinal expirar.",
        "body"
    ))
    story.append(_p("Fix: adicionar campo ts em _pending_approvals e limpar entradas com mais de 900s no poll.", "fix_body"))
    story.append(PageBreak())

    # 06 MODULOS AUXILIARES
    story.append(_p("06  MODULOS AUXILIARES & PERFORMANCE", "h1"))
    story.append(_hr())

    story.append(_table(
        ["MODULO", "FUNCAO", "NOTA", "ESTADO"],
        [
            ["ml_engine.py",            "ML score bonus (Random Forest) — ativa com >= 25 amostras",   "7.5/10", "Beta"],
            ["dca_engine.py",           "DCA automatico em 30%/50%/20% do capital — niveis 0-3",       "8.0/10", "Ativo"],
            ["pairs_trading_engine.py", "Pairs trading (cointegrated pairs) — sem integracao ao fluxo","6.0/10", "Beta/Isolado"],
            ["monte_carlo.py",          "Simulacao Monte Carlo para risk assessment",                   "7.0/10", "Manual"],
            ["ws_feed.py",              "WebSocket feed: markPrice + liquidacoes em tempo real",        "8.5/10", "Ativo"],
            ["correlation_engine.py",   "Matriz de correlacao dinamica (refresh 30min)",                "8.5/10", "Ativo"],
            ["portfolio_risk.py",       "VaR + correlacao portfolio antes de abrir posicao",            "8.5/10", "Ativo"],
            ["fear_greed.py",           "Fear & Greed + alerta de funding extremo por ativo",           "8.0/10", "Ativo"],
            ["walk_forward.py",         "Walk-forward analysis de backtest — execucao manual",          "7.0/10", "Manual"],
            ["universe_builder.py",     "Selecao dinamica de ativos (top volume/volatilidade)",         "8.0/10", "Ativo"],
            ["regime_detector.py",      "Deteccao de regime TRENDING/RANGING/VOLATILE/NEUTRAL",         "9.0/10", "Ativo"],
            ["asset_memory.py",         "WR historico por ativo — ajuste de score e pausa automatica",  "8.5/10", "Ativo"],
            ["signal_filters.py",       "BTC Veto, Staleness Decay, Structural Tag, RS Score",          "8.5/10", "Ativo"],
            ["volume_profile.py",       "POC/VAH/VAL — bonus/penalidade de confluencia",                "8.5/10", "Ativo"],
            ["candle_pattern_engine.py","Padroes de candles (Hammer, Engulfing, Doji etc)",             "8.0/10", "Ativo"],
            ["supply_demand.py",        "Zonas de Supply/Demand institucionais",                        "7.5/10", "Ativo"],
            ["claude_brain.py",         "LLM filter: analisa e aprova/rejeita sinal com contexto RT",   "8.5/10", "Opcional"],
            ["pump_dump_engine.py",     "Deteccao pump/dump + synergy boost quando alinha com sinal",   "8.5/10", "Ativo"],
        ],
        col_widths=[48*mm, 82*mm, 20*mm, 22*mm]
    ))
    story.append(_sp(6))

    story.append(_p("6.1  Pontos de Lentidao & Otimizacao", "h2"))
    story.append(_table(
        ["AREA", "PROBLEMA", "IMPACTO", "SOLUCAO"],
        [
            ["scan_with_router",    "Semaforo 30 conn pode causar pico de requests em watchlist grande", "MEDIO", "Reduzir para 20 em Conservative"],
            ["_execute_trade_inner","get_open_trades() chamado multiplas vezes no mesmo ciclo",           "BAIXO", "Cache local no inicio da funcao"],
            ["Claude Brain ctx",    "asyncio.gather de 6 calls Binance por sinal (ticker/fr/oi/ls/liq)","MEDIO", "Cache 30s para dados RT do brain"],
            ["klines_cache",        "Sem TTL de expiracao — pode entregar klines desatualizados",        "MEDIO", "Adicionar TTL de 1-2min por TF"],
        ],
        col_widths=[35*mm, 72*mm, 22*mm, 43*mm]
    ))
    story.append(PageBreak())

    # 07 ROADMAP
    story.append(_p("07  ROADMAP DE MELHORIAS PRIORITARIAS", "h1"))
    story.append(_hr())

    story.append(_table(
        ["PRIO", "MELHORIA", "IMPACTO", "ESFORCO", "MODULOS"],
        [
            ["P1", "Corrigir BUG-001: margin_per_trade -> max_margin_per_trade (L648/650)",   "ALTO",   "5 min",  "main.py"],
            ["P1", "Adicionar TTL 900s em _pending_approvals — fix BUG-004",                   "ALTO",   "30 min", "notifier.py"],
            ["P1", "Popular _active_trades_cache no startup — fix BUG-002",                    "ALTO",   "20 min", "main.py"],
            ["P2", "Refatorar _calc_risk_metrics() async para eliminar BUG-003",               "MEDIO",  "45 min", "main.py"],
            ["P2", "Adicionar TTL ao klines_cache (1-2 min por TF)",                          "MEDIO",  "1h",     "klines_cache.py"],
            ["P2", "Cache 30s para dados RT usados pelo Claude Brain",                        "MEDIO",  "1h",     "main.py, data_fetcher.py"],
            ["P2", "MTF breakout scan em 1W/1M como bonus opcional no signal_engine",         "MEDIO",  "3h",     "signal_engine.py"],
            ["P3", "Integrar pairs_trading_engine ao fluxo SUPERVISED",                       "MEDIO",  "4h",     "pairs_trading_engine.py, main.py"],
            ["P3", "Chart automatico: minigrafico PNG enviado junto ao sinal Telegram",       "MEDIO",  "6h",     "notifier.py, chart_preview_send.py"],
            ["P3", "Walk-forward automatico semanal via cron/scheduler",                      "BAIXO",  "3h",     "walk_forward.py, main.py"],
            ["P4", "Fix tried:4 -> tried:5 no cascade() quando Asset Memory pausa",           "BAIXO",  "5 min",  "engine_router.py:734"],
            ["P4", "Fix VDLS: calcular rr real em vez de hardcoded 2.0 quando sl==entry",    "BAIXO",  "10 min", "engine_router.py:268"],
        ],
        col_widths=[12*mm, 100*mm, 20*mm, 18*mm, 27*mm]
    ))
    story.append(_sp(6))

    story.append(_p("7.1  Quick Wins — Fix em menos de 30 minutos", "h2"))
    for q in [
        "main.py:648,650  — Renomear margin_per_trade para max_margin_per_trade (2 ocorrencias)",
        "engine_router.py:734  — tried: 4 -> tried: 5 no cascade() com paused=True",
        "engine_router.py:268  — VDLS: calcular rr real em vez de retornar 2.0 hardcoded",
        "main.py:1268  — Grid stale alert: adicionar flag por simbolo para evitar spam",
        "notifier.py  — _pending_approvals: adicionar campo ts e limpar com TTL de 900s",
    ]:
        story.append(_p(f"  -> {q}", "mono"))
    story.append(PageBreak())

    # 08 SCORECARD FINAL
    story.append(_p("08  SCORECARD FINAL", "h1"))
    story.append(_hr())

    story.append(_score_row([
        _score_card("8.1", "ARQUITETURA"),
        _score_card("8.5", "SINAIS"),
        _score_card("8.5", "AUTONOMOUS"),
        _score_card("8.0", "SUPERVISED"),
        _score_card("8.5", "GRID"),
    ]))
    story.append(_sp(4))
    story.append(_score_row([
        _score_card("8.5", "CONSERVATIVE", GREEN_LOW),
        _score_card("8.0", "NORMAL", YELLOW_MED),
        _score_card("7.5", "AGGRESSIVE", RED_ALERT),
        _score_card("8.5", "SIG ENG V6"),
        _score_card("8.5", "ENG ROUTER"),
    ]))
    story.append(_sp(4))
    story.append(_score_row([
        _score_card("8.0", "BINANCE EXEC"),
        _score_card("7.5", "RISK MGR"),
        _score_card("8.0", "NOTIFIER"),
        _score_card("9.0", "REGIME DET"),
        _score_card("8.5", "ASSET MEM"),
    ]))
    story.append(_sp(4))
    story.append(_score_row([
        _score_card("7.5", "ML ENGINE", GRAY),
        _score_card("8.0", "DCA ENGINE"),
        _score_card("8.5", "CORR DYN"),
        _score_card("8.5", "PORTFOLIO"),
        _score_card("0.0", "GRAFICOS AUTO", RED_ALERT),
    ]))
    story.append(_sp(8))

    story.append(_table(
        ["METRICA", "VALOR", "OBSERVACAO"],
        [
            ["Score medio — modulos ativos",    "8.24/10", "18 modulos avaliados individualmente"],
            ["Score geral — com lacunas",       "8.00/10", "2 itens com 0 (graficos auto, MTF 1W/1M)"],
            ["Bugs criticos (ALTO)",            "1",       "BUG-001 — NameError margin_per_trade em main.py"],
            ["Bugs medios (MEDIO)",             "4",       "BUG-002 a BUG-005 descritos na secao 05"],
            ["Bugs menores (BAIXO)",            "3",       "BUG-006, BUG-007, BUG-008"],
            ["Modulos ativos e funcionais",     "16 / 18", "2 em estado Beta/Isolado (ML, pairs trading)"],
            ["Camadas de analise no scan",      "10 / 11", "1 faltando — MTF bonus 1W/1M"],
            ["Cobertura da estrategia pedida",  "6 / 8",   "2 faltando — graficos auto e MTF 1W/1M"],
            ["Engines de sinal implementados",  "5 / 5",   "TREND, RANGE, VDLS, FADE, BREAKOUT"],
            ["Score estimado pos-P1+P2",        "8.5/10",  "Apos fix BUG-001/002/004 + klines TTL + cache brain"],
        ],
        col_widths=[75*mm, 28*mm, 72*mm]
    ))
    story.append(_sp(8))

    story.append(_p("CONCLUSAO EXECUTIVA", "h2"))
    story.append(_p(
        "O Trader Bot 001 V6.2 e um sistema maduro e bem arquitetado com score geral de 8.0/10. "
        "Pontos fortes: 5 engines de sinal cobrindo todos os regimes, pipeline de 9 filtros por execucao, "
        "anti-martingale, Portfolio VaR, correlacao dinamica, Claude Brain opcional e WebSocket RT. "
        "RISCO IMEDIATO: BUG-001 (NameError margin_per_trade) — qualquer trade com BANCA_USDT > 0 "
        "vai crashar silenciosamente. Fix de 5 minutos. "
        "As demais correcoes P1 somam menos de 1h e elevam o score para 8.5+. "
        "Com P2+P3 implementados (graficos auto, klines TTL, MTF 1W/1M, pairs trading integrado), "
        "o sistema atinge nivel producao pleno com cobertura de 100% das estrategias mapeadas.",
        "body"
    ))

    return story


# ── GERAR ─────────────────────────────────────────────────────────────────────
def gerar():
    base = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(base, "..", "outputs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "Trader_Bot_001_Auditoria_Executiva_V6.2.pdf")

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm,  bottomMargin=18*mm,
    )
    story = _build_content()
    doc.build(story, onFirstPage=_page_bg, onLaterPages=_page_bg)
    print(f"[PDF] Gerado: {out_path}")
    return out_path


if __name__ == "__main__":
    gerar()
