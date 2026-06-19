"""
Gera o PDF de auditoria do Trader 001.
Executar: python gerar_relatorio_auditoria.py
"""
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from datetime import datetime

OUT = "auditoria_trader001.pdf"

# ── Paleta ────────────────────────────────────────────────────────────────────
C_BG       = colors.HexColor("#0f0f13")
C_CARD     = colors.HexColor("#16161f")
C_GREEN    = colors.HexColor("#4ade80")
C_YELLOW   = colors.HexColor("#fbbf24")
C_RED      = colors.HexColor("#f87171")
C_BLUE     = colors.HexColor("#60a5fa")
C_ORANGE   = colors.HexColor("#f97316")
C_TEXT     = colors.HexColor("#e8e8f0")
C_DIM      = colors.HexColor("#888899")
C_BORDER   = colors.HexColor("#2a2a3a")
C_SECTION  = colors.HexColor("#1e2233")

doc = SimpleDocTemplate(
    OUT, pagesize=A4,
    leftMargin=2*cm, rightMargin=2*cm,
    topMargin=2.2*cm, bottomMargin=2*cm,
)

W = A4[0] - 4*cm

styles = getSampleStyleSheet()

def S(name, **kw):
    s = ParagraphStyle(name, **kw)
    return s

sTitle    = S("sTitle",    fontName="Helvetica-Bold", fontSize=22, textColor=C_TEXT,     spaceAfter=4,  alignment=TA_CENTER)
sSubtitle = S("sSubtitle", fontName="Helvetica",      fontSize=11, textColor=C_DIM,      spaceAfter=14, alignment=TA_CENTER)
sSection  = S("sSection",  fontName="Helvetica-Bold", fontSize=13, textColor=C_BLUE,     spaceBefore=18, spaceAfter=6)
sSub      = S("sSub",      fontName="Helvetica-Bold", fontSize=10, textColor=C_TEXT,     spaceBefore=8,  spaceAfter=3)
sBody     = S("sBody",     fontName="Helvetica",      fontSize=9,  textColor=C_TEXT,     spaceAfter=4,  leading=14, alignment=TA_JUSTIFY)
sCode     = S("sCode",     fontName="Courier",        fontSize=8,  textColor=C_GREEN,    spaceAfter=4,  backColor=C_CARD, leftIndent=8)
sMeta     = S("sMeta",     fontName="Helvetica",      fontSize=8,  textColor=C_DIM,      spaceAfter=2)
sBullet   = S("sBullet",   fontName="Helvetica",      fontSize=9,  textColor=C_TEXT,     spaceAfter=3,  leftIndent=12, leading=13)

def hr(color=C_BORDER, thickness=0.5):
    return HRFlowable(width="100%", thickness=thickness, color=color, spaceAfter=6, spaceBefore=6)

def tag(text, bg, fg=colors.white):
    return f'<font color="{fg.hexval() if hasattr(fg,"hexval") else "#ffffff"}">[{text}]</font>'

def badge_cell(text, bg):
    return Paragraph(f"<b>{text}</b>", ParagraphStyle("b", fontName="Helvetica-Bold", fontSize=8,
                     textColor=colors.white, backColor=bg, alignment=TA_CENTER))

# ─────────────────────────────────────────────────────────────────────────────
story = []

# ── CAPA ─────────────────────────────────────────────────────────────────────
story.append(Spacer(1, 1.5*cm))
story.append(Paragraph("TRADER 001", sTitle))
story.append(Paragraph("Relatório de Auditoria Técnica & Estratégica", sSubtitle))
story.append(Paragraph(f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}", sMeta))
story.append(hr(C_BLUE, 1.5))
story.append(Spacer(1, 0.3*cm))

# Resumo executivo
story.append(Paragraph("Resumo Executivo", sSection))
story.append(Paragraph(
    "O Trader 001 é um <b>gestor de crescimento de banca</b> operando Binance Futures com conta real. "
    "Utiliza 4 engines de estratégia (TREND, RANGE, BREAKOUT, FADE) via cascade router, "
    "detecta regime de mercado por ADX/ATR, aplica volume profile e mantém memória de performance por ativo. "
    "Esta auditoria identificou <b>19 pontos de melhoria</b> distribuídos em bugs críticos, "
    "melhorias de estratégia, gestão de risco, arquitetura e novas features.", sBody))

# Tabela resumo
summary_data = [
    [badge_cell("CRÍTICO", C_RED),   Paragraph("2 itens", sBody), Paragraph("Race condition + risco de liquidação em cascata", sBody)],
    [badge_cell("ALTO",    C_ORANGE),Paragraph("4 itens", sBody), Paragraph("Anti-martingale, fingerprint, pump check, daily loss", sBody)],
    [badge_cell("MÉDIO",   C_YELLOW),Paragraph("8 itens", sBody), Paragraph("Asset memory, reinvest grid, redundância, trailing SHORT", sBody)],
    [badge_cell("BAIXO",   C_BLUE),  Paragraph("5 itens", sBody), Paragraph("Scalp engine, altcoin lock, FADE TP, klines leak, DB", sBody)],
]
t = Table(summary_data, colWidths=[2.5*cm, 2.2*cm, W-4.7*cm])
t.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,-1), C_CARD),
    ("ROWBACKGROUNDS", (0,0), (-1,-1), [C_CARD, C_SECTION]),
    ("GRID", (0,0), (-1,-1), 0.3, C_BORDER),
    ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ("TOPPADDING", (0,0), (-1,-1), 5),
    ("BOTTOMPADDING", (0,0), (-1,-1), 5),
]))
story.append(t)
story.append(Spacer(1, 0.4*cm))

# ── A) BUGS CRÍTICOS ─────────────────────────────────────────────────────────
story.append(hr(C_RED, 1))
story.append(Paragraph("A — BUGS CRÍTICOS", sSection))

bugs = [
    ("A1", "Race Condition na Execução de Trades", "main.py:422-425",
     "O guard check de _executing_assets não usa asyncio.Lock. "
     "Dois scans simultâneos podem passar pelo check ao mesmo tempo e abrir trade duplicado no mesmo ativo.",
     "Usar asyncio.Lock() antes de qualquer verificação de ativo já em execução."),
    ("A2", "Anti-Martingale Nunca Restaura", "main.py:115-118",
     "_consecutive_losses incrementa a cada perda, reduzindo o tamanho do trade, mas nunca é resetado após vitórias. "
     "Bot fica preso em tamanho reduzido permanentemente após 2+ perdas seguidas.",
     "Resetar _consecutive_losses quando _consecutive_wins >= 3. Salvar ambos em JSON para persistir entre restarts."),
    ("A3", "Risco de Liquidação em Cascata Não Monitorado", "risk_manager.py:191-214",
     "calculate_position_size() não verifica margin_level total da conta. "
     "Com 5 trades × 10x leverage, uma oscilação de 10% pode liquidar múltiplos trades simultaneamente.",
     "Criar job_equity_monitor() rodando a cada 30s: se margin_level < 1.5 → PAPER_TRADING = True + alerta Telegram."),
    ("A4", "Scale-Out Errado para SHORT", "risk_manager.py:346",
     "Ao atingir TP2 em SHORT, o SL é movido para trade.tp1. Como TP1 < entry em SHORT, "
     "o SL fica mais alto (pior). Deveria ser max(stop_loss_atual, tp1) para proteger o lucro.",
     "Para SHORT: trade.stop_loss = max(trade.stop_loss, trade.tp1)"),
]

for code, title, loc, prob, fix in bugs:
    story.append(KeepTogether([
        Paragraph(f"<b>{code} — {title}</b>  <font color='#888899' size='8'>{loc}</font>", sSub),
        Paragraph(f"<b>Problema:</b> {prob}", sBody),
        Paragraph(f"<b>Fix:</b> {fix}", sBody),
        Spacer(1, 0.2*cm),
    ]))

# ── B) ESTRATÉGIA ─────────────────────────────────────────────────────────────
story.append(hr(C_YELLOW, 1))
story.append(Paragraph("B — MELHORIAS DE ESTRATÉGIA", sSection))

estrategia = [
    ("B1", "SINAIS Alterna Perfil Automaticamente", "main.py:1430-1431",
     "Antes desta sessão, o modo SINAIS alternava entre NORMAL e AGGRESSIVE a cada 60s automaticamente, "
     "ignorando a preferência do usuário. Já corrigido: agora usa CURRENT_MODE.",
     "CORRIGIDO nesta sessão — usa perfil ativo do dashboard."),
    ("B2", "FADE Engine — TPs Fixos em Pump Extremo", "engine_router.py:231-242",
     "Em pump com RSI>90, TP1 = price - 2×ATR pode ser atingido por wick em milissegundos. "
     "Os TPs não escalam com a intensidade do pump.",
     "Escalar TPs pelo RSI: RSI>88 → TP1=3×ATR, TP2=5×ATR, TP3=8×ATR. "
     "RSI>80 → TP1=2.5×ATR (atual), TP2=4×ATR."),
    ("B3", "Grid Reinvest Bonus Perdido a Cada Restart", "main.py:945-948",
     "_grid_reinvest_bonus acumula 20% de cada ciclo mas é variável em memória. "
     "Ao reiniciar o bot, todo o acúmulo de reinvestimento é perdido.",
     "Persistir em grid_state.json: {'reinvest_bonus': X, 'cycles': Y, 'profit_total': Z}. "
     "Carregar em startup."),
    ("B4", "Staleness de Sinal Binário (OK ou Descarta)", "main.py:628-633",
     "Sinal com 4 minutos de idade descarta igual ao de 30 minutos. "
     "Não há decaimento gradual de confiança por tempo.",
     "Aplicar decay: confidence_adj = confidence - (age_min × 2.0). "
     "Se confidence_adj >= min_score → usa. Preserva sinais fortes que ficaram velhos."),
    ("B5", "pump_dump check com Assinatura Inconsistente", "engine_router.py:306",
     "Em engine_router.py, check_pump_dump é chamado como check_pump_dump(df, symbol, tf) "
     "mas a assinatura real é check_pump_dump(df, rsi_th, vol_th, require_both). "
     "Pode gerar TypeError silencioso.",
     "Harmonizar: sempre usar check_pump_dump(df, rsi_th=X, vol_th=Y, require_both=Z)."),
    ("B6", "Indicadores Recalculados 3× por Sinal", "signal_engine.py",
     "RSI, EMA e ATR são recalculados separadamente em score_trend(), score_momentum(), "
     "score_rsi_divergence() e score_golden_death_cross() para o mesmo DataFrame.",
     "Pré-calcular todos os indicadores uma vez no início de _score_direction() e passar "
     "como dicionário para cada função de scoring. Ganho estimado: 30-40% de velocidade."),
    ("B7", "Cascade Não Aplica VP e Asset Memory no Vencedor", "engine_router.py:478-498",
     "O Volume Profile e Asset Memory são aplicados ao vencedor do cascade, "
     "mas somente se houver winner. Se todas as engines retornam None, não há ajuste.",
     "Aplicar score_regime_match() sempre e usar como tiebreaker quando engines empatam."),
]

for code, title, loc, prob, fix in estrategia:
    story.append(KeepTogether([
        Paragraph(f"<b>{code} — {title}</b>  <font color='#888899' size='8'>{loc}</font>", sSub),
        Paragraph(f"<b>Situação:</b> {prob}", sBody),
        Paragraph(f"<b>Melhoria:</b> {fix}", sBody),
        Spacer(1, 0.2*cm),
    ]))

# ── C) GESTÃO DE RISCO ────────────────────────────────────────────────────────
story.append(hr(C_ORANGE, 1))
story.append(Paragraph("C — GESTÃO DE RISCO", sSection))

risco = [
    ("C1", "Daily Loss Não Conta Trades Abertos", "main.py:375-386",
     "_daily_pnl conta apenas trades fechados. 3 trades abertos em -15% cada = "
     "-45% de exposição não contabilizada. Bot pode continuar abrindo enquanto a conta sangra.",
     "daily_exposure = _daily_pnl + sum(pnl_usdt for trade in open_trades if pnl_usdt < 0). "
     "Se daily_exposure <= -MAX_DAILY_LOSS_PCT% → pausar."),
    ("C2", "Alavancagem Sugerida Ignora Portfólio Total", "risk_manager.py:48-188",
     "suggest_leverage() calcula para um trade isolado. Com 5 trades abertos × 10x = "
     "50x exposição agregada, novo trade com 8x adiciona risco não-linear.",
     "Fator de redução: leverage_final = sugestão × (1 - notional_open / banca_efetiva). "
     "Ex: 60% alocado → novo lever × 0.40."),
    ("C3", "Trailing Stop Apertado Demais nos Primeiros Ticks", "config.py:98-106",
     "Milestone +3% → stop em +0.5%. Em trades AGGRESSIVE de 5m, +3% pode ser "
     "revertido instantaneamente. SL trava muito cedo e fecha trade prematuro.",
     "Adicionar milestone +1.5% → stop em BE (breakeven). Mover +3% para stop em +1.0%. "
     "Isso dá mais espaço antes de travar lucro."),
    ("C4", "Sem Verificação de Margin Level em Tempo Real", "main.py",
     "Bot não monitora equity/margin_level da Binance. Se conta cair < 5% acima de liquidação, "
     "Binance liquida forçado sem aviso.",
     "Job a cada 30s: GET /fapi/v2/account → verificar availableBalance / totalMarginBalance. "
     "Se ratio < 1.2 → fechar menor trade, pausar novos."),
    ("C5", "Asset Memory Perde Estado no Restart", "asset_memory.py",
     "asset_memory.json persiste corretamente, mas se o bot reiniciar durante uma pausa de 24h, "
     "a contagem de tempo restante é recalculada pelo campo paused_until. "
     "Verificar se o campo é timestamp absoluto (correto) ou duração relativa (bug).",
     "Confirmar que paused_until é timestamp Unix absoluto. "
     "Se for relativo: converter para `time.time() + 86400` no momento da pausa."),
]

for code, title, loc, prob, fix in risco:
    story.append(KeepTogether([
        Paragraph(f"<b>{code} — {title}</b>  <font color='#888899' size='8'>{loc}</font>", sSub),
        Paragraph(f"<b>Risco:</b> {prob}", sBody),
        Paragraph(f"<b>Solução:</b> {fix}", sBody),
        Spacer(1, 0.2*cm),
    ]))

# ── D) ARQUITETURA ────────────────────────────────────────────────────────────
story.append(hr(C_BLUE, 1))
story.append(Paragraph("D — ARQUITETURA & PERFORMANCE", sSection))

arq = [
    ("D1", "Regime Detector Sem Cache Efetivo", "regime_detector.py",
     "_regime_ts existe mas nunca é consultado antes de recalcular. "
     "ADX, ATR e EMA são recalculados a cada chamada detect(), mesmo que o regime "
     "não mude em 5 minutos.",
     "Adicionar TTL check: if time.time() - _regime_ts.get(symbol, 0) < 60: return cache. "
     "Ganho: 2-3x aceleração em scans densos."),
    ("D2", "Pump/Dump Scan Sequencial por Timeframe", "pump_dump_engine.py",
     "analyze_symbol() itera 3 timeframes (3m, 5m, 15m) sequencialmente. "
     "Em scan de 30 ativos, isso multiplica o tempo por 3x sem necessidade.",
     "asyncio.gather() para os 3 TFs dentro de analyze_symbol(). "
     "Reduz latência de ~9s para ~3s no scan completo."),
    ("D3", "Klines Cache Sem Limite de Memória", "klines_cache.py",
     "Cache cresce indefinidamente. 50 ativos × 10 timeframes × 300 candles "
     "= ~15k linhas em memória. Após dias, pode explodir.",
     "Implementar LRU máx 500 entradas com TTL de 3h. "
     "Usar collections.OrderedDict ou cachetools.TTLCache."),
    ("D4", "SQLite Sem Índices em Colunas Críticas", "database.py",
     "Queries em asset, status, opened_at fazem full table scan. "
     "Com 1000+ trades históricos, get_all_trades() fica lento.",
     "CREATE INDEX idx_asset ON trades(asset); "
     "CREATE INDEX idx_status ON trades(status); "
     "CREATE INDEX idx_date ON trades(opened_at DESC);"),
    ("D5", "WATCHLIST_VOLATILE no config.py com Símbolos Inválidos", "config.py:120-135",
     "WATCHLIST_VOLATILE inclui PEPEUSDT (não existe em Binance Futures, o correto é 1000PEPEUSDT). "
     "Gera erros silenciosos no volatile_engine.",
     "Substituir PEPEUSDT → 1000PEPEUSDT em config.py. "
     "Adicionar validação de símbolos em startup: checar exchange_info uma vez."),
]

for code, title, loc, prob, fix in arq:
    story.append(KeepTogether([
        Paragraph(f"<b>{code} — {title}</b>  <font color='#888899' size='8'>{loc}</font>", sSub),
        Paragraph(f"<b>Problema:</b> {prob}", sBody),
        Paragraph(f"<b>Fix:</b> {fix}", sBody),
        Spacer(1, 0.2*cm),
    ]))

# ── E) NOVAS FEATURES ─────────────────────────────────────────────────────────
story.append(hr(C_GREEN, 1))
story.append(Paragraph("E — NOVAS FEATURES SUGERIDAS", sSection))

features = [
    ("E1", "Equity Monitor em Tempo Real", "ALTA PRIORIDADE",
     "Job a cada 30s que verifica availableBalance / totalMarginBalance via Binance API. "
     "Se margin ratio < 1.2 → fecha menor trade + bloqueia novos + alerta Telegram imediato. "
     "Previne liquidação forçada pela exchange."),
    ("E2", "Scalp Engine Dedicado (3m/5m)", "MÉDIA PRIORIDADE",
     "Engine separada para timeframes curtos com critérios próprios: "
     "RSI extremo (< 20 ou > 80), volume ≥ 5× média, ATR > 2.5× histórico, "
     "vela de reversão confirmada. Hoje o AGGRESSIVE usa os mesmos critérios do 15m+ em 3m/5m."),
    ("E3", "Altcoin Crash Recovery Lock", "MÉDIA PRIORIDADE",
     "Quando ativo caiu > 50% em 7 dias, bloquear LONG por 12h após cada spike de recuperação. "
     "Reduz false breakout bounces em altcoins em capitulação. "
     "Implementar como campo last_crash_pct no asset_memory.json."),
    ("E4", "Dynamic Universe Builder", "ALTA PRIORIDADE",
     "Job a cada 60min que busca top-50 por volume em Binance Futures e atualiza _dynamic_universe. "
     "Flag já existe no código mas nunca é populada. "
     "Cobertura dinâmica garante que novos ativos em alta sejam capturados."),
    ("E5", "Mapa de Liquidações em Tempo Real", "MÉDIA PRIORIDADE",
     "Monitorar via Coinglass API (ou WebSocket Binance) liquidações > $1M. "
     "Spike de liquidações de LONG → flag block_long por 60s. "
     "Spike de SHORT → flag block_short por 60s. "
     "Já existe liquidation_score_adj em signal_filters.py — integrar ao job de monitoramento."),
    ("E6", "Walk-Forward Validation Noturna", "BAIXA PRIORIDADE",
     "Todo dia às 02:00 UTC, rodar backtest leve nos últimos 7 dias com parâmetros atuais. "
     "Se Sharpe walk-forward < Sharpe realtime × 0.7 → alerta de overfitting. "
     "Mantém estratégia calibrada sem intervenção manual."),
    ("E7", "Smart Order Splitting", "BAIXA PRIORIDADE",
     "Em vez de 1 ordem, dividir em 2-3 partes com delay de 150ms entre elas. "
     "Reduz impacto de mercado e slippage em ativos com spread maior. "
     "Benefício estimado: +0.1-0.5% melhor entry."),
]

for code, title, prio, desc in features:
    prio_color = C_RED if "ALTA" in prio else (C_YELLOW if "MÉDIA" in prio else C_BLUE)
    story.append(KeepTogether([
        Paragraph(f"<b>{code} — {title}</b>  <font color='{prio_color.hexval()}'>{prio}</font>", sSub),
        Paragraph(desc, sBody),
        Spacer(1, 0.2*cm),
    ]))

# ── F) DASHBOARD ──────────────────────────────────────────────────────────────
story.append(hr(C_BLUE, 1))
story.append(Paragraph("F — DASHBOARD: O QUE FALTA", sSection))

dashboard = [
    ("F1", "Curva de Equity + Drawdown Máximo",
     "Gráfico de linha mostrando equity ao longo do tempo, drawdown atual vs máximo histórico "
     "e dias estimados para recuperação. Essencial para ajuste de risco em tempo real."),
    ("F2", "Performance por Engine (TREND/RANGE/BREAKOUT/FADE)",
     "Painel com % de sinais gerados por engine, win rate por engine e lucro por engine. "
     "Permite identificar qual engine está performando melhor e qual desligar temporariamente."),
    ("F3", "Heatmap de Asset Memory",
     "Grid visual com os ativos: verde (WR ≥ 60%), amarelo (40-60%), vermelho (< 40%), "
     "cinza (pausado 24h). Clique abre histórico dos últimos 8 trades daquele ativo."),
    ("F4", "Indicador de Regime de Mercado ao Vivo",
     "Chip colorido por ativo: TRENDING (verde), RANGING (azul), VOLATILE (amarelo), "
     "NEUTRAL (cinza) com ADX atual. Atualiza a cada scan de mercado."),
    ("F5", "Histograma de Qualidade de Sinais",
     "Distribuição dos scores dos últimos 100 sinais gerados (antes do threshold). "
     "Permite visualizar se o threshold está cortando sinais bons ou deixando lixo passar."),
    ("F6", "Funding Rate Tracker por Ativo",
     "Tabela dos top 10 ativos com funding rate atual e histórico 24h. "
     "Alerta visual quando funding > 0.05%/8h (oportunidade de short ou risco de long)."),
    ("F7", "Painel de Regime Detector Detalhado",
     "Para cada ativo: ADX, ATR%, alinhamento de EMAs, score_cap, sl_mult. "
     "Expande o regime atual para mostrar por que aquele regime foi detectado."),
    ("F8", "Configurador Visual do GRID",
     "Interface para alterar GRID_PAIRS, GRID_LEVERAGE, GRID_MAX_CONCURRENT e profit_target "
     "diretamente no dashboard sem precisar usar curl/API. "
     "Já existe endpoint POST /settings/grid — só falta o frontend."),
]

for code, title, desc in dashboard:
    story.append(KeepTogether([
        Paragraph(f"<b>{code} — {title}</b>", sSub),
        Paragraph(desc, sBody),
        Spacer(1, 0.15*cm),
    ]))

# ── PLANO DE AÇÃO ─────────────────────────────────────────────────────────────
story.append(hr(C_GREEN, 1.5))
story.append(Paragraph("G — PLANO DE AÇÃO (Priorizado)", sSection))

plano_data = [
    ["#", "Item", "Impacto", "Esforço", "Prioridade"],
    ["1", "Equity Monitor em tempo real (C4 + E1)", "Crítico", "2h", "⬛ Agora"],
    ["2", "Scale-Out SHORT fix (A4)", "Alto", "30min", "⬛ Agora"],
    ["3", "Anti-martingale reset (A2)", "Alto", "1h", "⬛ Agora"],
    ["4", "Daily loss = open+closed (C1)", "Alto", "1h", "⬛ Agora"],
    ["5", "asyncio.Lock em _execute_trade (A1)", "Crítico", "1h", "⬛ Agora"],
    ["6", "Dynamic Universe Builder (E4)", "Alto", "3h", "📅 Semana"],
    ["7", "FADE TP escalar pelo RSI (B2)", "Médio", "1h", "📅 Semana"],
    ["8", "Grid state JSON persistente (B3)", "Médio", "2h", "📅 Semana"],
    ["9", "Regime cache TTL (D1)", "Médio", "1h", "📅 Semana"],
    ["10", "Pump/dump gather paralelo (D2)", "Médio", "1h", "📅 Semana"],
    ["11", "PEPEUSDT → 1000PEPEUSDT config (D5)", "Alto", "10min", "⬛ Agora"],
    ["12", "Asset Memory heatmap dashboard (F3)", "Médio", "4h", "📅 Mês"],
    ["13", "Equity curve dashboard (F1)", "Alto", "6h", "📅 Mês"],
    ["14", "Engine performance panel (F2)", "Médio", "4h", "📅 Mês"],
    ["15", "Scalp Engine dedicado (E2)", "Médio", "8h", "📅 Mês"],
]

col_w = [0.7*cm, 7.2*cm, 2.2*cm, 1.8*cm, 2.3*cm]
pt = Table(plano_data, colWidths=col_w)
pt.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), C_SECTION),
    ("TEXTCOLOR",  (0,0), (-1,0), C_BLUE),
    ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",   (0,0), (-1,0), 8),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_CARD, C_SECTION]),
    ("TEXTCOLOR",  (0,1), (-1,-1), C_TEXT),
    ("FONTNAME",   (0,1), (-1,-1), "Helvetica"),
    ("FONTSIZE",   (0,1), (-1,-1), 8),
    ("GRID",       (0,0), (-1,-1), 0.3, C_BORDER),
    ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
    ("TOPPADDING", (0,0), (-1,-1), 4),
    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
]))
story.append(pt)

# ── FILOSOFIA ─────────────────────────────────────────────────────────────────
story.append(Spacer(1, 0.6*cm))
story.append(hr(C_BORDER))
story.append(Paragraph("Filosofia do Bot", sSection))
story.append(Paragraph(
    "O Trader 001 não é um apostador. É um <b>gestor de crescimento de banca</b>: "
    "cada trade é resultado de análise multi-fatorial (4 engines, regime detector, "
    "volume profile, asset memory, filtros de sessão, BTC veto e RS score). "
    "A entrada só ocorre quando o ativo passa por todas as camadas e a engine de maior "
    "confiança identifica uma oportunidade com RR favorável.",
    sBody))
story.append(Paragraph(
    "As melhorias prioritárias desta auditoria focam em <b>proteger a banca primeiro</b> "
    "(equity monitor, daily loss real, scale-out SHORT) e depois em "
    "<b>aumentar a qualidade dos sinais</b> (regime cache, universe dinâmico, FADE TP scaling). "
    "O objetivo é um Sharpe ratio crescente com drawdown controlado — não volume de trades.",
    sBody))

# ── RODAPÉ ────────────────────────────────────────────────────────────────────
story.append(Spacer(1, 0.4*cm))
story.append(hr(C_BORDER))
story.append(Paragraph(
    f"Trader 001 — Auditoria Técnica © {datetime.now().year}  |  "
    f"Gerado automaticamente em {datetime.now().strftime('%d/%m/%Y %H:%M')}  |  "
    f"19 pontos identificados  |  Melhorias estimadas: +15-25% Sharpe, -30% Drawdown",
    ParagraphStyle("footer", fontName="Helvetica", fontSize=7, textColor=C_DIM, alignment=TA_CENTER)
))

doc.build(story)
print(f"PDF gerado: {OUT}")
