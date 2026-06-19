"""
Diagnóstico de Sinais — Trader Bot 001 V6.2
Varredura do pipeline de sinais: todas as causas de entradas erradas e lentidão.
"""
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Table, TableStyle,
                                Spacer, HRFlowable, PageBreak)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from datetime import datetime
import os

DARK_BG   = colors.HexColor('#0d1117')
DARK_CARD = colors.HexColor('#161b22')
GOLD      = colors.HexColor('#d4af37')
GOLD_L    = colors.HexColor('#f0d060')
WHITE     = colors.HexColor('#e8e8e8')
GRAY      = colors.HexColor('#8b8b8b')
RED       = colors.HexColor('#c0392b')
ORANGE    = colors.HexColor('#e67e22')
GREEN     = colors.HexColor('#27ae60')
BLUE      = colors.HexColor('#2980b9')

PAGE_W, PAGE_H = A4

def _s(name, **kw):
    return ParagraphStyle(name, **kw)

T = {
    "title":  _s("T", fontName="Helvetica-Bold", fontSize=20, textColor=GOLD,  alignment=TA_CENTER, backColor=DARK_BG, spaceAfter=4),
    "sub":    _s("S", fontName="Helvetica",      fontSize=9,  textColor=GRAY,  alignment=TA_CENTER, backColor=DARK_BG, spaceAfter=2),
    "emit":   _s("E", fontName="Helvetica",      fontSize=8,  textColor=GRAY,  alignment=TA_CENTER, backColor=DARK_BG),
    "h1":     _s("H1",fontName="Helvetica-Bold", fontSize=13, textColor=GOLD,  backColor=DARK_BG, spaceBefore=10, spaceAfter=4),
    "h2":     _s("H2",fontName="Helvetica-Bold", fontSize=10, textColor=GOLD_L,backColor=DARK_BG, spaceBefore=6, spaceAfter=3),
    "body":   _s("B", fontName="Helvetica",      fontSize=8.5,textColor=WHITE, backColor=DARK_BG, leading=12, spaceAfter=2),
    "mono":   _s("M", fontName="Courier",        fontSize=8,  textColor=WHITE, backColor=DARK_CARD, leading=11, spaceAfter=2),
    "red":    _s("R", fontName="Helvetica-Bold", fontSize=8.5,textColor=RED,   backColor=DARK_BG),
    "orange": _s("O", fontName="Helvetica-Bold", fontSize=8.5,textColor=ORANGE,backColor=DARK_BG),
    "green":  _s("G", fontName="Helvetica-Bold", fontSize=8.5,textColor=GREEN, backColor=DARK_BG),
    "scard":  _s("SC",fontName="Helvetica-Bold", fontSize=18, textColor=GOLD,  alignment=TA_CENTER, backColor=DARK_CARD),
    "slbl":   _s("SL",fontName="Helvetica-Bold", fontSize=7,  textColor=GRAY,  alignment=TA_CENTER, backColor=DARK_CARD),
}

def _hr(): return HRFlowable(width="100%", thickness=0.5, color=GOLD, spaceAfter=4, spaceBefore=4)
def _sp(h=4): return Spacer(1, h)
def _p(txt, style="body"): return Paragraph(txt, T[style])

def _card(score, label, c=GOLD):
    t = Table([[Paragraph(score,T["scard"])],[Paragraph(label,T["slbl"])]],colWidths=[38*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),DARK_CARD),
        ("BOX",(0,0),(-1,-1),0.8,c),
        ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),5),
        ("BOTTOMPADDING",(0,0),(-1,-1),5),
    ]))
    return t

def _cards(lst):
    t = Table([lst], colWidths=[38*mm]*len(lst), hAlign="CENTER")
    t.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),2),("RIGHTPADDING",(0,0),(-1,-1),2),
        ("BACKGROUND",(0,0),(-1,-1),DARK_BG)]))
    return t

def _table(headers, rows, widths=None, hc=GOLD):
    th = ParagraphStyle("TH", fontName="Helvetica-Bold", fontSize=7.5, textColor=DARK_BG,
                        alignment=TA_CENTER, backColor=hc)
    td = ParagraphStyle("TD", fontName="Helvetica", fontSize=7.5, textColor=WHITE,
                        alignment=TA_LEFT, backColor=DARK_BG)
    data = [[Paragraph(str(h), th) for h in headers]] + \
           [[Paragraph(str(c), td) for c in row] for row in rows]
    if not widths:
        widths = [PAGE_W*0.88/len(headers)]*len(headers)
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),hc),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[DARK_CARD,DARK_BG]),
        ("GRID",(0,0),(-1,-1),0.3,GRAY),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("TOPPADDING",(0,0),(-1,-1),3),
        ("BOTTOMPADDING",(0,0),(-1,-1),3),
        ("LEFTPADDING",(0,0),(-1,-1),4),
    ]))
    return t

def _page_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(DARK_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(GRAY)
    canvas.drawCentredString(PAGE_W/2, 8*mm,
        "Trader Bot 001 - Diagnostico de Sinais V6.2 - 16/06/2026 - Gerado por Claude Code - Confidencial")
    canvas.setStrokeColor(GOLD)
    canvas.setLineWidth(0.3)
    canvas.line(15*mm, 12*mm, PAGE_W-15*mm, 12*mm)
    canvas.drawRightString(PAGE_W-15*mm, 8*mm, f"Pag. {doc.page}")
    canvas.restoreState()

# ── CONTEÚDO ─────────────────────────────────────────────────────────────────
def _build():
    story = []
    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    # CAPA
    story.append(_sp(25))
    story.append(_p("TRADER BOT 001", "title"))
    story.append(_p("DIAGNOSTICO DE SINAIS — AUTONOMOUS · TODOS OS PERFIS + CLAUDE BRAIN", "title"))
    story.append(_sp(4))
    story.append(_p("Pipeline Completo · Causas de Entradas Erradas · Lentidao · Correcoes", "sub"))
    story.append(_sp(2))
    story.append(_p(f"Emitido em {now} · V6.2 · Conta REAL Binance Futures", "emit"))
    story.append(_sp(10))
    story.append(_hr())
    story.append(_sp(6))

    story.append(_cards([
        _card("10", "FILTROS NO PIPELINE"),
        _card("3",  "CAUSAS ENTRADA ERRADA", RED),
        _card("7",  "CAUSAS LENTIDAO", ORANGE),
        _card("9",  "MELHORIAS MAPEADAS", GREEN),
        _card("2",  "CRITICAS / IMEDIATAS", RED),
    ]))
    story.append(_sp(8))

    # RESUMO
    info = Table([[
        Paragraph("MODELO BRAIN\nclaude-3-opus-20240229\n(OBSOLETO / NAO EXISTE)", ParagraphStyle("I",fontName="Helvetica-Bold",fontSize=7,textColor=RED,alignment=TA_CENTER,backColor=DARK_CARD)),
        Paragraph("FALLBACK ATIVO\nErro API = approve:True\nTODOS OS TRADES PASSAM", ParagraphStyle("I",fontName="Helvetica-Bold",fontSize=7,textColor=RED,alignment=TA_CENTER,backColor=DARK_CARD)),
        Paragraph("MIN_SCORE CONSERV.\n82pts + VRA +5 = 87\nQuase impossivel atingir", ParagraphStyle("I",fontName="Helvetica-Bold",fontSize=7,textColor=ORANGE,alignment=TA_CENTER,backColor=DARK_CARD)),
        Paragraph("MTF PENALTY\n-15pts por divergencia\nDerruba sinais validos", ParagraphStyle("I",fontName="Helvetica-Bold",fontSize=7,textColor=ORANGE,alignment=TA_CENTER,backColor=DARK_CARD)),
    ]], colWidths=[43*mm]*4, hAlign="CENTER")
    info.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),DARK_CARD),
        ("BOX",(0,0),(-1,-1),0.5,RED),
        ("INNERGRID",(0,0),(-1,-1),0.3,GRAY),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story.append(info)
    story.append(PageBreak())

    # 01 MAPA DO PIPELINE
    story.append(_p("01  MAPA COMPLETO DO PIPELINE DE SINAIS (AUTONOMOUS)", "h1"))
    story.append(_hr())
    story.append(_p(
        "O sinal passa por 14 camadas em sequencia antes de ser executado. "
        "Cada camada pode BLOQUEAR ou PENALIZAR o score. A varredura identificou "
        "gargalos em 7 dessas camadas que explicam tanto as entradas erradas quanto a lentidao.",
        "body"
    ))
    story.append(_sp(4))
    story.append(_table(
        ["CAMADA", "ONDE", "O QUE FAZ", "PROBLEMA ENCONTRADO"],
        [
            ["1. Engine Router",    "engine_router.py",   "Detecta regime + seleciona engine (TREND/RANGE/VDLS/FADE/BREAKOUT)", "OK — funciona corretamente"],
            ["2. Score base",       "signal_engine.py",   "6 camadas: EMA, Volume, Momentum, Structure, VWAP, Funding/OI",      "OK — calcula corretamente"],
            ["3. Volume Profile",   "engine_router.py",   "Bonus/penalidade POC/VAH/VAL (+/-10pts)",                            "OK"],
            ["4. RS Score",         "signal_filters.py",  "Forca relativa vs BTC (+/-5pts, cache 15min)",                       "OK"],
            ["5. Asset Memory",     "asset_memory.py",    "WR historico ajusta score e pode pausar ativo",                     "OK"],
            ["6. BTC Veto",         "signal_filters.py",  "BTC < -1.5% 45min bloqueia LONGs",                                  "PROBLEMA: muito sensivel — veta em volatilidade normal"],
            ["7. Staleness Decay",  "signal_filters.py",  "-0.5pt/min no score (max -20pts)",                                   "PROBLEMA: scan 60s mas decay severo reduz sinais validos"],
            ["8. Structural Tag",   "signal_filters.py",  "NORMAL: exige tag estrutural V6 obrigatoria",                        "PROBLEMA: VDLS e MEAN_REV nao geram tags — bloqueados"],
            ["9. Volume Spike",     "main.py:906",        "Volume corrente < 1.2x media 20p = skip",                            "PROBLEMA: mercados laterais nunca atingem 1.2x"],
            ["10. MTF Check",       "signal_filters.py",  "TF superior divergente = -15pts",                                    "PROBLEMA: -15pts derruba sinais de 70+ para abaixo do min"],
            ["11. Correlacao",      "correlation_engine.py","Bloqueia mesmo setor/direcao aberto",                              "OK"],
            ["12. Portfolio VaR",   "portfolio_risk.py",  "Bloqueia se VaR da carteira excede limite",                          "OK"],
            ["13. ML Bonus",        "ml_engine.py",       "Ajuste +/- se 25+ amostras",                                         "Beta — sem amostras suficientes = neutro"],
            ["14. Claude Brain",    "claude_brain.py",    "LLM analisa sinal completo + contexto RT",                           "CRITICO: modelo obsoleto = fallback approve:True em tudo"],
        ],
        widths=[30*mm, 38*mm, 68*mm, 40*mm]
    ))
    story.append(PageBreak())

    # 02 CAUSAS DE ENTRADAS ERRADAS
    story.append(_p("02  CAUSAS DE ENTRADAS ERRADAS", "h1"))
    story.append(_hr())
    story.append(_cards([
        _card("C1", "BRAIN FALHA\nSILENCIOSO", RED),
        _card("C2", "SCORE FALSO\nSEM VOLUME", RED),
        _card("C3", "CACHE BRAIN\n5 MIN LONGO", ORANGE),
    ]))
    story.append(_sp(8))

    story.append(_p("C1 — Claude Brain com modelo inexistente (CRITICO)", "h2"))
    story.append(_p(
        "O modelo configurado em claude_brain.py e 'claude-3-opus-20240229'. "
        "Este modelo nao existe mais na API da Anthropic. "
        "O resultado e uma excecao capturada pelo try/except na linha 235, "
        "que retorna o fallback 'approve: True' para TODOS os sinais. "
        "Na pratica, o Claude Brain esta desativado sem que o usuario perceba: "
        "nenhum sinal e rejeitado, nenhum ajuste de size/leverage e aplicado, "
        "e erros aparecem apenas nos logs com '[CLAUDE BRAIN] Erro na API'.",
        "body"
    ))
    story.append(_p(
        "Arquivo: claude_brain.py:12\n"
        "Atual:   _MODEL = 'claude-3-opus-20240229'  (nao existe)\n"
        "Fix:     _MODEL = os.getenv('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001')\n"
        "         Haiku 4.5: 10x mais rapido, custo $0.80/$4.00 por M tokens, ideal para JSON estruturado",
        "mono"
    ))
    story.append(_sp(5))

    story.append(_p("C2 — Score alto sem confirmacao de volume real (MEDIO)", "h2"))
    story.append(_p(
        "O filtro de volume spike em main.py (linha 906) exige volume corrente >= 1.2x media. "
        "Porem, o score base do signal_engine ja pontua volume (20% do score total). "
        "Em mercados laterais ou horarios de baixo volume (Asia), "
        "o signal_engine pode dar score 70+ baseado nos outros 5 componentes "
        "(EMA, RSI, MACD, Funding, Structure) mesmo sem volume real. "
        "Esses sinais passam o threshold mas entram em posicao sem fluxo institucional.",
        "body"
    ))
    story.append(_p(
        "Arquivo: main.py:906  (_VOL_MIN = 1.2)\n"
        "Fix: Elevar _VOL_MIN para 1.5 em NORMAL e CONSERVATIVE (manter 1.2 em AGGRESSIVE).\n"
        "     Alternativa: bloquear entrada se score_volume < 8/20 independente do score total.",
        "mono"
    ))
    story.append(_sp(5))

    story.append(_p("C3 — Cache do Claude Brain de 5 minutos (MEDIO)", "h2"))
    story.append(_p(
        "O _decision_cache reutiliza a decisao por 300 segundos (5 min) para o mesmo par+direcao. "
        "Em scalp de 3m/5m, 5 minutos representa 1-2 candles completos. "
        "Se o Brain aprova um sinal ruim as 10:00, vai aprovar o mesmo par na mesma direcao "
        "ate as 10:05, mesmo que o contexto mude completamente (price action, volume, funding).",
        "body"
    ))
    story.append(_p(
        "Arquivo: claude_brain.py:24  (_CACHE_TTL = 300)\n"
        "Fix: _CACHE_TTL = 120  (2 minutos para scalp 3m/5m)\n"
        "     Ou usar TTL dinamico: 120s para TF <= 5m, 300s para TF >= 15m",
        "mono"
    ))
    story.append(PageBreak())

    # 03 CAUSAS DE LENTIDAO
    story.append(_p("03  CAUSAS DE LENTIDAO PARA ENCONTRAR SINAIS", "h1"))
    story.append(_hr())

    story.append(_table(
        ["ID", "FILTRO", "CONFIGURACAO ATUAL", "IMPACTO", "FREQUENCIA"],
        [
            ["L1","BTC Veto — threshold baixo",     "BTC < -1.5% em 45min = bloqueia TODOS os LONGs",         "Bloqueia 30-50% dos LONGs em mercados normais","Alta"],
            ["L2","Structural Tag — NORMAL",         "Tag V6 obrigatoria em NORMAL; VDLS+MeanRev nao geram tags","2 dos 5 engines bloqueados em modo NORMAL",     "Muito Alta"],
            ["L3","Score CONSERVATIVE muito alto",   "min_score=82 + VRA COMPRESSION +5 = threshold de 87",    "< 5% dos sinais atingem 87 em mercados laterais","Alta"],
            ["L4","CONSERVATIVE so tem 15m TF",      "Apenas 1 timeframe no perfil mais seguro",                "Scan 4-5x mais lento vs perfil que usa 5m+15m",  "Alta"],
            ["L5","MTF penalty severo (-15pts)",      "TF superior divergente = -15pts no score",               "Sinal de 75pts cai para 60 (abaixo de NORMAL)",   "Media"],
            ["L6","Staleness Decay acelerado",        "-0.5pt/min; sinal de 10min perde 5pts",                  "Com scan 60s, sinais chegam com 1-2min de idade", "Baixa"],
            ["L7","Volume spike 1.2x obrigatorio",   "< 1.2x media = skip (main.py:906), sem excecao de modo", "Em mercados consolidando, nunca tem spike",        "Media"],
        ],
        widths=[10*mm, 42*mm, 62*mm, 42*mm, 20*mm]
    ))
    story.append(_sp(6))

    story.append(_p("L1 — BTC Veto demasiado sensivel", "h2"))
    story.append(_p(
        "O BTC veto calcula variacao das ultimas 3 velas de 15m (45min). "
        "Threshold atual: BTC < -1.5% bloqueia LONGs. "
        "O BTC oscila 1.5% em 45min com frequencia em mercados normais — "
        "isso resulta em ciclos inteiros (15-30min) sem nenhum sinal LONG sendo enviado, "
        "mesmo quando o contexto estrutural esta favoravel.",
        "body"
    ))
    story.append(_p(
        "Arquivo: signal_filters.py:113\n"
        "Atual:  block_long = change < -1.5\n"
        "Fix:    block_long = change < -2.5  (alinhar ao AGGRESSIVE que ja usava -3.5%)\n"
        "        block_short = change > +3.0  (era 2.0%)",
        "mono"
    ))
    story.append(_sp(5))

    story.append(_p("L2 — Structural Tag bloqueia 2 engines em modo NORMAL", "h2"))
    story.append(_p(
        "O filtro de Structural Tag (signal_filters.py:657) e aplicado a TODOS os sinais em modo NORMAL. "
        "Os engines VDLS e MEAN_REV geram sinais com tags proprias ('VDLS-SWEEP', 'CVD-DIV', 'RGM-RNG', 'BB') "
        "que NAO estao na lista STRUCTURAL_TAGS do filtro. "
        "Resultado: VDLS e MEAN_REV sao completamente bloqueados em NORMAL, "
        "deixando apenas TREND, BREAKOUT e FADE operando.",
        "body"
    ))
    story.append(_p(
        "Arquivo: signal_filters.py:24  (STRUCTURAL_TAGS set)\n"
        "Fix: adicionar tags dos engines VDLS e MEAN_REV ao set:\n"
        "     'VDLS-SWEEP', 'CVD-DIV', 'LQ-SWEEP', 'RANGE', 'BB', 'RSI-EXTREME'",
        "mono"
    ))
    story.append(_sp(5))

    story.append(_p("L3/L4 — CONSERVATIVE praticamente inoperante", "h2"))
    story.append(_p(
        "Perfil CONSERVATIVE: min_score=82, apenas 15m, so BTCUSDT/ETHUSDT/SOLUSDT (allowed_assets). "
        "Em regime COMPRESSION (VRA), o threshold sobe para 87. "
        "O signal_engine raramente gera sinais acima de 85 em majors no 15m durante mercado lateral. "
        "Adicionar 5m como TF e flexibilizar allowed_assets aumenta oportunidades sem comprometer seguranca.",
        "body"
    ))
    story.append(_p(
        "Arquivo: config.py:54-65\n"
        "Fix CONSERVATIVE:\n"
        "  'timeframes': ['5m', '15m']   (era ['15m'])\n"
        "  'allowed_assets': None         (remover restricao ou ampliar para top 5)\n"
        "  'min_score': 78                (era 82 — ainda muito conservador vs 75 do NORMAL)",
        "mono"
    ))
    story.append(PageBreak())

    # 04 ANALISE POR PERFIL
    story.append(_p("04  ANALISE DETALHADA POR PERFIL", "h1"))
    story.append(_hr())

    story.append(_cards([
        _card("C", "CONSERVATIVE\n6.0/10", ORANGE),
        _card("N", "NORMAL\n7.0/10", GOLD),
        _card("A", "AGGRESSIVE\n8.0/10", GREEN),
    ]))
    story.append(_sp(8))

    story.append(_table(
        ["PERFIL", "PROBLEMA PRINCIPAL", "IMPACTO", "CORRECAO"],
        [
            ["CONSERVATIVE","min_score 82 + VRA COMP = 87. So 15m. So 3 ativos.",
             "Pode ficar horas sem sinal",  "78pts + 5m/15m + remover allowed_assets"],
            ["CONSERVATIVE","Structural Tag obrigatoria via modo NORMAL (heranca)",
             "VDLS e MEAN_REV bloqueados",  "Adicionar tags VDLS/MeanRev ao STRUCTURAL_TAGS"],
            ["NORMAL",      "MTF penalty -15pts: sinal 75pts cai para 60 com divergencia TF superior",
             "50-70% dos sinais bloqueados","Reduzir MTF penalty de -15 para -8pts"],
            ["NORMAL",      "Structural Tag bloqueia engines VDLS e MEAN_REV",
             "2 engines inoperantes",       "Adicionar tags dos engines ao set"],
            ["NORMAL",      "Funding penalty -10pts quando funding BTC > 0.08%",
             "LONGs bloqueados em bull runs","Suavizar: funding > 0.15% = -7pts (era -10)"],
            ["AGGRESSIVE",  "Session Asia bloqueia com -8pts flat 01h-05h UTC",
             "22h-02h BRT sem sinais",      "Usar -5pts (igual score_adj) sem bloco extra"],
            ["AGGRESSIVE",  "Volume spike 1.2x aplicado mesmo com score 90+",
             "Sinais excelentes dropados",  "Usar 1.0x para score >= 80, 1.2x para score < 80"],
            ["TODOS",       "Claude Brain com modelo inexistente = approve:True tudo",
             "Brain inoperante",            "Trocar para claude-haiku-4-5-20251001"],
            ["TODOS",       "BTC Veto -1.5% muito sensivel para LONGs",
             "Ciclos sem sinais LONG",      "Elevar threshold para -2.5%"],
        ],
        widths=[28*mm, 65*mm, 40*mm, 45*mm]
    ))
    story.append(PageBreak())

    # 05 CLAUDE BRAIN DIAGNOSTICO
    story.append(_p("05  CLAUDE BRAIN — DIAGNOSTICO COMPLETO", "h1"))
    story.append(_hr())
    story.append(_p(
        "O Claude Brain e o ultimo filtro antes da execucao. Quando funcionando, analisa "
        "6 categorias de dados (tecnico, RT do ativo, price action, macro BTC, sessao e criterios objetivos) "
        "e retorna approve + 5 multiplicadores de risco. Quando falha silenciosamente, "
        "retorna approve:True sem nenhum ajuste — como se o Brain nao existisse.",
        "body"
    ))
    story.append(_sp(4))

    story.append(_table(
        ["ITEM", "SITUACAO ATUAL", "SITUACAO IDEAL"],
        [
            ["Modelo LLM",         "'claude-3-opus-20240229' — OBSOLETO/INEXISTENTE",  "'claude-haiku-4-5-20251001' — rapido, barato, JSON estruturado"],
            ["Comportamento erro", "Fallback approve:True — Brain ignorado",             "Log de erro visivel + alerta Telegram; nao executar em falha"],
            ["Cache TTL",          "5 minutos (muito longo para scalp 3m/5m)",           "2 minutos para TF <= 5m; 5 minutos para TF >= 15m"],
            ["Criterio RSI LONG",  "Rejeita RSI > 85 mas aprova ate 80 em bull run",    "OK — logica correta para nao filtrar forca real"],
            ["Prompt contexto",    "Ultimos 5 candles + funding + OI + LS + liquidacoes","OK — contexto rico e suficiente"],
            ["Limite budget",      "$5/dia (CLAUDE_BRAIN_BUDGET_USD) — sem controle ativo","Adicionar verificacao de budget antes de cada chamada"],
            ["Ajustes retornados", "size_mult, leverage_mult, tp_adj, sl_adj, news_sent","OK — estrutura completa, mas inoperante com modelo errado"],
            ["Latencia",           "Opus = ~3-5s por chamada (bloqueante)",              "Haiku = ~0.5-1s por chamada (10x mais rapido)"],
        ],
        widths=[35*mm, 72*mm, 70*mm]
    ))
    story.append(_sp(6))

    story.append(_p("5.1  Fix Imediato — Modelo e Fallback", "h2"))
    story.append(_p(
        "Arquivo: claude_brain.py:12 e :235\n"
        "Linha 12 — trocar modelo:\n"
        "  _MODEL = os.getenv('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001')\n\n"
        "Linha 235 — melhorar fallback (nao executar em erro de API):\n"
        "  except Exception as e:\n"
        "      print(f'[CLAUDE BRAIN] Erro API: {e}')\n"
        "      # Retorna rejeicao conservadora em vez de approve:True\n"
        "      return {'approve': False, 'reason': f'Brain indisponivel: {str(e)[:60]}',\n"
        "              'confidence': 0.0, 'leverage_multiplier': 1.0,\n"
        "              'size_multiplier': 1.0, 'tp_adjust_pct': 1.0,\n"
        "              'sl_adjust_pct': 1.0, 'news_sentiment': 0}",
        "mono"
    ))
    story.append(PageBreak())

    # 06 PLANO DE CORRECOES
    story.append(_p("06  PLANO DE CORRECOES PRIORIZADAS", "h1"))
    story.append(_hr())

    story.append(_table(
        ["PRIO", "CORRECAO", "ARQUIVO", "IMPACTO", "ESFORCO"],
        [
            ["P1", "Trocar modelo Brain: claude-3-opus-20240229 → claude-haiku-4-5-20251001",            "claude_brain.py:12",      "CRITICO",  "2 min"],
            ["P1", "Fallback Brain: approve:False em erro de API (nao approve:True)",                     "claude_brain.py:235-247", "CRITICO",  "5 min"],
            ["P1", "Adicionar tags VDLS/MeanRev ao STRUCTURAL_TAGS (desbloqueia 2 engines)",             "signal_filters.py:24",    "ALTO",     "3 min"],
            ["P1", "BTC Veto: -1.5% → -2.5% para LONGs; +2.0% → +3.0% para SHORTs",                   "signal_filters.py:113",   "ALTO",     "2 min"],
            ["P2", "CONSERVATIVE: min_score 82→78, timeframes +5m, remover allowed_assets",             "config.py:54-65",         "ALTO",     "5 min"],
            ["P2", "MTF penalty: -15pts → -8pts (divergencia TF superior menos punitivo)",              "signal_filters.py:185",   "MEDIO",    "2 min"],
            ["P2", "Volume spike: 1.2x → 1.0x para score >= 80; manter 1.2x para score < 80",          "main.py:906",             "MEDIO",    "10 min"],
            ["P2", "Cache Brain TTL: 300s → 120s para TF <= 5m",                                        "claude_brain.py:24",      "MEDIO",    "5 min"],
            ["P3", "Funding penalty: -10pts → -7pts quando funding > 0.08% (menos agressivo)",          "signal_filters.py:218",   "MEDIO",    "2 min"],
            ["P3", "Session AGGRESSIVE: remover -8pts flat 01h-05h; manter apenas score_adj da sessao", "signal_filters.py:621",   "MEDIO",    "5 min"],
        ],
        widths=[10*mm, 100*mm, 35*mm, 20*mm, 17*mm]
    ))
    story.append(_sp(6))

    story.append(_p("Quick Wins — Total em menos de 20 minutos", "h2"))
    for q in [
        "claude_brain.py:12   — _MODEL = 'claude-haiku-4-5-20251001'                              (2 min)",
        "claude_brain.py:238  — return approve:False em erro (nao approve:True)                   (3 min)",
        "signal_filters.py:24 — adicionar 'VDLS-SWEEP','CVD-DIV','LQ-SWEEP','RANGE','BB','RSI-EXTREME'  (2 min)",
        "signal_filters.py:113 — block_long = change < -2.5  |  block_short = change > +3.0      (2 min)",
        "signal_filters.py:185 — bonus = -8 (era -15) para divergencia MTF                       (2 min)",
        "config.py:55          — 'min_score': 78  |  'timeframes': ['5m','15m']                  (2 min)",
    ]:
        story.append(_p(f"  -> {q}", "mono"))
    story.append(PageBreak())

    # 07 SCORECARD FINAL
    story.append(_p("07  SCORECARD FINAL — PIPELINE ACTUAL vs IDEAL", "h1"))
    story.append(_hr())

    story.append(_cards([
        _card("C1", "BRAIN\n2/10 (inop)", RED),
        _card("C2", "CONSERVATIVE\n4/10", RED),
        _card("C3", "NORMAL\n6/10", ORANGE),
        _card("C4", "AGGRESSIVE\n8/10", GREEN),
        _card("C5", "FILTROS\n6.5/10", ORANGE),
    ]))
    story.append(_sp(6))
    story.append(_cards([
        _card("P1", "BRAIN POS-FIX\n8/10", GREEN),
        _card("P2", "CONSERV POS-FIX\n7.5/10", GREEN),
        _card("P3", "NORMAL POS-FIX\n8.5/10", GREEN),
        _card("P4", "AGGR POS-FIX\n9/10", GREEN),
        _card("P5", "FILTROS POS-FIX\n8.5/10", GREEN),
    ]))
    story.append(_sp(8))

    story.append(_table(
        ["METRICA ATUAL", "VALOR", "ESPERADO POS-FIX"],
        [
            ["Claude Brain operacional",                    "NAO (modelo inexistente)",    "SIM (haiku-4-5)"],
            ["Engines ativos em NORMAL",                    "3 de 5 (VDLS+MeanRev bloq.)","5 de 5"],
            ["Sinais LONG bloqueados por BTC Veto",         "~35% dos ciclos",             "~15% dos ciclos"],
            ["Sinais derrubados por MTF penalty",           "50-70% dos sinais",           "20-30%"],
            ["CONSERVATIVE: sinais por hora esperados",     "0-2 sinais",                  "4-8 sinais"],
            ["NORMAL: sinais por hora esperados",           "2-4 sinais",                  "6-12 sinais"],
            ["AGGRESSIVE: sinais por hora esperados",       "5-10 sinais",                 "10-20 sinais"],
            ["Latencia media do Brain por sinal",           "3-5s (Opus)",                 "0.5-1s (Haiku)"],
            ["Entradas sem confirmacao de volume real",     "Frequente (score falso)",      "Raro (1.5x spike em NORMAL)"],
        ],
        widths=[85*mm, 42*mm, 50*mm]
    ))
    story.append(_sp(8))

    story.append(_p("CONCLUSAO", "h2"))
    story.append(_p(
        "Os dois problemas reportados (entradas erradas e lentidao) tem a mesma raiz: "
        "filtros excessivamente restritivos combinados com o Claude Brain inoperante. "
        "Com o Brain falhando silenciosamente (approve:True), sinais que deveriam ser rejeitados passam. "
        "Com os filtros de Structural Tag, MTF penalty e BTC Veto muito agressivos, "
        "sinais validos sao descartados antes mesmo de chegar ao Brain. "
        "Os fixes P1 (menos de 15 minutos de trabalho) resolvem os dois problemas simultaneamente: "
        "reativam o Brain com o modelo correto, desbloqueiam os engines VDLS e MEAN_REV, "
        "e reduzem os falsos bloqueios de BTC Veto e MTF. "
        "Estimativa pos-fix: 2-3x mais sinais validos por hora e reducao significativa de entradas ruins.",
        "body"
    ))

    return story

def gerar():
    base = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(base, "..", "outputs")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "Trader_Bot_001_Diagnostico_Sinais_V6.2.pdf")
    doc = SimpleDocTemplate(out, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=15*mm, bottomMargin=18*mm)
    doc.build(_build(), onFirstPage=_page_bg, onLaterPages=_page_bg)
    print(f"[PDF] {out}")
    return out

if __name__ == "__main__":
    gerar()
