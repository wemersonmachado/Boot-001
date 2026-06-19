import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
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
# CONSERVATIVE: score>=82, RR>=3.0, scan 90s — só majors, risco 0.5%, lev<=10x
# NORMAL:       score>=75, RR>=2.5, scan 60s — watchlist+trending, risco 1.0%
# AGGRESSIVE:   score>=60, RR>=1.7, scan 45s — universo dinâmico, risco 1.5%
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
        "min_score": 78,         # era 82 — com VRA COMPRESSION subia para 87, praticamente impossível
        "min_rr": 3.0,
        "scan_interval_s": 90,
        "max_open_trades": 3,
        "risk_pct": 0.5,
        "timeframes": ["5m", "15m"],  # era ["15m"] — adicionado 5m para mais oportunidades
        "bonus_cap": 12.0,
        "leverage_cap": 10,
        "allowed_assets": None,  # era ["BTCUSDT","ETHUSDT","SOLUSDT"] — removido para ampliar universo
        "max_spread_pct": 0.10,
    },
    "NORMAL": {
        "min_score": 75,
        "min_rr": 2.5,
        "scan_interval_s": 60,
        "max_open_trades": 5,
        "risk_pct": 1.0,
        "timeframes": ["5m", "15m"],
        "bonus_cap": 17.0,
        "leverage_cap": None,
        "allowed_assets": None,
        "max_spread_pct": 0.25,
    },
    "AGGRESSIVE": {
        "min_score": 65,  # era 60 — floor elevado para reduzir entradas de baixa qualidade
        "min_rr": 1.7,
        "scan_interval_s": 45,    # mínimo seguro após IP ban — nunca < 45s
        "max_open_trades": 8,
        "risk_pct": 1.5,
        "timeframes": ["1m", "3m", "5m", "15m"],
        "bonus_cap": 20.0,
        "leverage_cap": None,
        "allowed_assets": None,
        "max_spread_pct": 0.50,
    },
}

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
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_001.db")

# Telegram (preencher após criar o bot — ver SETUP.md)
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")         # chat pessoal — msgs operacionais
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")  # canal público @mestressinais_br
TELEGRAM_VIP_ID     = os.getenv("TELEGRAM_VIP_ID", "")      # grupo VIP privado

# CoinMarketCap (opcional — obter em coinmarketcap.com/api, plano gratuito)
CMC_API_KEY: str = os.getenv("CMC_API_KEY", "")
