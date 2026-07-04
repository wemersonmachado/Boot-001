import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# Senha do dashboard/API (Basic Auth em TODAS as rotas exceto /health e /webhook).
# Sem ela, a URL pública do Railway deixaria qualquer pessoa ativar o modo
# Autônomo/fechar posições na conta REAL via curl. Fallback: WEBHOOK_SECRET.
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "") or WEBHOOK_SECRET
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Risk per trade (% of balance)
DEFAULT_RISK_PCT = float(os.getenv("DEFAULT_RISK_PCT", "1.0"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "5"))

# Leverage rules
LEVERAGE_MAP = {
    "BTC": 15,
    "ETH": 15,
    "SOL": 15,
    "DEFAULT": 5,  # altcoins
}
LEVERAGE_MAX = {"BTC": 20, "ETH": 20, "SOL": 20, "DEFAULT": 5}

# Signal thresholds — DEPRECATED: a fonte da verdade são os perfis em MODE_SETTINGS.
# Mantidos apenas como fallback global; nunca usados diretamente no scoring.
MIN_SIGNAL_SCORE = 75
MIN_RR = 2.5

# ── Perfis de risco ───────────────────────────────────────────────────────────
# CURRENT_MODE (em main.py) seleciona qual perfil está ativo.
# Eixo ORTOGONAL ao OPERATION_MODE (AUTONOMOUS/SUPERVISED/GRID/SINAIS).
#
# CONSERVATIVE: score>=74, RR>=2.5, scan 90s — risco 0.5%, lev<=10x (5m/15m)
# NORMAL:       score>=72, RR>=2.0, scan 60s — watchlist+trending, risco 1.0% (3m/5m/15m)
# AGGRESSIVE:   score>=65, RR>=1.7, scan 45s — universo dinâmico, risco 1.5% (1m/3m/5m/15m)
DEFAULT_RISK_PROFILE = "NORMAL"   # perfil padrão de inicialização

# Aliases retrocompatíveis
TRADING_MODE = DEFAULT_RISK_PROFILE

# Campos por perfil:
#   min_score, min_rr      → thresholds de entrada
#   scan_interval_s        → intervalo REAL do scheduler (usado por _update_scan_interval)
#   max_open_trades        → limite de posições simultâneas (usado por can_open_trade)
#   risk_pct               → % do saldo por trade
#   timeframes             → TFs varridos
#   bonus_cap              → teto de bônus positivos no score
#   leverage_cap           → alavancagem MÁXIMA permitida (None = usa LEVERAGE_MAX)
#   allowed_assets         → restringe universo (None = sem restrição)
#   max_spread_pct         → spread bid/ask máximo aceito (gate de liquidez único)
MODE_SETTINGS = {
    "CONSERVATIVE": {
        "min_score": 74,         # 2026-06-19: 78→74 — mais oportunidades mantendo seletividade
        "min_rr": 2.5,           # 2026-06-19: 3.0→2.5 — RR 3.0 filtrava quase tudo
        "scan_interval_s": 90,
        "max_open_trades": 3,
        "risk_pct": 0.5,
        "timeframes": ["5m", "15m", "1h"],  # 2026-06-26: +1h (swing) — pedido do usuário
        "bonus_cap": 14.0,       # 2026-06-19: 12→14
        "leverage_cap": 10,
        "allowed_assets": None,
        "max_spread_pct": 0.16,  # 2026-06-19: 0.10→0.16 — liberar mais ativos líquidos
        "entry_cadence_s": 0,    # AUTÔNOMO: entradas consecutivas (sem espera entre aberturas)
    },
    "NORMAL": {
        "min_score": 72,                 # 2026-06-19: 75→72
        "min_rr": 2.0,                   # 2026-06-19: 2.5→2.0 — principal gargalo de sinais
        "scan_interval_s": 60,
        "max_open_trades": 5,
        "risk_pct": 1.0,
        "timeframes": ["3m", "5m", "15m", "1h"],  # 2026-06-19: +3m | 2026-06-26: +1h
        "bonus_cap": 18.0,               # 2026-06-19: 17→18
        "leverage_cap": None,
        "allowed_assets": None,
        "max_spread_pct": 0.35,          # 2026-06-19: 0.25→0.35
        "entry_cadence_s": 120,          # AUTÔNOMO: 1 entrada a cada 2 min
    },
    "AGGRESSIVE": {
        "min_score": 62,  # 2026-07-03: 65→62 — meio-termo; volume do canal SINAIS
                          # caiu demais após os gates globais de 22/06 (ver
                          # SINAIS_MTF_HARD_GATE/SINAIS_REQUIRE_STRUCT_ALL abaixo,
                          # agora mode-aware para restaurar a via rápida do Agressivo).
        "min_rr": 1.7,
        "scan_interval_s": 45,    # mínimo seguro após IP ban — nunca < 45s
        "max_open_trades": 8,
        "risk_pct": 1.5,
        "timeframes": ["1m", "3m", "5m", "15m", "1h"],  # 2026-06-26: +1h
        "bonus_cap": 20.0,
        "leverage_cap": None,
        "allowed_assets": None,
        "max_spread_pct": 0.50,
        "entry_cadence_s": 180,   # AUTÔNOMO: 1 entrada a cada 3 min
    },
}

# ── Kill-switch do modo AUTÔNOMO ──────────────────────────────────────────────
# Se a sessão autônoma perder esta % da banca inicial, PARA TUDO (não abre novas
# entradas) até reset manual via /killswitch reset ou endpoint /bot/killswitch/reset.
AUTO_KILLSWITCH_PCT = 20.0

# ── Melhorias de segurança/eficácia do modo AUTÔNOMO (2026-06-22) ──────────────
# NÃO ENVIADO AO RAILWAY — ver Documento_Melhorias_Nao_Enviadas.docx.
#
# (A) [REMOVIDO a pedido] Janela em PAPER ao ativar o AUTÔNOMO. O bot agora opera
#     assim que acionado e encontrar entradas do perfil, sem espera. Constante
#     mantida apenas por compatibilidade de import (sem efeito).
AUTO_PAPER_WARMUP_MIN = 0          # DESATIVADO

# (D) Auto-tune do score: ajusta o min_score conforme a taxa de acerto recente.
AUTOTUNE_SCORE_ENABLED = True
AUTOTUNE_LOOKBACK      = 30        # nº de trades fechados considerados
AUTOTUNE_MAX_TIGHTEN   = 8         # quanto pode SUBIR o corte (mais seletivo)
AUTOTUNE_MAX_LOOSEN    = 3         # quanto pode BAIXAR o corte (quando ganha muito)

# (E) Alavancagem por volatilidade (ATR%): reduz a alavancagem em ativos mais voláteis.
LEVERAGE_BY_VOLATILITY = True
ATR_PCT_REF            = 1.5       # ATR% de referência (acima disto, reduz proporcional)
LEVERAGE_VOL_FLOOR     = 3         # alavancagem mínima após redução por volatilidade
#
# (B) Anti-overtrading: após abrir uma entrada num ativo, só permite NOVA entrada no
#     MESMO ativo depois de SAME_ASSET_COOLDOWN_MIN, e apenas se o sinal for "claro".
SAME_ASSET_COOLDOWN_MIN = 15
CLEAR_SIGNAL_MIN_SCORE  = 80        # "sinal claro" = score >= isto para reentrar no ativo
#
# (C) Circuit breaker: pede autorização no Telegram (/continuar ou /pausar). Sem
#     resposta em CB_AUTH_TIMEOUT_S → pausa TUDO. Dois gatilhos:
#     - CB_LOSS_THRESHOLD trades PERDEDORES seguidos (gatilho principal — proteção de banca).
#     - CB_ERROR_THRESHOLD erros consecutivos da Binance (gatilho secundário — falha técnica).
CIRCUIT_BREAKER_ENABLED = True
CB_LOSS_THRESHOLD       = 3         # nº de trades perdedores seguidos que dispara o breaker
CB_ERROR_THRESHOLD      = 3
# Gatilho por ERROS da Binance DESLIGADO por padrão: a fapi no Railway sofre timeout/
# rate-limit intermitente e isso disparava a mensagem "3 erros seguidos da Binance"
# constantemente. O circuit breaker agora é só por TRADES PERDEDORES. (True p/ reativar.)
CB_ERROR_TRIGGER_ENABLED = False
CB_AUTH_TIMEOUT_S       = 300       # 5 min
#
# Teto de exposição agregada (notional_total / banca) e nº máximo de entradas/dia.
MAX_TOTAL_EXPOSURE_RATIO = 1.5      # bloqueia nova entrada se exposição > isto
MAX_TRADES_PER_DAY       = 30       # 0 = sem limite

# ── Anti-topo/fundo REFORÇADO (2026-06-22) — afeta TODOS os modos (signal_engine) ──
# Corrige a brecha do prox-gate: ele só bloqueava quando a resistência estava ACIMA do
# preço; um LONG JÁ acima da resistência (breakout pelado = "comprar topo") passava.
# (#1/#3) Breakout pelado: LONG acima da resistência / SHORT abaixo do suporte só passa
#         com CONFIRMAÇÃO forte (volume + fechamento na ponta da vela). Senão BLOQUEIA —
#         espera o reteste (alinhado à estratégia V5.3: reteste + confirmação).
# 2026-06-23: LIGADOS (paridade com Railway confirmada antes de ativar).
PROX_BLOCK_NAKED_BREAKOUT = True
PROX_BREAKOUT_VOL_MULT    = 1.8     # volume da vela ≥ X× a média p/ confirmar o breakout
PROX_BREAKOUT_CLOSE_PCT   = 0.70    # fechamento ≥70% do range da vela (perto da máx, LONG)
# (#2) Multi-TF: um sinal de scalp não entra colado na resistência/suporte do TF MAIOR.
PROX_MULTI_TF_GATE        = True
PROX_HIGHER_TF            = {"1m": "15m", "3m": "15m", "5m": "1h", "15m": "1h", "1h": "4h"}

# (#4) Topo/fundo FRESCO (2026-06-23) — em teste a pedido do usuário. Bloqueia
# entrada a menos de PROX_FRESH_EXTREME_PCT% da máxima/mínima das últimas
# PROX_FRESH_EXTREME_N velas (pega o "acabou de fazer máxima nova" que o gate
# estrutural por swing confirmado não vê). False desliga.
PROX_BLOCK_FRESH_EXTREME = True
PROX_FRESH_EXTREME_N     = 10
PROX_FRESH_EXTREME_PCT   = 0.3

# Override de reversão genuína (2026-06-23) — em teste a pedido do usuário.
# Libera entrada bloqueada pelos 4 gates acima quando há CVD-divergência
# (absorção/exaustão), RSI-divergência ou liquidity sweep (SMC) confirmando
# reversão real. False desliga (gates voltam a bloquear sempre).
PROX_REVERSAL_OVERRIDE = True

# Gate STOCH-SATURADO (2026-06-23) — achado real: LONG comprando com StochRSI
# K>=90 (momentum já no topo) só ganhou 27.8% (5/18) em 164 outcomes reais.
# Bloqueia LONG/SHORT entrando em momentum saturado, salvo override de reversão.
STOCH_SATURATION_GATE = True
STOCH_SATURATION_HIGH = 90.0   # LONG bloqueado se K >= isso
STOCH_SATURATION_LOW  = 10.0   # SHORT bloqueado se K <= isso

# ── Acurácia do canal SINAIS (2026-06-22) — NÃO ENVIADO AO RAILWAY ────────────
# Melhorias que afetam SOMENTE o canal SINAIS (evaluate_signal é chamado apenas
# pelo job_sinais_scan; o modo autônomo/real NÃO é tocado por estes gates).
#
# (1) MTF como GATE: se o timeframe superior diverge da direção, BLOQUEIA o sinal
#     (antes era só penalidade de -5/-8 e o sinal ainda passava se o score fosse alto).
SINAIS_MTF_HARD_GATE      = True
# (2) Tag estrutural V6 obrigatória em TODOS os perfis (antes só no NORMAL). Sem
#     pelo menos 1 tag (BOS/OB-FVG/sweep/FIB/divergência/...) o sinal é bloqueado.
SINAIS_REQUIRE_STRUCT_ALL = True
# (3) Confluência mínima: nº de tags estruturais DISTINTAS que devem concordar.
#     Bloqueia o "score alto solitário" (1 fator inflado). Por perfil.
# FIX (2026-06-25): NORMAL estava em 2 mas o diagnóstico real de 22/06 (400 sinais
# reais) mediu que confluência=2 sozinha bloqueava 31,5% do NORMAL — caindo a taxa
# de aprovação de 19,8% para... só 19,8% MESMO (o "2" nunca foi de fato revertido
# pra 1 no código, apesar de a correção ter sido validada e registrada). Voltando
# para 1 (mesmo valor do AGGRESSIVE), que mediu 45,2% de aprovação com dados reais.
SINAIS_MIN_CONFLUENCE     = {"CONSERVATIVE": 2, "NORMAL": 1, "AGGRESSIVE": 0}
# 2026-07-03: AGGRESSIVE volta a 0 (não exige confluência) — restaura a "via
# rápida" que o perfil tinha antes de 22/06 sem mexer em CONSERVATIVE/NORMAL.
# Ver também evaluate_signal() em signal_filters.py: MTF hard gate e tag
# estrutural obrigatória agora são condicionais ao modo (AGGRESSIVE mais leve).
# (5) Claude Brain avalia o sinal a partir deste score (antes era 65 — sinais de
#     55-64 do Agressivo escapavam da IA).
SINAIS_BRAIN_MIN_SCORE    = 55
# (6/7) Rastreio de resultado dos sinais transmitidos (paper) + auto-tune do corte
#     do SINAIS conforme a taxa de acerto MEDIDA. Sem isto não há como medir acerto.
SINAIS_OUTCOME_TRACKING   = True
SINAIS_OUTCOME_MAX_AGE_H  = 6        # fallback p/ TF não listado em SINAIS_OUTCOME_MAX_AGE_H_BY_TF
# Janela máx. para resolver um sinal antes de fechar como TIMEOUT, por timeframe.
# Calibrado em 2026-06-29: janela única de 6h gerava TIMEOUT desproporcional em TFs
# maiores (15m: 24% dos sinais expiravam sem tocar TP/SL) — alvo é ~o tempo que o
# par TP/SL desse TF historicamente leva para resolver, com folga.
SINAIS_OUTCOME_MAX_AGE_H_BY_TF = {
    # 15m: 8h→24h (2026-07-02) — janela curta marcava TIMEOUT/LOSS em sinais que
    # ainda iam bater o TP; era a métrica "invertida" que enganava o auto-tune.
    "1m": 1, "3m": 2, "5m": 3, "15m": 24, "1h": 24,
}

# ── Filtro de performance por ativo (Fase 0, calibrado em 2026-06-29 com 740
# signal_outcomes reais). RELAXADO em 2026-07-01: a WR que calibrou estes
# throttles é medida sobre a janela de TIMEOUT antiga (15m reconhecidamente
# invertido), então o pacote estava sufocando o volume de sinais para os dois
# canais. Sem bloqueio total (só malus leve) até o motor ser revalidado.
ASSET_PERFORMANCE_BLOCKLIST = set()   # nenhum ativo 100% bloqueado
ASSET_PERFORMANCE_MALUS = {
    # Malus leve (metade do calibrado em 29/06) — penaliza sem esvaziar o universo.
    "SOLUSDT": -4, "AXSUSDT": -3, "XMRUSDT": -3, "ETHUSDT": -3,
    "DYDXUSDT": -2, "ONDOUSDT": -2,
}
# 15m: malus -5 CONFIRMADO por revalidação com janela justa de 24h (2026-07-02,
# tools/revalidate_15m_outcomes.py): WR real 25.2% (34/135), expectância
# -0.775%/sinal, -104.6% acumulado. Não era medição invertida — o 15m é
# genuinamente o pior TF. Auto-tune agora opera sobre métrica confiável.
TIMEFRAME_PERFORMANCE_MALUS = {"15m": -5}
SINAIS_AUTOTUNE_ENABLED   = True
SINAIS_AUTOTUNE_LOOKBACK  = 30       # nº de sinais resolvidos considerados
SINAIS_AUTOTUNE_MAX_TIGHTEN = 3      # corte do SINAIS sobe no máx +3 (era 8 —
                                     # +8 estrangulava o volume via loop de WR)
SINAIS_AUTOTUNE_MAX_LOOSEN  = 3      # quanto pode BAIXAR

# ── Claude Brain ─────────────────────────────────────────────────────────────
# Budget diário em USD para o Claude Brain (0 = sem limite definido)
CLAUDE_BRAIN_BUDGET_USD = float(os.getenv("CLAUDE_BRAIN_BUDGET_USD", "5.0"))
# Claude Brain ativo em TODOS os modos quando API key presente
CLAUDE_BRAIN_ALL_MODES: bool = True

# ── Grid Settings por modo ────────────────────────────────────────────────────
# Otimizado v2: thresholds calibrados para encontrar sinais com mais frequência
# mantendo os controles de risco essenciais.
#
# Mudanças vs v1:
#   CONSERVATIVE: min_confidence 65→58, session_min_score 5→3, btc_veto -1%→-2%, max_concurrent 1→2
#   NORMAL:       min_confidence 55→50, session_min_score 4→3, btc_veto -1.5%→-2.5%, max_concurrent 2→3
#   AGGRESSIVE:   min_confidence 45→40, session_min_score 3→2, btc_veto -2.5%→-3.5%, max_concurrent 4→5
GRID_SETTINGS = {
    "CONSERVATIVE": {
        "min_confidence":    58,    # ↓ de 65→58: engine 15m raramente >65 em sideways
        "min_rr":            1.8,   # ↓ de 2.0→1.8: mantém qualidade mas abre mais oportunidades
        "session_min_score": 3,     # ↓ de 5→3: não bloquear em Asia Pre/Madrugada (score 3)
        "profit_target_usdt": 6.0,
        "max_concurrent":    2,     # ↑ de 1→2: permite 2 posições simultâneas
        "btc_veto_pct":     -2.0,   # ↑ de -1%→-2%: menos sensível, veto só em quedas reais
        "timeframes":        ["5m", "15m"],  # usa 5m adicional para mais oportunidades
    },
    "NORMAL": {
        "min_confidence":    50,    # ↓ de 55→50: threshold mais realista para 15m/5m
        "min_rr":            1.4,   # ↓ de 1.5→1.4: calibrado ao RR médio real do signal_engine
        "session_min_score": 3,     # ↓ de 4→3: captura sessões moderadas
        "profit_target_usdt": 5.0,
        "max_concurrent":    3,     # ↑ de 2→3: mais pares simultâneos
        "btc_veto_pct":     -2.5,   # ↑ de -1.5%→-2.5%: alinhado ao AGGRESSIVE original
        "timeframes":        ["5m", "15m"],
    },
    "AGGRESSIVE": {
        "min_confidence":    40,    # ↓ de 45→40: agressivo de verdade
        "min_rr":            1.2,   # ↓ de 1.3→1.2: ainda lucrativo com boa WR
        "session_min_score": 2,     # ↓ de 3→2: opera em quase todos os horários
        "profit_target_usdt": 3.0,
        "max_concurrent":    5,     # ↑ de 4→5: mais diversificação
        "btc_veto_pct":     -3.5,   # ↑ de -2.5%→-3.5%: só veta em crash real
        "timeframes":        ["1m", "3m", "5m", "15m"],
    },
}

# ── Pump/Dump Channel ─────────────────────────────────────────────────────────
# Sinais de pump/dump são separados do canal normal para controle de risco
PUMP_DUMP_CHANNEL_ENABLED = True     # habilita canal separado no modo SINAIS
PUMP_DUMP_SIZE_PCT = 0.40            # 40% do tamanho normal para PD signals
PUMP_DUMP_MIN_VOL_RATIO = 3.0        # volume >= 3x média para qualificar
PUMP_DUMP_RSI_MIN = 65               # RSI mínimo para considerar pump
PUMP_DUMP_MAX_PER_CYCLE = 1          # máx 1 sinal PD por ciclo de scan

# Scale-Out parcial por TP
# (nivel_tp, fracao_a_fechar): fecha 35% em TP1, 35% em TP2, 30% em TP3
SCALE_OUT_MILESTONES = [
    (1, 0.35),   # TP1: fecha 35% + move SL para breakeven
    (2, 0.35),   # TP2: fecha mais 35% + trailing apertado
    (3, 1.00),   # TP3: fecha os 30% restantes
]

# Trailing stop milestones (profit % → move stop to %)
TRAILING_MILESTONES = [
    (3.0, 0.5),    # at +3% profit → move stop to +0.5%
    (5.0, 2.0),    # at +5% → stop to +2%
    (8.0, 4.0),    # at +8% → stop to +4%
    (10.0, 6.5),   # at +10% → stop to +6.5%
    (15.0, 10.0),  # at +15% → stop to +10%
]

# ── Watchlist principal — signal_engine padrão ────────────────────────────────
WATCHLIST = [
    # Tier 1 — Alta liquidez (15x–20x alavancagem)
    "BTCUSDT", "ETHUSDT", "SOLUSDT",
    # Tier 2 — Altcoins com liquidez suficiente para TA funcionar
    "XRPUSDT", "HYPEUSDT",
    # V6: adicionados por performance em backtest
    "BNBUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]

# ── Micro-caps voláteis — volatile_engine + V6 structural (OB/FVG/sweeps) ─────
# Top V6: STGUSDT 1.85, BEATUSDT 1.76, AINUSDT 1.71, VELVETUSDT 1.68, MOVEUSDT 1.64
# Removidos (piores V6): VVVUSDT 0.66, MUSDT 0.72, XAUUSDT 0.72
WATCHLIST_VOLATILE = [
    # Top performers V6 (PF >= 1.5)
    "STGUSDT",   "BEATUSDT",  "AINUSDT",
    "VELVETUSDT","MOVEUSDT",  "MEGAUSDT",
    # Bons performers V6 (PF >= 1.1)
    "HYPEUSDT",  "SKYAIUSDT", "LABUSDT",
    # Outros da watchlist original (liquidity ok)
    "SIRENUSDT", "GUSDT",     "UAIUSDT",
    "PLAYUSDT",  "AIOUSDT",   "CRVUSDT",
    # Expansão universo — top 24h volume Binance Futures
    "SUIUSDT",   "1000PEPEUSDT", "WIFUSDT",
    "FETUSDT",   "RENDERUSDT","JUPUSDT",
    "TIAUSDT",   "SEIUSDT",
]

# Ativos com alavancagem premium (15x-20x)
PREMIUM_ASSETS = {"BTC", "ETH", "SOL"}

# Filtro: não abrir correlacionados ao mesmo tempo
# (ex: BTC long + ETH long = muito correlacionado)
CORRELATION_GROUPS = [
    {"BTCUSDT", "ETHUSDT", "SOLUSDT"},  # só 1 por vez deste grupo
]

# Limite de perda diária — para operar se atingir
MAX_DAILY_LOSS_PCT = 3.0   # para tudo se perder 3% do saldo no dia

# Horários ruins para operar (UTC) — baixa liquidez
AVOID_HOURS_UTC = []  # ex: [1, 2, 3, 4] para evitar 1h-4h UTC

TIMEFRAMES = ["1m", "3m", "5m", "15m"]

# Caminho absoluto ancorado na pasta deste arquivo — independe do diretório de
# onde o processo foi iniciado (evita criar um DB vazio em CWD diferente).
# Banco PERSISTENTE: por padrão fica ao lado do código (zera no deploy do Railway).
# Defina a env DB_PATH apontando para um VOLUME montado (ex.: /data/trader_001.db)
# para o histórico/kill-switch/sessão SOBREVIVEREM aos deploys.
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_001.db"),
)
# Garante que o diretório do banco exista (caso aponte para um volume novo).
try:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
except Exception:
    pass

# Telegram (preencher após criar o bot — ver SETUP.md)
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")         # chat pessoal — msgs operacionais
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")  # canal público @mestressinais_br
TELEGRAM_VIP_ID     = os.getenv("TELEGRAM_VIP_ID", "")      # grupo VIP privado
TELEGRAM_VIP_BOT_LINK = os.getenv("TELEGRAM_VIP_BOT_LINK", "@MestresVipAcesso_bot")  # CTA nas msgs públicas (bot oficial VIP)

# CoinMarketCap (opcional — obter em coinmarketcap.com/api, plano gratuito)
CMC_API_KEY: str = os.getenv("CMC_API_KEY", "")
