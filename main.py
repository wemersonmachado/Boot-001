"""
TRADER 001 — FastAPI Server
Endpoints:
  POST /webhook          — receive TradingView alerts
  GET  /signals/scan     — manual full watchlist scan
  GET  /signals/latest   — last scanned signals
  GET  /market           — live market snapshot
  GET  /trades/active    — open positions
  GET  /trades/history   — closed trades
  GET  /performance      — stats (win rate, PnL, etc.)
  GET  /news             — latest crypto news
  POST /trades/{id}/close — manually close a trade
  GET  /dashboard        — serve HTML dashboard
"""
import sys
# Garante saída UTF-8 no Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Rotação de log (2026-07-02): stdout/stderr → logs/bot.log (10MB x 5).
# Precisa vir ANTES dos outros imports pra capturar tudo, inclusive uvicorn.
import log_rotation
log_rotation.install()

import asyncio
import atexit
import json
import os
import time
import requests as _requests
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── FIX: serialização JSON de tipos numpy ─────────────────────────────────────
# Endpoints que devolvem dados derivados de pandas/numpy (sinais, análises) podem
# conter numpy.bool_/int64/float32/ndarray. O jsonable_encoder do FastAPI quebra
# nesses tipos (HTTP 500: "'numpy.bool' object is not iterable"). Registramos os
# encoders e reconstruímos o cache interno p/ corrigir TODOS os endpoints de uma vez.
import numpy as _np
import fastapi.encoders as _fe
_fe.ENCODERS_BY_TYPE[_np.bool_]    = bool
_fe.ENCODERS_BY_TYPE[_np.integer]  = int
_fe.ENCODERS_BY_TYPE[_np.floating] = float
_fe.ENCODERS_BY_TYPE[_np.ndarray]  = lambda a: a.tolist()
if hasattr(_fe, "generate_encoders_by_class_tuples"):
    _fe.encoders_by_class_tuples = _fe.generate_encoders_by_class_tuples(_fe.ENCODERS_BY_TYPE)

from config import (WEBHOOK_SECRET, HOST, PORT, WATCHLIST, MAX_DAILY_LOSS_PCT,
                    CORRELATION_GROUPS, MODE_SETTINGS, TRADING_MODE as _DEFAULT_MODE,
                    GRID_SETTINGS, CLAUDE_BRAIN_ALL_MODES, AUTO_KILLSWITCH_PCT,
                    AUTO_PAPER_WARMUP_MIN, SAME_ASSET_COOLDOWN_MIN, CLEAR_SIGNAL_MIN_SCORE,
                    CIRCUIT_BREAKER_ENABLED, CB_ERROR_THRESHOLD, CB_AUTH_TIMEOUT_S,
                    CB_LOSS_THRESHOLD, CB_ERROR_TRIGGER_ENABLED,
                    MAX_TOTAL_EXPOSURE_RATIO, MAX_TRADES_PER_DAY,
                    AUTOTUNE_SCORE_ENABLED, AUTOTUNE_LOOKBACK, AUTOTUNE_MAX_TIGHTEN,
                    AUTOTUNE_MAX_LOOSEN, LEVERAGE_BY_VOLATILITY, ATR_PCT_REF, LEVERAGE_VOL_FLOOR,
                    SINAIS_BRAIN_MIN_SCORE, SINAIS_OUTCOME_TRACKING, SINAIS_OUTCOME_MAX_AGE_H,
                    SINAIS_OUTCOME_MAX_AGE_H_BY_TF,
                    SINAIS_AUTOTUNE_ENABLED, SINAIS_AUTOTUNE_LOOKBACK,
                    SINAIS_AUTOTUNE_MAX_TIGHTEN, SINAIS_AUTOTUNE_MAX_LOOSEN)
import market_engine
from models import WebhookAlert, Direction, ActiveTrade
from signal_engine import scan_watchlist, analyze_asset, scan_anomalies, analyze_smart_flow
import pump_dump_engine
import supply_demand
import engine_router
import asset_memory as _asset_memory
from data_fetcher import get_trending_futures
from risk_manager import create_trade, process_trade_update, can_open_trade
from binance_executor import open_trade, update_stop_loss, close_position, get_futures_balance, get_client, get_account_balance_detail, get_binance_trade_history, execute_dca_order
from data_fetcher import get_market_snapshot, get_crypto_news, get_ticker
from database import (
    init_db, save_trade, save_signal, get_open_trades,
    get_all_trades, get_performance_stats, save_snapshot, log_event,
    update_trade_close, mark_signal_executed,
    upsert_asset_profile, upsert_confluence_pattern,
    record_signal_outcome, upsert_daily_stats, get_score_adjustment,
    get_recent_trade_stats, get_recent_signal_stats, get_signal_kpi_summary,
)
from notifier import (
    send_signal_alert, send_trade_opened, send_trade_closed,
    send_trailing_update, send_daily_summary, send_alert,
    send_sinais_alert, send_daily_target_reached,
    send_grid_trend_alert, send_grid_stale_alert,
    send_news_broadcast, send_weekly_sinais_stats,
    send_pd_monitor_alert, send_claude_brain_toggle_alert,
    poll_telegram_responses, test_connection, set_command_handler, set_close_handler,
    register_bot_commands,
    send_macro_decision, get_pending_assets,
    create_vip_invite_link, remove_vip_member,
    get_vip_member_count, get_channel_subscriber_count,
    send_social_proof,
    set_alerts_paused, is_alerts_paused,
)

# ── State (in-memory cache) ───────────────────────────────────────────────────

_latest_signals: list = []
_market_cache: dict = {}
_news_cache: list = []
_active_trades_cache: dict = {}  # id → ActiveTrade
_anomalies_cache: list = []
_trending_cache: list = []
_last_scan_at: str = ""
_last_scan_ts: float = 0.0          # epoch do último scan de mercado concluído
_last_scan_count: int = 0           # nº de sinais do último scan
_sinais_last_empty_alert_ts: float = 0.0   # throttle do alerta de cache vazio
_sinais_last_blocked_alert_ts: float = 0.0 # throttle do alerta de "achou mas filtros bloquearam tudo"
_mode_started_at: str = ""

# ── Health Monitor — heartbeat do event loop + estado de saúde ──────────────
_loop_lag_max_recent: float = 0.0     # maior atraso (s) detectado desde a última checagem
_loop_lag_last_reset: float = 0.0
_health_state: dict = {              # flags de alerta já enviado (evita spam)
    "scan_stuck": False, "loop_lag": False, "binance_down": False,
}
_health_last_ok_ts: float = 0.0       # epoch da última checagem 100% saudável
CURRENT_MODE: str = "AGGRESSIVE"   # inicia sempre em AGGRESSIVE
scheduler = AsyncIOScheduler()
_INSTANCE_LOCK_FD = None
_INSTANCE_LOCK_PATH = os.path.join(os.getcwd(), "trader_001.pid")


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _acquire_instance_lock() -> None:
    global _INSTANCE_LOCK_FD
    if os.path.exists(_INSTANCE_LOCK_PATH):
        try:
            with open(_INSTANCE_LOCK_PATH, "r", encoding="utf-8") as fh:
                existing_pid = int((fh.read() or "0").strip() or "0")
        except Exception:
            existing_pid = 0
        if _pid_is_alive(existing_pid):
            raise RuntimeError(
                f"Outra instancia do Trader 001 ja esta ativa (pid={existing_pid}). "
                f"Lock: {_INSTANCE_LOCK_PATH}"
            )
        try:
            os.remove(_INSTANCE_LOCK_PATH)
        except FileNotFoundError:
            pass

    _INSTANCE_LOCK_FD = os.open(_INSTANCE_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(_INSTANCE_LOCK_FD, str(os.getpid()).encode("utf-8"))
    atexit.register(_release_instance_lock)


def _release_instance_lock() -> None:
    global _INSTANCE_LOCK_FD
    if _INSTANCE_LOCK_FD is not None:
        try:
            os.close(_INSTANCE_LOCK_FD)
        finally:
            _INSTANCE_LOCK_FD = None
    try:
        if os.path.exists(_INSTANCE_LOCK_PATH):
            with open(_INSTANCE_LOCK_PATH, "r", encoding="utf-8") as fh:
                lock_pid = int((fh.read() or "0").strip() or "0")
            if lock_pid == os.getpid():
                os.remove(_INSTANCE_LOCK_PATH)
    except Exception as e:
        print(f"[LOCK] Falha ao liberar lock: {e}")

# ── Modo de Operação ──────────────────────────────────────────────────────────
# AUTONOMOUS  → bot executa trades sozinho, notifica Telegram após abertura
# SUPERVISED  → bot envia sinal ao Telegram e aguarda aprovação do usuário
OPERATION_MODE: str = "SINAIS"   # "AUTONOMOUS" | "SUPERVISED" | "GRID" | "SINAIS"

# ── Dual-Mode: SINAIS + Operacional simultâneos ──────────────────────────────
DUAL_MODE_ENABLED: bool    = False   # True = SINAIS roda em paralelo com modo operacional
SINAIS_ENABLED: bool       = True    # True = Canal de sinais ativo e transmitindo
SINAIS_PROFILE: str        = "AGGRESSIVE"  # perfil independente do modo SINAIS
EXEC_MODE: str             = "SINAIS"      # modo de execução ("AUTONOMOUS"|"SUPERVISED"|"GRID")
_sinais_claude_brain: bool = False   # Claude Brain específico para o canal SINAIS
_exec_claude_brain: bool   = False   # Claude Brain específico para o modo operacional

# Banca alocada para o bot (definida pelo usuário no dashboard)
# No modo AUTONOMOUS o bot usa 10% desse valor por trade
BANCA_USDT: float = 0.0        # 0 = usa saldo disponível × risk_pct do config
EXPOSURE_PCT: float = 10.0     # % da banca por trade (padrão 10%)
TRADES_PER_SESSION: int = 0    # ilimitado (0 = sem limite de trades por sessao)
DAILY_TARGET_USDT: float = 0.0 # objetivo diário de lucro em USDT (0 = desativado)

PAPER_TRADING: bool = False    # True = simulação sem dinheiro real (NÃO é pausa!)
BOT_PAUSED: bool = False        # True = bot pausado (não abre trades). Separado de PAPER_TRADING.

# Sincronização centralizada com o BotState
from state import state as bot_state

def sync_state_to_globals():
    """Sincroniza as variáveis de BotState para as variáveis globais do main.py."""
    global CURRENT_MODE, OPERATION_MODE, DUAL_MODE_ENABLED, SINAIS_ENABLED, SINAIS_PROFILE, EXEC_MODE
    global _sinais_claude_brain, _exec_claude_brain, BANCA_USDT, EXPOSURE_PCT, TRADES_PER_SESSION, DAILY_TARGET_USDT, PAPER_TRADING, _mode_started_at
    CURRENT_MODE = bot_state.current_mode
    OPERATION_MODE = bot_state.operation_mode
    DUAL_MODE_ENABLED = False  # dual mode removido — sistema single-mode (1 modo por vez)
    SINAIS_ENABLED = bot_state.sinais_enabled
    SINAIS_PROFILE = bot_state.sinais_profile
    EXEC_MODE = bot_state.operation_mode  # EXEC_MODE espelha o modo único ativo
    _sinais_claude_brain = bot_state.sinais_claude_brain
    _exec_claude_brain = bot_state.exec_claude_brain
    BANCA_USDT = bot_state.banca_usdt
    EXPOSURE_PCT = bot_state.exposure_pct
    TRADES_PER_SESSION = bot_state.trades_per_session
    DAILY_TARGET_USDT = bot_state.daily_target_usdt
    PAPER_TRADING = bot_state.paper_trading
    _mode_started_at = bot_state.mode_started_at
    import notifier
    notifier.RATE_LIMIT_PUBLIC_PER_HOUR = bot_state.sinais_max_hour_public
    notifier.RATE_LIMIT_VIP_PER_HOUR = bot_state.sinais_max_hour_vip
    notifier.set_public_tier_pct(bot_state.public_tier_pct)

async def save_global_state_to_db():
    """Salva o estado atual das globais de volta para o SQLite através de BotState."""
    await bot_state.save_key("current_mode", CURRENT_MODE)
    await bot_state.save_key("operation_mode", OPERATION_MODE)
    await bot_state.save_key("dual_mode_enabled", DUAL_MODE_ENABLED)
    await bot_state.save_key("sinais_enabled", SINAIS_ENABLED)
    await bot_state.save_key("sinais_profile", SINAIS_PROFILE)
    await bot_state.save_key("exec_mode", EXEC_MODE)
    await bot_state.save_key("sinais_brain_enabled", _sinais_claude_brain)
    await bot_state.save_key("exec_brain_enabled", _exec_claude_brain)
    await bot_state.save_key("banca_usdt", BANCA_USDT)
    await bot_state.save_key("exposure_pct", EXPOSURE_PCT)
    await bot_state.save_key("trades_per_session", TRADES_PER_SESSION)
    await bot_state.save_key("daily_target_usdt", DAILY_TARGET_USDT)
    await bot_state.save_key("paper_trading", PAPER_TRADING)
    await bot_state.save_key("mode_started_at", _mode_started_at)

_VALID_EXEC_MODES = ("SUPERVISED", "AUTONOMOUS", "GRID", "SINAIS")

def _resolve_effective_mode() -> str:
    """Fonte ÚNICA do modo efetivo de operação.

    Sistema SINGLE-MODE: roda exatamente UM modo por vez. O modo ativo é sempre
    OPERATION_MODE (SINAIS | SUPERVISED | AUTONOMOUS | GRID). O dual mode foi
    removido — não há mais canal de execução paralelo ao de sinais.
    """
    return OPERATION_MODE

# ── Watchlists por modo ────────────────────────────────────────────────────────
# [] = usa WATCHLIST global do config.py; lista não-vazia restringe ao modo
SUPERVISED_WATCHLIST: list = []    # ex: ["BTCUSDT", "ETHUSDT"]
AUTONOMOUS_WATCHLIST: list = []    # ex: ["SOLUSDT", "XRPUSDT"]
SINAIS_WATCHLIST: list     = []    # ex: ["BTCUSDT", "ETHUSDT"] ou [] = watchlist global

# ── Estado modo SINAIS ────────────────────────────────────────────────────────
_sinais_toggle: bool        = False  # alterna NORMAL/AGGRESSIVE a cada execucao
_sinais_cooldown: dict      = {}     # "BTCUSDT_LONG_15m" → timestamp (cooldown por TF)
_signal_fingerprints: dict  = {}     # fingerprint → timestamp (dedup A+B: price zone)
_sinais_last_direction: dict = {}    # asset → (direction, timestamp) — evita LONG/SHORT
                                      # opostos no mesmo ativo em TFs diferentes minutos depois
_SINAIS_DIRECTION_LOCK_MIN = 20      # janela mínima entre sinais opostos no mesmo ativo
_sinais_session_count: int  = 0      # sinais enviados nesta sessao (reset via /settings/reset_session)
_sinais_empty_cycles: int   = 0      # ciclos consecutivos sem sinais (alerta em ≥2)

# ── Heartbeat dos modos de execução ───────────────────────────────────────────
# Nos modos AUTONOMOUS/SUPERVISED/GRID o bot é silencioso até um sinal passar em
# todos os filtros. O heartbeat manda ao chat pessoal um resumo curto (recebidos/
# enviados/maior bloqueio) para o usuário VER que está vivo e por que não operou.
_exec_heartbeat_ts: dict       = {}    # modo → timestamp do último heartbeat enviado
_EXEC_HEARTBEAT_INTERVAL_S: int = 900  # throttle: no máximo 1 heartbeat a cada 15 min

async def _send_exec_heartbeat(mode: str, recv: int, sent: int, brk: str,
                               min_score, min_rr) -> None:
    """Envia (com throttle) um pulso de vida do modo de execução ao Telegram pessoal."""
    now_ts = time.time()
    if now_ts - _exec_heartbeat_ts.get(mode, 0) < _EXEC_HEARTBEAT_INTERVAL_S:
        return
    _exec_heartbeat_ts[mode] = now_ts
    _icon = {"AUTONOMOUS": "🤖", "SUPERVISED": "👤", "GRID": "⚡"}.get(mode, "🔎")
    try:
        await send_alert(
            f"{_icon} {mode} vivo (perfil {CURRENT_MODE}) — 0 trades neste ciclo.\n"
            f"Sinais recebidos: {recv} · qualificados: {sent}\n"
            f"Filtro: score≥{min_score} · RR≥{min_rr}\n"
            f"Maior bloqueio: {brk}"
        )
    except Exception as _e:
        print(f"[HEARTBEAT] erro ao enviar: {_e}")

# ── Grid Mode ─────────────────────────────────────────────────────────────────
GRID_PAIRS: list        = [
    # Tier 1 — Majors (liquidez premium)
    "BTCUSDT", "ETHUSDT", "SOLUSDT",
    # Tier 2 — Altcoins líquidas com boa vol/ATR
    "HYPEUSDT", "XRPUSDT", "BNBUSDT",
    # Tier 3 — Volatilidade alta para mais oportunidades
    "SUIUSDT", "AVAXUSDT", "DOTUSDT",
]
GRID_PROFIT_TARGET_USDT: float = 0.0   # alvo de lucro por ciclo (0 = ilimitado, altera no dashboard)
GRID_LEVERAGE: int      = 10            # alavancagem no modo grid
GRID_MAX_CONCURRENT: int = 2            # máx trades grid simultâneos
_grid_cycles: dict      = {}            # symbol → ciclos completados
_grid_profit_total: float = 0.0         # lucro acumulado em ciclos grid
_grid_last_cycle_ts: dict = {}          # symbol → timestamp do ultimo ciclo completo
_grid_stale_alerted: dict = {}          # symbol → timestamp do ultimo stale alert enviado
_grid_reinvest_bonus: float = 0.0       # bonus de reinvestimento acumulado (20% de cada ciclo)
GRID_REINVEST_PCT: float = 20.0         # % do lucro do ciclo reinvestido na banca efetiva

# Anti-martingale: reduz tamanho apos sequencia de perdas
_consecutive_losses: int = 0
_consecutive_wins:   int = 0   # FIX: exige 3 wins para reset completo
_ANTI_MARTINGALE = [(0, 1.0), (2, 0.75), (4, 0.50), (6, 0.25)]  # (min_perdas, multiplicador)
_ANTI_MARTINGALE_WINS_TO_RESET = 3  # wins consecutivos necessários para restaurar sizing

# Meta diária — flag para evitar notificacao duplicada
_daily_target_notified: bool = False

# SINAIS — estatisticas semanais
_sinais_weekly: dict = {"total": 0, "alta": 0, "media": 0, "baixa": 0, "reset_date": None}

_daily_pnl = 0.0
_session_trades = 0            # contador de trades abertos nesta sessão

# ── Modo AUTÔNOMO: cadência de entradas + kill-switch -20% ───────────────────
_last_auto_entry_ts: float    = 0.0    # ts da última abertura autônoma (controla cadência por perfil)
_auto_killswitch_tripped: bool = False # True = perdeu AUTO_KILLSWITCH_PCT da banca → para até reset manual
_auto_session_start_banca: float = 0.0 # banca de referência (capturada na 1ª entrada autônoma)
_auto_session_pnl: float      = 0.0    # PnL realizado acumulado da sessão autônoma (base do kill-switch)
_auto_killswitch_notified: bool = False # evita spam do alerta de kill-switch

# ── Segurança/eficácia AUTÔNOMO (2026-06-22, NÃO enviado ao Railway) ──────────
_auto_session_started_ts: float = 0.0  # início da sessão autônoma (janela PAPER warmup)
_asset_last_entry_ts: dict      = {}   # asset -> ts da última abertura (anti-overtrading)
_trades_today: int              = 0    # nº de entradas autônomas hoje (teto diário)
_trades_today_date              = None
# Circuit breaker (perdas seguidas OU erros Binance → autorização Telegram → pausa)
_binance_error_streak: int      = 0
_cb_pending: bool               = False  # True = aguardando /continuar ou /pausar
_cb_deadline: float             = 0.0    # prazo (epoch) para autorizar antes de pausar
_cb_loss_ack_streak: int        = 0      # baseline de perdas já reconhecido (evita re-alertar
                                         # a cada nova perda após o usuário dar /continuar)
# Auto-tune do score: deslocamento aplicado ao min_score conforme a taxa de acerto.
_score_offset: int              = 0
# Auto-tune e rastreio de acerto ESPECÍFICOS do canal SINAIS (não tocam o autônomo).
_sinais_score_offset: int       = 0          # deslocamento no min_score do SINAIS
# Rastreio de resolução dos sinais SINAIS (TP1/SL) é lido direto do banco em
# job_sinais_outcome_watch (signals + telegram_sent), não fica mais em memória —
# assim sobrevive a reinícios do bot local.

# ── Sharpe / Sortino ao vivo ───────────────────────────────────────────────
_session_returns: list  = []      # pnl_pct de cada trade fechado nesta sessao
_sortino_pause:   bool  = False   # True = bot pausado por Sortino baixo
SORTINO_PAUSE_THRESHOLD = 0.8     # pausa se Sortino cair abaixo disto
SORTINO_MIN_TRADES      = 8       # minimo de trades para calcular (evita falso positivo)

# ── Macro Event Guard — PERGUNTA ao usuário (não pausa sozinho) ─────────────
# Comportamento: ao entrar na janela de um evento HIGH, o bot AVISA no Telegram
# com botões Pausar/Continuar. Ele CONTINUA operando normalmente até o usuário
# clicar em "Pausar" (ou enviar /macro pausar). A pausa é só por decisão humana.
_macro_alerted: dict = {}    # eventos já avisados (evita spam)
_macro_user_paused: bool = False  # True SOMENTE se o usuário escolheu pausar


def _macro_event_active() -> bool:
    """True se há evento macro HIGH impact no dia atual (apenas informativo)."""
    try:
        import market_engine
        for ev in (market_engine.get_market_state().get("macro_events", []) or []):
            if ev.get("impact") == "HIGH" and ev.get("days_away", 99) == 0:
                return True
    except Exception:
        pass
    return False


def _macro_pause_active() -> bool:
    """
    True apenas se o USUÁRIO escolheu pausar durante um evento macro.
    Nunca pausa automaticamente — o bot roda até o usuário decidir parar.
    """
    return _macro_user_paused


async def job_macro_guard():
    """
    Roda a cada 30 min. Quando um evento HIGH impact chega (D-1/D-0 ou iminente < 1h),
    AVISA no Telegram com botões Pausar/Continuar — sem pausar sozinho.
    Quando o evento passa, reseta a pausa do usuário.
    """
    global _macro_user_paused, _macro_alerted
    try:
        # Pruning loop: remove entries older than 48 hours (172800 seconds)
        now_ts = time.time()
        _macro_alerted = {k: ts for k, ts in _macro_alerted.items() if now_ts - ts <= 172800}

        import market_engine
        st = market_engine.get_market_state()
        events = st.get("macro_events", []) or []

        # Evento passou? Reseta a pausa do usuário (auto-resume pós-evento)
        if _macro_user_paused and not _macro_event_active():
            _macro_user_paused = False
            await send_alert("✅ Evento macro encerrado — bot retomou operação normal.")
            print("[MACRO-GUARD] Janela encerrada — pausa do usuário resetada.")

        for ev in events:
            if ev.get("impact") != "HIGH":
                continue
            
            name = ev.get("name", "?")
            date_str = ev.get("date", "")
            hours_away = ev.get("hours_away", 999.0)
            days = ev.get("days_away", 99)

            # 1. Alerta iminente (menos de 1 hora)
            if 0.0 <= hours_away <= 1.0:
                key = f"{name}_h1"
                if key not in _macro_alerted:
                    _macro_alerted[key] = time.time()
                    await send_macro_decision(
                        f"🚨 *EVENTO MACRO IMINENTE (EM {hours_away*60:.0f} MIN)*\n"
                        f"`{name}` ({ev.get('type','MACRO')}) — impacto ALTO\n"
                        f"Horário: {date_str}\n\n"
                        f"O bot continua operando normalmente. Deseja *pausar* novos trades ou *prosseguir* operando?"
                    )
                    print(f"[MACRO-GUARD] Alerta iminente enviado: {name} ({hours_away:.2f}h)")
            
            # 2. Alerta prévio (1 dia antes)
            elif days in (0, 1):
                key = f"{name}_d{days}"
                if key not in _macro_alerted:
                    _macro_alerted[key] = time.time()
                    quando = "HOJE" if days == 0 else "AMANHÃ"
                    await send_macro_decision(
                        f"*EVENTO MACRO {quando}*\n"
                        f"`{name}` ({ev.get('type','MACRO')}) — impacto ALTO\n"
                        f"Data: {date_str}\n\n"
                        f"O bot continua operando normalmente. Deseja *pausar* novos trades ou *prosseguir* operando?"
                    )
                    print(f"[MACRO-GUARD] Decisão de D-{days} solicitada: {name}")
    except Exception as e:
        print(f"[MACRO-GUARD] Erro: {e}")


async def _job_correlation_refresh():
    """Recalcula a matriz de correlação dinâmica a cada 30 min (background)."""
    try:
        import correlation_engine as _corr
        from config import WATCHLIST, WATCHLIST_VOLATILE
        assets = list(set(WATCHLIST + WATCHLIST_VOLATILE))
        if _dynamic_universe:
            assets = list(set(assets + _dynamic_universe))
        await _corr.refresh_correlation_matrix(assets)
    except Exception as e:
        print(f"[CORR] refresh erro: {e}")

# ── ML Engine — inicializa em background ──────────────────────────────────
_ml_ready: bool = False

# ── Walk-Forward — resultado da última análise ────────────────────────────
_walk_forward_result: dict = {}
_daily_reset_date = datetime.utcnow().date()
_balance_cache: dict = {}
_signal_cooldown: dict = {}
_executing_assets: set = set()  # assets currently mid-execution (prevents race-condition duplicates)

# Claude Brain — desativado por padrão; ativado via botão /brain/toggle
import claude_brain
_claude_brain_enabled: bool = False
import ml_engine
import dca_engine
import pairs_trading_engine
import monte_carlo
import ws_feed
import regime_detector
import portfolio_risk
import fear_greed as _fear_greed_mod
import walk_forward as _walk_forward_mod
import universe_builder

# Universo dinâmico — atualizado a cada 1h, usado apenas em AGGRESSIVE
_dynamic_universe: list = []


def _get_binance_client_synced():
    """Retorna cliente Binance com offset de tempo e recvWindow largo, usando o cache de get_client()."""
    return get_client()


async def _get_balance() -> float:
    """Retorna o saldo de futuros de forma assíncrona, sem bloquear o event loop."""
    try:
        client = await asyncio.to_thread(_get_binance_client_synced)
        return await asyncio.to_thread(get_futures_balance, client)
    except Exception as e:
        print(f"[BALANCE] Erro: {e}")
        return 0.0


def _refresh_balance_cache():
    """Atualiza o cache de saldo detalhado (sync — APScheduler roda em thread pool)."""
    global _balance_cache
    try:
        client = _get_binance_client_synced()
        _balance_cache = get_account_balance_detail(client)
        print(f"[BALANCE] Cache atualizado: wallet=${_balance_cache.get('wallet_balance',0):.2f}")
    except Exception as e:
        print(f"[BALANCE] Refresh error: {e}")


async def _refresh_balance_cache_async():
    """Wrapper async — garante que a chamada sync não bloqueia o event loop."""
    await asyncio.to_thread(_refresh_balance_cache)


# ── Background Jobs ───────────────────────────────────────────────────────────

def _make_fp(s: dict) -> str:
    """Fingerprint A+B: ativo+direção+timeframe+bucket de preço (±0.3% de precisão)."""
    import math as _m
    entry     = float(s.get("entry", 0))
    direction = str(s.get("direction", "")).split(".")[-1].strip().upper()
    try:
        sig   = _m.floor(_m.log10(abs(entry))) if entry > 0 else 0
        e_key = round(entry, max(0, 2 - sig))
    except Exception:
        e_key = round(entry, 4)
    return f"{s.get('asset','')}_{direction}_{s.get('timeframe','')}_{e_key}"


def _prune_cooldowns():
    """Remove entradas de cooldown com mais de 1h — evita crescimento sem limite."""
    cutoff = time.time() - 3600
    for d in (_signal_cooldown, _sinais_cooldown, _signal_fingerprints):
        stale = [k for k, ts in d.items() if ts < cutoff]
        for k in stale:
            d.pop(k, None)
        # Cap de segurança: se ainda houver muitas entradas, remove as mais antigas
        if len(d) > 500:
            oldest = sorted(d, key=lambda k: d[k])[:len(d) - 500]
            for k in oldest:
                d.pop(k, None)


async def job_scan_market():
    global _latest_signals, _market_cache, _news_cache, _trending_cache, _last_scan_at
    global _last_scan_ts, _last_scan_count
    _prune_cooldowns()
    # Scan de mercado roda sempre; só bloqueia execução de trades (job_auto_trade verifica PAPER_TRADING)
    print(f"[SCHEDULER] Scanning @ {datetime.utcnow().strftime('%H:%M:%S')} | {CURRENT_MODE} | {'PAUSADO' if PAPER_TRADING else 'ATIVO'}")
    try:
        # Paralleliza news + trending + snapshot
        news_task     = asyncio.create_task(get_crypto_news())
        trending_task = asyncio.create_task(get_trending_futures(10))
        snapshot_task = asyncio.create_task(get_market_snapshot())

        news, trending, snapshot = await asyncio.gather(
            news_task, trending_task, snapshot_task, return_exceptions=True
        )
        if isinstance(news, Exception):     news = []
        if isinstance(trending, Exception): trending = []
        if isinstance(snapshot, Exception): snapshot = _market_cache

        _news_cache     = news
        _trending_cache = trending
        _market_cache   = snapshot
        asyncio.create_task(save_snapshot(snapshot))

        # Usa universo dinâmico sempre que disponível (expande cobertura)
        _active_universe = _dynamic_universe if _dynamic_universe else None

        # ── Engine Router: calcula RS Scores antes do scan ────────────────────
        try:
            from signal_filters import refresh_rs_scores as _refresh_rs, session_score_threshold as _sess_thresh
            from config import WATCHLIST, WATCHLIST_VOLATILE
            _all_syms = list(dict.fromkeys(WATCHLIST + WATCHLIST_VOLATILE + (trending or [])))
            _rs_scores = await _refresh_rs(_all_syms)
        except Exception as _e:
            _rs_scores = {}
            print(f"[SCAN] RS Score erro: {_e}")

        # Usa engine router para scan principal (mantém scan_watchlist como fallback)
        try:
            from config import MODE_SETTINGS as _ms
            _tf_list = _ms.get(CURRENT_MODE, _ms["NORMAL"]).get("timeframes", ["15m", "1h", "4h"])
            _scan_syms = list(dict.fromkeys(WATCHLIST + WATCHLIST_VOLATILE + (trending or []) + (_active_universe or [])))
            signals = await engine_router.scan_with_router(
                symbols=_scan_syms, timeframes=_tf_list,
                news_data=news, mode=CURRENT_MODE, rs_scores=_rs_scores,
            )
        except Exception as _e:
            print(f"[SCAN] engine_router fallback para scan_watchlist: {_e}")
            signals = await scan_watchlist(news_data=news, mode=CURRENT_MODE, trending=trending,
                                           dynamic_universe=_active_universe)
        _ts_now = time.time()

        # Salva sinais no DB primeiro para capturar IDs, depois popula cache
        _id_map: dict = {}
        for s in signals[:20]:
            _db_id = await save_signal({
                "asset": s.asset,
                "direction": s.direction.value,
                "entry": s.entry,
                "stop_loss": s.stop_loss,
                "tp1": s.tp1, "tp2": s.tp2, "tp3": s.tp3,
                "rr": s.rr,
                "confidence": s.confidence,
                "score_total": s.score.total,
                "reason": s.reason,
                "timeframe": s.timeframe,
            })
            _id_map[s.asset] = _db_id

        # Cache inclui db_signal_id para rastrear envio ao Telegram
        _latest_signals = [
            {**s.model_dump(), "generated_ts": _ts_now,
             "db_signal_id": _id_map.get(s.asset, 0)}
            for s in signals[:20]
        ]
        _last_scan_at = datetime.now().strftime('%H:%M:%S')
        _last_scan_ts = time.time()
        _last_scan_count = len(signals)
        print(f"[SCHEDULER] {len(signals)} sinais encontrados")
        await log_event("SCAN", f"{len(signals)} sinais encontrados", {"mode": CURRENT_MODE})

        # ── AUTO-EXECUÇÃO ─────────────────────────────────────────────────────
        if signals:
            await job_auto_trade(signals)

    except Exception as e:
        print(f"[SCHEDULER] Erro: {e}")
        await log_event("ERROR", f"Scan error: {e}")


# ── Health Monitor ────────────────────────────────────────────────────────────
# Heartbeat: task contínua que mede o atraso real do event loop. Se algo travar
# o loop (ex.: scan CPU-bound sem yield, como o caso já visto neste bot), o
# atraso entre "deveria ter despertado em 1s" e "despertou de fato" aumenta.
async def _loop_heartbeat():
    global _loop_lag_max_recent
    while True:
        t0 = time.time()
        await asyncio.sleep(1.0)
        drift = (time.time() - t0) - 1.0
        if drift > _loop_lag_max_recent:
            _loop_lag_max_recent = drift


async def job_health_watch():
    """
    Roda a cada 5 min e checa 3 sinais de vida do bot:
      1. Scan de mercado não travou (último scan recente)
      2. Event loop não travou (heartbeat sem atraso grande)
      3. API da Binance está respondendo
    Manda alerta no Telegram só na TRANSIÇÃO saudável→problema (evita spam),
    e manda um alerta de "recuperado" quando volta ao normal.
    """
    global _loop_lag_max_recent, _health_last_ok_ts
    problems = []

    # 1) Scan travado — limiar generoso (3x o intervalo normal de 60s)
    scan_age = time.time() - _last_scan_ts if _last_scan_ts else 1e9
    scan_stuck = scan_age > 180
    if scan_stuck:
        problems.append(f"Scan de mercado parado há {int(scan_age)}s (esperado: a cada ~60s)")

    # 2) Event loop com atraso grande (acumulado desde a última checagem)
    loop_lag = _loop_lag_max_recent
    lag_bad = loop_lag > 5.0
    if lag_bad:
        problems.append(f"Event loop travando — atraso máximo de {loop_lag:.1f}s detectado")
    _loop_lag_max_recent = 0.0  # reset para a próxima janela

    # 3) Binance API respondendo
    binance_down = False
    try:
        t0 = time.time()
        await asyncio.wait_for(get_ticker("BTCUSDT"), timeout=10)
        if time.time() - t0 > 8:
            binance_down = True
            problems.append("API da Binance respondendo, mas muito lenta (>8s)")
    except Exception as e:
        binance_down = True
        problems.append(f"API da Binance não respondeu: {type(e).__name__}")

    # Debounce: só alerta na transição de estado, não a cada checagem
    was_unhealthy = any(_health_state.values())
    _health_state["scan_stuck"]   = scan_stuck
    _health_state["loop_lag"]     = lag_bad
    _health_state["binance_down"] = binance_down
    is_unhealthy = bool(problems)

    if is_unhealthy and not was_unhealthy:
        msg = "🚨 *Alerta de Saúde — Trader 001*\n\n" + "\n".join(f"⚠️ {p}" for p in problems)
        msg += "\n\nVerifique o dashboard ou o servidor."
        print(f"[HEALTH] PROBLEMA: {problems}")
        await send_alert(msg)
    elif was_unhealthy and not is_unhealthy:
        print("[HEALTH] Recuperado")
        await send_alert("✅ *Trader 001* — saúde normalizada, tudo operando normalmente novamente.")
    elif is_unhealthy:
        print(f"[HEALTH] Problema contínuo: {problems}")
    else:
        _health_last_ok_ts = time.time()



def _check_correlation(asset: str, open_assets: set, direction: str = "", open_trades: list = None) -> bool:
    """Retorna False se ja existe trade na MESMA DIRECAO em ativo correlacionado (BTC/ETH/SOL)."""
    if not direction or not open_trades:
        # fallback conservador: bloqueia qualquer correlacionado aberto
        for group in CORRELATION_GROUPS:
            if asset in group and group & open_assets:
                return False
        return True
    dir_up = direction.upper()
    for group in CORRELATION_GROUPS:
        if asset in group:
            for t in open_trades:
                if t["asset"] in group and t["asset"] != asset:
                    t_dir = str(t.get("direction", "")).upper()
                    if ("LONG" in dir_up and "LONG" in t_dir) or ("SHORT" in dir_up and "SHORT" in t_dir):
                        print(f"[CORR] Skip {asset} {dir_up} — {t['asset']} {t_dir} ja aberto (correlacao)")
                        return False
    return True


def _calc_risk_metrics() -> dict:
    """Calcula Sharpe e Sortino da sessao atual. Pausa bot se Sortino < threshold."""
    global _sortino_pause
    rets = _session_returns
    if len(rets) < SORTINO_MIN_TRADES:
        return {"sharpe": None, "sortino": None, "n": len(rets), "paused": False}

    import numpy as np
    arr     = np.array(rets, dtype=float)
    mean_r  = float(np.mean(arr))
    std_r   = float(np.std(arr)) or 1e-9
    sharpe  = round(mean_r / std_r * (252 ** 0.5), 3)

    downside = arr[arr < 0]
    down_std = float(np.std(downside)) if len(downside) > 0 else 1e-9
    sortino  = round(mean_r / down_std * (252 ** 0.5), 3)

    # Pausa automatica se Sortino cair abaixo do threshold
    pause_alert = None
    if sortino < SORTINO_PAUSE_THRESHOLD and not _sortino_pause:
        _sortino_pause = True
        print(f"[RISK] Sortino={sortino:.2f} < {SORTINO_PAUSE_THRESHOLD} — bot PAUSADO automaticamente")
        pause_alert = (
            f"⚠️ *Pausa automática ativada*\n"
            f"Sortino da sessão caiu para `{sortino:.2f}` (limite: {SORTINO_PAUSE_THRESHOLD})\n"
            f"Bot pausado até reset manual ou melhora das métricas."
        )
    elif sortino >= SORTINO_PAUSE_THRESHOLD and _sortino_pause:
        _sortino_pause = False
        print(f"[RISK] Sortino recuperado ({sortino:.2f}) — bot REATIVADO")

    return {"sharpe": sharpe, "sortino": sortino, "n": len(rets), "paused": _sortino_pause, "pause_alert": pause_alert}


def _check_daily_loss() -> bool:
    """Retorna False se limite de perda diária foi atingido."""
    global _daily_pnl, _daily_reset_date, _daily_target_notified
    today = datetime.utcnow().date()
    if today != _daily_reset_date:
        _daily_pnl = 0.0
        _daily_target_notified = False
        _daily_reset_date = today
    # FIX: comparar perda em USDT (não %) contra MAX_DAILY_LOSS_PCT × banca
    _banca_ref = BANCA_USDT if BANCA_USDT > 0 else max(_balance_cache.get("wallet_balance", 100), 100)
    _loss_limit_usdt = _banca_ref * MAX_DAILY_LOSS_PCT / 100
    return _daily_pnl > -_loss_limit_usdt


def _entry_cadence_s() -> int:
    """Espera mínima (s) entre aberturas no modo AUTÔNOMO, por perfil (MODE_SETTINGS).
    0 = consecutivas (Conservador). 120 = Normal (2min). 180 = Agressivo (3min)."""
    cfg = MODE_SETTINGS.get(CURRENT_MODE, MODE_SETTINGS["NORMAL"])
    return int(cfg.get("entry_cadence_s", 0))


def _auto_killswitch_ref_banca() -> float:
    """Banca de referência do kill-switch — inicial da sessão autônoma, com fallback."""
    if _auto_session_start_banca > 0:
        return _auto_session_start_banca
    if BANCA_USDT > 0:
        return BANCA_USDT
    return max(float(_balance_cache.get("wallet_balance", 0) or 0), 0.0)


def _check_auto_killswitch() -> bool:
    """Retorna False se o kill-switch de -AUTO_KILLSWITCH_PCT% da banca disparou.
    Captura a banca inicial na 1ª chamada e trava (até reset manual) ao atingir o limite."""
    global _auto_killswitch_tripped, _auto_session_start_banca
    if _auto_killswitch_tripped:
        return False
    if _auto_session_start_banca <= 0:
        _auto_session_start_banca = _auto_killswitch_ref_banca()
    ref = _auto_session_start_banca
    if ref <= 0:
        return True  # sem banca de referência → não trava (evita falso positivo)
    loss_limit = ref * AUTO_KILLSWITCH_PCT / 100.0
    if _auto_session_pnl <= -loss_limit:
        _auto_killswitch_tripped = True
        return False
    return True


def _register_auto_pnl(pnl: float):
    """Acumula PnL realizado da sessão autônoma (alimenta o kill-switch de -20%)."""
    global _auto_session_pnl
    _auto_session_pnl += float(pnl or 0.0)


def _reset_auto_killswitch() -> dict:
    """Reinicia a sessão autônoma: libera kill-switch, zera PnL/banca de referência
    e renova o orçamento de entradas (_session_trades). Chamado na ativação do modo
    AUTÔNOMO e no reset manual do kill-switch."""
    global _auto_killswitch_tripped, _auto_session_pnl, _auto_session_start_banca
    global _auto_killswitch_notified, _session_trades, _last_auto_entry_ts
    global _auto_session_started_ts, _asset_last_entry_ts
    _auto_killswitch_tripped  = False
    _auto_killswitch_notified = False
    _auto_session_pnl         = 0.0
    _auto_session_start_banca = 0.0
    _session_trades           = 0      # renova orçamento de N entradas da sessão
    _last_auto_entry_ts       = 0.0    # libera a 1ª entrada sem esperar cadência
    _auto_session_started_ts  = time.time()  # inicia janela PAPER warmup
    _asset_last_entry_ts      = {}     # zera cooldown anti-overtrading por ativo
    print("[KILLSWITCH] Sessão autônoma reiniciada (kill-switch liberado, orçamento renovado, warmup iniciado).")
    return {"ok": True, "killswitch": "reset", "session_pnl": 0.0}


# ── Helpers: anti-overtrading + teto diário/exposição ────────────────────────
# (Janela PAPER warm-up REMOVIDA a pedido: o autônomo opera assim que acionado e
#  encontrar entradas conforme o perfil, sem espera.)
def _same_asset_blocked(asset: str, score: float) -> bool:
    """Anti-overtrading: bloqueia reentrada no MESMO ativo dentro de
    SAME_ASSET_COOLDOWN_MIN; passado o cooldown, só libera se sinal 'claro'
    (score >= CLEAR_SIGNAL_MIN_SCORE)."""
    last = _asset_last_entry_ts.get(asset, 0)
    if last <= 0:
        return False
    if (time.time() - last) < SAME_ASSET_COOLDOWN_MIN * 60:
        return True
    return float(score or 0) < CLEAR_SIGNAL_MIN_SCORE


def _trades_today_blocked() -> bool:
    """Teto diário de entradas autônomas (MAX_TRADES_PER_DAY; 0 = sem limite)."""
    global _trades_today, _trades_today_date
    today = datetime.utcnow().date()
    if today != _trades_today_date:
        _trades_today = 0
        _trades_today_date = today
    return MAX_TRADES_PER_DAY > 0 and _trades_today >= MAX_TRADES_PER_DAY


async def _exposure_blocked() -> bool:
    """True se a exposição agregada (notional_total / banca) excede o teto."""
    try:
        open_trades = await get_open_trades()
        notional = sum(float(t.get("size_usdt", 0) or 0) for t in open_trades)
        banca = BANCA_USDT if BANCA_USDT > 0 else max(float(_balance_cache.get("wallet_balance", 0) or 0), 1.0)
        return banca > 0 and (notional / banca) > MAX_TOTAL_EXPOSURE_RATIO
    except Exception:
        return False


# ── Circuit breaker da Binance (erros consecutivos → autorização → pausa) ─────
def _register_binance_ok():
    """Zera o contador de erros consecutivos após uma chamada Binance bem-sucedida."""
    global _binance_error_streak
    _binance_error_streak = 0


async def _cb_arm(reason: str):
    """Arma o circuit breaker: pede autorização no Telegram e inicia o prazo de pausa.
    Não re-arma se já está pendente ou o bot já está pausado."""
    global _cb_pending, _cb_deadline
    if _cb_pending or BOT_PAUSED:
        return
    _cb_pending  = True
    _cb_deadline = time.time() + CB_AUTH_TIMEOUT_S
    await send_alert(
        f"⚠️ *CIRCUIT BREAKER* — {reason}\n"
        f"Responda em até *{CB_AUTH_TIMEOUT_S // 60} min*:\n"
        f"`/continuar` — segue operando\n"
        f"`/pausar` — para agora\n\n"
        f"_Sem resposta → o bot PAUSA TUDO automaticamente._"
    )


async def _maybe_trip_loss_breaker():
    """GATILHO PRINCIPAL: dispara o circuit breaker após CB_LOSS_THRESHOLD trades
    PERDEDORES seguidos. Usa _cb_loss_ack_streak como baseline para só re-alertar a
    cada novo bloco de CB_LOSS_THRESHOLD perdas (e não a cada perda após /continuar).
    Chamado logo após cada incremento de _consecutive_losses no fechamento."""
    global _cb_loss_ack_streak
    if not CIRCUIT_BREAKER_ENABLED:
        return
    # Se o streak de perdas caiu abaixo do baseline (zerado por vitórias), recomeça limpo.
    if _consecutive_losses < _cb_loss_ack_streak:
        _cb_loss_ack_streak = 0
    if _consecutive_losses >= _cb_loss_ack_streak + CB_LOSS_THRESHOLD:
        print(f"[CIRCUIT-BREAKER] {_consecutive_losses} perdas seguidas → pedindo autorização")
        await _cb_arm(f"`{_consecutive_losses}` trades *perdedores* seguidos.")


async def _register_binance_error(ctx: str = ""):
    """GATILHO SECUNDÁRIO: conta erros consecutivos da Binance. Ao atingir
    CB_ERROR_THRESHOLD, dispara o mesmo circuit breaker (falha técnica)."""
    global _binance_error_streak
    if not CIRCUIT_BREAKER_ENABLED:
        return
    _binance_error_streak += 1
    print(f"[CIRCUIT-BREAKER] Erro Binance #{_binance_error_streak} ({ctx})")
    # Gatilho por erros DESLIGADO por padrão (era a mensagem chata/constante no Railway).
    # Conta para log, mas não arma o breaker nem alerta — só se reativado por config.
    if CB_ERROR_TRIGGER_ENABLED and _binance_error_streak >= CB_ERROR_THRESHOLD:
        await _cb_arm(f"`{_binance_error_streak}` erros seguidos da Binance.")


def _cb_resume() -> str:
    """Libera o circuit breaker (resposta /continuar). Reconhece o nível atual de perdas
    como baseline para não re-alertar imediatamente na próxima perda."""
    global _cb_pending, _binance_error_streak, _cb_deadline, _cb_loss_ack_streak
    _cb_pending = False
    _binance_error_streak = 0
    _cb_loss_ack_streak = _consecutive_losses   # baseline = perdas já reconhecidas
    _cb_deadline = 0.0
    print("[CIRCUIT-BREAKER] Autorizado a continuar pelo usuário.")
    return "✅ Circuit breaker liberado — seguindo operando."


async def job_circuit_breaker_watch():
    """Se o circuit breaker está pendente e o prazo de autorização expirou,
    pausa TUDO (BOT_PAUSED=True). Roda a cada 30s."""
    global _cb_pending, BOT_PAUSED
    if not _cb_pending:
        return
    if time.time() >= _cb_deadline:
        BOT_PAUSED  = True
        _cb_pending = False
        print("[CIRCUIT-BREAKER] Timeout sem resposta → BOT_PAUSED=True")
        await send_alert(
            "🛑 *BOT PAUSADO* — sem resposta ao circuit breaker em "
            f"{CB_AUTH_TIMEOUT_S // 60} min.\nUse `/continuar` (ou `/bot/resume`) para retomar."
        )


# ── #12 Auto-tune do min_score por taxa de acerto recente ────────────────────
def _eff_min_score(mode_cfg: dict) -> int:
    """min_score efetivo = base do perfil + deslocamento do auto-tune (com piso)."""
    base = int(mode_cfg.get("min_score", 70))
    return max(40, base + _score_offset)


async def job_autotune_score():
    """Ajusta _score_offset conforme a taxa de acerto dos últimos N trades fechados:
    win-rate baixa → corte MAIS alto (seletivo); win-rate alta → corte mais baixo.
    Conservador e com limites (AUTOTUNE_MAX_TIGHTEN / AUTOTUNE_MAX_LOOSEN)."""
    global _score_offset
    if not AUTOTUNE_SCORE_ENABLED:
        return
    try:
        stats = await get_recent_trade_stats(AUTOTUNE_LOOKBACK)
        if stats["n"] < 8:           # amostra pequena → não mexe
            return
        wr  = stats["win_rate"]
        old = _score_offset
        if   wr < 35:  _score_offset = min(AUTOTUNE_MAX_TIGHTEN,  _score_offset + 2)
        elif wr < 45:  _score_offset = min(AUTOTUNE_MAX_TIGHTEN,  _score_offset + 1)
        elif wr > 60:  _score_offset = max(-AUTOTUNE_MAX_LOOSEN,  _score_offset - 1)
        else:          _score_offset = 0 if _score_offset == 0 else (_score_offset - (1 if _score_offset > 0 else -1))
        if _score_offset != old:
            print(f"[AUTOTUNE] win-rate {wr:.0f}% ({stats['n']} trades) → score_offset {old:+d} -> {_score_offset:+d}")
    except Exception as e:
        print(f"[AUTOTUNE] Erro: {e}")


# ── SINAIS: rastreio de resultado dos sinais transmitidos + auto-tune ─────────
async def job_sinais_outcome_watch():
    """Resolve os sinais SINAIS transmitidos comparando os candles após a entrada
    com TP1/SL: WIN se o preço tocou o alvo primeiro, LOSS se tocou o stop. Após
    SINAIS_OUTCOME_MAX_AGE_H sem resolver, fecha como TIMEOUT pelo preço atual.
    Alimenta signal_outcomes → base do win-rate medível, do auto-tune do SINAIS e
    dos KPIs do dashboard. Conservador: na dúvida (TP e SL no mesmo candle) conta
    como LOSS.
    Lê os sinais pendentes direto do banco (tabela signals + telegram_sent) em vez
    de uma lista em memória — assim o rastreio sobrevive a reinícios do bot local
    (antes, reiniciar o processo zerava a lista e signal_outcomes nunca enchia)."""
    if not SINAIS_OUTCOME_TRACKING:
        return
    import pandas as pd
    from klines_cache import get_klines_cached as _gkl
    from database import get_unresolved_sinais_signals
    _max_age_fetch = max([*SINAIS_OUTCOME_MAX_AGE_H_BY_TF.values(), SINAIS_OUTCOME_MAX_AGE_H])
    pending = await get_unresolved_sinais_signals(_max_age_fetch)
    if not pending:
        return
    _now = time.time()
    for it in pending:
        try:
            asset, dirx = it["asset"], it["direction"]
            entry, sl, tp = it["entry"], it["sl"], it["tp"]
            is_long = "LONG" in dirx
            _ts = datetime.fromisoformat(it["timestamp"]).timestamp()
            kl = await _gkl(asset, it["timeframe"], limit=120)
            hit_tp = hit_sl = False
            if kl is not None and len(kl) > 0:
                # só candles a partir da entrada (índice = timestamp datetime UTC)
                _entry_dt = pd.Timestamp(_ts, unit="s")
                _sub = kl[kl.index >= (_entry_dt - pd.Timedelta(seconds=60))]
                for hi, lo in zip(_sub["high"].values, _sub["low"].values):
                    hi, lo = float(hi), float(lo)
                    if is_long:
                        if hi >= tp: hit_tp = True
                        if lo <= sl: hit_sl = True
                    else:
                        if lo <= tp: hit_tp = True
                        if hi >= sl: hit_sl = True
                    if hit_tp or hit_sl:
                        break
            outcome = None
            exit_px = entry
            if hit_sl:                       # conservador: SL tem prioridade
                outcome, exit_px = "LOSS", sl
            elif hit_tp:
                outcome, exit_px = "WIN", tp
            elif _now - _ts > SINAIS_OUTCOME_MAX_AGE_H_BY_TF.get(it["timeframe"], SINAIS_OUTCOME_MAX_AGE_H) * 3600:
                # timeout: resolve pelo último preço conhecido
                try:
                    exit_px = float(kl["close"].iloc[-1]) if kl is not None and len(kl) else entry
                except Exception:
                    exit_px = entry
                outcome = "TIMEOUT"
            if outcome is None:
                continue                      # ainda em aberto — tentativa seguinte
            pnl_pct = ((exit_px - entry) / entry * 100.0) if is_long else ((entry - exit_px) / entry * 100.0)
            asyncio.create_task(record_signal_outcome(
                int(it.get("db_id", 0) or 0), asset, dirx, it["timeframe"],
                entry, exit_px, round(pnl_pct, 3), outcome,
                tags=it.get("tags", ""), rsi_val=0.0,
            ))
            print(f"[SINAIS-OUTCOME] {asset} {dirx} {it['timeframe']} → {outcome} ({pnl_pct:+.2f}%)")
        except Exception as e:
            # erro pontual: sinal permanece pendente no banco, nova tentativa no próximo ciclo
            print(f"[SINAIS-OUTCOME] erro {it.get('asset')}: {e}")


async def job_sinais_autotune():
    """Ajusta _sinais_score_offset conforme o acerto MEDIDO dos sinais SINAIS
    (signal_outcomes). win-rate baixa → corte mais alto; alta → corte mais baixo.
    Espelha o auto-tune dos trades, mas alimentado pelos resultados dos sinais."""
    global _sinais_score_offset
    if not SINAIS_AUTOTUNE_ENABLED:
        return
    try:
        stats = await get_recent_signal_stats(SINAIS_AUTOTUNE_LOOKBACK)
        if stats["n"] < 10:              # amostra pequena → não mexe
            return
        wr  = stats["win_rate"]
        old = _sinais_score_offset
        if   wr < 35:  _sinais_score_offset = min(SINAIS_AUTOTUNE_MAX_TIGHTEN, _sinais_score_offset + 2)
        elif wr < 45:  _sinais_score_offset = min(SINAIS_AUTOTUNE_MAX_TIGHTEN, _sinais_score_offset + 1)
        elif wr > 60:  _sinais_score_offset = max(-SINAIS_AUTOTUNE_MAX_LOOSEN, _sinais_score_offset - 1)
        else:          _sinais_score_offset = 0 if _sinais_score_offset == 0 else (_sinais_score_offset - (1 if _sinais_score_offset > 0 else -1))
        if _sinais_score_offset != old:
            print(f"[SINAIS-AUTOTUNE] win-rate {wr:.0f}% ({stats['n']} sinais) → offset {old:+d} -> {_sinais_score_offset:+d}")
    except Exception as e:
        print(f"[SINAIS-AUTOTUNE] Erro: {e}")


async def job_db_prune():
    """Retenção do banco (diário 04:10 BRT): apaga linhas >30 dias das tabelas de
    alto volume (signals/market_snapshots/logs/telegram_sent). signal_outcomes é
    preservada para sempre (base do auto-tune). Evita o DB crescer sem limite
    (estava em 42MB com 116k signals em 02/07)."""
    try:
        from database import prune_old_rows
        deleted = await prune_old_rows(days=30)
        total = sum(deleted.values())
        if total:
            print(f"[DB-PRUNE] {total} linhas removidas (>30d): {deleted}")
    except Exception as e:
        print(f"[DB-PRUNE] Erro: {e}")


def _build_signal_from_dict(signal_dict: dict):
    """Reconstrói TradeSignal a partir de dict (usado em _execute_trade)."""
    from models import TradeSignal, SignalScore, Direction as Dir
    score_data = signal_dict.get("score", {})
    score_obj  = SignalScore(**score_data) if isinstance(score_data, dict) else score_data
    dir_val    = signal_dict.get("direction", "LONG")
    direction  = Dir(dir_val) if isinstance(dir_val, str) else dir_val
    return TradeSignal(
        asset=signal_dict["asset"],
        direction=direction,
        entry=signal_dict["entry"],
        stop_loss=signal_dict["stop_loss"],
        tp1=signal_dict["tp1"], tp2=signal_dict["tp2"], tp3=signal_dict["tp3"],
        rr=signal_dict["rr"],
        confidence=signal_dict.get("confidence", 80),
        reason=signal_dict.get("reason", ""),
        score=score_obj,
        timeframe=signal_dict.get("timeframe", "15m"),
    )


async def _execute_trade(signal_dict: dict):
    """
    Wrapper de execução com guards completos:
    - Impede race conditions (executing_assets)
    - Respeita TRADES_PER_SESSION (max trades abertos)
    - Bloqueia mesma direção no mesmo ativo
    - Permite hedge (LONG + SHORT no mesmo ativo, máx 2)
    """
    asset     = signal_dict.get("asset", "")
    direction = str(signal_dict.get("direction", "")).upper()

    # Guard: impede execução concorrente para o mesmo ativo
    if asset in _executing_assets:
        print(f"[GUARD] {asset} ja em execucao, sinal ignorado.")
        return
    _executing_assets.add(asset)

    try:
        _existing = await get_open_trades()

        # Guard 1: TRADES_PER_SESSION = máx trades ABERTOS simultaneamente
        if TRADES_PER_SESSION > 0 and len(_existing) >= TRADES_PER_SESSION:
            print(f"[GUARD] Limite de {TRADES_PER_SESSION} trades atingido ({len(_existing)} abertos) — sinal {asset} ignorado")
            return

        # Guard 2: bloqueia se ja tem a MESMA DIREÇÃO aberta no mesmo ativo
        if any(t["asset"] == asset and str(t.get("direction", "")).upper() == direction for t in _existing):
            print(f"[GUARD] {asset} {direction} ja tem trade aberto, sinal ignorado.")
            return

        # Guard 3: max 2 trades por ativo (LONG + SHORT — modo hedge)
        if sum(1 for t in _existing if t["asset"] == asset) >= 2:
            print(f"[GUARD] {asset} ja tem 2 trades abertos (LONG+SHORT), sinal ignorado.")
            return

        await _execute_trade_inner(signal_dict)
    finally:
        _executing_assets.discard(asset)


async def _execute_trade_inner(signal_dict: dict):
    global BANCA_USDT, OPERATION_MODE, _session_trades, _consecutive_losses

    signal = _build_signal_from_dict(signal_dict)
    from risk_manager import get_leverage as _get_lev
    user_leverage = int(signal_dict.get("leverage") or _get_lev(signal.asset))

    # Teto de alavancagem do perfil ativo (CONSERVATIVE = 10x)
    _lev_cap = MODE_SETTINGS.get(CURRENT_MODE, MODE_SETTINGS["NORMAL"]).get("leverage_cap")
    if _lev_cap:
        user_leverage = min(user_leverage, int(_lev_cap))

    # Redução de alavancagem adaptativa baseada na exposição total da carteira (Recomendação C2 da Auditoria)
    try:
        open_trades = await get_open_trades()
        notional_open = sum(float(t.get("size_usdt", 0)) for t in open_trades)
        effective_banca = BANCA_USDT if BANCA_USDT > 0 else (await _get_balance() or 10)
        
        if effective_banca > 0:
            exposure_ratio = notional_open / effective_banca
            reduction_factor = max(0.2, min(1.0, 1.0 - exposure_ratio))
            if reduction_factor < 1.0:
                old_lev = user_leverage
                user_leverage = max(1, int(user_leverage * reduction_factor))
                print(f"[LEVERAGE ADAPTIVE] Reducao por exposicao ({exposure_ratio:.1%} exp): {old_lev}x -> {user_leverage}x")
    except Exception as _le_ex:
        print(f"[LEVERAGE ADAPTIVE] Erro ao calcular reducao: {_le_ex}")

    # ── #3 Alavancagem por VOLATILIDADE (ATR%) — reduz em ativos mais voláteis ──
    if LEVERAGE_BY_VOLATILITY:
        try:
            _entry_px = float(signal.entry or 0)
            _atr_abs  = float(signal_dict.get("atr") or abs(_entry_px - float(signal.stop_loss)))
            _atr_pct  = (_atr_abs / _entry_px * 100.0) if _entry_px > 0 else 0.0
            if _atr_pct > ATR_PCT_REF and ATR_PCT_REF > 0:
                _vol_factor = ATR_PCT_REF / _atr_pct
                _old = user_leverage
                user_leverage = max(int(LEVERAGE_VOL_FLOOR), int(user_leverage * _vol_factor))
                if user_leverage < _old:
                    print(f"[LEVERAGE VOL] {signal.asset} ATR%={_atr_pct:.2f} > {ATR_PCT_REF} → {_old}x -> {user_leverage}x")
        except Exception as _ve_ex:
            print(f"[LEVERAGE VOL] Erro: {_ve_ex}")

    # ── Sizing: Usa Sizing baseado em Risco com Margin Cap quando banca definida ──
    if BANCA_USDT > 0:
        from config import MAX_OPEN_TRADES as _MAX
        n = TRADES_PER_SESSION if TRADES_PER_SESSION > 0 else _MAX
        max_margin_per_trade = round(BANCA_USDT / n, 2)
        
        # Anti-martingale: reduz margem máxima após sequência de perdas
        anti_mult = 1.0
        for threshold, mult in sorted(_ANTI_MARTINGALE, reverse=True):
            if _consecutive_losses >= threshold:
                anti_mult = mult
                break
        if anti_mult < 1.0:
            max_margin_per_trade = round(max_margin_per_trade * anti_mult, 2)
            print(f"[ANTI-MARTINGALE] {_consecutive_losses} perdas consecutivas → margem máxima x{anti_mult} = ${max_margin_per_trade:.2f}")

        # Risco em USDT (ex: 1% de BANCA_USDT)
        _profile_risk_pct = MODE_SETTINGS.get(CURRENT_MODE, MODE_SETTINGS["NORMAL"]).get("risk_pct", 1.0)
        risk_usdt = BANCA_USDT * _profile_risk_pct / 100
        
        _size_mult = float(signal_dict.get("size_multiplier", 1.0))
        if _size_mult != 1.0:
            risk_usdt *= _size_mult
            print(f"[CLAUDE BRAIN] Sizing multiplier aplicado: {_size_mult}x -> novo risco = ${risk_usdt:.2f}")

        # Distância percentual do Stop Loss
        sl_distance_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100
        if sl_distance_pct > 0:
            notional = (risk_usdt / sl_distance_pct * 100)
        else:
            notional = BANCA_USDT * 0.1 * user_leverage * _size_mult  # fallback se SL inválido
            
        margin_needed = notional / user_leverage
        
        # Margin Cap: se a margem necessária exceder a margem máxima permitida por trade, limita e reduz notional
        if margin_needed > max_margin_per_trade:
            margin = max_margin_per_trade
            notional = margin * user_leverage
            print(f"[SIZING] {signal.asset} limitado pela margem cap: req=${margin_needed:.2f} > max=${max_margin_per_trade:.2f} -> notional=${notional:.2f}")
        else:
            margin = margin_needed
            print(f"[SIZING] {signal.asset} sizing por risco: risco_usd=${risk_usdt:.2f} ({_profile_risk_pct}%) | SL_dist={sl_distance_pct:.2f}% -> notional=${notional:.2f} | margem=${margin:.2f}")
            
        notional = round(notional, 2)
        margin = round(margin, 2)

        from models import ActiveTrade as _AT
        import uuid as _uuid
        trade = _AT(
            id=str(_uuid.uuid4())[:8],
            asset=signal.asset,
            direction=signal.direction,
            entry_price=signal.entry,
            current_price=signal.entry,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1, tp2=signal.tp2, tp3=signal.tp3,
            rr=signal.rr,
            leverage=user_leverage,
            size_usdt=notional,
            reason=signal.reason,
            confidence=signal.confidence,
        )
        origin = (
            f"Banca ${BANCA_USDT:.2f} / {n} trades = ${max_margin_per_trade:.2f} margem"
            f" | Nocional ${notional:.2f} | {user_leverage}x"
        )
        print(f"[SIZING] {signal.asset}: margem=${max_margin_per_trade:.2f} nocional=${notional:.2f} (banca ${BANCA_USDT:.2f}/{n})")
    else:
        # Sem banca definida: usa risk_pct do PERFIL ATIVO (CONSERVATIVE=0.5%, NORMAL=1%, AGGRESSIVE=1.5%)
        balance = await _get_balance() or 100
        _profile_risk = MODE_SETTINGS.get(CURRENT_MODE, MODE_SETTINGS["NORMAL"]).get("risk_pct")
        
        _size_mult = float(signal_dict.get("size_multiplier", 1.0))
        if _size_mult != 1.0:
            _profile_risk *= _size_mult
            
        trade = create_trade(signal, balance, risk_pct=_profile_risk)
        trade.leverage = user_leverage
        origin = f"Risk-based ${trade.size_usdt:.2f} nocional | {user_leverage}x"

    # Ajuste de tamanho se DCA estiver ativo
    is_dca_active = dca_engine.is_dca_enabled() and signal_dict.get("trade_type") != "GRID"
    if is_dca_active:
        if BANCA_USDT > 0:
            banca_alocada = margin
        else:
            banca_alocada = trade.size_usdt / trade.leverage
            
        trade_atr = float(signal_dict.get("atr") or (abs(trade.entry_price - trade.stop_loss) / 1.5))
        
        # Inicializa a posição DCA
        dca_engine.open_dca_position(trade.asset, direction, trade.entry_price, trade_atr, banca_alocada)
        
        # O trade inicial (Nível 0) na exchange terá apenas 30% da margem/tamanho total
        trade.size_usdt = round(trade.size_usdt * 0.30, 2)
        
        # Recalcula stop_loss e tp1/tp2 iniciais baseados no dca_engine
        dca_pos = dca_engine._dca_positions.get(trade.asset)
        if dca_pos:
            trade.stop_loss = dca_pos.stop_loss
            trade.tp1 = dca_pos.take_profit
            trade.tp2 = dca_pos.take_profit
            trade.tp3 = dca_pos.take_profit
        origin += " [DCA Nível 0 - 30%]"

    # ── Paper Trading mode — simula sem chamar Binance ───────────────────────
    if PAPER_TRADING:
        result = {"status": "SIMULATED", "msg": "PAPER"}
    else:
        # open_trade e sync (python-binance) — roda em thread p/ nao travar o event loop
        result = await asyncio.to_thread(open_trade, trade)

    if result["status"] in ("OK", "SIMULATED"):
        trade_dict = trade.model_dump()
        trade_dict["opened_at"] = trade.opened_at.isoformat()
        trade_dict["score_json"] = json.dumps(signal.score.model_dump())
        trade_dict["timeframe"]  = signal.timeframe
        trade_dict["trade_type"] = signal_dict.get("trade_type", "DAY_TRADE")
        exec_status = str(result.get("status", "")).upper()
        is_simulated = exec_status == "SIMULATED"
        trade_dict["paper"] = PAPER_TRADING or is_simulated
        trade_dict["execution_status"] = exec_status
        trade_dict["order_id"] = result.get("order_id")
        trade_dict["mode"]  = OPERATION_MODE   # tag de modo (#11): separa AUTÔNOMO/SINAIS/etc.
        await save_trade(trade_dict)
        _active_trades_cache[trade.id] = trade_dict
        _session_trades += 1  # conta trades abertos (funciona em todos os modos)
        paper_tag = " [PAPER]" if trade_dict["paper"] else ""
        await send_trade_opened(trade_dict, OPERATION_MODE)
        print(f"[TRADE]{paper_tag} Aberto {signal.direction.value} {signal.asset} | {origin} | sessao={_session_trades}/{TRADES_PER_SESSION}")
        await log_event("TRADE_OPEN", f"{signal.direction.value} {signal.asset}", trade_dict)
    else:
        await send_alert(f"Falha ao abrir {signal.asset}: {result.get('msg','?')}")


async def _reject_trade(signal_dict: dict):
    print(f"[TRADE] Rejeitado pelo usuario: {signal_dict.get('asset')}")


async def _telegram_close_trade(trade_id: str, msg_id: int = None, chat_id: int = None):
    """Fecha trade via botão do Telegram — chamado pelo notifier."""
    from models import Direction as Dir
    trades  = await get_open_trades()
    target  = next((t for t in trades if t["id"] == trade_id), None)
    if not target:
        await send_alert(f"Trade {trade_id} nao encontrado para fechar")
        return
    # Cancela ordens e fecha posicao (sync em thread — nao trava o event loop)
    if not PAPER_TRADING:
        def _do_close():
            client   = _get_binance_client_synced()
            dir_val  = Dir(target["direction"])
            close_side = "SELL" if dir_val == Dir.LONG else "BUY"
            pos_side   = "LONG" if dir_val == Dir.LONG else "SHORT"
            from binance_executor import _is_hedge_mode, get_position_qty
            hedge = _is_hedge_mode(client)
            # Cancela todas as ordens pendentes (TPs + SL)
            client.futures_cancel_all_open_orders(symbol=target["asset"])
            # Fecha posicao a mercado com a qty REAL da posicao
            qty = get_position_qty(client, target["asset"], pos_side if hedge else "BOTH")
            if qty > 0:
                kwargs = {"positionSide": pos_side} if hedge else {"reduceOnly": True}
                client.futures_create_order(
                    symbol=target["asset"], side=close_side,
                    type="MARKET", quantity=qty, **kwargs
                )
        try:
            await asyncio.to_thread(_do_close)
        except Exception as e:
            print(f"[TELEGRAM CLOSE] Erro ao fechar {trade_id}: {e}")
    target["status"]    = "CLOSED"
    target["closed_at"] = datetime.utcnow().isoformat()
    await save_trade(target)
    _active_trades_cache.pop(trade_id, None)
    await send_trade_closed(target, "Fechado via Telegram")
    if (target.get("pnl_usdt") or 0) > 0:
        _eff_mode = _resolve_effective_mode()
        asyncio.create_task(send_social_proof(target, _eff_mode))
    print(f"[TELEGRAM] Trade {trade_id} fechado via botao Telegram")


async def job_auto_trade(signals: list):
    """
    Filtra sinais e age conforme OPERATION_MODE.
    SUPERVISED  → envia ao Telegram aguardando aprovação
    AUTONOMOUS  → executa + notifica Telegram com mesmo formato do supervisionado
    Respeita: limite de trades/sessão, objetivo diário, cooldown de 5min.
    """
    global _daily_pnl, _session_trades, _last_auto_entry_ts, _auto_killswitch_notified, _trades_today

    _effective_mode = _resolve_effective_mode()
    if _effective_mode in ("SINAIS", "GRID"):
        return

    # Bot pausado (BOT_PAUSED via /bot/pause): nao abre novos trades.
    # OBS: PAPER_TRADING NÃO pausa mais — em paper o fluxo segue e abre trades SIMULADOS.
    if BOT_PAUSED:
        print("[AUTO] Bot pausado — sem novos trades.")
        return

    # Pausa automatica por Sortino baixo
    if _sortino_pause:
        print("[AUTO] Bot pausado por Sortino baixo — aguardando recuperacao.")
        return

    # Pausa por DECISÃO do usuário durante evento macro (botão Pausar no Telegram)
    if _macro_pause_active():
        print("[AUTO] Trades pausados pelo usuário durante evento macro.")
        return

    # ── Verificações de limites ───────────────────────────────────────────────
    if not _check_daily_loss():
        await send_alert(f"🛑 Limite de perda diária atingido ({MAX_DAILY_LOSS_PCT}%). Pausado.")
        return

    # Kill-switch -20% da banca (modo AUTÔNOMO): para de abrir até reset manual
    if _effective_mode == "AUTONOMOUS" and not _check_auto_killswitch():
        if not _auto_killswitch_notified:
            _auto_killswitch_notified = True
            _ref = _auto_killswitch_ref_banca()
            await send_alert(
                f"🛑 *KILL-SWITCH -{AUTO_KILLSWITCH_PCT:.0f}%* — sessão autônoma parada.\n"
                f"Perda acumulada: `${_auto_session_pnl:.2f}` sobre banca `${_ref:.2f}`.\n"
                f"Posições abertas seguem até SL/TP. Para retomar: `/killswitch reset`."
            )
        print(f"[AUTO] Kill-switch -{AUTO_KILLSWITCH_PCT:.0f}% ativo — sem novas entradas.")
        return

    # Circuit breaker pendente: aguardando /continuar ou /pausar — não abre nada.
    if _cb_pending:
        print("[AUTO] Circuit breaker pendente — aguardando autorização, sem novas entradas.")
        return

    # Teto de exposição agregada (notional_total / banca).
    if _effective_mode == "AUTONOMOUS" and await _exposure_blocked():
        print(f"[AUTO] Exposição agregada > {MAX_TOTAL_EXPOSURE_RATIO:.1f}x banca — sem novas entradas.")
        return

    # Objetivo diário atingido?
    if DAILY_TARGET_USDT > 0 and _daily_pnl >= DAILY_TARGET_USDT:
        print(f"[AUTO] 🎯 Objetivo diário atingido! ${_daily_pnl:.2f} >= ${DAILY_TARGET_USDT:.2f}")
        return

    # Trades por sessão
    if TRADES_PER_SESSION > 0 and _session_trades >= TRADES_PER_SESSION:
        print(f"[AUTO] Limite de trades/sessão atingido ({TRADES_PER_SESSION}). Aguardando próxima sessão.")
        return

    if not await can_open_trade(_active_max_open()):
        print("[AUTO] Máximo de trades abertos atingido.")
        return

    balance = await _get_balance()
    effective_banca = BANCA_USDT if BANCA_USDT > 0 else balance
    if effective_banca < 5:
        print(f"[AUTO] Banca insuficiente: ${effective_banca:.2f}")
        return

    open_trades  = await get_open_trades()
    # Inclui ativos com aprovação pendente no Telegram — evita duplicar sinal enquanto aguarda
    open_assets  = {t["asset"] for t in open_trades} | get_pending_assets() | _executing_assets
    mode_cfg     = MODE_SETTINGS.get(CURRENT_MODE, MODE_SETTINGS["NORMAL"])
    now_ts       = time.time()
    from risk_manager import get_leverage
    sent = 0

    # ── Cadência de entradas (modo AUTÔNOMO) ──────────────────────────────────
    # Conservador (0s) = entradas consecutivas. Normal (120s) e Agressivo (180s)
    # = 1 entrada por janela. Bloqueia abrir se ainda dentro da janela.
    _cadence_s     = _entry_cadence_s() if _effective_mode == "AUTONOMOUS" else 0
    _cadence_block = _cadence_s > 0 and (now_ts - _last_auto_entry_ts) < _cadence_s
    _cadence_break = False
    if _cadence_block:
        _wait = int(_cadence_s - (now_ts - _last_auto_entry_ts))
        print(f"[AUTO-CADÊNCIA] {CURRENT_MODE}: aguardando {_wait}s p/ próxima entrada (janela {_cadence_s}s).")

    # FIX #2 — BTC Veto: aplica em AUTÔNOMO e SUPERVISED (igual ao SINAIS)
    from signal_filters import refresh_btc_veto as _refresh_btc_veto, btc_veto_passes as _btc_veto_passes
    _btc_veto_ctx = await _refresh_btc_veto()
    if _btc_veto_ctx.get("block_long") or _btc_veto_ctx.get("block_short"):
        _chg45 = _btc_veto_ctx.get("change_45m", 0)
        _blocked_dir = "LONGs" if _btc_veto_ctx["block_long"] else "SHORTs"
        print(f"[AUTO-VETO] BTC {_chg45:+.2f}% 45m — bloqueando {_blocked_dir} neste ciclo")

    # FIX #8 — Staleness Decay: importa para uso por sinal
    from signal_filters import apply_staleness_decay as _staleness_decay
    _STALE_MAX_MIN = 15 if CURRENT_MODE == "NORMAL" else 8  # afrouxado: NORMAL 15min | demais 8min (era 10/5)

    # FIX #9 — Structural Tag obrigatória em AUTÔNOMO NORMAL
    from signal_filters import has_structural_tag as _has_struct_tag

    # Aplica watchlist específica do modo se configurada
    _mode_wl = None
    if _effective_mode == "SUPERVISED" and SUPERVISED_WATCHLIST:
        _mode_wl = {s.upper() for s in SUPERVISED_WATCHLIST}
    elif _effective_mode == "AUTONOMOUS" and AUTONOMOUS_WATCHLIST:
        _mode_wl = {s.upper() for s in AUTONOMOUS_WATCHLIST}

    # Diagnóstico: conta cada motivo de exclusão para o resumo final (observabilidade)
    _recv = len(signals)
    _blocks: dict = {}
    def _blk(reason: str):
        _blocks[reason] = _blocks.get(reason, 0) + 1

    for signal in signals:
        # Revalida limites a cada iteração
        if TRADES_PER_SESSION > 0 and _session_trades >= TRADES_PER_SESSION:
            _blk(f"limite_trades_sessao({TRADES_PER_SESSION})")
            break
        if not await can_open_trade(mode_cfg.get("max_open_trades", 5)):
            _blk("max_trades_abertos")
            break
        if signal.asset in open_assets:
            _blk("ja_aberto/pendente")
            continue
        if _mode_wl and signal.asset not in _mode_wl:
            _blk("fora_watchlist_modo")
            continue  # Fora da watchlist do modo atual
        _min_score_eff = _eff_min_score(mode_cfg)
        if signal.confidence < _min_score_eff or signal.rr < mode_cfg["min_rr"]:
            _blk(f"score/rr<min({_min_score_eff}/{mode_cfg['min_rr']})")
            continue
        # MACRO RISK-OFF: com BTC caindo forte (<= -1.5% em 1h), só aceita pares premium
        # (BTC/ETH/SOL). Bloqueia scalp de low-cap, que é triturado quando o mercado desaba.
        _btc_chg_1h = float(_market_cache.get("btc_change_1h", 0) or 0)
        if _btc_chg_1h <= -1.5:
            _base_sym = signal.asset.upper().replace("USDT", "").replace("USD", "")
            if _base_sym not in ("BTC", "ETH", "SOL"):
                _blk(f"risk_off_macro(BTC {_btc_chg_1h:.1f}%/1h)")
                continue
        if not _check_correlation(signal.asset, open_assets, signal.direction.value, open_trades):
            _blk("correlacao_estatica")
            continue
        # Correlação DINÂMICA: bloqueia ativos que se movem juntos na mesma direção
        try:
            import correlation_engine as _corr
            _hi_corr, _corr_reason = _corr.is_highly_correlated(
                signal.asset, open_assets, signal.direction.value, open_trades
            )
            if _hi_corr:
                print(f"[CORR-DYN] Skip {signal.asset} — {_corr_reason}")
                _blk("correlacao_dinamica")
                continue
        except Exception:
            pass
        # Cooldown afrouxado: 60s por ativo+direção (era 90s)
        # NOTA: o timestamp é marcado SÓ quando o sinal qualifica e é enviado (mais
        # abaixo, após todos os filtros). Assim sinais reprovados por vol_spike/portfolio
        # voltam a ser reavaliados no próximo ciclo em vez de ficarem presos no cooldown.
        cooldown_key = f"{signal.asset}_{signal.direction.value}"
        if now_ts - _signal_cooldown.get(cooldown_key, 0) < 60:
            _blk("cooldown_60s")
            continue

        # FIX #2 — BTC Veto por direção do sinal
        _sig_as_dict = {"direction": signal.direction.value}
        if not _btc_veto_passes(_sig_as_dict, _btc_veto_ctx):
            print(f"[AUTO-VETO] {signal.asset} {signal.direction.value} bloqueado por BTC veto")
            _blk("btc_veto")
            continue

        # FIX #8 — Staleness Decay: rejeita sinais velhos
        # Usa signal.timestamp (campo real do model TradeSignal, UTC).
        try:
            _sig_ts = getattr(signal, "timestamp", None)
            if _sig_ts is not None:
                _age_min = (datetime.utcnow() - _sig_ts).total_seconds() / 60.0
                if _age_min > _STALE_MAX_MIN:
                    print(f"[STALE] {signal.asset} sinal com {_age_min:.0f}min — limite {_STALE_MAX_MIN}min — skip")
                    _blk("staleness")
                    continue
        except Exception:
            pass

        # FIX #9 — Structural Tag obrigatória em modo NORMAL
        if CURRENT_MODE == "NORMAL":
            _sig_dict_for_tag = {"reason": getattr(signal, "reason", ""),
                                 "confirmed_signals": getattr(signal, "confirmed_signals", [])}
            if not _has_struct_tag(_sig_dict_for_tag):
                print(f"[STRUCT] {signal.asset} sem tag estrutural V6 (NORMAL) — skip")
                _blk("sem_tag_estrutural")
                continue

        # NOTA: o filtro de spread/liquidez foi UNIFICADO no Orderbook Gate do
        # signal_engine (score_orderbook_liquidity, sensível ao perfil). O sinal
        # que chega aqui já passou por ele — não há mais checagem duplicada.

        # ── Filtro volume spike (≥ 1.5x média 20 períodos) ───────────────────
        try:
            from klines_cache import get_klines_cached as _gkl2
            _kl2 = await _gkl2(signal.asset, signal.timeframe, 22)
            if _kl2 is not None and len(_kl2) >= 21:
                _vol_avg  = float(_kl2["volume"].iloc[-21:-1].mean())
                _vol_last = float(_kl2["volume"].iloc[-1])
                _VOL_MIN  = 1.2
                if _vol_avg > 0 and _vol_last < _vol_avg * _VOL_MIN:
                    print(f"[VOL] ❌ {signal.asset} vol={_vol_last:.0f} < {_VOL_MIN}x media={_vol_avg:.0f} — skip")
                    _blk("vol_spike<1.2x")
                    continue
        except Exception:
            pass

        signal_dict = signal.model_dump()
        signal_dict["direction"]  = signal.direction.value
        signal_dict["score"]      = signal.score.model_dump()
        signal_dict["leverage"]   = signal.suggested_leverage if getattr(signal, "suggested_leverage", 0) > 0 else get_leverage(signal.asset)

        # Aplica ajuste de vies do Market Intelligence Engine (max +/-10pts)
        bias_adj = market_engine.get_bias_score_adjustment(signal.direction.value)
        if bias_adj != 0.0:
            signal_dict["confidence"] = round(signal.confidence + bias_adj, 1)
            signal_dict["market_bias_adj"] = round(bias_adj, 1)

        # ── Pump/Dump synergy boost (AUTONOMOUS/SUPERVISED) ──────────────────
        # Se há alerta ativo no mesmo ativo + mesma direção → boost de confiança
        try:
            from pump_dump_engine import get_cached as _pd_cache_auto
            _pd_match = next(
                (a for a in _pd_cache_auto()
                 if a["symbol"] == signal.asset
                 and a.get("confidence", 0) >= 50
                 and (
                     (a["type"] == "PUMP" and signal.direction.value == "LONG") or
                     (a["type"] == "DUMP" and signal.direction.value == "SHORT")
                 )),
                None,
            )
            if _pd_match:
                _pd_boost = min(8, int(_pd_match["confidence"] / 10))
                signal_dict["confidence"] = min(100, round(signal_dict["confidence"] + _pd_boost, 1))
                signal_dict["pd_synergy"] = f"{_pd_match['type']} conf={_pd_match['confidence']} +{_pd_boost}pts"
                print(f"[PD-SYNERGY] {signal.asset} {signal.direction.value} +{_pd_boost}pts "
                      f"(PD {_pd_match['type']} conf={_pd_match['confidence']})")
        except Exception:
            pass

        # ── ML Engine: ajuste de score ───────────────────────────────────────
        _ML_MIN_SAMPLES = 25
        if _ml_ready:
            _ml_status = ml_engine.get_ml_status()
            _ml_n = _ml_status.get("global_n_samples", 0)
            if _ml_n >= _ML_MIN_SAMPLES:
                ml_bonus = ml_engine.ml_score_bonus(signal.asset, signal_dict)
                if ml_bonus != 0.0:
                    prev_conf = signal_dict["confidence"]
                    signal_dict["confidence"] = round(max(0, min(100, prev_conf + ml_bonus)), 1)
                    signal_dict["ml_bonus"]   = ml_bonus
                    print(f"[ML] {signal.asset} score {prev_conf:.1f} → {signal_dict['confidence']:.1f} (bonus {ml_bonus:+.1f})")
                # Revalida threshold após ajuste ML
                if signal_dict["confidence"] < _eff_min_score(mode_cfg):
                    print(f"[ML] {signal.asset} reprovado após ajuste ML ({signal_dict['confidence']:.1f} < {_eff_min_score(mode_cfg)})")
                    _blk("ml_reprovou")
                    continue

        # ── Filtro Claude Brain (opcional) — só acima de score 65 ────────────
        _brain_active = _claude_brain_enabled
        if _brain_active and signal_dict.get("confidence", 0) >= 65:
            from data_fetcher import get_ticker as _gt, get_funding_rate as _gfr, \
                get_open_interest as _goi, get_long_short_ratio as _gls, \
                get_liquidations as _gliq
            from klines_cache import get_klines_cached as _gkl
            try:
                _tk, _fr, _oi, _ls, _liq, _kl = await asyncio.gather(
                    _gt(signal.asset),
                    _gfr(signal.asset),
                    _goi(signal.asset),
                    _gls(signal.asset, "15m"),
                    _gliq(signal.asset, 15),
                    _gkl(signal.asset, signal.timeframe, 50),
                    return_exceptions=True,
                )
                # RSI 14 + EMA21 + volume relativo
                _rsi_val = _ema_pos = _vol_rel = "--"
                _recent_cands = ""
                if not isinstance(_kl, Exception) and _kl is not None and len(_kl) >= 20:
                    _cl = _kl["close"]
                    _d  = _cl.diff()
                    _rs = _d.clip(lower=0).rolling(14).mean() / (-_d.clip(upper=0)).rolling(14).mean().replace(0, 1e-9)
                    _rsi_val = round(float((100 - 100 / (1 + _rs)).iloc[-1]), 1)
                    _ema21   = float(_cl.ewm(span=21).mean().iloc[-1])
                    _ema_pos = f"{'ACIMA' if float(_cl.iloc[-1]) > _ema21 else 'ABAIXO'} da EMA21 ({_ema21:,.4f})"
                    _va      = float(_kl["volume"].iloc[-21:-1].mean())
                    _vl      = float(_kl["volume"].iloc[-1])
                    _vol_rel = f"{_vl/_va:.1f}x da media" if _va > 0 else "--"
                    # Ultimos 5 candles para price action puro
                    for idx, row in _kl.iloc[-5:].iterrows():
                        _recent_cands += f"O:{row['open']:.4f} H:{row['high']:.4f} L:{row['low']:.4f} C:{row['close']:.4f} V:{row['volume']:.0f}\n"

                # Trend do BTC
                _btc_trend = "--"
                try:
                    import market_engine as _me
                    _st = _me.get_market_state()
                    _btc_trend = _st.get("btc_trend", "--")
                except Exception:
                    pass

                # Liquidacoes resumidas
                _liq_s = "--"
                if not isinstance(_liq, Exception) and _liq:
                    _ll = sum(x["qty"] for x in _liq if x["side"] == "SELL")
                    _ls2 = sum(x["qty"] for x in _liq if x["side"] == "BUY")
                    _liq_s = f"Longs liq: {_ll:.1f} | Shorts liq: {_ls2:.1f}"
                _brain_ctx = {
                    # Macro
                    "fear_greed":         _market_cache.get("fear_greed", "--"),
                    "btc_change":         _market_cache.get("btc_change_24h", "--"),
                    "btc_funding":        _market_cache.get("btc_funding", "--"),
                    "btc_oi":             f"${_market_cache.get('btc_oi_usdt', 0):,.0f}",
                    "btc_trend":          _btc_trend,
                    # Sessao
                    "open_trades":        len(open_trades),
                    "daily_pnl":          _daily_pnl,
                    "consecutive_losses": _consecutive_losses,
                    "banca":              effective_banca,
                    # Asset especifico — tempo real
                    "asset_price":        _tk["price"]                     if not isinstance(_tk, Exception) else "--",
                    "asset_vol24h":       f"${_tk['volume_24h']:,.0f}"      if not isinstance(_tk, Exception) else "--",
                    "asset_funding":      f"{_fr:+.4f}%"                   if not isinstance(_fr, Exception) else "--",
                    "asset_oi":           f"${_oi['oi_usdt']:,.0f}"         if not isinstance(_oi, Exception) else "--",
                    "asset_ls":           f"Long {_ls['long_pct']:.1f}% / Short {_ls['short_pct']:.1f}%" if not isinstance(_ls, Exception) else "--",
                    "asset_liquidations": _liq_s,
                    "asset_rsi":          _rsi_val,
                    "asset_ema":          _ema_pos,
                    "asset_vol_rel":      _vol_rel,
                    "recent_candles":     _recent_cands,
                    # Noticias
                    "news_summary":       " | ".join(
                        str(n.get("title","") if isinstance(n, dict) else n)[:80]
                        for n in _news_cache[:3]
                    ),
                }
            except Exception as _brain_ex:
                print(f"[CLAUDE BRAIN] Contexto parcial: {_brain_ex}")
                _brain_ctx = {
                    "fear_greed": _market_cache.get("fear_greed", "--"),
                    "btc_change": _market_cache.get("btc_change_24h", "--"),
                    "open_trades": len(open_trades), "daily_pnl": _daily_pnl,
                    "consecutive_losses": _consecutive_losses, "banca": effective_banca,
                }
            _brain_result = await claude_brain.analyze_signal(signal_dict, _brain_ctx)
            if not _brain_result.get("approve", True):
                print(f"[CLAUDE BRAIN] ❌ {signal.asset} rejeitado: {_brain_result.get('reason','')}")
                continue
                
            # Aplica ajustes na signal_dict
            _leverage_mult = _brain_result.get("leverage_multiplier", 1.0)
            if _leverage_mult != 1.0:
                signal_dict["leverage"] = max(1, int(signal_dict.get("leverage", 10) * _leverage_mult))
            
            _size_mult = _brain_result.get("size_multiplier", 1.0)
            signal_dict["size_multiplier"] = _size_mult
            
            _tp_adj = _brain_result.get("tp_adjust_pct", 1.0)
            _sl_adj = _brain_result.get("sl_adjust_pct", 1.0)
            
            if _tp_adj != 1.0 or _sl_adj != 1.0:
                _ent = float(signal_dict["entry"])
                if _tp_adj != 1.0:
                    signal_dict["tp1"] = round(_ent + (float(signal_dict["tp1"]) - _ent) * _tp_adj, 8)
                    signal_dict["tp2"] = round(_ent + (float(signal_dict["tp2"]) - _ent) * _tp_adj, 8)
                    signal_dict["tp3"] = round(_ent + (float(signal_dict["tp3"]) - _ent) * _tp_adj, 8)
                if _sl_adj != 1.0:
                    signal_dict["stop_loss"] = round(_ent - (_ent - float(signal_dict["stop_loss"])) * _sl_adj, 8)
            
            # Adiciona tag do sentimento de notícia na razão
            _ns = _brain_result.get("news_sentiment", 0)
            if _ns != 0:
                signal_dict["reason"] = f"{signal_dict.get('reason', '')} [NewsSent:{_ns}]"

        # ── Portfolio Risk — VaR e correlação ────────────────────────────────
        try:
            # FIX #3: sizing real baseado em BANCA_USDT/n_trades (não 10% fixo)
            _n_trades_ref = TRADES_PER_SESSION if TRADES_PER_SESSION > 0 else 5
            _notional_est = (effective_banca / _n_trades_ref) * get_leverage(signal.asset)
            # FIX #3: ATR_PCT = % de stop do sinal (não score.total que é 0-100)
            _sl_entry = float(signal_dict.get("entry", 1) or 1)
            _sl_price = float(signal_dict.get("stop_loss", _sl_entry) or _sl_entry)
            _atr_pct_est = abs(_sl_entry - _sl_price) / _sl_entry * 100 if _sl_entry > 0 else 2.0
            _atr_pct_est = max(0.1, min(15.0, _atr_pct_est))  # clamp razoável
            _port_ok, _port_reason = portfolio_risk.can_open_position(
                signal.asset,
                signal.direction.value,
                _notional_est,
                _atr_pct_est,
                [{"asset": t["asset"], "direction": t.get("direction",""), "notional_usdt": float(t.get("notional_usdt", 10)), "atr_pct": 2.0} for t in open_trades],
                effective_banca,
                leverage=get_leverage(signal.asset),
                max_concurrent=_n_trades_ref,
            )
            if not _port_ok:
                print(f"[PORTFOLIO] {signal.asset} bloqueado: {_port_reason}")
                _blk("portfolio_risk")
                continue
        except Exception:
            pass

        # ── Funding alert ─────────────────────────────────────────────────────
        try:
            _fund_alert = _fear_greed_mod.funding_needs_alert(signal.asset)
            if _fund_alert:
                asyncio.create_task(send_alert(_fund_alert))
        except Exception:
            pass

        if _effective_mode == "AUTONOMOUS":
            # Cadência por perfil: dentro da janela, não abre nova entrada neste ciclo.
            if _cadence_block:
                _blk(f"cadencia({_cadence_s}s)")
                continue
            # Teto diário de entradas autônomas.
            if _trades_today_blocked():
                _blk("max-trades-dia")
                continue
            # Anti-overtrading: mesmo ativo só após 15min e com sinal claro (score alto).
            if _same_asset_blocked(signal.asset, signal.confidence):
                _blk("anti-overtrading(mesmo-ativo)")
                continue
            print(f"[AUTÔNOMO] {signal.asset} {signal.direction.value} score={signal.confidence:.0f}")
            _before_open = _session_trades
            await _execute_trade(signal_dict)
            # Notificação Telegram disparada por send_trade_opened dentro de _execute_trade_inner (só após sucesso)
            # _session_trades é incrementado dentro de _execute_trade_inner após sucesso
            if _session_trades > _before_open:
                _last_auto_entry_ts = now_ts
                _asset_last_entry_ts[signal.asset] = now_ts  # arma cooldown anti-overtrading
                _trades_today += 1                            # conta no teto diário
                # Normal/Agressivo: 1 entrada por janela de cadência → encerra o ciclo.
                if _cadence_s > 0:
                    _cadence_break = True
        elif _effective_mode == "GRID":
            # GRID usa job_grid_scan dedicado, não o fluxo normal
            pass
        elif _effective_mode == "SINAIS":
            # SINAIS usa job_sinais_scan dedicado — sem execucao de trades
            pass
        else:
            # Supervised: envia com botões de aprovação.
            # FIX (2026-06-25): faltava o anti-overtrading aqui — só o AUTÔNOMO
            # chamava _same_asset_blocked. O cooldown genérico (60s, linha acima)
            # é quase igual ao intervalo de scan (60s), então assim que o usuário
            # aprova/rejeita um sinal, o ativo volta a ficar livre e o próximo scan
            # manda outro "parecido" quase em seguida — exatamente o bug reportado
            # (2 sinais de BNBUSDT numa janela curta). Agora usa o mesmo cooldown
            # de SAME_ASSET_COOLDOWN_MIN do autônomo, vale pra qualquer ativo.
            if _same_asset_blocked(signal.asset, signal.confidence):
                _blk("anti-overtrading(mesmo-ativo)")
                continue
            await send_signal_alert(signal_dict, _execute_trade, _reject_trade)
            _asset_last_entry_ts[signal.asset] = now_ts
            print(f"[SUPERVISIONADO] 📲 {signal.asset} {signal.direction.value} score={signal.confidence:.0f}")

        open_assets.add(signal.asset)
        _signal_cooldown[cooldown_key] = now_ts  # marca cooldown SÓ após qualificar/enviar
        sent += 1

        # Cadência (Normal/Agressivo): após abrir 1 entrada, encerra o ciclo.
        if _cadence_break:
            break

    # Resumo do ciclo de execução — mostra exatamente quantos sinais entraram,
    # quantos enviaram e o detalhamento de cada bloqueio (observabilidade total).
    _brk = " | ".join(f"{k}={v}" for k, v in sorted(_blocks.items(), key=lambda x: -x[1])) or "—"
    _ctx = f"{_effective_mode}/{CURRENT_MODE}"
    print(f"[EXEC-RESUMO {_ctx}] recebidos={_recv} enviados={sent} | bloqueios: {_brk}")
    if sent == 0:
        print(f"[{_ctx}] Execução viva — 0 enviados de {_recv} sinais "
              f"(score>={mode_cfg['min_score']}, RR>={mode_cfg['min_rr']}). Maior bloqueio acima.")
        # Heartbeat ao Telegram pessoal — mostra que o modo está vivo (com throttle)
        await _send_exec_heartbeat(_effective_mode, _recv, sent, _brk,
                                   mode_cfg["min_score"], mode_cfg["min_rr"])


async def job_grid_monitor():
    """
    Monitora trades GRID a cada 15s.
    Quando o lucro de um trade atinge GRID_PROFIT_TARGET_USDT → fecha e agenda re-entrada.
    """
    global _grid_profit_total, _grid_cycles, _grid_last_cycle_ts, _grid_reinvest_bonus, OPERATION_MODE
    _effective_mode = _resolve_effective_mode()
    if _effective_mode != "GRID" or PAPER_TRADING:
        return

    open_trades = await get_open_trades()
    grid_trades = [t for t in open_trades if t.get("trade_type") == "GRID"]

    for trade_data in grid_trades:
        try:
            current = ws_feed.get_price(trade_data["asset"])
            if current is None:
                ticker = await get_ticker(trade_data["asset"])
                current = float(ticker["price"])
            entry    = float(trade_data.get("entry_price", current))
            size_usd = float(trade_data.get("size_usdt", 0))
            direction = str(trade_data.get("direction", "LONG"))

            pnl_usdt = (
                (current - entry) / entry * size_usd
                if "LONG" in direction
                else (entry - current) / entry * size_usd
            )

            _grid_target = GRID_PROFIT_TARGET_USDT  # 0 = ilimitado (fecha só pelo TP do sinal)
            if _grid_target > 0 and pnl_usdt >= _grid_target:
                symbol = trade_data["asset"]
                print(f"[GRID] TARGET! {symbol} PnL=${pnl_usdt:.2f} >= ${_grid_target:.2f} ({CURRENT_MODE})")

                # Fecha o trade
                if not PAPER_TRADING:
                    try:
                        await asyncio.to_thread(
                            lambda: close_position(
                                symbol, trade_data.get("direction"), _get_binance_client_synced()
                            )
                        )
                    except Exception as e:
                        print(f"[GRID] Erro ao fechar {symbol}: {e}")

                trade_data["status"]    = "CLOSED"
                trade_data["pnl_usdt"]  = round(pnl_usdt, 2)
                trade_data["closed_at"] = datetime.utcnow().isoformat()
                await save_trade(trade_data)
                _active_trades_cache.pop(trade_data.get("id", ""), None)

                _grid_cycles[symbol] = _grid_cycles.get(symbol, 0) + 1
                _grid_profit_total  += pnl_usdt
                _grid_last_cycle_ts[symbol] = time.time()  # registra timestamp do ciclo

                # Reinvestimento automatico: 20% do lucro va para bonus de banca
                _grid_reinvest_bonus += pnl_usdt * GRID_REINVEST_PCT / 100
                print(f"[GRID-REINVEST] +${pnl_usdt * GRID_REINVEST_PCT / 100:.2f} bonus | total bonus=${_grid_reinvest_bonus:.2f}")

                paper_tag = " [PAPER]" if PAPER_TRADING else ""
                await _post_grid_notification(
                    symbol, pnl_usdt, _grid_cycles[symbol], _grid_profit_total, paper_tag
                )

                # Re-entrada automática após 10s (aguarda volatilidade assentar)
                async def _delayed_reentry(s=symbol):
                    await asyncio.sleep(10)
                    await job_grid_scan([s])
                asyncio.create_task(_delayed_reentry())

        except Exception as e:
            print(f"[GRID MONITOR] {trade_data.get('asset','?')}: {e}")


async def _post_grid_notification(symbol, pnl, cycles, total_profit, paper_tag=""):
    """Envia notificação Telegram quando um ciclo grid é completado."""
    from notifier import _post, TELEGRAM_CHAT_ID
    if not TELEGRAM_CHAT_ID:
        return
    msg = (
        f"✅ *GRID CICLO {cycles} COMPLETO{paper_tag}*\n\n"
        f"Ativo: `{symbol}`\n"
        f"Lucro do ciclo: `+${pnl:.2f} USDT`\n"
        f"Lucro acumulado: `+${total_profit:.2f} USDT`\n"
        f"Ciclos: `{cycles}` | Alvo/ciclo: `{'ilimitado' if GRID_PROFIT_TARGET_USDT == 0 else f'${GRID_PROFIT_TARGET_USDT:.2f}'}`\n\n"
        f"Buscando proxima entrada..."
    )
    await _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


async def job_grid_scan(pairs: list = None):
    """
    Scan focado apenas nos pares do Grid.
    Abre novos trades se o número de trades grid abertos < GRID_MAX_CONCURRENT.
    NORMAL/AGGRESSIVE: usa GRID_SETTINGS para thresholds adaptativos.
    Inclui: session filter, BTC veto, funding window, VRA, ML, Claude Brain,
            portfolio risk, pump/dump awareness, market bias.
    """
    global _grid_cycles
    _effective_mode = _resolve_effective_mode()
    if _effective_mode != "GRID" or PAPER_TRADING:
        return

    grid_cfg     = GRID_SETTINGS.get(CURRENT_MODE, GRID_SETTINGS["NORMAL"])
    target_pairs = pairs or GRID_PAIRS
    open_trades  = await get_open_trades()

    effective_max = grid_cfg["max_concurrent"]
    if TRADES_PER_SESSION > 0:
        effective_max = min(effective_max, TRADES_PER_SESSION)

    grid_trades    = [t for t in open_trades if t.get("trade_type") == "GRID"]
    open_asset_dir = {f"{t['asset']}_{str(t.get('direction','')).upper()}" for t in grid_trades}

    if len(grid_trades) >= effective_max:
        return

    from signal_engine import analyze_asset, analyze_smart_flow as _smart_flow, ema as _ema
    from klines_cache import get_klines_cached as _gk
    from pump_dump_engine import _is_funding_window, get_cached as _pd_cached

    def _rsi14(close_series):
        delta = close_series.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        return 100 - 100 / (1 + gain / loss.replace(0, 1e-9))

    now_ts = time.time()

    # ── Stale alert: par sem ciclo completo há mais de 2h ─────────────────────
    _STALE_ALERT_INTERVAL = 43200  # 12h entre alertas do mesmo par
    for sym in target_pairs:
        last_ts = _grid_last_cycle_ts.get(sym)
        if last_ts and now_ts - last_ts > 7200:
            if now_ts - _grid_stale_alerted.get(sym, 0) >= _STALE_ALERT_INTERVAL:
                hours = (now_ts - last_ts) / 3600
                print(f"[GRID] {sym} sem ciclo completo ha {hours:.1f}h")
                # Notificação desativada — só abrir/fechar ordem ou atingir alvo/stop devem notificar.
                _grid_stale_alerted[sym] = now_ts

    # ── Verificações globais (uma vez por ciclo, antes de iterar pares) ────────
    # [G1] Funding window: evita abrir em spike de liquidação
    if _is_funding_window():
        print(f"[GRID] Skip ciclo — janela funding ativa")
        return

    # [G2] Sessão: score mínimo por modo
    sess     = _get_session_info()
    sess_min = grid_cfg["session_min_score"]
    if sess["score"] < sess_min:
        print(f"[GRID] Skip ciclo — sessao fraca ({sess['session']} score={sess['score']}<{sess_min})")
        return

    # [G3] BTC veto: se BTC caiu muito em 1h, protege LONGs
    btc_chg_1h = float(_market_cache.get("btc_change_1h", 0) or 0)
    btc_veto   = grid_cfg["btc_veto_pct"]

    # Cache pump/dump para consulta por símbolo
    pd_alerts  = {a["symbol"]: a for a in _pd_cached() if a.get("confidence", 0) >= 50}

    df15 = None
    _grid_entries_before = len(grid_trades)   # baseline p/ detectar 0 entradas no ciclo
    for symbol in target_pairs:
        if len(grid_trades) >= effective_max:
            break
        try:
            # [G3] BTC veto por símbolo/direção (avaliado dentro do loop pois a direção vem do sinal)
            # Será reavaliado após analyze_asset

            # Engine router: detecta regime e usa engine certa por ativo.
            # Multi-TF: escaneia os timeframes configurados para o modo atual
            _tfs_to_scan = grid_cfg.get("timeframes", ["15m"])
            signal = None
            for _tf in _tfs_to_scan:
                try:
                    from klines_cache import get_klines_cached as _gkl_r
                    _df_r = await _gkl_r(symbol, _tf, limit=200)
                    if _df_r is None or len(_df_r) < 50:
                        continue
                    _sig = await engine_router.route(symbol, _tf, _df_r, mode=CURRENT_MODE)
                    if _sig is not None:
                        # Prefere TF maior se score maior (qualidade > velocidade)
                        if signal is None or _sig.confidence > signal.confidence:
                            signal = _sig
                except Exception as _er:
                    pass
            # Fallback: analyze_asset sem TF específico
            if signal is None:
                try:
                    signal = await analyze_asset(symbol, mode=CURRENT_MODE)
                except Exception as _ea:
                    pass
            if signal is None:
                print(f"[GRID] {symbol} nenhum sinal ({', '.join(grid_cfg.get('timeframes', ['15m']))})") 
                continue

            # Threshold dinâmico por modo
            if signal.confidence < grid_cfg["min_confidence"] or signal.rr < grid_cfg["min_rr"]:
                print(f"[GRID] {symbol} score={signal.confidence:.0f}<{grid_cfg['min_confidence']} "
                      f"ou RR={signal.rr:.2f}<{grid_cfg['min_rr']:.1f} — descartado")
                continue

            asset_dir_key = f"{symbol}_{signal.direction.value}"
            if asset_dir_key in open_asset_dir:
                continue

            # [G3] BTC veto aplicado à direção
            if signal.direction.value == "LONG" and btc_chg_1h < btc_veto:
                print(f"[GRID] {symbol} LONG bloqueado — BTC veto ({btc_chg_1h:.1f}% < {btc_veto}%)")
                continue

            # [G4] Pump/dump awareness: evita contratendência de alertas ativos
            pd_alert = pd_alerts.get(symbol)
            if pd_alert:
                if pd_alert["type"] == "PUMP" and signal.direction.value == "SHORT":
                    print(f"[GRID] {symbol} skip SHORT — PUMP ativo conf={pd_alert['confidence']}")
                    continue
                if pd_alert["type"] == "DUMP" and signal.direction.value == "LONG":
                    print(f"[GRID] {symbol} skip LONG — DUMP ativo conf={pd_alert['confidence']}")
                    continue

            # ── Trend detection com EMA + RSI (v2: relaxado, exige dupla confirmação) ──
            df15     = await _gk(symbol, "15m", limit=60)
            rsi_last = 50.0
            if df15 is not None and len(df15) >= 30:
                close       = df15["close"]
                e9          = _ema(close, 9).iloc[-1]
                e21         = _ema(close, 21).iloc[-1]
                e55         = _ema(close, 55).iloc[-1]
                rsi_series  = _rsi14(close)
                rsi_last    = float(rsi_series.iloc[-1]) if not rsi_series.isna().all() else 50.0
                strong_up   = e9 > e21 > e55 and close.iloc[-1] > e9
                strong_down = e9 < e21 < e55 and close.iloc[-1] < e9
                # [G7] GRID SÓ OPERA EM RANGE: pula tendência forte direcional. Grid acumula
                # ordens contra um preço que não para de andar e "explode" a conta em trend.
                # Exige dupla confirmação (EMA forte + regime TRENDING/VOLATILE) p/ não bloquear consolidações.
                if strong_up or strong_down:
                    _rg = (regime_detector.get_cache(symbol) or {}).get("regime", "NEUTRAL")
                    if _rg in ("TRENDING", "VOLATILE"):
                        print(f"[GRID] {symbol} skip — regime {_rg} (grid só opera em RANGE)")
                        continue
                # RELAXADO v2: só bloqueia quando EMA trend forte E RSI confirma
                # (RSI sobrecomprado ao tentar LONG contra tendência, ou vice-versa)
                if signal.direction.value == "LONG" and strong_down and rsi_last > 55:
                    reason = f"EMA bearish 9<21<55 + RSI {rsi_last:.0f}>55"
                    print(f"[GRID] {symbol} skip — contra-trend duplo ({reason})")
                    # Notificação desativada — só abrir/fechar ordem ou atingir alvo/stop devem notificar.
                    continue
                if signal.direction.value == "SHORT" and strong_up and rsi_last < 45:
                    reason = f"EMA bullish 9>21>55 + RSI {rsi_last:.0f}<45"
                    print(f"[GRID] {symbol} skip — contra-trend duplo ({reason})")
                    # Notificação desativada — só abrir/fechar ordem ou atingir alvo/stop devem notificar.
                    continue
                flow = _smart_flow(df15)
                # Só bloqueia breakout extremo (era 40, agora 60 para não bloquear consolidações)
                if flow.get("phase") == "Breakout" and abs(flow.get("imbalance", 0)) > 60:
                    print(f"[GRID] {symbol} skip — breakout extremo imbalance={flow.get('imbalance',0):.0f}")
                    continue

            signal_dict = signal.model_dump()
            signal_dict["direction"]  = signal.direction.value
            signal_dict["score"]      = signal.score.model_dump()
            signal_dict["leverage"]   = GRID_LEVERAGE
            signal_dict["trade_type"] = "GRID"

            # [G5] Market bias adjustment (mesmo pipeline do AUTONOMOUS)
            bias_adj = market_engine.get_bias_score_adjustment(signal.direction.value)
            if bias_adj != 0.0:
                signal_dict["confidence"]       = round(signal.confidence + bias_adj, 1)
                signal_dict["market_bias_adj"]  = round(bias_adj, 1)

            # [G6] VRA regime: COMPRESSION exige score mais alto
            try:
                _regime_data = regime_detector.get_cache(symbol)
                if _regime_data:
                    _reg = _regime_data.get("regime", "NORMAL")
                    if _reg == "COMPRESSION" and signal_dict["confidence"] < grid_cfg["min_confidence"] + 10:
                        print(f"[GRID] {symbol} skip — VRA COMPRESSION + score insuficiente")
                        continue
                    if _reg == "EXPANSION" and signal_dict["confidence"] >= grid_cfg["min_confidence"]:
                        signal_dict["confidence"] = min(100, signal_dict["confidence"] + 3)
            except Exception:
                pass

            # [G7] ML score bonus — aplica com >= 25 amostras
            if _ml_ready:
                _gml_n = ml_engine.get_ml_status().get("global_n_samples", 0)
                if _gml_n >= 25:
                    ml_bonus = ml_engine.ml_score_bonus(signal.asset, signal_dict)
                    if ml_bonus != 0.0:
                        prev = signal_dict["confidence"]
                        signal_dict["confidence"] = round(max(0, min(100, prev + ml_bonus)), 1)
                        signal_dict["ml_bonus"]   = ml_bonus
                        if signal_dict["confidence"] < grid_cfg["min_confidence"]:
                            print(f"[GRID ML] {symbol} reprovado pos-ML ({signal_dict['confidence']:.0f})")
                            continue

            # [G8] Portfolio risk (VaR + correlação) — FIX #3: sizing e ATR% corretos
            try:
                _gcfg_risk  = GRID_SETTINGS.get(CURRENT_MODE, GRID_SETTINGS["NORMAL"])
                _banca_risk = BANCA_USDT if BANCA_USDT > 0 else (await _get_balance() or 10)
                _notional_est = (_banca_risk / max(_gcfg_risk["max_concurrent"], 1)) * GRID_LEVERAGE
                _sl_e  = float(signal_dict.get("entry", 1) or 1)
                _sl_p  = float(signal_dict.get("stop_loss", _sl_e) or _sl_e)
                _atr_pct_est = max(0.1, min(15.0, abs(_sl_e - _sl_p) / _sl_e * 100)) if _sl_e > 0 else 2.0
                _port_ok, _port_reason = portfolio_risk.can_open_position(
                    signal.asset, signal.direction.value,
                    _notional_est, _atr_pct_est,
                    [{"asset": t["asset"], "direction": t.get("direction",""),
                      "notional_usdt": float(t.get("notional_usdt", 10)), "atr_pct": 2.0}
                     for t in open_trades],
                    BANCA_USDT if BANCA_USDT > 0 else (await _get_balance() or 10),
                    leverage=GRID_LEVERAGE,
                    max_concurrent=_gcfg_risk["max_concurrent"],
                )
                if not _port_ok:
                    print(f"[GRID PORTFOLIO] {symbol} bloqueado: {_port_reason}")
                    continue
            except Exception:
                pass

            # [G9] Claude Brain — apenas quando ativado e score >= 65
            _brain_active = _claude_brain_enabled
            if _brain_active and signal_dict.get("confidence", 0) >= 65:
                try:
                    from data_fetcher import get_ticker as _gt, get_funding_rate as _gfr
                    _tk, _fr = await asyncio.gather(_gt(symbol), _gfr(symbol), return_exceptions=True)
                    _brain_ctx = {
                        "fear_greed":         _market_cache.get("fear_greed", "--"),
                        "btc_change":         _market_cache.get("btc_change_24h", "--"),
                        "btc_funding":        _market_cache.get("btc_funding", "--"),
                        "btc_oi":             f"${_market_cache.get('btc_oi_usdt', 0):,.0f}",
                        "open_trades":        len(open_trades),
                        "daily_pnl":          _daily_pnl,
                        "consecutive_losses": _consecutive_losses,
                        "banca":              BANCA_USDT if BANCA_USDT > 0 else (await _get_balance() or 10),
                        "asset_price":        _tk["price"]     if not isinstance(_tk, Exception) else "--",
                        "asset_vol24h":       f"${_tk.get('volume_24h',0):,.0f}" if not isinstance(_tk, Exception) else "--",
                        "asset_funding":      f"{_fr:+.4f}%"   if not isinstance(_fr, Exception) else "--",
                        "asset_rsi":          round(rsi_last, 1),
                        "asset_ema":          "GRID mode",
                        "asset_vol_rel":      "--",
                        "news_summary":       "",
                        "session":            sess["session"],
                        "pd_alert":           f"{pd_alert['type']} conf={pd_alert['confidence']}" if pd_alert else "none",
                    }
                    _brain_result = await claude_brain.analyze_signal(signal_dict, _brain_ctx)
                    if not _brain_result.get("approve", True):
                        print(f"[GRID BRAIN] {symbol} rejeitado: {_brain_result.get('reason','')}")
                        continue
                except Exception as _be:
                    print(f"[GRID BRAIN] erro contexto {symbol}: {_be}")

            # ── Grid Assimetrico: ajusta TPs baseado no RSI ─────────────────
            entry = signal_dict.get("entry", signal.entry)
            tp1   = signal_dict.get("tp1",   signal.tp1)
            tp2   = signal_dict.get("tp2",   signal.tp2)
            if rsi_last < 30 and signal.direction.value == "LONG":
                signal_dict["tp1"] = round(entry + (tp1 - entry) * 1.15, 6)
                signal_dict["tp2"] = round(entry + (tp2 - entry) * 1.20, 6)
                print(f"[GRID-ASS] {symbol} LONG sobrevendido RSI={rsi_last:.0f} -> TPs ampliados")
            elif rsi_last > 70 and signal.direction.value == "SHORT":
                signal_dict["tp1"] = round(entry - (entry - tp1) * 1.15, 6)
                signal_dict["tp2"] = round(entry - (entry - tp2) * 1.20, 6)
                print(f"[GRID-ASS] {symbol} SHORT sobrecomprado RSI={rsi_last:.0f} -> TPs ampliados")

            # ── V6 Grid Zones — define range estrutural OB/FVG ───────────────
            try:
                from signal_engine import v6_grid_zones as _v6gz
                _df_gz = df15 if df15 is not None else await _gk(symbol, "15m", limit=200)
                if _df_gz is not None and len(_df_gz) >= 50:
                    gz = _v6gz(_df_gz, signal.entry)
                    signal_dict["v6_grid_zones"] = gz
                    if gz.get("found"):
                        lt, ut = gz["lower_type"], gz["upper_type"]
                        lo, hi = gz["lower"], gz["upper"]
                        rng    = gz.get("range_pct", gz.get("dist_lo_pct", 0) + gz.get("dist_hi_pct", 0))
                        print(f"[GRID V6] {symbol} zona: {lt}${lo:.2f}<->{ut}${hi:.2f} ({rng:.2f}%)")
                    else:
                        print(f"[GRID V6] {symbol} sem zona estrutural -> ATR fallback")
                else:
                    signal_dict["v6_grid_zones"] = {}
            except Exception as _gz_err:
                print(f"[GRID V6] zones erro: {_gz_err}")
                signal_dict["v6_grid_zones"] = {}

            await _execute_grid_trade(signal_dict)
            open_asset_dir.add(asset_dir_key)
            grid_trades.append({"asset": symbol, "trade_type": "GRID", "direction": signal.direction.value})
            if symbol not in _grid_last_cycle_ts:
                _grid_last_cycle_ts[symbol] = now_ts
            print(f"[GRID] ENTRADA: {symbol} {signal.direction.value} "
                  f"score={signal_dict['confidence']:.0f} RSI={rsi_last:.0f} "
                  f"sess={sess['session']} mode={CURRENT_MODE}")

        except Exception as e:
            print(f"[GRID SCAN] {symbol}: {e}")

    # Heartbeat: nenhuma entrada nova neste ciclo → avisa que o GRID está vivo
    _grid_entries_now = len(grid_trades) - _grid_entries_before
    if _grid_entries_now <= 0:
        _gmin = grid_cfg.get("min_score", "—")
        await _send_exec_heartbeat(
            "GRID", len(target_pairs), 0,
            f"sem entrada qualificada em {', '.join(target_pairs)}",
            _gmin, grid_cfg.get("min_rr", "—"),
        )


async def _execute_grid_trade(signal_dict: dict):
    """Executa trade em modo GRID — guards: race condition, duplicata, limite de trades."""
    import uuid
    from models import ActiveTrade

    asset     = signal_dict.get("asset", "")
    direction = str(signal_dict.get("direction", "")).upper()

    # Guard: race condition
    if asset in _executing_assets:
        print(f"[GRID GUARD] {asset} ja em execucao.")
        return
    _executing_assets.add(asset)

    try:
        open_trades_all = await get_open_trades()

        # Limite de trades por sessão (respeita TRADES_PER_SESSION como teto global)
        max_grid = GRID_MAX_CONCURRENT
        if TRADES_PER_SESSION > 0:
            max_grid = min(GRID_MAX_CONCURRENT, TRADES_PER_SESSION)
        grid_open = [t for t in open_trades_all if t.get("trade_type") == "GRID"]
        if len(grid_open) >= max_grid:
            print(f"[GRID GUARD] Limite de {max_grid} trades grid atingido.")
            return

        # Guard: bloqueia mesma direção no mesmo ativo
        if any(t["asset"] == asset and str(t.get("direction","")).upper() == direction for t in open_trades_all):
            print(f"[GRID GUARD] {asset} {direction} ja aberto.")
            return

        # FIX #6: usa max_concurrent do GRID_SETTINGS (NORMAL=2, AGGRESSIVE=4) em vez do global fixo
        _gcfg = GRID_SETTINGS.get(CURRENT_MODE, GRID_SETTINGS["NORMAL"])
        n = max(_gcfg["max_concurrent"], 1)
        effective_banca = (BANCA_USDT if BANCA_USDT > 0 else (await _get_balance() or 10)) + _grid_reinvest_bonus
        margin   = round(effective_banca / n, 2)
        notional = round(margin * GRID_LEVERAGE, 2)

        signal = _build_signal_from_dict(signal_dict)
        trade  = ActiveTrade(
            id=str(uuid.uuid4())[:8],
            asset=signal.asset,
            direction=signal.direction,
            entry_price=signal.entry,
            current_price=signal.entry,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1, tp2=signal.tp2, tp3=signal.tp3,
            rr=signal.rr,
            leverage=GRID_LEVERAGE,
            size_usdt=notional,
            reason=signal.reason,
            confidence=signal.confidence,
        )
        trade_dict = trade.model_dump()
        trade_dict["opened_at"]      = trade.opened_at.isoformat()
        trade_dict["score_json"]     = "{}"
        trade_dict["timeframe"]      = signal_dict.get("timeframe", "5m")
        trade_dict["trade_type"]     = "GRID"
        trade_dict["paper"]          = PAPER_TRADING
        trade_dict["v6_grid_zones"]  = signal_dict.get("v6_grid_zones", {})

        if PAPER_TRADING:
            result = {"status": "SIMULATED"}
        else:
            result = await asyncio.to_thread(open_trade, trade)

        if result.get("status") in ("OK", "SIMULATED"):
            exec_status = str(result.get("status", "")).upper()
            trade_dict["paper"] = PAPER_TRADING or exec_status == "SIMULATED"
            trade_dict["execution_status"] = exec_status
            trade_dict["order_id"] = result.get("order_id")
            await save_trade(trade_dict)
            _active_trades_cache[trade.id] = trade_dict
            paper_tag = " [PAPER]" if trade_dict["paper"] else ""
            await send_trade_opened(trade_dict, OPERATION_MODE)
            print(f"[GRID] ABERTO{paper_tag} {signal.direction.value} {signal.asset} | margem=${margin:.2f} nocional=${notional:.2f} alvo=+${GRID_PROFIT_TARGET_USDT}")
        else:
            print(f"[GRID] FALHA ao abrir {signal.asset}: {result.get('msg','?')}")

    finally:
        _executing_assets.discard(asset)


def _get_session_info() -> dict:
    """Retorna qualidade da sessao atual e proxima janela ideal (horario de Brasilia)."""
    now_utc   = datetime.utcnow()
    hour_utc  = now_utc.hour
    hour_brt  = (hour_utc - 3) % 24
    dow       = now_utc.strftime("%A")  # Monday, Tuesday...
    dow_pt    = {"Monday":"Seg","Tuesday":"Ter","Wednesday":"Qua","Thursday":"Qui",
                 "Friday":"Sex","Saturday":"Sab","Sunday":"Dom"}.get(dow, dow)

    # Score por hora UTC (liquidez e volatilidade historica cripto)
    _scores = {
        0:(6,"Asia"),  1:(5,"Asia"),  2:(4,"Asia Low"), 3:(3,"Madrugada"),
        4:(3,"Madrugada"), 5:(3,"Madrugada"), 6:(5,"Pre-Europa"), 7:(7,"Europa"),
        8:(9,"Londres"), 9:(9,"Londres"), 10:(8,"Londres"), 11:(8,"Londres"),
        12:(9,"Overlap EU-US"), 13:(10,"NY Open"), 14:(10,"NY"),
        15:(9,"NY"), 16:(9,"NY"), 17:(8,"NY Tarde"),
        18:(7,"NY Tarde"), 19:(6,"NY Fechando"), 20:(6,"Asia Pre"),
        21:(6,"Asia Pre"), 22:(7,"Asia Inicio"), 23:(6,"Asia"),
    }
    score, session = _scores.get(hour_utc, (5, "Normal"))

    if score >= 9:   quality = "Excelente 🟢"
    elif score >= 7: quality = "Boa 🟡"
    elif score >= 5: quality = "Moderada 🟠"
    else:            quality = "Baixa 🔴"

    # Proxima janela ideal (score >= 9)
    best_utc = [h for h in range(24) if _scores[h][0] >= 9]
    next_h   = next((h for h in best_utc if h > hour_utc), best_utc[0] if best_utc else 8)
    next_brt = (next_h - 3) % 24
    amanha   = next_h <= hour_utc

    return {
        "hour_brt": hour_brt, "dow": dow_pt, "score": score,
        "session": session,   "quality": quality,
        "next_brt": next_brt, "amanha": amanha,
        "janelas_brt": "05h-09h  |  10h-14h  |  13h-17h",
    }


async def _send_startup_test_notification():
    """Envia notificacao de startup ao Telegram com configuracao atual."""
    await asyncio.sleep(5)
    from notifier import _post, TELEGRAM_CHAT_ID
    if not TELEGRAM_CHAT_ID:
        return

    paper_tag = " _(PAPER TRADING)_" if PAPER_TRADING else ""
    trades_label = "Ilimitado" if TRADES_PER_SESSION == 0 else str(TRADES_PER_SESSION)
    now_brt = (datetime.utcnow().hour - 3) % 24
    hora_brt_s = f"{now_brt:02d}:{datetime.utcnow().minute:02d}"

    # Janela de sessao atual
    si = _get_session_info()
    sessao_info = f"Sessao: *{si['session']}* | Qualidade: *{si['quality']}* ({si['score']}/10)"

    msg = (
        f"🤖 *TRADER 001 Online!*{paper_tag}\n\n"
        f"*Modo de Execucao:* `{OPERATION_MODE}`\n"
        f"*Perfil:* `{CURRENT_MODE.capitalize()}`\n"
        f"*Trades por Sessao:* `{trades_label}`\n"
        f"*Risco por Trade:* definir para cada sinal com base na estrategia.\n"
        f"*Alavancagem:* definir para cada sinal com base na estrategia.\n\n"
        f"⏰ {hora_brt_s} BRT | {sessao_info}\n\n"
        f"*Melhores janelas BRT:*\n"
        f"  🟢 05h-09h  Abertura Londres\n"
        f"  🟢 10h-14h  NY + overlap\n"
        f"  🟡 14h-17h  NY tarde\n"
        f"  🔴 00h-05h  Liquidez baixa\n\n"
        f"_Motor V6+V4 ativo | Pump/Dump monitor ativo_"
    )
    await _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


async def job_pump_dump_scan():
    """Atualiza cache de pump/dump a cada 2 minutos."""
    try:
        await pump_dump_engine.scan_pump_dump()
        extremos = [a for a in pump_dump_engine.get_cached() if a.get("intensity") == "EXTREMO"]
        if extremos:
            print(f"[PUMP/DUMP] {len(extremos)} alertas EXTREMO encontrados")
    except Exception as e:
        print(f"[PUMP/DUMP] Erro: {e}")


async def job_sinais_scan():
    """
    Modo SINAIS: transmite sinais ao Telegram sem executar trades.
    Usa o perfil de risco ativo (CURRENT_MODE) para os thresholds.
    Prioridade: 60% pump/dump, 40% outros movimentos.
    """
    global _sinais_toggle, _sinais_cooldown, _signal_fingerprints, _sinais_session_count, _sinais_weekly, _latest_signals, SINAIS_PROFILE, DUAL_MODE_ENABLED, _sinais_empty_cycles, _sinais_last_empty_alert_ts, _sinais_last_blocked_alert_ts
    
    from state import state
    # Se sinais_enabled está OFF, o motor de sinais deve parar completamente.
    if not state.sinais_enabled:
        return

    # Se o bot está pausado, desliga envio de sinais. (PAPER_TRADING NÃO pausa — em
    # simulação os sinais/trades simulados continuam sendo enviados ao Telegram.)
    if BOT_PAUSED:
        return

    if OPERATION_MODE != "SINAIS":
        return

    if TRADES_PER_SESSION > 0 and _sinais_session_count >= TRADES_PER_SESSION:
        print(f"[SINAIS] Limite de {TRADES_PER_SESSION} sinais/sessao atingido — aguardando reset.")
        return

    # Single-mode: o perfil de risco ativo (CURRENT_MODE) governa os thresholds do
    # canal de sinais — assim o seletor de perfil realmente afeta os sinais.
    scan_mode = CURRENT_MODE if CURRENT_MODE in MODE_SETTINGS else "AGGRESSIVE"
    mode_cfg  = MODE_SETTINGS.get(scan_mode, MODE_SETTINGS["AGGRESSIVE"])

    signals = _latest_signals[:]
    if not signals:
        _sinais_empty_cycles += 1
        # Diferencia DOIS casos antes de alarmar:
        #  (a) SCAN CAÍDO: o job_scan_market não conclui há >3min → problema real.
        #  (b) DEDUP/MERCADO QUIETO: o scan rodou e até achou sinais, mas todos já
        #      foram enviados (fingerprint/cooldown) ou o mercado não dá setup. NÃO
        #      é falha do scan — não faz sentido pedir "verifique o scan".
        _scan_age = time.time() - _last_scan_ts if _last_scan_ts else 1e9
        _scan_down = _scan_age > 180
        if _scan_down:
            _motivo = f"scan de mercado parado há {_scan_age/60:.0f}min (sem sinais no cache)"
            print(f"[SINAIS] Cache vazio + scan parado há {_scan_age:.0f}s (ciclo #{_sinais_empty_cycles}).")
            # Alerta no máx. 1x a cada 30min para não floodar o canal.
            if time.time() - _sinais_last_empty_alert_ts > 1800:
                _sinais_last_empty_alert_ts = time.time()
                asyncio.create_task(send_alert(
                    f"⚠️ SINAIS: scan de mercado parado há {_scan_age/60:.0f}min "
                    f"(sem sinais no cache). Verifique conectividade Binance/rate-limit."
                ))
        else:
            # scan saudável; só não há sinais NOVOS a enviar agora
            _motivo = f"sem sinais novos (scan ok há {_scan_age:.0f}s, últimos {_last_scan_count} já deduplicados/em cooldown)"
            print(f"[SINAIS] Sem sinais novos a enviar (scan ok há {_scan_age:.0f}s, "
                  f"últimos {_last_scan_count} já deduplicados). Ciclo #{_sinais_empty_cycles}.")
        asyncio.create_task(log_event("SINAIS_CYCLE", f"{scan_mode} | 0 enviados | {_motivo}", {"mode": scan_mode}))
        return
    _sinais_empty_cycles = 0  # reset quando há sinais

    # ── Contexto de filtros (BTC veto, VRA, funding, sessão) — 1 chamada por ciclo
    from signal_filters import fetch_scan_context, evaluate_signal
    ctx = await fetch_scan_context()
    sess  = ctx["session"]
    vra   = ctx["vra"]
    print(f"[SINAIS] {scan_mode} | Sessão:{sess['name']} | VRA:{vra['regime']} | "
          f"BTC:{ctx['btc_veto'].get('change_45m',0):+.2f}%")

    # Pump/dump cache — sem chamada extra de API
    pd_alerts  = pump_dump_engine.get_cached()
    pd_map     = {a["symbol"]: a for a in pd_alerts}
    pd_symbols = set(pd_map.keys())

    # Watchlist filter (BUG-008 Fix + 2026-07-01: NORMAL/CONSERVATIVE ficavam presos
    # aos 8 símbolos estáticos de WATCHLIST enquanto só o AGGRESSIVE usava o universo
    # dinâmico completo — isso reduzia drasticamente o volume de sinais nos perfis
    # mais conservadores, mesmo eles tendo thresholds de score/RR mais seletivos
    # (que já filtram qualidade). Universo agora é o mesmo pra todos os perfis;
    # quem diferencia por perfil é o min_score/min_rr/timeframes do MODE_SETTINGS.
    if _dynamic_universe and not SINAIS_WATCHLIST:
        wl = {s.upper() for s in _dynamic_universe}
    else:
        wl = {s.upper() for s in SINAIS_WATCHLIST} if SINAIS_WATCHLIST else {s.upper() for s in WATCHLIST}

    def _norm(s: dict) -> dict:
        c = dict(s)
        c["direction"] = str(c.get("direction", "")).split(".")[-1].strip().upper()
        return c

    # Pré-filtragem rápida (score/rr/watchlist) antes dos filtros avançados
    pre_filtered = [
        _norm(s) for s in signals
        if float(s.get("confidence", 0)) >= mode_cfg["min_score"] - 10  # margem p/ filtros adj
        and float(s.get("rr", 0)) >= mode_cfg["min_rr"]
        and (wl is None or s.get("asset", "") in wl)
    ]

    pd_signals  = [s for s in pre_filtered if s.get("asset", "") in pd_symbols]
    reg_signals = [s for s in pre_filtered if s.get("asset", "") not in pd_symbols]

    # VRA ajusta o número máximo de sinais por ciclo
    from signal_filters import vra_adjustments
    vra_adj     = vra_adjustments(vra["regime"])
    MAX_SIGNALS = vra_adj["max_signals"]
    pd_budget   = max(1, round(MAX_SIGNALS * 0.6))
    reg_budget  = MAX_SIGNALS - pd_budget

    now_ts  = time.time()
    to_send = []

    # Método D: remove sinais já enviados do cache
    _latest_signals = [s for s in _latest_signals if _make_fp(_norm(s)) not in _signal_fingerprints]

    SINAIS_COOLDOWN_REG = 1800
    SINAIS_COOLDOWN_PD  = 300
    FP_TTL              = 1800

    def _passes_dedup(s: dict, cooldown_s: int) -> bool:
        fp  = _make_fp(s)
        if now_ts - _signal_fingerprints.get(fp, 0) < FP_TTL:
            return False
        direction = str(s.get("direction", "")).split(".")[-1].strip().upper()
        asset = s.get("asset", "")
        key = f"{asset}_{direction}_{s.get('timeframe','')}"
        if now_ts - _sinais_cooldown.get(key, 0) < cooldown_s:
            return False
        # Evita mandar LONG e SHORT do mesmo ativo em TFs/engines diferentes
        # minutos um do outro — confunde o assinante (viu no print: DYDXUSDT
        # LONG 3m e SHORT 5m quase juntos).
        _last_dir, _last_ts = _sinais_last_direction.get(asset, (None, 0))
        if _last_dir and _last_dir != direction and (now_ts - _last_ts) < _SINAIS_DIRECTION_LOCK_MIN * 60:
            print(f"[SINAIS] Bloqueado {asset} {direction} — conflita com {_last_dir} enviado há "
                  f"{(now_ts - _last_ts)/60:.0f}min")
            return False
        _signal_fingerprints[fp]       = now_ts
        _sinais_cooldown[key]          = now_ts
        _sinais_last_direction[asset]  = (direction, now_ts)
        return True

    blocked_log = []

    # Penalidade de score por timeframe (2026-06-25): 1m gera muito mais ruído
    # que sinal (volume alto, WR mediano) — eleva a barra só pra ele, sem
    # afetar 3m/5m/15m que já são mais seletivos por natureza.
    # Reforço (2026-06-25, pedido do usuário: "só passa sinal altamente
    # qualificado" no 1m após o canal VIP ter recebido 226/544 sinais em 1m
    # em 3 dias): bump de score 10→15 + exigência extra de R:R só pro 1m.
    _tf_score_bump  = {"1m": 15, "3m": 3}
    _tf_min_rr_bump = {"1m": 0.5}

    # Cooldown mínimo por timeframe (2026-06-26): 1h/4h/1d entram em todos os
    # perfis agora — o candle de 1h fica "vivo" por 60min, então o cooldown
    # genérico de 30min reenviaria o MESMO sinal a cada scan enquanto o candle
    # não fecha. Piso por TF evita reenvio repetido do swing ainda sem fechar.
    _tf_cooldown_floor = {"1h": 3600, "4h": 14400, "1d": 86400}

    for s in pd_signals:
        if len(to_send) >= pd_budget:
            break
        _cd_pd = max(SINAIS_COOLDOWN_PD, _tf_cooldown_floor.get(s.get("timeframe", ""), 0))
        if not _passes_dedup(s, _cd_pd):
            continue
        if float(s.get("rr", 0)) < mode_cfg["min_rr"] + _tf_min_rr_bump.get(s.get("timeframe", ""), 0):
            blocked_log.append(f"{s.get('asset')} [rr<min_tf]")
            continue
        # Pipeline de 9 filtros
        result = evaluate_signal(s, ctx, pre_filtered, scan_mode,
                                 mode_cfg["min_score"] + _sinais_score_offset
                                 + _tf_score_bump.get(s.get("timeframe", ""), 0))
        if not result["passes"]:
            blocked_log.append(f"{s.get('asset')} [{result['block_reason']}]")
            continue
        sc = result["effective_score"]
        s["conf_label"]       = "Alta" if sc >= 80 else "Media" if sc >= 65 else "Baixa"
        s["size_modifier"]    = 0.40 * result["kelly_mult"]
        s["effective_score"]  = sc
        s["filter_notes"]     = result["notes"]
        to_send.append((s, pd_map.get(s["asset"])))

    for s in reg_signals:
        if sum(1 for _, pd in to_send if pd is None) >= reg_budget:
            break
        _cd_reg = max(SINAIS_COOLDOWN_REG, _tf_cooldown_floor.get(s.get("timeframe", ""), 0))
        if not _passes_dedup(s, _cd_reg):
            continue
        if float(s.get("rr", 0)) < mode_cfg["min_rr"] + _tf_min_rr_bump.get(s.get("timeframe", ""), 0):
            blocked_log.append(f"{s.get('asset')} [rr<min_tf]")
            continue
        result = evaluate_signal(s, ctx, pre_filtered, scan_mode,
                                 mode_cfg["min_score"] + _sinais_score_offset
                                 + _tf_score_bump.get(s.get("timeframe", ""), 0))
        if not result["passes"]:
            blocked_log.append(f"{s.get('asset')} [{result['block_reason']}]")
            continue
        sc = result["effective_score"]
        s["conf_label"]       = "Alta" if sc >= 80 else "Media" if sc >= 65 else "Baixa"
        s["size_modifier"]    = 1.0 * result["kelly_mult"]
        s["effective_score"]  = sc
        s["filter_notes"]     = result["notes"]
        to_send.append((s, None))

    if blocked_log:
        print(f"[SINAIS] Bloqueados pelos filtros: {' | '.join(blocked_log[:5])}")
        asyncio.create_task(log_event(
            "SINAIS_CYCLE",
            f"{scan_mode} | 0 enviados | {len(blocked_log)} bloqueado(s) pelos filtros",
            {"mode": scan_mode, "blocked": blocked_log[:20]}
        ))
        # Avisa no chat pessoal o motivo (throttle 1x/30min p/ não floodar).
        if not to_send and time.time() - _sinais_last_blocked_alert_ts > 1800:
            _sinais_last_blocked_alert_ts = time.time()
            _reasons = ' | '.join(blocked_log[:5])
            asyncio.create_task(send_alert(
                f"ℹ️ SINAIS {scan_mode}: {len(blocked_log)} candidato(s) encontrado(s) neste ciclo, "
                f"mas nenhum passou nos filtros. Motivos: {_reasons}"
            ))

    from risk_manager import get_leverage as _get_lev
    for signal_dict, pd_info in to_send:
        if TRADES_PER_SESSION > 0 and _sinais_session_count >= TRADES_PER_SESSION:
            break
        if not signal_dict.get("leverage"):
            signal_dict["leverage"] = _get_lev(signal_dict.get("asset", ""))
        signal_dict["perfil"] = scan_mode  # NORMAL | AGGRESSIVE

        # ── Filtro Claude Brain (opcional) — só acima de score 65 ────────────
        _brain_active = _claude_brain_enabled
        if _brain_active and signal_dict.get("confidence", 0) >= SINAIS_BRAIN_MIN_SCORE:
            try:
                from data_fetcher import get_ticker as _gt, get_funding_rate as _gfr
                from klines_cache import get_klines_cached as _gkl_s
                _tk, _fr, _kl_s = await asyncio.gather(
                    _gt(signal_dict.get("asset", "")),
                    _gfr(signal_dict.get("asset", "")),
                    _gkl_s(signal_dict.get("asset", ""), "15m", limit=20),
                    return_exceptions=True
                )
                # Calcula RSI/EMA/Vol a partir de klines (mesmo padrão do modo AUTO)
                _rsi_s = _ema_s = _vol_rel_s = "--"
                if not isinstance(_kl_s, Exception) and _kl_s is not None and len(_kl_s) >= 15:
                    _cl_s = _kl_s["close"]
                    _d_s  = _cl_s.diff()
                    _rs_s = _d_s.clip(lower=0).rolling(14).mean() / (-_d_s.clip(upper=0)).rolling(14).mean().replace(0, 1e-9)
                    _rsi_s = round(float((100 - 100 / (1 + _rs_s)).iloc[-1]), 1)
                    _ema21_s = _cl_s.ewm(span=21).mean().iloc[-1]
                    _ema_s   = "acima EMA21" if float(_cl_s.iloc[-1]) > float(_ema21_s) else "abaixo EMA21"
                    _vol_s   = _kl_s["volume"]
                    _vmean_s = float(_vol_s.iloc[:-1].mean())
                    _vol_rel_s = f"{float(_vol_s.iloc[-1]) / _vmean_s:.1f}x" if _vmean_s > 0 else "--"
                _brain_ctx = {
                    "fear_greed":         _market_cache.get("fear_greed", "--"),
                    "btc_change":         _market_cache.get("btc_change_24h", "--"),
                    "btc_funding":        _market_cache.get("btc_funding", "--"),
                    "btc_oi":             f"${_market_cache.get('btc_oi_usdt', 0):,.0f}",
                    "open_trades":        0,  # SINAIS canal não abre trades
                    "daily_pnl":          _daily_pnl,
                    "consecutive_losses": 0,
                    "banca":              BANCA_USDT if BANCA_USDT > 0 else (await _get_balance() or 10),
                    "asset_price":        _tk["price"]     if not isinstance(_tk, Exception) else "--",
                    "asset_vol24h":       f"${_tk.get('volume_24h',0):,.0f}" if not isinstance(_tk, Exception) else "--",
                    "asset_funding":      f"{_fr:+.4f}%"   if not isinstance(_fr, Exception) else "--",
                    "asset_rsi":          _rsi_s,
                    "asset_ema":          _ema_s,
                    "asset_vol_rel":      _vol_rel_s,
                    "news_summary":       "",
                }
                _brain_result = await claude_brain.analyze_signal(signal_dict, _brain_ctx)
                if not _brain_result.get("approve", True):
                    print(f"[CLAUDE BRAIN SINAIS] ❌ {signal_dict.get('asset', '')} rejeitado: {_brain_result.get('reason','')}")
                    continue
                
                # Aplica ajustes na signal_dict
                _leverage_mult = _brain_result.get("leverage_multiplier", 1.0)
                if _leverage_mult != 1.0:
                    signal_dict["leverage"] = max(1, int(signal_dict.get("leverage", 10) * _leverage_mult))
                
                _tp_adj = _brain_result.get("tp_adjust_pct", 1.0)
                _sl_adj = _brain_result.get("sl_adjust_pct", 1.0)
                
                if _tp_adj != 1.0 or _sl_adj != 1.0:
                    _ent = float(signal_dict["entry"])
                    if _tp_adj != 1.0:
                        signal_dict["tp1"] = round(_ent + (float(signal_dict["tp1"]) - _ent) * _tp_adj, 8)
                        signal_dict["tp2"] = round(_ent + (float(signal_dict["tp2"]) - _ent) * _tp_adj, 8)
                        signal_dict["tp3"] = round(_ent + (float(signal_dict["tp3"]) - _ent) * _tp_adj, 8)
                    if _sl_adj != 1.0:
                        signal_dict["stop_loss"] = round(_ent - (_ent - float(signal_dict["stop_loss"])) * _sl_adj, 8)
            except Exception as _be:
                print(f"[CLAUDE BRAIN SINAIS] erro contexto {signal_dict.get('asset', '')}: {_be}")

        # ── Adaptive Memory: ajuste de score baseado no histórico do ativo ──────
        try:
            _now_h  = datetime.utcnow()
            _adj    = await get_score_adjustment(
                signal_dict.get("asset", ""),
                signal_dict.get("timeframe", "15m"),
                _now_h.hour, _now_h.weekday(),
            )
            if _adj != 0.0:
                _prev_conf = float(signal_dict.get("confidence", 0))
                signal_dict["confidence"] = round(min(100, max(0, _prev_conf + _adj)), 1)
                signal_dict["adaptive_adj"] = _adj
                print(f"[ADAPTIVE] {signal_dict.get('asset')} score {_prev_conf:.1f}"
                      f" → {signal_dict['confidence']:.1f} ({_adj:+.1f} memória)")
        except Exception:
            pass
        # Atualiza estatisticas semanais
        _sinais_weekly["total"] += 1
        cl = signal_dict.get("conf_label", "Baixa")
        if cl == "Alta":   _sinais_weekly["alta"]  += 1
        elif cl == "Media": _sinais_weekly["media"] += 1
        else:              _sinais_weekly["baixa"] += 1
        sent_ok = await send_sinais_alert(signal_dict, pd_info)
        if sent_ok:
            _db_sig_id = signal_dict.get("db_signal_id")
            if _db_sig_id:
                asyncio.create_task(mark_signal_executed(
                    _db_sig_id, signal_dict.get("asset", ""), "vip"
                ))
            asyncio.create_task(upsert_daily_stats(0, None, signals_delta=1))
            # Rastreio de resultado (TP1/SL) é lido direto do banco em
            # job_sinais_outcome_watch via signals + telegram_sent(destination='vip'),
            # já gravado pelo mark_signal_executed acima — nada a fazer aqui.
        _sinais_session_count += 1
        await asyncio.sleep(1)

    pd_n  = sum(1 for _, p in to_send if p is not None)
    reg_n = sum(1 for _, p in to_send if p is None)
    print(f"[SINAIS] {scan_mode} | {len(to_send)} enviados (pd={pd_n} reg={reg_n})")
    if to_send:
        asyncio.create_task(log_event(
            "SINAIS_CYCLE", f"{scan_mode} | {len(to_send)} enviados (pd={pd_n} reg={reg_n})",
            {"mode": scan_mode}
        ))


async def job_pd_monitor():
    """
    Monitor continuo de Pump/Dump — roda a cada 60s no modo SINAIS.
    Envia alerta imediato ao Telegram sempre que detectar pump/dump,
    independente do canal de sinais regulares.
    Cooldown por ativo: 10 min (evita spam do mesmo simbolo).
    """
    from state import state
    if not state.sinais_enabled:
        return

    if OPERATION_MODE != "SINAIS":
        return

    pump_dump_engine.prune_alert_cooldowns()

    # Usa o cache existente (scan_pump_dump roda em paralelo via job_scan_market)
    new_alerts = pump_dump_engine.get_new_alerts(min_confidence=18)

    if not new_alerts:
        return

    # Ordena por score descendente, envia no max 3 por ciclo
    new_alerts.sort(key=lambda x: x["confidence"], reverse=True)

    sent = 0
    for alert in new_alerts[:3]:
        sym = alert["symbol"]
        ok = await send_pd_monitor_alert(alert)
        if ok:
            pump_dump_engine.mark_alert_sent(sym)
            sent += 1
            await asyncio.sleep(0.5)

    if sent:
        intensidades = [a["intensity"] for a in new_alerts[:sent]]
        print(f"[PD-MONITOR] {sent} alerta(s) enviado(s): {intensidades}")
    else:
        print(f"[PD-MONITOR] {len(new_alerts)} detectado(s) mas nenhum enviado (falha Telegram)")


async def job_pairs_arbitrage():
    """Varre e executa o motor de arbitragem estatística de pares."""
    global BANCA_USDT
    try:
        balance = await _get_balance()
        effective_banca = BANCA_USDT if BANCA_USDT > 0 else (balance or 100)
        await pairs_trading_engine.run_pairs_trading_cycle(effective_banca, paper_trading=PAPER_TRADING)
    except Exception as e:
        print(f"[PAIRS JOB] Erro na execução da arbitragem de pares: {e}")


async def job_pattern_scan():
    """
    Análise de padrões de candlestick MTF — roda a cada 4h.
    Escaneia todos os ativos do watchlist nos 9 timeframes (3m→1S),
    salva no DB e envia ao Telegram quando detecta padrões relevantes.
    Só envia se houver ao menos 2 padrões com força ≥ 2 em TFs ≥ 1h.
    """
    from candle_pattern_engine import analyze_asset_mtf, summarize_bias, MTF_TIMEFRAMES
    from database import save_candle_patterns
    from notifier import send_pattern_analysis

    watchlist = WATCHLIST[:5]  # primeiros 5 ativos para não sobrecarregar
    print(f"[PATTERN] Iniciando scan MTF para {watchlist}")

    for symbol in watchlist:
        try:
            mtf_results = await analyze_asset_mtf(symbol)
            bias_info   = summarize_bias(mtf_results)

            # Verifica se há padrões relevantes em TFs maiores (≥ 1h)
            major_tfs  = ["1h", "2h", "4h", "12h", "1d", "1w"]
            major_pats = [p for tf in major_tfs
                          for p in mtf_results.get(tf, [])
                          if p.strength >= 2]

            if len(major_pats) >= 2:
                await save_candle_patterns(symbol, mtf_results, bias_info["bias"])
                await send_pattern_analysis(symbol, mtf_results, bias_info)
                print(f"[PATTERN] {symbol} → {bias_info['bias']} | "
                      f"{bias_info['bull_count']}🟢 {bias_info['bear_count']}🔴")
            else:
                print(f"[PATTERN] {symbol} → sem padrões relevantes em TFs maiores")

            await asyncio.sleep(1.0)  # respeita rate limit da Binance
        except Exception as e:
            print(f"[PATTERN] Erro em {symbol}: {e}")


async def _job_daily_summary():
    stats = await get_performance_stats()
    await send_daily_summary(stats)


async def _job_weekly_sinais_stats():
    """Envia estatisticas semanais do modo SINAIS todo domingo."""
    global _sinais_weekly
    perf = await get_performance_stats()
    stats = {
        "total_sent": _sinais_weekly.get("total", 0),
        "alta":       _sinais_weekly.get("alta", 0),
        "media":      _sinais_weekly.get("media", 0),
        "baixa":      _sinais_weekly.get("baixa", 0),
        "period":     "semana",
        "win_rate":   perf.get("win_rate_pct", "--"),
        "avg_rr":     perf.get("avg_rr", "--"),
    }
    await send_weekly_sinais_stats(stats)
    # Reseta contadores semanais
    _sinais_weekly = {"total": 0, "alta": 0, "media": 0, "baixa": 0, "reset_date": None}


async def _fetch_realized_pnl(symbol: str, opened_at) -> float:
    """Busca o PnL REALIZADO da Binance para o símbolo desde a abertura do trade.
    Soma os registros REALIZED_PNL (futures income). Retorna 0.0 se falhar
    (mantém o comportamento antigo como fallback seguro)."""
    try:
        import calendar
        start_ms = 0
        if opened_at:
            try:
                _s  = str(opened_at).replace("Z", "").split("+")[0].split(".")[0]
                _dt = datetime.fromisoformat(_s)
                start_ms = int(calendar.timegm(_dt.timetuple()) * 1000) - 120_000  # 2min de folga
            except Exception:
                start_ms = 0

        def _inc():
            client = _get_binance_client_synced()
            kw = {"symbol": symbol, "incomeType": "REALIZED_PNL", "limit": 1000}
            if start_ms > 0:
                kw["startTime"] = start_ms
            return client.futures_income_history(**kw)

        rows = await asyncio.to_thread(_inc)
        return round(sum(float(r.get("income", 0) or 0) for r in rows), 6)
    except Exception as e:
        print(f"[SYNC-PNL] {symbol} falha ao buscar realized PnL: {e}")
        return 0.0


async def _job_sync_binance():
    """Sincroniza posições abertas da Binance com o DB a cada 2 min.
    Quando a Binance fecha um trade (SL/TP), grava o PnL REALIZADO real
    (corrige o bug que zerava o PnL) e notifica o fechamento no Telegram."""
    global _daily_pnl
    try:
        def _fetch_positions():
            client = _get_binance_client_synced()
            return client.futures_position_information()

        positions = await asyncio.to_thread(_fetch_positions)
        active_pos = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
        # Mapa symbol_DIRECTION para detectar mismatch de direção
        open_map: dict[str, str] = {}
        for p in active_pos:
            amt = float(p.get("positionAmt", 0))
            pos_side = p.get("positionSide", "BOTH")
            if pos_side in ("LONG", "SHORT"):
                open_map[f"{p['symbol']}_{pos_side}"] = pos_side
            else:
                direction = "LONG" if amt > 0 else "SHORT"
                open_map[f"{p['symbol']}_{direction}"] = direction
        open_symbols = {p["symbol"] for p in active_pos}

        db_trades = await get_open_trades()
        for t in db_trades:
            symbol = t["asset"]
            db_dir = str(t.get("direction", "LONG")).upper()
            key    = f"{symbol}_{db_dir}"
            if symbol not in open_symbols or key not in open_map:
                # ── Captura o PnL REALIZADO real da Binance (antes gravava 0) ──
                _pnl_real = await _fetch_realized_pnl(symbol, t.get("opened_at"))
                _lev      = float(t.get("leverage", 1) or 1)
                _margin   = (float(t.get("size_usdt", 0) or 0) / _lev) if _lev else 0.0
                _pnl_pct  = (_pnl_real / _margin * 100.0) if _margin > 0 else 0.0
                try:
                    _exit_px = ws_feed.get_price(symbol) or float(t.get("entry_price", 0) or 0)
                except Exception:
                    _exit_px = float(t.get("entry_price", 0) or 0)

                t["status"]    = "CLOSED"
                t["closed_at"] = datetime.utcnow().isoformat()
                t["current_price"] = _exit_px
                t["pnl_usdt"]  = _pnl_real
                t["pnl_pct"]   = _pnl_pct
                await update_trade_close(t["id"], _exit_px, _pnl_real, _pnl_pct)
                _active_trades_cache.pop(t.get("id", ""), None)

                _is_win = _pnl_real > 0
                _daily_pnl += _pnl_real
                _register_auto_pnl(_pnl_real)  # alimenta o kill-switch -20%
                try:
                    await upsert_daily_stats(_pnl_real, _is_win)
                except Exception:
                    pass
                # Notifica o fechamento (stop/alvo) no Telegram — em QUALQUER modo
                _res_tag = "🎯 ALVO" if _is_win else "🛑 STOP"
                asyncio.create_task(send_trade_closed(t, f"{_res_tag} (SL/TP Binance)"))
                # #10/#14 — fecha o loop de aprendizado: registra o resultado real
                # (alimenta signal_outcomes -> base do ML/score adaptativo), que antes
                # só era gravado no fechamento manual.
                try:
                    asyncio.create_task(record_signal_outcome(
                        int(t.get("signal_db_id", 0) or 0), symbol, db_dir,
                        t.get("timeframe", "15m") or "15m",
                        float(t.get("entry_price", 0) or 0), _exit_px, _pnl_pct,
                        "WIN" if _is_win else "LOSS", str(t.get("reason", ""))[:80], 0.0,
                    ))
                    _sync_tf = t.get("timeframe", "15m") or "15m"
                    _sync_now = datetime.utcnow()
                    _sync_tags = str(t.get("reason", ""))[:80]
                    asyncio.create_task(upsert_asset_profile(
                        symbol, _sync_tf, _sync_now.hour, _sync_now.weekday(), _is_win, _pnl_pct
                    ))
                    asyncio.create_task(upsert_confluence_pattern(
                        symbol, _sync_tags, _is_win, _pnl_pct
                    ))
                except Exception as _ro_ex:
                    print(f"[SYNC] record_signal_outcome falhou: {_ro_ex}")
                print(f"[SYNC] {symbol} {db_dir} fechado na Binance → PnL realizado ${_pnl_real:.4f} gravado")
        _register_binance_ok()  # chamada Binance bem-sucedida → zera streak de erros
    except Exception as e:
        print(f"[SYNC] Erro: {e}")
        await _register_binance_error("sync")  # alimenta o circuit breaker


def _check_daily_target_notify():
    """Dispara notificacao de meta diaria atingida (uma vez por dia)."""
    global _daily_target_notified
    if DAILY_TARGET_USDT > 0 and _daily_pnl >= DAILY_TARGET_USDT and not _daily_target_notified:
        _daily_target_notified = True
        asyncio.create_task(send_daily_target_reached(_daily_pnl, DAILY_TARGET_USDT))
        print(f"[META] Meta diaria atingida! ${_daily_pnl:.2f} >= ${DAILY_TARGET_USDT:.2f}")


def _build_open_positions_summary() -> str:
    """Resumo das posições abertas no formato MARGEM/ALAVANCAGEM/PnL aberto.
    MARGEM = margem real comprometida (notional / alavancagem). PnL aberto é
    calculado AO VIVO (preço atual vs entrada). Usado pelo /status e pelo
    relatório periódico de 10 min."""
    trades = [t for t in _active_trades_cache.values() if isinstance(t, dict)]
    if not trades:
        return "📭 *Nenhuma posição aberta no momento.*"

    lines = [f"⚠️ *POSIÇÕES ABERTAS ({len(trades)})*"]
    _total_pnl = 0.0
    for t in trades:
        _dir      = str(t.get("direction", "?")).upper().replace("DIRECTION.", "")
        _icon     = "🟢" if "LONG" in _dir else "🔴"
        _lev      = float(t.get("leverage", 1) or 1)
        _notional = float(t.get("size_usdt", 0) or 0)
        _margin   = (_notional / _lev) if _lev else _notional
        _entry    = float(t.get("entry_price", 0) or 0)
        _cur      = float(t.get("current_price", 0) or 0) or _entry
        # PnL aberto AO VIVO (preço atual vs entrada), em vez do valor estático do cache
        if _entry > 0 and _cur > 0 and _notional > 0:
            _sign  = 1.0 if "LONG" in _dir else -1.0
            _qty   = _notional / _entry
            _pnl_u = (_cur - _entry) * _qty * _sign
            _pnl_p = ((_cur - _entry) / _entry) * 100.0 * _sign * _lev
        else:
            _pnl_u = float(t.get("pnl_usdt", 0) or 0)
            _pnl_p = float(t.get("pnl_pct", 0) or 0)
        _total_pnl += _pnl_u
        _su = "+" if _pnl_u >= 0 else "-"
        _sp = "+" if _pnl_p >= 0 else "-"
        _paper = " 🔸PAPER" if t.get("paper") else ""
        lines.append(
            f"\n{_icon}{t.get('asset','?')}{_icon}{_dir}{_paper}\n\n"
            f"MARGEM ${_margin:.2f} (USDT)\n\n"
            f"ALAVANCAGEM {int(_lev)}X\n\n"
            f"💰 PNL ABERTO ATUAL: {_su}${abs(_pnl_u):.2f} / {_sp}{abs(_pnl_p):.1f}%"
        )
    if len(trades) > 1:
        _st = "+" if _total_pnl >= 0 else "-"
        lines.append(f"\n━━━━━━━━\n💰 *PNL ABERTO TOTAL: {_st}${abs(_total_pnl):.2f}*")
    return "\n".join(lines)


async def job_open_positions_report():
    """Relatório periódico (10 min) das posições abertas no Telegram pessoal.
    Silencioso quando não há posições, para evitar ruído."""
    try:
        if not any(isinstance(t, dict) for t in _active_trades_cache.values()):
            return
        msg = _build_open_positions_summary()
        if _auto_killswitch_tripped:
            msg += f"\n\n🛑 *Kill-switch -{AUTO_KILLSWITCH_PCT:.0f}% ATIVO* — sem novas entradas (use `/killswitch reset`)."
        await send_alert(msg)
    except Exception as e:
        print(f"[STATUS-REPORT] Erro: {e}")


async def job_update_trades():
    """Update PnL and trailing stops for all open trades."""
    global _active_trades_cache, _consecutive_losses, _daily_pnl
    open_trades = await get_open_trades()
    for trade_data in open_trades:
        try:
            price = ws_feed.get_price(trade_data["asset"])
            if price is None:
                ticker = await get_ticker(trade_data["asset"])
                price = ticker["price"]
            from models import ActiveTrade
            trade = ActiveTrade(**{k: v for k, v in trade_data.items()
                                   if k in ActiveTrade.model_fields})

            # ── DCA Update Logic ──────────────────────────────────────────────
            if dca_engine.is_dca_enabled() and trade_data.get("trade_type") != "GRID" and trade.asset in dca_engine._dca_positions:
                level_info = await dca_engine.get_next_dca_level(trade.asset, price)
                if level_info:
                    dca_pos = dca_engine._dca_positions[trade.asset]
                    qty_usd_to_add = level_info["qty_usdt"] * trade.leverage
                    
                    temp_entries = dca_pos.entries + [(price, level_info["qty_usdt"])]
                    temp_avg = sum(p * q for p, q in temp_entries) / sum(q for _, q in temp_entries)
                    if dca_pos.direction == "LONG":
                        new_sl = temp_avg - dca_pos.atr * dca_engine.DCA_SL_MULT
                        new_tp = temp_avg + dca_pos.atr * dca_engine.DCA_TP_MULT
                    else:
                        new_sl = temp_avg + dca_pos.atr * dca_engine.DCA_SL_MULT
                        new_tp = temp_avg - dca_pos.atr * dca_engine.DCA_TP_MULT
                        
                    if PAPER_TRADING:
                        exec_res = {"status": "OK", "qty": qty_usd_to_add / price}
                    else:
                        exec_res = await asyncio.to_thread(
                            execute_dca_order,
                            trade.asset, trade.direction.value, qty_usd_to_add, new_sl, new_tp
                        )
                        
                    if exec_res["status"] == "OK":
                        dca_engine.execute_dca_level(trade.asset, price, level_info["qty_usdt"], level_info["level"])
                        trade.entry_price = dca_pos.avg_entry
                        trade.size_usdt = dca_pos.total_usdt * trade.leverage
                        trade.stop_loss = dca_pos.stop_loss
                        trade.tp1 = dca_pos.take_profit
                        trade.tp2 = dca_pos.take_profit
                        trade.tp3 = dca_pos.take_profit
                        
                        trade_dict = trade.model_dump()
                        trade_dict["opened_at"] = trade_data.get("opened_at")
                        trade_dict["trade_type"] = trade_data.get("trade_type", "DAY_TRADE")
                        trade_dict["paper"] = trade_data.get("paper", True)
                        await save_trade(trade_dict)
                        _active_trades_cache[trade.id] = trade_dict
                        
                        await send_alert(
                            f"⚡ *DCA Nível {level_info['level']} executado* em `{trade.asset}`\n"
                            f"Preço: `${price:.6f}` | Novo preço médio: `${trade.entry_price:.6f}`\n"
                            f"Novo SL: `${trade.stop_loss:.6f}` | Novo TP: `${trade.tp1:.6f}`"
                        )
                        
                exit_reason = dca_engine.check_dca_exit(trade.asset, price)
                if exit_reason:
                    if not PAPER_TRADING:
                        await asyncio.to_thread(
                            close_position, trade.asset, trade.direction, _get_binance_client_synced()
                        )
                    dca_res = dca_engine.close_dca_position(trade.asset, price, exit_reason)
                    
                    trade.status = "CLOSED"
                    trade.closed_at = datetime.utcnow()
                    trade.current_price = price
                    trade.pnl_usdt = dca_res["pnl_usdt"]
                    trade.pnl_pct = dca_res["pnl_pct"]
                    
                    await update_trade_close(trade.id, price, dca_res["pnl_usdt"], dca_res["pnl_pct"])
                    _active_trades_cache.pop(trade.id, None)
                    
                    await send_trade_closed(trade.model_dump(), f"DCA {exit_reason} ({dca_res['levels_done']} níveis)")
                    
                    _is_win = dca_res["pnl_usdt"] > 0
                    await upsert_daily_stats(dca_res["pnl_usdt"], _is_win)
                    # P1 — fecha o loop de aprendizado tambem no fechamento via DCA
                    _dca_tf  = getattr(trade, "timeframe", "15m") or "15m"
                    _dca_now = datetime.utcnow()
                    asyncio.create_task(upsert_asset_profile(
                        trade.asset, _dca_tf, _dca_now.hour, _dca_now.weekday(), _is_win, dca_res["pnl_pct"]
                    ))
                    asyncio.create_task(upsert_confluence_pattern(
                        trade.asset, str(getattr(trade, "reason", ""))[:80], _is_win, dca_res["pnl_pct"]
                    ))
                    asyncio.create_task(record_signal_outcome(
                        0, trade.asset, trade.direction.value, _dca_tf,
                        float(getattr(trade, "entry_price", 0) or 0), price, dca_res["pnl_pct"],
                        "WIN" if _is_win else "LOSS", f"DCA {exit_reason}"[:80], 0.0
                    ))

                    if dca_res["pnl_usdt"] < 0:
                        _consecutive_losses += 1
                        _consecutive_wins = 0
                        await _maybe_trip_loss_breaker()   # circuit breaker por perdas
                    else:
                        _consecutive_wins += 1
                        if _consecutive_wins >= _ANTI_MARTINGALE_WINS_TO_RESET:
                            _consecutive_losses = 0
                            _consecutive_wins = 0
                    _daily_pnl += dca_res["pnl_usdt"]
                    _register_auto_pnl(dca_res["pnl_usdt"])  # alimenta kill-switch -20%
                    _check_daily_target_notify()
                    _session_returns.append(dca_res["pnl_pct"])
                    _rm = _calc_risk_metrics()
                    if _rm.get("pause_alert"):
                        asyncio.create_task(send_alert(_rm["pause_alert"]))
                    asyncio.create_task(ml_engine.train_all_models())
                    
                continue

            result = await process_trade_update(trade, price)
            trade = result["trade"]

            # Execute actions on Binance (sync calls em thread para não bloquear event loop)
            for action in result["actions"]:
                if action["action"] == "UPDATE_STOP":
                    _new_stop = action["new_stop"]
                    _asset    = trade.asset
                    _dir      = trade.direction.value
                    await asyncio.to_thread(
                        lambda: update_stop_loss(_asset, _dir, _new_stop, _get_binance_client_synced())
                    )
                    # Notificação de trailing stop desativada — só abrir/fechar ordem ou atingir alvo/stop notificam.
                elif action["action"] == "PARTIAL_CLOSE":
                    _asset = trade.asset
                    _dir   = trade.direction.value
                    _pct   = action["pct"]
                    _orig_size = action.get("original_size", trade.size_usdt)
                    _qty_to_close = (_orig_size / trade.entry_price) * _pct
                    
                    if trade_data.get("paper") or PAPER_TRADING:
                        print(f"[TRADE UPDATE] PAPER partial close {_asset} {_pct*100:.0f}%")
                    else:
                        # TPs parciais reais ja ficam em ordens reduce-only na Binance.
                        # Evita mandar uma segunda ordem MARKET e fechar mais que o previsto.
                        await asyncio.to_thread(
                            lambda: update_stop_loss(_asset, _dir, trade.stop_loss, _get_binance_client_synced())
                        )
                    
                    # Notify Telegram
                    await send_alert(
                        f"⚠️ *PARTIAL CLOSE ({action.get('reason', 'TP')})*\n"
                        f"Ativo: `{_asset}` | Direção: `{_dir}`\n"
                        f"Fechado: `{_pct*100:.0f}%` da posição (Qty: `{_qty_to_close:.6f}`)\n"
                        f"Novo Stop Loss movido para: `${trade.stop_loss:,.6f}`"
                    )
                elif action["action"] == "CLOSE":
                    _asset = trade.asset
                    _dir   = trade.direction
                    await asyncio.to_thread(
                        lambda: close_position(_asset, _dir, _get_binance_client_synced())
                    )
                    _exit_px   = float(price)
                    _entry     = float(trade.entry_price)
                    _qty       = float(trade.size_usdt) / _entry if _entry > 0 else 0.0
                    
                    if getattr(trade.direction, "value", trade.direction) == "LONG":
                        _pnl_close = (_exit_px - _entry) * _qty * trade.leverage
                        _pnl_pct   = ((_exit_px - _entry) / _entry) * 100.0 * trade.leverage
                    else:
                        _pnl_close = (_entry - _exit_px) * _qty * trade.leverage
                        _pnl_pct   = ((_entry - _exit_px) / _entry) * 100.0 * trade.leverage
                        
                    _closed_dict = trade.model_dump()
                    _closed_dict["pnl_usdt"] = _pnl_close
                    _closed_dict["pnl_pct"] = _pnl_pct

                    # FIX: grava exit_price e PnL real no DB
                    asyncio.create_task(update_trade_close(
                        trade.id, _exit_px, _pnl_close, _pnl_pct
                    ))

                    asyncio.create_task(send_trade_closed(
                        _closed_dict, action.get("reason", "CLOSE")
                    ))
                    if _pnl_close > 0:
                        _eff_mode = _resolve_effective_mode()
                        asyncio.create_task(send_social_proof(_closed_dict, _eff_mode))

                    # Adaptive Memory: atualiza perfil do ativo com resultado real
                    _is_win = _pnl_close > 0
                    _tf     = _closed_dict.get("timeframe", "15m")
                    _now    = datetime.utcnow()
                    _tags   = _closed_dict.get("reason", "")
                    asyncio.create_task(upsert_asset_profile(
                        _asset, _tf, _now.hour, _now.weekday(), _is_win, _pnl_pct
                    ))
                    asyncio.create_task(upsert_confluence_pattern(
                        _asset, _tags[:80], _is_win, _pnl_pct
                    ))
                    # P1 — fecha o loop de aprendizado: registra o resultado do sinal
                    # (alimenta signal_outcomes -> base para ML e score adaptativo)
                    asyncio.create_task(record_signal_outcome(
                        int(getattr(trade, "signal_db_id", 0) or 0), _asset,
                        trade.direction.value, _tf,
                        float(getattr(trade, "entry_price", 0) or 0),
                        _exit_px, _pnl_pct, "WIN" if _is_win else "LOSS", _tags[:80], 0.0
                    ))
                    asyncio.create_task(upsert_daily_stats(_pnl_close, _is_win))

                    # Asset Memory legado
                    try:
                        _asset_memory.record_trade(_asset, _pnl_close)
                    except Exception:
                        pass
                    # Anti-martingale: rastreia ganhos/perdas consecutivos
                    if _pnl_close < 0:
                        _consecutive_losses += 1
                        _consecutive_wins    = 0
                        await _maybe_trip_loss_breaker()   # circuit breaker por perdas
                    else:
                        _consecutive_wins += 1
                        # FIX: exige ANTI_MARTINGALE_WINS_TO_RESET wins para zerar perdas
                        if _consecutive_wins >= _ANTI_MARTINGALE_WINS_TO_RESET:
                            _consecutive_losses = 0
                            _consecutive_wins   = 0
                    # Meta diária: atualiza PnL
                    _daily_pnl += _pnl_close
                    _register_auto_pnl(_pnl_close)  # alimenta kill-switch -20%
                    _check_daily_target_notify()
                    # Sharpe/Sortino: registra retorno da sessao
                    _session_returns.append(_pnl_pct)
                    _rm = _calc_risk_metrics()
                    if _rm.get("pause_alert"):
                        asyncio.create_task(send_alert(_rm["pause_alert"]))
                    # ML: verifica se precisa retreinar
                    asyncio.create_task(ml_engine.train_all_models())
            _active_trades_cache[trade.id] = trade.model_dump()
        except Exception as e:
            print(f"[TRADE UPDATE] {trade_data.get('id')} error: {e}")


# ── Telegram Command Handler ──────────────────────────────────────────────────

async def _telegram_command_handler(text: str) -> str:
    """
    Processa comandos enviados pelo usuario via Telegram.
    Retorna a resposta como string (sera enviada de volta ao chat).
    """
    global OPERATION_MODE, CURRENT_MODE, BANCA_USDT, TRADES_PER_SESSION, PAPER_TRADING
    global GRID_PROFIT_TARGET_USDT, GRID_PAIRS, GRID_LEVERAGE, EXEC_MODE
    global DUAL_MODE_ENABLED, SINAIS_PROFILE, _claude_brain_enabled, _sinais_claude_brain, _exec_claude_brain
    global BOT_PAUSED

    parts = text.strip().split()
    cmd   = parts[0].lower()
    args  = parts[1:] if len(parts) > 1 else []

    # /continuar e /pausar — resposta ao CIRCUIT BREAKER (e pausa/retomada manual)
    if cmd in ("/continuar", "/continue", "/retomar"):
        BOT_PAUSED = False
        return _cb_resume()
    global _cb_pending
    if cmd in ("/pausar", "/pause"):
        BOT_PAUSED = True
        _cb_pending = False
        print("[CONTROL] Pausado pelo usuário via /pausar.")
        return ("⏸️ *Bot pausado* — não abre novas entradas. Posições abertas e "
                "monitoramento continuam. Use `/continuar` para retomar.")
    # /parar — STOP forte: pausa entradas E corta operação com dinheiro REAL (vira paper)
    if cmd in ("/parar", "/pararbot"):
        BOT_PAUSED = True
        PAPER_TRADING = True
        _cb_pending = False
        print("[CONTROL] PARADO pelo usuário via /parar (BOT_PAUSED + PAPER ON).")
        return ("🛑 *Bot PARADO* — sem novas entradas e *sem operar dinheiro real* "
                "(modo simulação ON).\n"
                "• `/continuar` retoma as entradas\n"
                "• `/paper off` reativa execução REAL")

    # /menu ou /painel — cabeçalho do Painel de Controle (os botões são
    # anexados pelo notifier; aqui só retornamos o texto com o estado atual)
    if cmd in ("/menu", "/painel"):
        paper_str = "ON (simulacao)" if PAPER_TRADING else "OFF (conta REAL)"
        return (
            f"*🎛️ TRADER 001 — Painel de Controle*\n\n"
            f"Modo atual: `{OPERATION_MODE}`\n"
            f"Perfil atual: `{CURRENT_MODE}`\n"
            f"Paper: `{paper_str}`\n\n"
            f"Toque abaixo para escolher *modo* e *perfil*.\n"
            f"_(💰 = opera com dinheiro real, pede confirmacao)_"
        )

    # /ajuda ou /start
    if cmd in ("/ajuda", "/start", "/help"):
        brain_str = "ON" if _claude_brain_enabled else "OFF"
        return (
            f"*TRADER 001 — Menu Completo*\n"
            f"Modo: `{OPERATION_MODE}` | Perfil: `{CURRENT_MODE}` | Brain: `{brain_str}`\n\n"
            "👉 *Use `/menu` para o painel de botões* (escolher modo e perfil com 1 toque).\n\n"
            "*📊 Informacoes:*\n"
            "`/status` — Estado atual do bot\n"
            "`/resumo` — Snapshot rapido (saldo, PnL, sinais)\n"
            "`/sinais` — Ultimos sinais gerados com scores\n"
            "`/trades` — Trades abertos com PnL\n"
            "`/posicao BTCUSDT` — Detalhe de uma posicao\n"
            "`/performance` — Estatisticas de performance\n"
            "`/pnl` — PnL detalhado: hoje / historico\n"
            "`/risco` — Metricas de risco e exposicao\n"
            "`/mercado` — Market Intelligence\n\n"
            "*⚙️ Modos de Operacao:*\n"
            "`/auto on` — AUTONOMO (executa sozinho)\n"
            "`/auto off` — SUPERVISIONADO (pede aprovacao)\n"
            "`/auto grid` — GRID (scalp automatico)\n"
            "`/auto sinais` — So SINAIS (sem operar)\n"
            "_(1 modo por vez — ativar um desliga o anterior)_\n\n"
            "*🧠 Claude Brain:*\n"
            "`/brain on|off` — Liga/desliga Brain (todos canais)\n"
            "`/brain sinais on|off` — Brain so no canal SINAIS\n"
            "`/brain exec on|off` — Brain so no canal execucao\n\n"
            "*🔧 Configuracao:*\n"
            "`/modo normal|agressivo|conservador` — Perfil de risco\n"
            "`/banca 500` — Definir banca em USDT\n"
            "`/ntrades 3` — Limite de trades por sessao\n"
            "`/paper on|off` — Paper trading\n"
            "`/grid alvo 10` — Alvo por ciclo GRID\n"
            "`/macro pausar|continuar` — Pausar em evento macro\n\n"
            "*⚡ Acoes:*\n"
            "`/scan` — Forcar varredura agora\n"
            "`/fechar BTCUSDT` — Fechar posicao especifica\n"
            "`/fechar tudo` — Fechar TODAS as posicoes\n"
            "`/killswitch` — Estado do freio -20% (AUTONOMO)\n"
            "`/killswitch reset` — Libera apos disparo do -20%\n"
        )

    # /status
    if cmd == "/status":
        balance_str = f"${_balance_cache.get('available_balance', 0):.2f}" if _balance_cache else "N/A"
        trades_n    = len(_active_trades_cache)
        paper_str   = "ON (simulacao)" if PAPER_TRADING else "OFF (real)"
        brain_str   = "ON" if _claude_brain_enabled else "OFF"
        
        mode_desc = f"Modo: `{OPERATION_MODE}` | Perfil: `{CURRENT_MODE}` | Claude Brain: `{brain_str}`"

        # Estado do kill-switch -20% (modo AUTÔNOMO)
        if _auto_killswitch_tripped:
            ks_str = f"🛑 ATIVO (perda `${_auto_session_pnl:.2f}`) — `/killswitch reset` p/ retomar"
        else:
            _ks_ref = _auto_killswitch_ref_banca()
            _ks_lim = _ks_ref * AUTO_KILLSWITCH_PCT / 100.0
            ks_str  = f"OK — sessao `{'+' if _auto_session_pnl >= 0 else ''}${_auto_session_pnl:.2f}` (limite `-${_ks_lim:.2f}`)"

        return (
            f"*TRADER 001 — Status*\n\n"
            f"{mode_desc}\n"
            f"Paper Trading: `{paper_str}`\n"
            f"Banca: `${BANCA_USDT:.2f}` | Trades/sessao: `{TRADES_PER_SESSION}`\n"
            f"Cadencia entradas: `{_entry_cadence_s()}s` ({CURRENT_MODE})\n"
            f"Kill-switch -{AUTO_KILLSWITCH_PCT:.0f}%: {ks_str}\n"
            f"Saldo disponivel: `{balance_str}`\n"
            f"PnL diario: `{'+' if _daily_pnl >= 0 else ''}${_daily_pnl:.2f}`\n"
            f"Sinais na fila: `{len(_latest_signals)}`\n\n"
            f"{_build_open_positions_summary()}"
        )

    # /killswitch [reset|status] — kill-switch -20% do modo AUTÔNOMO
    if cmd in ("/killswitch", "/reset20", "/ks"):
        sub = args[0].lower() if args else "status"
        if sub in ("reset", "resetar", "off", "liberar"):
            _reset_auto_killswitch()
            return (
                f"✅ *Kill-switch resetado.*\n"
                f"Sessao autonoma reiniciada (PnL zerado, banca recapturada).\n"
                f"O bot volta a abrir entradas no proximo ciclo."
            )
        _ref = _auto_killswitch_ref_banca()
        _lim = _ref * AUTO_KILLSWITCH_PCT / 100.0
        _st  = "🛑 ATIVO (parado)" if _auto_killswitch_tripped else "✅ OK (operando)"
        return (
            f"*Kill-switch -{AUTO_KILLSWITCH_PCT:.0f}%*\n\n"
            f"Estado: {_st}\n"
            f"Banca ref.: `${_ref:.2f}`\n"
            f"PnL sessao: `{'+' if _auto_session_pnl >= 0 else ''}${_auto_session_pnl:.2f}`\n"
            f"Limite de perda: `-${_lim:.2f}`\n\n"
            f"_Use `/killswitch reset` para liberar após disparo._"
        )

    # /scan
    if cmd == "/scan":
        asyncio.create_task(job_scan_market())
        return "Varredura iniciada. Resultados chegam em 1-2 minutos via notificacoes de sinal."

    # /sinais
    if cmd == "/sinais":
        if not _latest_signals:
            return "Nenhum sinal recente. Use /scan para rodar uma varredura agora."
        lines = ["*Ultimos Sinais:*\n"]
        for s in _latest_signals[:8]:
            dir_c = "L" if "LONG" in str(s.get("direction","")) else "S"
            lines.append(
                f"`{s.get('asset','?')}` [{dir_c}] score={s.get('score_total',0):.0f} "
                f"tf={s.get('timeframe','?')} rr={s.get('rr',0):.1f}"
            )
        return "\n".join(lines)

    # /performance
    if cmd == "/performance":
        from database import get_performance_stats
        try:
            stats = await get_performance_stats()
            wr    = stats.get("win_rate", 0)
            pnl   = stats.get("total_pnl", 0)
            total = stats.get("total_trades", 0)
            pf    = stats.get("profit_factor", 0)
            return (
                f"*Performance Geral*\n\n"
                f"Total de trades: `{total}`\n"
                f"Taxa de acerto: `{wr:.1f}%`\n"
                f"PnL total: `{'+' if pnl>=0 else ''}${pnl:.2f}`\n"
                f"Fator de lucro: `{pf:.2f}`"
            )
        except Exception as e:
            return f"Erro ao buscar performance: {e}"

    # /trades
    if cmd == "/trades":
        if not _active_trades_cache:
            return "Nenhum trade aberto no momento."
        lines = ["*Trades Abertos:*\n"]
        for t in list(_active_trades_cache.values())[:10]:
            if isinstance(t, dict):
                lines.append(
                    f"`{t.get('asset','?')}` {t.get('direction','?')} "
                    f"entrada=${t.get('entry_price',0):.4f} "
                    f"pnl={t.get('pnl_pct',0):+.1f}%"
                )
        return "\n".join(lines)

    # /auto on|off|grid|sinais [confirmar]
    if cmd == "/auto":
        val     = args[0].lower() if args else ""
        confirm = len(args) > 1 and args[1].lower() in ("confirmar", "sim", "ok", "yes")

        # ── Volta para SINAIS — sempre permitido, sem validacao ─────────────
        if val in ("sinais", "signal", "signals"):
            await bot_state.activate_mode("SINAIS", profile=CURRENT_MODE)
            sync_state_to_globals()
            wl_str = ", ".join(SINAIS_WATCHLIST) if SINAIS_WATCHLIST else "watchlist global"
            return (
                f"Modo SINAIS ativado!\n"
                f"Watchlist: {wl_str}\n"
                f"Transmitindo sinais sem executar trades.\n"
                f"Perfil: {CURRENT_MODE} | Pump/Dump monitor: ativo"
            )

        # ── Modos de execucao real — validar antes de ativar ────────────────
        if val in ("on", "off", "grid"):
            # Coleta configuracoes atuais
            bal_usdt = float(_balance_cache.get("available_balance", 0)) if _balance_cache else 0.0
            banca_efetiva = BANCA_USDT if BANCA_USDT > 0 else bal_usdt
            from config import MODE_SETTINGS
            cfg = MODE_SETTINGS.get(CURRENT_MODE, {})
            risk_pct = cfg.get("risk_pct", 1.0)
            max_trades = cfg.get("max_open_trades", 5)
            trades_sess = TRADES_PER_SESSION if TRADES_PER_SESSION > 0 else "ilimitado"

            # Itens de validacao — cada um retorna (ok: bool, descricao: str)
            checks = []

            if val in ("on", "off"):
                checks.append((
                    banca_efetiva > 0,
                    f"Banca: {'$' + str(round(banca_efetiva, 2)) if banca_efetiva > 0 else '⚠️  $0 — configure /banca <valor> antes'}"
                ))
                checks.append((
                    True,
                    f"Perfil: {CURRENT_MODE}  |  Risco/trade: {risk_pct}%  |  Max trades: {max_trades}"
                ))
                checks.append((
                    TRADES_PER_SESSION >= 0,
                    f"Trades/sessao: {trades_sess}  |  Paper: {'SIM' if PAPER_TRADING else 'NAO — conta REAL'}"
                ))
                checks.append((
                    True,
                    f"Alavancagem: BTC/ETH 15x | Altcoins 5x (via config)"
                ))
            elif val == "grid":
                checks.append((
                    banca_efetiva > 0,
                    f"Banca: {'$' + str(round(banca_efetiva, 2)) if banca_efetiva > 0 else '⚠️  $0 — configure /banca <valor>'}"
                ))
                checks.append((
                    bool(GRID_PAIRS),
                    f"Pares: {', '.join(GRID_PAIRS) if GRID_PAIRS else '⚠️  vazio — configure /grid pares BTC ETH'}"
                ))
                checks.append((
                    GRID_PROFIT_TARGET_USDT > 0,
                    f"Alvo/ciclo: {'$' + str(round(GRID_PROFIT_TARGET_USDT, 2)) if GRID_PROFIT_TARGET_USDT > 0 else '⚠️  $0 — configure /grid alvo 10'}"
                ))
                checks.append((
                    GRID_LEVERAGE > 0,
                    f"Alavancagem: {GRID_LEVERAGE}x"
                ))

            all_ok  = all(ok for ok, _ in checks)
            has_warn = not all_ok

            # Resumo de validacao
            lines = ["*Verificacao antes de ativar:*\n"]
            for ok, desc in checks:
                icon = "✅" if ok else "❌"
                lines.append(f"{icon} {desc}")

            summary = "\n".join(lines)

            # Se ha erros e nao veio confirmacao explicita, bloqueia
            if has_warn and not confirm:
                return (
                    f"{summary}\n\n"
                    f"⚠️ *Ha configuracoes incompletas.*\n"
                    f"Para prosseguir mesmo assim:\n"
                    f"`/auto {val} confirmar`\n\n"
                    f"Ou corrija primeiro e tente novamente."
                )

            # Tudo ok (ou usuario confirmou) — ativa o modo ÚNICO (single-mode)
            if val == "on":
                await bot_state.activate_mode("AUTONOMOUS", profile=CURRENT_MODE)
                sync_state_to_globals()
                _reset_auto_killswitch()  # sessão autônoma limpa (kill-switch -20%)
                return (
                    f"{summary}\n\n"
                    f"✅ *Modo AUTONOMO ativado!*\n"
                    f"Bot executa trades sem aprovacao.\n"
                    f"Banca efetiva: ${banca_efetiva:.2f}"
                )
            elif val == "off":
                await bot_state.activate_mode("SUPERVISED", profile=CURRENT_MODE)
                sync_state_to_globals()
                return (
                    f"{summary}\n\n"
                    f"✅ *Modo SUPERVISIONADO ativado!*\n"
                    f"Bot envia sinal e aguarda sua aprovacao antes de executar."
                )
            elif val == "grid":
                await bot_state.activate_mode("GRID", profile=CURRENT_MODE)
                sync_state_to_globals()
                asyncio.create_task(job_grid_scan())
                return (
                    f"{summary}\n\n"
                    f"✅ *Modo GRID ativado!*\n"
                    f"Buscando entradas em: {', '.join(GRID_PAIRS)}"
                )

        return (
            f"Uso: /auto on|off|grid|sinais  (atual: {OPERATION_MODE})\n"
            f"Para forcar ativacao: /auto on confirmar"
        )

    # /grid — configurar grid
    if cmd == "/grid":
        if not args:
            total_cycles = sum(_grid_cycles.values())
            cycles_detail = " | ".join(f"{s}:{n}" for s, n in _grid_cycles.items()) if _grid_cycles else "nenhum ciclo"
            return (
                f"*GRID STATUS*\n\n"
                f"Pares: `{', '.join(GRID_PAIRS)}`\n"
                f"Alvo/ciclo: `${GRID_PROFIT_TARGET_USDT:.2f}`\n"
                f"Alavancagem: `{GRID_LEVERAGE}x`\n"
                f"Max simultaneos: `{GRID_MAX_CONCURRENT}`\n"
                f"Ciclos totais: `{total_cycles}`\n"
                f"Lucro acumulado: `+${_grid_profit_total:.2f}`\n"
                f"Ciclos: {cycles_detail}"
            )
        sub = args[0].lower()
        # /grid alvo 10
        if sub == "alvo" and len(args) > 1:
            try:
                _new_alvo = float(args[1])
                # FIX #7: atualiza GRID_SETTINGS (fonte real do monitor) + global (exibição)
                GRID_PROFIT_TARGET_USDT = _new_alvo
                GRID_SETTINGS["NORMAL"]["profit_target_usdt"]     = _new_alvo
                GRID_SETTINGS["AGGRESSIVE"]["profit_target_usdt"] = round(_new_alvo * 0.6, 2)
                return (f"Alvo grid definido: ${_new_alvo:.2f} por ciclo (NORMAL) | "
                        f"${_new_alvo * 0.6:.2f} (AGGRESSIVE)")
            except ValueError:
                return "Uso: /grid alvo 10"
        # /grid pares BTC ETH SOL
        if sub == "pares" and len(args) > 1:
            GRID_PAIRS = [p.upper() if "USDT" in p.upper() else p.upper()+"USDT" for p in args[1:]]
            return f"Pares grid: {', '.join(GRID_PAIRS)}"
        # /grid lev 10
        if sub == "lev" and len(args) > 1:
            try:
                GRID_LEVERAGE = int(args[1])
                return f"Alavancagem grid: {GRID_LEVERAGE}x"
            except ValueError:
                return "Uso: /grid lev 10"
        return "Uso: /grid | /grid alvo 10 | /grid pares BTC ETH SOL | /grid lev 10"

    # /paper on|off
    if cmd == "/paper":
        val = args[0].lower() if args else ""
        if val == "on":
            PAPER_TRADING = True
            return "Paper Trading ATIVADO. Trades serao simulados sem dinheiro real."
        elif val == "off":
            PAPER_TRADING = False
            return "Paper Trading DESATIVADO. Bot opera com conta real."
        return f"Uso: /paper on|off  (atual: {'ON' if PAPER_TRADING else 'OFF'})"

    # /modo conservador|normal|agressivo
    if cmd == "/modo":
        val = args[0].lower() if args else ""
        _new = None
        if "conserv" in val:
            _new = "CONSERVATIVE"
        elif "agres" in val or "aggress" in val:
            _new = "AGGRESSIVE"
        elif "normal" in val:
            _new = "NORMAL"
        if _new:
            CURRENT_MODE = _new
            _update_scan_interval()
            _c = MODE_SETTINGS[_new]
            return (f"Perfil de risco alterado para: `{_new}`\n"
                    f"Score>=`{_c['min_score']}` | RR>=`{_c['min_rr']}` | "
                    f"Risco `{_c['risk_pct']}%` | Scan `{_active_scan_interval_s()}s`")
        return f"Uso: /modo conservador|normal|agressivo  (atual: {CURRENT_MODE})"

    # /macro pausar|continuar|status — controle da pausa em eventos macro
    if cmd == "/macro":
        global _macro_user_paused
        val = args[0].lower() if args else "status"
        if "paus" in val:
            _macro_user_paused = True
            return "⏸️ Trades PAUSADOS durante o evento macro. Use `/macro continuar` para retomar."
        if "contin" in val or "retom" in val:
            _macro_user_paused = False
            return "▶️ Bot retomado — operando normalmente."
        estado = "PAUSADO (sua escolha)" if _macro_user_paused else "operando normalmente"
        evento = "SIM — evento HIGH hoje" if _macro_event_active() else "nenhum hoje"
        return (f"*MACRO GUARD*\n"
                f"Estado: `{estado}`\n"
                f"Evento de alto impacto: `{evento}`\n"
                f"Uso: /macro pausar | continuar")

    # /banca 500
    if cmd == "/banca":
        if args:
            try:
                BANCA_USDT = float(args[0])
                margin = round(BANCA_USDT / max(TRADES_PER_SESSION, 1), 2)
                return f"Banca definida: `${BANCA_USDT:.2f}` / {TRADES_PER_SESSION} trades = `${margin:.2f}` por trade"
            except ValueError:
                return "Uso: /banca 500  (valor em USDT)"
        return f"Banca atual: ${BANCA_USDT:.2f}"

    # /ntrades 3
    if cmd == "/ntrades":
        if args:
            try:
                TRADES_PER_SESSION = int(args[0])
                margin = round(BANCA_USDT / max(TRADES_PER_SESSION, 1), 2) if BANCA_USDT > 0 else 0
                return (
                    f"Trades por sessao: `{TRADES_PER_SESSION}`\n"
                    f"Margem por trade: `${margin:.2f}`" if BANCA_USDT > 0 else
                    f"Trades por sessao: `{TRADES_PER_SESSION}` (defina banca com /banca)"
                )
            except ValueError:
                return "Uso: /ntrades 3  (numero inteiro)"
        return f"Trades por sessao atual: {TRADES_PER_SESSION}"

    # /capital — capital alocado vs disponível
    if cmd in ("/capital", "/capitalalocado"):
        banca  = BANCA_USDT
        avail  = _balance_cache.get("available_balance", 0) if _balance_cache else 0
        wallet = _balance_cache.get("wallet_balance", 0)    if _balance_cache else 0
        upnl   = _balance_cache.get("unrealized_pnl", 0)    if _balance_cache else 0
        notional_tot = margin_tot = 0.0
        n_open = 0
        for _t in _active_trades_cache.values():
            td = _t if isinstance(_t, dict) else _t.model_dump()
            _notional = float(td.get("size_usdt", 0) or 0)
            _lev      = float(td.get("leverage", 1) or 1)
            notional_tot += _notional
            margin_tot   += _notional / max(_lev, 1)
            n_open       += 1
        exp_ratio = (notional_tot / banca) if banca > 0 else 0
        return (
            f"*💰 Capital — TRADER 001*\n\n"
            f"Banca configurada: `${banca:.2f}`\n"
            f"Saldo carteira (real): `${wallet:.2f}`\n"
            f"Disponível: `${avail:.2f}`\n"
            f"PnL não realizado: `{'+' if upnl >= 0 else ''}${upnl:.2f}`\n\n"
            f"*Capital alocado ({n_open} posições):*\n"
            f"Nocional total: `${notional_tot:.2f}`\n"
            f"Margem usada: `${margin_tot:.2f}`\n"
            f"Exposição: `{exp_ratio:.2f}x` da banca (teto `{MAX_TOTAL_EXPOSURE_RATIO:.1f}x`)"
        )

    # /alavancagem [N] — mostra ou define alavancagem (GRID)
    if cmd in ("/alavancagem", "/lev", "/leverage"):
        if args:
            try:
                _v = int(args[0])
                if not (1 <= _v <= 25):
                    return "Alavancagem deve ser entre 1 e 25x."
                GRID_LEVERAGE = _v
                return (f"⚡ Alavancagem GRID definida: `{GRID_LEVERAGE}x`\n"
                        "_(Sinais/Autônomo usam alavancagem adaptativa por ativo/volatilidade.)_")
            except ValueError:
                return "Uso: /alavancagem 10  (1–25)"
        from config import MODE_SETTINGS as _MS
        cap     = _MS.get(CURRENT_MODE, {}).get("leverage_cap")
        cap_str = f"{cap}x" if cap else "sem teto fixo"
        return (
            f"*🎚️ Alavancagem — TRADER 001*\n\n"
            f"GRID: `{GRID_LEVERAGE}x`\n"
            f"Perfil `{CURRENT_MODE}` — teto: `{cap_str}`\n"
            f"BTC/ETH/SOL: `15x` | Altcoins: `5x` (base)\n"
            f"Ajuste por volatilidade: `{'ON' if LEVERAGE_BY_VOLATILITY else 'OFF'}` "
            f"(piso `{LEVERAGE_VOL_FLOOR}x`)\n\n"
            f"Definir GRID: `/alavancagem 10`"
        )

    # /limites [N] — limites de trade
    if cmd in ("/limites", "/limite", "/limits"):
        if args:
            try:
                TRADES_PER_SESSION = int(args[0])
                _lbl = "ilimitado" if TRADES_PER_SESSION == 0 else str(TRADES_PER_SESSION)
                return f"🔢 Limite de trades/sessão: `{_lbl}`"
            except ValueError:
                return "Uso: /limites 5  (0 = ilimitado)"
        max_open = _active_max_open()
        sess_lbl = "Ilimitado" if TRADES_PER_SESSION == 0 else str(TRADES_PER_SESSION)
        return (
            f"*🔢 Limites de Trade — TRADER 001*\n\n"
            f"Trades por sessão: `{sess_lbl}`\n"
            f"Máx. posições simultâneas (`{CURRENT_MODE}`): `{max_open}`\n"
            f"Máx. trades por dia: `{MAX_TRADES_PER_DAY or 'sem limite'}`\n"
            f"Teto de exposição: `{MAX_TOTAL_EXPOSURE_RATIO:.1f}x` da banca\n\n"
            f"Definir sessão: `/limites 5` | Banca: `/banca 500`"
        )

    # /fechar BTCUSDT | /fechar tudo
    if cmd == "/fechar":
        symbol = args[0].upper() if args else ""
        if not symbol:
            return "Uso: /fechar BTCUSDT | /fechar tudo"
        # /fechar tudo — fecha todas as posicoes abertas
        if symbol == "TUDO":
            if not _active_trades_cache:
                return "Nenhuma posicao aberta para fechar."
            closed, errors = [], []
            for trade_id, trade_data in list(_active_trades_cache.items()):
                td  = trade_data if isinstance(trade_data, dict) else trade_data.model_dump()
                sym = td.get("asset", "")
                try:
                    await asyncio.to_thread(
                        lambda s=sym, d=td: close_position(s, d.get("direction"), _get_binance_client_synced())
                    )
                    asyncio.create_task(send_trade_closed(td, "FECHADO MANUALMENTE — /fechar tudo"))
                    _active_trades_cache.pop(trade_id, None)
                    closed.append(sym)
                except Exception as e:
                    errors.append(f"{sym}: {str(e)[:40]}")
            result_lines = []
            if closed:
                result_lines.append(f"✅ Fechados: {', '.join(closed)}")
            if errors:
                result_lines.append(f"❌ Erros: {'; '.join(errors)}")
            return "\n".join(result_lines) or "Nenhuma posicao fechada."
        trade_id = None
        for tid, t in list(_active_trades_cache.items()):
            td = t if isinstance(t, dict) else t.model_dump()
            if td.get("asset", "").upper() == symbol:
                trade_id = tid
                break
        if trade_id is None:
            return f"Nenhum trade aberto para {symbol}."
        try:
            trade_data = _active_trades_cache[trade_id]
            td = trade_data if isinstance(trade_data, dict) else trade_data.model_dump()
            await asyncio.to_thread(
                lambda: close_position(symbol, td.get("direction"), _get_binance_client_synced())
            )
            asyncio.create_task(send_trade_closed(td, "FECHADO MANUALMENTE VIA TELEGRAM"))
            _active_trades_cache.pop(trade_id, None)
            return f"Trade {symbol} encerrado manualmente."
        except Exception as e:
            return f"Erro ao fechar {symbol}: {e}"

    # /mercado
    if cmd == "/mercado":
        ms = market_engine.get_market_state()
        bias   = ms.get("market_bias", "Neutral")
        conf   = ms.get("confidence", 50)
        trend  = ms.get("trend_score", 50)
        fg_val = ms.get("sentiment_value", 50)
        fg_lbl = ms.get("sentiment_label", "Neutral")
        btcd   = ms.get("btc_dominance", 50)
        btcfr  = ms.get("btc_funding", 0)
        ethfr  = ms.get("eth_funding", 0)
        oi_chg = ms.get("oi_change_24h", 0)
        ls     = ms.get("long_short_ratio", 1)
        mkt_chg= ms.get("mkt_cap_change_24h", 0)
        updated= ms.get("last_update", "N/A")
        headlines = ms.get("news_headlines", [])[:3]
        news_str = "\n".join(f"  • {h[:60]}" for h in headlines) if headlines else "  N/A"
        gainers = ms.get("top_gainers", [])[:3]
        gain_str = " | ".join(f"{g['symbol']} +{g['change']}%" for g in gainers) if gainers else "N/A"
        bias_icon = "BULLISH" if bias == "Bullish" else ("BEARISH" if bias == "Bearish" else "NEUTRAL")
        return (
            f"*MARKET INTELLIGENCE — {bias_icon} ({conf}%)*\n\n"
            f"Trend Score: `{trend}/100` | BTC Dom: `{btcd}%`\n"
            f"Mkt Cap 24h: `{'+' if mkt_chg >= 0 else ''}{mkt_chg:.1f}%`\n"
            f"Fear & Greed: `{fg_val} ({fg_lbl})`\n\n"
            f"*Binance Futures:*\n"
            f"BTC Funding: `{btcfr:+.4f}%` | ETH: `{ethfr:+.4f}%`\n"
            f"OI 24h: `{oi_chg:+.1f}%` | L/S Ratio: `{ls:.2f}`\n"
            f"Gainers: `{gain_str}`\n\n"
            f"*Ultimas Noticias:*\n{news_str}\n\n"
            f"Atualizado: `{updated}`"
        )

    # /stop
    if cmd == "/stop":
        return "Para parar o bot definitivamente, encerre o processo no servidor. Use /paper on para pausar operacoes reais sem derrubar o servidor."

    # /dual — REMOVIDO (sistema single-mode, 1 modo por vez)
    if cmd == "/dual":
        return ("⚠️ *Dual mode foi removido.*\n\n"
                "O bot agora roda *1 modo por vez* (menos conflitos, mais desempenho).\n\n"
                "Use:\n"
                "`/auto on` — AUTÔNOMO\n"
                "`/auto off` — SUPERVISIONADO\n"
                "`/auto grid` — GRID\n"
                "`/auto sinais` — SÓ SINAIS")

    # /brain on|off | sinais on|off | exec on|off | status
    if cmd == "/brain":
        val = args[0].lower() if args else "status"
        sub = args[1].lower() if len(args) > 1 else ""
        if val in ("on", "off"):
            state = val == "on"
            _claude_brain_enabled = state
            _sinais_claude_brain  = state
            _exec_claude_brain    = state
            icon = "🟢" if state else "🔴"
            return f"{icon} Claude Brain {'ATIVADO' if state else 'DESATIVADO'} em todos os canais."
        if val == "sinais":
            state = sub == "on"
            _sinais_claude_brain = state
            return f"🧠 Brain canal SINAIS: {'ON' if state else 'OFF'}"
        if val == "exec":
            state = sub == "on"
            _exec_claude_brain = state
            return f"🧠 Brain canal EXEC: {'ON' if state else 'OFF'}"
        # status
        usage = claude_brain.get_session_usage()
        conf_str = "SIM (API key presente)" if claude_brain.is_configured() else "NAO (ANTHROPIC_API_KEY ausente)"
        return (f"🧠 *Claude Brain*\n\n"
                f"Configurado: `{conf_str}`\n"
                f"Geral: `{'ON' if _claude_brain_enabled else 'OFF'}`\n"
                f"Canal SINAIS: `{'ON' if _sinais_claude_brain else 'OFF'}`\n"
                f"Canal EXEC: `{'ON' if _exec_claude_brain else 'OFF'}`\n\n"
                f"Sessao: `{usage['calls']} calls` | Custo: `${usage['cost_usd']:.4f}`\n"
                f"Budget: `${claude_brain.get_session_usage()['cost_usd']:.4f} / $5.00`")

    # /resumo — snapshot rapido
    if cmd == "/resumo":
        bal    = float(_balance_cache.get("wallet_balance", 0)) if _balance_cache else 0.0
        avail  = float(_balance_cache.get("available_balance", 0)) if _balance_cache else 0.0
        upnl   = float(_balance_cache.get("unrealized_pnl", 0)) if _balance_cache else 0.0
        trades_n = len(_active_trades_cache)
        sigs_n   = len(_latest_signals)
        mode_str = f"{OPERATION_MODE} ({CURRENT_MODE})"
        paper_tag = " | 📝 PAPER" if PAPER_TRADING else " | 🔴 REAL"
        brain_tag = " | 🧠 Brain ON" if _claude_brain_enabled else ""
        sig_lines = ""
        for s in _latest_signals[:3]:
            d = "🟢" if "LONG" in str(s.get("direction", "")).upper() else "🔴"
            sig_lines += f"  {d} `{s.get('asset','?')}` {s.get('timeframe','?')} score=`{s.get('confidence',0):.0f}`\n"
        return (
            f"📊 *RESUMO — TRADER 001*\n\n"
            f"Modo: `{mode_str}`{paper_tag}{brain_tag}\n"
            f"Saldo: `${bal:.2f}` | Livre: `${avail:.2f}`\n"
            f"PnL nao realizado: `${upnl:+.2f}`\n"
            f"PnL hoje: `${_daily_pnl:+.2f}`\n\n"
            f"Trades abertos: `{trades_n}` | Sinais cache: `{sigs_n}`\n"
            f"Ultimo scan: `{_last_scan_at}`\n"
            + (f"\n*Sinais recentes:*\n{sig_lines}" if sig_lines else "")
        )

    # /pnl — P&L detalhado
    if cmd == "/pnl":
        from database import get_performance_stats
        try:
            stats    = await get_performance_stats()
            total_t  = stats.get("total_trades", 0)
            wins     = stats.get("wins", 0)
            losses   = stats.get("losses", 0)
            total_pnl = stats.get("total_pnl", 0.0)
            avg_win  = stats.get("avg_win", 0.0)
            avg_loss = stats.get("avg_loss", 0.0)
            wr       = (wins / total_t * 100) if total_t else 0
            upnl     = float(_balance_cache.get("unrealized_pnl", 0)) if _balance_cache else 0.0
            return (
                f"💰 *P&L — TRADER 001*\n\n"
                f"*Hoje:* `${_daily_pnl:+.2f}`\n"
                f"*Nao realizado:* `${upnl:+.2f}`\n\n"
                f"*Historico geral:*\n"
                f"Total trades: `{total_t}` | Win rate: `{wr:.1f}%`\n"
                f"Wins: `{wins}` | Losses: `{losses}`\n"
                f"P&L acumulado: `${total_pnl:+.2f}`\n"
                f"Gain medio: `${avg_win:+.2f}` | Loss medio: `${avg_loss:+.2f}`"
            )
        except Exception as e:
            return f"Erro ao buscar P&L: {e}"

    # /posicao [SIMBOLO] — detalhe de posicao aberta
    if cmd == "/posicao":
        symbol = args[0].upper() if args else ""
        if not symbol:
            if not _active_trades_cache:
                return "Nenhum trade aberto. Use /trades para listar."
            lines = ["*Posicoes abertas:*\n"]
            for t in list(_active_trades_cache.values())[:8]:
                td  = t if isinstance(t, dict) else t.model_dump()
                pnl = td.get("pnl_pct", 0)
                icon = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"{icon} `{td.get('asset','?')}` {pnl:+.1f}%  /posicao {td.get('asset','?')}")
            return "\n".join(lines)
        trade_data = None
        for t in list(_active_trades_cache.values()):
            td = t if isinstance(t, dict) else t.model_dump()
            if td.get("asset", "").upper() == symbol:
                trade_data = td
                break
        if not trade_data:
            return f"Nenhuma posicao aberta para `{symbol}`."
        pnl      = trade_data.get("pnl_pct", 0)
        pnl_usdt = trade_data.get("pnl_usdt", 0)
        direction = str(trade_data.get("direction", "")).split(".")[-1].upper()
        entry = float(trade_data.get("entry_price", 0))
        sl    = float(trade_data.get("stop_loss", 0))
        tp1   = float(trade_data.get("tp1", 0))
        tp2   = float(trade_data.get("tp2", 0))
        size  = float(trade_data.get("size_usdt", 0))
        lev   = trade_data.get("leverage", 1)
        icon  = "🟢" if pnl >= 0 else "🔴"
        dir_icon = "📈 LONG" if "LONG" in direction else "📉 SHORT"
        return (
            f"{icon} *{symbol} — {dir_icon}*\n\n"
            f"Entrada: `${entry:.4f}`\n"
            f"SL: `${sl:.4f}` | TP1: `${tp1:.4f}` | TP2: `${tp2:.4f}`\n"
            f"Tamanho: `${size:.2f}` | Alavancagem: `{lev}x`\n\n"
            f"PnL: `{pnl:+.1f}%` (`${pnl_usdt:+.2f}`)\n\n"
            f"Para fechar: /fechar {symbol}"
        )

    # /risco — metricas de risco
    if cmd == "/risco":
        rm       = _calc_risk_metrics()
        bal      = float(_balance_cache.get("wallet_balance", 0)) if _balance_cache else 0
        avail    = float(_balance_cache.get("available_balance", 0)) if _balance_cache else 0
        upnl     = float(_balance_cache.get("unrealized_pnl", 0)) if _balance_cache else 0
        exp_pct  = ((bal - avail) / bal * 100) if bal > 0 else 0
        trades_n = len(_active_trades_cache)
        paused   = rm.get("paused", False) or rm.get("auto_paused", False) or _macro_user_paused
        status   = "⛔ PAUSADO" if paused else "✅ Operando"
        sortino  = rm.get("sortino")
        sharpe   = rm.get("sharpe")
        return (
            f"⚠️ *METRICAS DE RISCO*\n\n"
            f"Status: `{status}`\n"
            f"Trades abertos: `{trades_n}`\n"
            f"Exposicao: `{exp_pct:.1f}%` do saldo\n"
            f"PnL nao realizado: `${upnl:+.2f}`\n"
            f"PnL hoje: `${_daily_pnl:+.2f}`\n\n"
            f"Sortino: `{sortino:.3f}` | Sharpe: `{sharpe:.3f}`\n" if sortino and sharpe else
            f"Sortino/Sharpe: `N/A` (min {SORTINO_MIN_TRADES} trades)\n"
            f"Perda diaria maxima: `{MAX_DAILY_LOSS_PCT}%`\n"
            f"Pausado (macro): `{'SIM' if _macro_user_paused else 'NAO'}`"
        )

    return f"Comando desconhecido: `{cmd}`\nUse /ajuda para ver os comandos disponiveis."


# ── Lifespan ──────────────────────────────────────────────────────────────────

async def _run_walk_forward_job():
    """Job agendado — roda walk-forward e avisa se degradando."""
    global _walk_forward_result
    try:
        trades = await get_all_trades()
        closed = [t for t in (trades or []) if t.get("status") == "CLOSED"]
        result = _walk_forward_mod.run(closed)
        _walk_forward_result = _walk_forward_mod.to_dict(result)
        if result.alert_needed:
            await send_alert(
                f"Walk-Forward: *{result.overall_status}*\n{result.recommendation}"
            )
    except Exception as e:
        print(f"[WALK-FWD] Job erro: {e}")


async def _job_universe_builder():
    """Roda a cada 1h — atualiza universo dinâmico AGGRESSIVE."""
    global _dynamic_universe
    if CURRENT_MODE != "AGGRESSIVE":
        return
    # Respeita watchlist manual: se alguma está definida, não substitui
    mode_wl_set = SUPERVISED_WATCHLIST or AUTONOMOUS_WATCHLIST or SINAIS_WATCHLIST
    if mode_wl_set:
        return
    try:
        symbols = await universe_builder.build_universe()
        _dynamic_universe = symbols
    except Exception as e:
        print(f"[UNIVERSE] Job erro: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global OPERATION_MODE, EXEC_MODE, DUAL_MODE_ENABLED, SINAIS_ENABLED, PAPER_TRADING, BOT_PAUSED
    _acquire_instance_lock()
    await init_db()
    print("[TRADER 001] Database initialized")

    # Carrega estado persistido do banco e injeta nas variáveis globais no startup
    try:
        await bot_state.load_from_db()
        sync_state_to_globals()
        # SEGURANÇA (conta REAL): boota com TODOS os modos DESLIGADOS, aguardando o
        # usuário ativar pelo dashboard. Preserva parâmetros (banca, alavancagem,
        # perfil, watchlists) mas nada transmite nem executa até a ativação manual.
        # SINGLE-MODE: boot OCIOSO — nenhum modo transmite/executa até o usuário
        # ativar pelo dashboard. OPERATION_MODE é a base nominal; SINAIS_ENABLED=False
        # mantém tudo inerte. EXEC_MODE apenas espelha o modo único ativo.
        OPERATION_MODE    = "SINAIS"
        EXEC_MODE         = "SINAIS"
        DUAL_MODE_ENABLED = False      # dual mode removido (1 modo por vez)
        SINAIS_ENABLED    = False      # canal de sinais desligado (ocioso)
        BOT_PAUSED        = False      # bot não-pausado; ocioso é garantido por SINAIS/mode
        # PAPER_TRADING preservado do DB (não force aqui) — simulação é sticky entre restarts.
        set_alerts_paused(False)       # gate liberado; ativação controla o envio
        await save_global_state_to_db()
        print(f"[STARTUP] Boot OCIOSO (single-mode) — aguardando o usuário. Banca: {BANCA_USDT} | Perfil: {CURRENT_MODE}")
    except Exception as e:
        print(f"[STARTUP] Erro ao carregar/sincronizar configurações do DB: {e}")

    # Popula cache de trades abertos (evita _active_trades_cache vazio após restart)
    for _t in await get_open_trades():
        _active_trades_cache[_t["id"]] = _t

    # Popula cache de saldo antes do primeiro scan
    _refresh_balance_cache()

    # ── Scheduler — intervalos balanceados para não sobrecarregar API ───────────
    # scan:60s | trades:30s | telegram:3s | grid:15s | balance:60s | sync:120s
    # jitter: desfaz o efeito "manada" (jobs disparando juntos no boundary do minuto),
    # espalhando o início e reduzindo picos de saturação no event loop. coalesce=True
    # mantido — sem risco de pile-up; sem misfire_grace_time para não dropar execuções.
    scheduler.add_job(job_scan_market,              "interval", seconds=60,  id="scan",              max_instances=1, coalesce=True, jitter=8)
    scheduler.add_job(job_update_trades,             "interval", seconds=30,  id="update_trades",     max_instances=1, coalesce=True, jitter=5)
    scheduler.add_job(poll_telegram_responses,       "interval", seconds=10,  id="telegram_poll",     max_instances=1, coalesce=True, jitter=3)
    scheduler.add_job(job_scan_anomalies,            "interval", seconds=120, id="anomalies",         max_instances=1, coalesce=True, jitter=20)
    scheduler.add_job(_refresh_balance_cache_async,  "interval", seconds=60,  id="balance_refresh",   max_instances=1, coalesce=True, jitter=10)
    scheduler.add_job(_job_sync_binance,             "interval", seconds=120, id="sync_binance",      max_instances=1, coalesce=True, jitter=20)
    # Resumo diário/semanal desativados — só abrir/fechar ordem ou atingir alvo/stop devem notificar.
    # scheduler.add_job(_job_daily_summary,            "cron",     hour=23, minute=55, id="daily_summary")
    # scheduler.add_job(_job_weekly_sinais_stats,      "cron",     day_of_week="sun", hour=20, minute=0, id="weekly_sinais")
    scheduler.add_job(job_grid_monitor,              "interval", seconds=45,  id="grid_monitor",      max_instances=1, coalesce=True, jitter=8)
    scheduler.add_job(job_grid_scan,                 "interval", seconds=180, id="grid_scan",         max_instances=1, coalesce=True, jitter=20)
    scheduler.add_job(job_pump_dump_scan,                "interval", seconds=120, id="pump_dump",           max_instances=1, coalesce=True, jitter=20)
    scheduler.add_job(job_sinais_scan,                   "interval", seconds=60,  id="sinais_scan",          max_instances=1, coalesce=True, jitter=10)
    scheduler.add_job(job_pd_monitor,                    "interval", seconds=60,  id="pd_monitor",           max_instances=1, coalesce=True, jitter=10)
    scheduler.add_job(market_engine.refresh_market_intelligence, "interval", hours=1, id="market_intelligence", max_instances=1, coalesce=True, jitter=120)
    scheduler.add_job(market_engine.mini_refresh,    "interval", minutes=10, id="mie_mini",           max_instances=1, coalesce=True, jitter=30)
    scheduler.add_job(_run_walk_forward_job,             "interval", hours=12,   id="walk_forward",       max_instances=1, coalesce=True)
    scheduler.add_job(_job_universe_builder,         "interval", hours=1,    id="universe_builder",   max_instances=1, coalesce=True, jitter=120)
    scheduler.add_job(job_macro_guard,               "interval", minutes=30, id="macro_guard",        max_instances=1, coalesce=True, jitter=60)
    # Relatório periódico de posições desativado — só abrir/fechar ordem ou atingir alvo/stop devem notificar.
    # scheduler.add_job(job_open_positions_report,     "interval", minutes=10, id="positions_report",   max_instances=1, coalesce=True, jitter=20)
    scheduler.add_job(job_circuit_breaker_watch,     "interval", seconds=30, id="circuit_breaker",    max_instances=1, coalesce=True)
    scheduler.add_job(job_autotune_score,            "interval", minutes=15, id="autotune_score",     max_instances=1, coalesce=True)
    scheduler.add_job(job_sinais_outcome_watch,      "interval", minutes=3,  id="sinais_outcome",     max_instances=1, coalesce=True, jitter=20)
    scheduler.add_job(job_sinais_autotune,           "interval", minutes=20, id="sinais_autotune",    max_instances=1, coalesce=True)
    scheduler.add_job(_job_correlation_refresh,      "interval", minutes=30, id="correlation",        max_instances=1, coalesce=True, jitter=60)
    scheduler.add_job(job_pairs_arbitrage,           "interval", minutes=15, id="pairs_arbitrage",      max_instances=1, coalesce=True, jitter=30)
    scheduler.add_job(job_health_watch,              "interval", minutes=5,  id="health_watch",       max_instances=1, coalesce=True, jitter=15)
    scheduler.add_job(job_db_prune,                  "cron", hour=4, minute=10, id="db_prune",        max_instances=1, coalesce=True)
    scheduler.start()
    asyncio.create_task(_loop_heartbeat())
    print("[TRADER 001] Scheduler — scan:60s | trades:30s | telegram:3s | grid:15s | balance:60s | sync:120s | universe:1h | pairs:15m")

    # ML Engine: carrega modelos salvos e treina em background
    async def _init_ml():
        global _ml_ready
        ml_engine.load_saved_models()
        await ml_engine.train_all_models()
        _ml_ready = True
        print("[ML] Engine pronto")
    asyncio.create_task(_init_ml())

    # WebSocket feeds — kline + markPrice + liquidações (substitui polling REST)
    async def _init_ws():
        from config import WATCHLIST
        all_symbols = list(set(WATCHLIST))
        # markPrice e liquidações NÃO dependem do warm-up de klines — sobem já.
        # (start_global_feed bloqueia até 120s no warm-up; não atrasar os outros)
        await ws_feed.start_mark_price_feed(all_symbols)
        await ws_feed.start_liquidation_feed()   # !forceOrder@arr — cascatas de liquidação
        await ws_feed.start_global_feed(all_symbols, interval="5m")
    asyncio.create_task(_init_ws())

    # Correlation Engine — primeira matriz em background
    async def _init_corr():
        try:
            import correlation_engine as _corr
            from config import WATCHLIST, WATCHLIST_VOLATILE
            await _corr.refresh_correlation_matrix(list(set(WATCHLIST + WATCHLIST_VOLATILE)))
        except Exception as e:
            print(f"[CORR] init erro: {e}")
    asyncio.create_task(_init_corr())

    # Walk-forward — análise inicial em background
    async def _run_walk_forward():
        global _walk_forward_result
        try:
            trades = await get_all_trades()
            closed = [t for t in (trades or []) if t.get("status") == "CLOSED"]
            result = _walk_forward_mod.run(closed)
            _walk_forward_result = _walk_forward_mod.to_dict(result)
            if result.alert_needed:
                await send_alert(
                    f"Walk-Forward: *{result.overall_status}*\n{result.recommendation}"
                )
            print(f"[WALK-FWD] {result.overall_status} | {len(result.windows)} janelas")
        except Exception as e:
            print(f"[WALK-FWD] Erro: {e}")
    asyncio.create_task(_run_walk_forward())

    # Registra handlers Telegram e menu de comandos '/'
    set_command_handler(_telegram_command_handler)
    set_close_handler(_telegram_close_trade)
    asyncio.create_task(register_bot_commands())

    # Universo dinâmico — carrega state do disco imediatamente, depois roda builder
    async def _init_universe():
        global _dynamic_universe
        # Primeiro: lê state já salvo em disco (instantâneo)
        cached = universe_builder.get_universe()
        if cached:
            _dynamic_universe = cached
            print(f"[UNIVERSE] Cache carregado: {len(cached)} ativos")
        # Depois: roda builder completo em background
        if CURRENT_MODE == "AGGRESSIVE":
            await _job_universe_builder()
    asyncio.create_task(_init_universe())

    # Market Intelligence Engine — carrega cache do disco e agenda refresh
    asyncio.create_task(market_engine.initialize())

    # Inicializa timestamp do modo de operacao (mostra no timer do dashboard)
    global _mode_started_at
    _mode_started_at = datetime.utcnow().isoformat()

    # Scan + sync inicial
    asyncio.create_task(job_scan_market())
    asyncio.create_task(test_connection())
    asyncio.create_task(_job_sync_binance())
    asyncio.create_task(_send_startup_test_notification())

    await log_event("STARTUP", "Trader 001 iniciado", {"mode": CURRENT_MODE})

    try:
        yield
    finally:
        scheduler.shutdown()
        _release_instance_lock()


app = FastAPI(
    title="TRADER 001",
    description="Crypto Futures Signal Engine + Execution Bot",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(alert: WebhookAlert, background: BackgroundTasks):
    if not WEBHOOK_SECRET or alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret or WEBHOOK_SECRET not configured")

    symbol = alert.asset.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    direction = Direction.LONG if alert.direction.upper() in ("LONG", "BUY") else Direction.SHORT
    background.add_task(_process_webhook_signal, symbol, direction, alert.timeframe, alert.reason)

    return {"status": "received", "asset": symbol, "direction": direction.value}


async def _process_webhook_signal(symbol: str, direction: Direction, timeframe: str, reason: str):
    print(f"[WEBHOOK] {symbol} {direction} {timeframe}")

    # FIX #5: webhook agora respeita os mesmos guards do fluxo normal
    if BOT_PAUSED:
        print(f"[WEBHOOK] Bot pausado (BOT_PAUSED) — sinal ignorado")
        return

    if not _check_daily_loss():
        print(f"[WEBHOOK] Limite de perda diária atingido — sinal ignorado")
        return

    # Cooldown: evita webhook spam (mesmo ativo + direção + 90s)
    _wh_key = f"WH_{symbol}_{direction.value}"
    if time.time() - _signal_cooldown.get(_wh_key, 0) < 90:
        print(f"[WEBHOOK] {symbol} em cooldown (90s) — sinal ignorado")
        return
    _signal_cooldown[_wh_key] = time.time()

    # BTC Veto também se aplica a webhooks
    from signal_filters import refresh_btc_veto as _rwv, btc_veto_passes as _bvp
    _veto = await _rwv()
    if not _bvp({"direction": direction.value}, _veto):
        print(f"[WEBHOOK] {symbol} {direction.value} bloqueado por BTC veto")
        return

    signal = await analyze_asset(symbol, timeframe or "15m", direction, _news_cache)
    if signal is None:
        print(f"[WEBHOOK] Signal rejected — score too low or RR insufficient")
        return

    if not await can_open_trade(_active_max_open()):
        print(f"[WEBHOOK] Max open trades reached")
        return

    balance = await _get_balance() or 100

    trade = create_trade(signal, balance)

    if PAPER_TRADING:
        result = {"status": "SIMULATED"}
    else:
        result = await asyncio.to_thread(open_trade, trade)

    if result["status"] in ("OK", "SIMULATED"):
        trade_dict = {
            **trade.model_dump(),
            "opened_at": trade.opened_at.isoformat(),
            "score_json": json.dumps(signal.score.model_dump()),
            "timeframe": timeframe,
            "paper": PAPER_TRADING,
        }
        await save_trade(trade_dict)
        _active_trades_cache[trade.id] = trade.model_dump()
        await send_trade_opened(trade_dict, "WEBHOOK")
        print(f"[WEBHOOK] Trade opened: {trade.id} {symbol} {direction}")
    else:
        print(f"[WEBHOOK] Trade failed: {result}")


# ── Signal Endpoints ──────────────────────────────────────────────────────────

@app.get("/signals/scan")
async def manual_scan():
    """Trigger an immediate full watchlist scan."""
    news = await get_crypto_news()
    signals = await scan_watchlist(news_data=news)
    global _latest_signals
    _latest_signals = [s.model_dump() for s in signals[:20]]
    return {"count": len(signals), "signals": _latest_signals}


@app.get("/signals/latest")
async def latest_signals():
    return {"count": len(_latest_signals), "signals": _latest_signals}


@app.get("/signals")
async def signals_alias():
    """Alias de /signals/latest — compatível com dashboard."""
    return {"count": len(_latest_signals), "signals": _latest_signals}


@app.get("/signals/analyze/{symbol}")
async def analyze_single(symbol: str, timeframe: str = "15m", direction: str = ""):
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    dir_ = Direction(direction.upper()) if direction else None
    signal = await analyze_asset(sym, timeframe, dir_, _news_cache, mode=CURRENT_MODE)
    if signal is None:
        return {"signal": None, "message": "No valid signal found"}
    return {"signal": signal.model_dump()}


# ── Market Endpoints ──────────────────────────────────────────────────────────

_hype_cache: dict = {"price": 0.0, "chg": 0.0, "ts": 0.0}


@app.get("/market")
async def market():
    data = _market_cache if _market_cache else await get_market_snapshot()
    # HYPE com cache de 30s — dashboard faz polling e cada hit era 1 chamada de API
    if time.time() - _hype_cache["ts"] > 30:
        try:
            hype_t = await get_ticker("HYPEUSDT")
            _hype_cache["price"] = hype_t.get("price", 0)
            _hype_cache["chg"]   = hype_t.get("change_24h", 0)
            _hype_cache["ts"]    = time.time()
        except Exception:
            pass
    hype_price = _hype_cache["price"]
    hype_chg   = _hype_cache["chg"]
    # Monta sub-objeto `prices` compatível com o dashboard
    prices = {
        "BTCUSDT":  {"price": data.get("btc_price", 0),  "price_change_pct": data.get("btc_change_24h", 0)},
        "ETHUSDT":  {"price": data.get("eth_price", 0),  "price_change_pct": data.get("eth_change_24h", 0)},
        "SOLUSDT":  {"price": data.get("sol_price", 0),  "price_change_pct": data.get("sol_change_24h", 0)},
        "HYPEUSDT": {"price": hype_price,                "price_change_pct": hype_chg},
    }
    return {**data, "prices": prices}


@app.get("/market/refresh")
async def refresh_market():
    snap = await get_market_snapshot()
    global _market_cache
    _market_cache = snap
    return snap


@app.get("/market/intelligence")
async def get_market_intelligence():
    """Retorna estado completo do Market Intelligence Engine (cache em memoria)."""
    return market_engine.get_market_state()


@app.post("/market/intelligence/refresh")
async def refresh_market_intelligence_now():
    """Forca refresh imediato do Market Intelligence Engine."""
    asyncio.create_task(market_engine.refresh_market_intelligence())
    return {"message": "Refresh iniciado — dados prontos em ~15 segundos"}


@app.get("/alerts/pump_dump")
async def get_pump_dump_alerts(force: bool = False):
    """Retorna alertas de Pump/Dump detectados (top 50 Binance Futures)."""
    alerts = await pump_dump_engine.scan_pump_dump(force=force)
    high   = [a for a in alerts if a["priority"] == "HIGH"]
    medium = [a for a in alerts if a["priority"] == "MEDIUM"]
    low    = [a for a in alerts if a["priority"] == "LOW"]
    return {
        "total":  len(alerts),
        "high":   len(high),
        "medium": len(medium),
        "low":    len(low),
        "alerts": alerts,
    }


@app.get("/signals/zones/{symbol}")
async def get_supply_demand_zones(symbol: str, timeframe: str = "1h"):
    """Retorna zonas de supply & demand + smart flow para um ativo."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    zones = await supply_demand.get_zones(sym, timeframe)
    # Also attach smart flow from 15m data
    flow: dict = {}
    try:
        from klines_cache import get_klines_cached as _gk
        df = await _gk(sym, "15m", limit=100)
        flow = analyze_smart_flow(df) if df is not None else {}
    except Exception:
        pass
    return {"symbol": sym, "zones": zones, "smart_flow": flow}


@app.get("/news")
async def news():
    if not _news_cache:
        items = await get_crypto_news()
        return {"items": items}
    return {"items": _news_cache}


@app.post("/news/broadcast")
async def news_broadcast():
    """Busca noticias (8 fontes), sentimento, on-chain, calendario macro e envia ao Telegram."""
    try:
        import aiohttp as _ah
        import xml.etree.ElementTree as _ET
        from datetime import date as _date, datetime as _dt

        def _parse_rss(text: str, source: str, max_items: int = 4) -> list:
            items = []
            try:
                root = _ET.fromstring(text)
                channel = root.find("channel") or root
                for item in (channel.findall("item") or [])[:max_items]:
                    title = (item.findtext("title") or "").strip()
                    link  = (item.findtext("link") or "").strip()
                    if not link:
                        for child in item:
                            if "link" in child.tag.lower() and child.get("href"):
                                link = child.get("href"); break
                    if title and link:
                        items.append({"title": title[:120], "url": link, "source": source})
            except Exception:
                pass
            return items

        try:
            _resolver = _ah.AsyncResolver(nameservers=["8.8.8.8", "1.1.1.1"])
        except Exception:
            _resolver = None
        connector = _ah.TCPConnector(resolver=_resolver, ssl=True)
        hdrs = {"User-Agent": "Mozilla/5.0 TraderBot/1.0"}

        async with _ah.ClientSession(connector=connector, headers=hdrs) as sess:

            async def _gjson(url, t=8):
                try:
                    async with sess.get(url, timeout=_ah.ClientTimeout(total=t)) as r:
                        return await r.json(content_type=None)
                except Exception:
                    return None

            async def _gbytes(url, t=8):
                try:
                    async with sess.get(url, timeout=_ah.ClientTimeout(total=t)) as r:
                        return await r.read()
                except Exception:
                    return None

            async def _fetch_fg():
                d = await _gjson("https://api.alternative.me/fng/?limit=1")
                if d:
                    it = d.get("data", [{}])[0]
                    return {"value": it.get("value", "--"), "value_classification": it.get("value_classification", "--")}
                return {}

            async def _fetch_btc():
                price = "--"; chg = "--"; dom = "--"
                p = await _gjson("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true")
                if p and "bitcoin" in p:
                    price = f"{p['bitcoin']['usd']:,.0f}"
                    chg   = f"{p['bitcoin'].get('usd_24h_change', 0):.2f}"
                elif _market_cache:
                    price = str(_market_cache.get("btc_price", "--"))
                g = await _gjson("https://api.coingecko.com/api/v3/global")
                if g:
                    dom = f"{g['data']['market_cap_percentage'].get('btc', 0):.1f}"
                return price, chg, dom

            async def _fetch_trending():
                d = await _gjson("https://api.coingecko.com/api/v3/search/trending")
                if d:
                    return [c["item"]["symbol"].upper() for c in d.get("coins", [])[:5]]
                return _trending_cache[:5]

            async def _fetch_movers():
                gainers = []; losers = []
                g = await _gjson("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=price_change_percentage_24h_desc&per_page=5&page=1&sparkline=false")
                if g:
                    gainers = [{"symbol": c.get("symbol","").upper(), "change": round(c.get("price_change_percentage_24h",0),1)} for c in g[:5] if c.get("price_change_percentage_24h",0) > 0]
                l = await _gjson("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=price_change_percentage_24h_asc&per_page=5&page=1&sparkline=false")
                if l:
                    losers = [{"symbol": c.get("symbol","").upper(), "change": round(c.get("price_change_percentage_24h",0),1)} for c in l[:5] if c.get("price_change_percentage_24h",0) < 0]
                return gainers, losers

            async def _fetch_mempool():
                fees = await _gjson("https://mempool.space/api/v1/fees/recommended")
                mp   = await _gjson("https://mempool.space/api/v1/mempool")
                b    = await _gbytes("https://mempool.space/api/blocks/tip/height")
                return {
                    "fee":          fees.get("halfHourFee", "--") if fees else "--",
                    "count":        mp.get("count", "--") if mp else "--",
                    "block_height": b.decode().strip() if b else "--",
                }

            async def _fetch_rss_url(url, source):
                body = await _gbytes(url)
                if body:
                    return _parse_rss(body.decode(errors="ignore"), source)
                return []

            async def _fetch_reddit():
                items = []
                for sub in ["criptomoedas", "investimentos"]:
                    d = await _gjson(f"https://www.reddit.com/r/{sub}/hot.json?limit=4")
                    if not d:
                        continue
                    for p in d.get("data", {}).get("children", [])[:3]:
                        pd = p.get("data", {})
                        title = pd.get("title", "")[:120]
                        link  = f"https://reddit.com{pd.get('permalink','')}"
                        ups   = pd.get("score", 0)
                        if title:
                            items.append({"title": title, "url": link, "source": f"r/{sub} ▲{ups:,}", "sentiment": "neutral"})
                return items[:5]

            async def _fetch_cryptopanic():
                d = await _gjson("https://cryptopanic.com/api/v1/posts/?public=true&currencies=BTC,ETH&language=pt")
                if not d or not d.get("results"):
                    d = await _gjson("https://cryptopanic.com/api/v1/posts/?public=true&currencies=BTC,ETH")
                if not d:
                    return []
                result = []
                for item in d.get("results", [])[:5]:
                    votes = item.get("votes", {})
                    pos = votes.get("positive", 0); neg = votes.get("negative", 0)
                    sent = "bullish" if pos > neg else "bearish" if neg > pos else "neutral"
                    icon = " 🔥" if sent == "bullish" else " ⚠️" if sent == "bearish" else ""
                    result.append({
                        "title":     item.get("title", "")[:120],
                        "url":       item.get("url", ""),
                        "source":    (item.get("domain", "CryptoPanic") + icon)[:40],
                        "sentiment": sent,
                    })
                return result

            async def _fetch_ff_calendar():
                d = await _gjson("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
                if not d:
                    return []
                today = _date.today()
                result = []
                flags = {"USD":"🇺🇸","EUR":"🇪🇺","GBP":"🇬🇧","JPY":"🇯🇵","CNY":"🇨🇳","CAD":"🇨🇦","AUD":"🇦🇺"}
                for ev in d:
                    if ev.get("impact") != "High":
                        continue
                    try:
                        raw = ev.get("date", "")
                        ev_date = _dt.fromisoformat(raw.replace("Z","+00:00")).date() if "T" in raw else _date.fromisoformat(raw[:10])
                    except Exception:
                        continue
                    delta = (ev_date - today).days
                    if 0 <= delta <= 2:
                        result.append({
                            "date":     ev_date.strftime("%d/%m"),
                            "time":     ev.get("time", ""),
                            "country":  flags.get(ev.get("country",""), "🌐"),
                            "title":    ev.get("title","")[:60],
                            "forecast": ev.get("forecast",""),
                            "previous": ev.get("previous",""),
                        })
                return result[:8]

            async def _fetch_cg_events():
                d = await _gjson("https://api.coingecko.com/api/v3/events?upcoming_events_only=true&page=1&per_page=8")
                if not d:
                    return []
                today = _date.today()
                result = []
                for ev in d.get("data", []):
                    try:
                        ev_date = _date.fromisoformat(str(ev.get("start_date",""))[:10])
                    except Exception:
                        continue
                    if 0 <= (ev_date - today).days <= 2:
                        result.append({
                            "date": ev_date.strftime("%d/%m"), "time": "", "country": "₿",
                            "title": ev.get("title","")[:60], "forecast": "", "previous": "",
                        })
                return result[:4]

            results = await asyncio.gather(
                _fetch_fg(),
                _fetch_btc(),
                _fetch_trending(),
                _fetch_movers(),
                _fetch_mempool(),
                _fetch_rss_url("https://br.cointelegraph.com/rss", "Cointelegraph BR"),
                _fetch_rss_url("https://portaldobitcoin.uol.com.br/feed/", "Portal do Bitcoin"),
                _fetch_rss_url("https://livecoins.com.br/feed/", "Livecoins"),
                _fetch_rss_url("https://news.google.com/rss/search?q=bitcoin+cripto&hl=pt-BR&gl=BR&ceid=BR:pt-419", "Google News BR"),
                _fetch_reddit(),
                _fetch_cryptopanic(),
                _fetch_ff_calendar(),
                _fetch_cg_events(),
                return_exceptions=True,
            )

        def _s(r, default):
            return r if not isinstance(r, Exception) else default

        fg_data      = _s(results[0], {})
        btc_tuple    = _s(results[1], ("--","--","--"))
        btc_price, btc_chg, btc_dom = btc_tuple if isinstance(btc_tuple, tuple) else ("--","--","--")
        trending     = _s(results[2], _trending_cache[:5])
        movers       = _s(results[3], ([],[]))
        gainers, losers = movers if isinstance(movers, tuple) else ([],[])
        mempool_data = _s(results[4], {})
        ct_news      = _s(results[5], [])
        pb_news      = _s(results[6], [])
        lc_news      = _s(results[7], [])
        gn_news      = _s(results[8], [])
        reddit_news  = _s(results[9], [])
        cp_news      = _s(results[10], [])
        ff_events    = _s(results[11], [])
        cg_events    = _s(results[12], [])

        # Merge + deduplicate news (prioridade: fontes PT-BR)
        all_news = []
        seen = set()
        for item in (cp_news or []) + (ct_news or []) + (pb_news or []) + (lc_news or []) + (gn_news or []) + (reddit_news or []):
            if not isinstance(item, dict):
                continue
            key = (item.get("title","").lower())[:50]
            if key and key not in seen:
                seen.add(key)
                all_news.append(item)
            if len(all_news) >= 8:
                break

        # Calendar merge
        calendar_events = [ev for ev in ((ff_events or []) + (cg_events or [])) if isinstance(ev, dict)][:8]

        # Score 0-10
        score = 5
        try:
            fgi = int(fg_data.get("value", 50))
            if fgi >= 75:   score += 2
            elif fgi >= 55: score += 1
            elif fgi <= 25: score -= 2
            elif fgi <= 45: score -= 1
        except Exception:
            pass
        try:
            chg_f = float(str(btc_chg).replace(",",""))
            if chg_f >= 3:    score += 1
            elif chg_f <= -3: score -= 1
        except Exception:
            pass
        try:
            bull = sum(1 for n in (cp_news or []) if isinstance(n, dict) and n.get("sentiment") == "bullish")
            bear = sum(1 for n in (cp_news or []) if isinstance(n, dict) and n.get("sentiment") == "bearish")
            if bull > bear: score += 1
            elif bear > bull: score -= 1
        except Exception:
            pass
        try:
            fee = mempool_data.get("fee","--") if isinstance(mempool_data, dict) else "--"
            if fee != "--" and int(fee) > 50: score -= 1
        except Exception:
            pass
        score = max(0, min(10, score))

        market_data = {
            "fear_greed":     fg_data if isinstance(fg_data, dict) else {},
            "btc_price":      btc_price if isinstance(btc_price, str) else "--",
            "btc_change_24h": btc_chg   if isinstance(btc_chg,   str) else "--",
            "btc_dominance":  btc_dom   if isinstance(btc_dom,   str) else "--",
            "eth_price":      _market_cache.get("eth_price", 0),
            "eth_change_24h": _market_cache.get("eth_change_24h", 0),
            "sol_change_24h": _market_cache.get("sol_change_24h", 0),
            "mempool":        mempool_data if isinstance(mempool_data, dict) else {},
            "trending":       trending  if isinstance(trending,  list) else _trending_cache[:5],
            "top_gainers":    gainers   if isinstance(gainers,   list) else [],
            "top_losers":     losers    if isinstance(losers,    list) else [],
            "news":           all_news,
            "calendar":       calendar_events,
            "score":          score,
        }

        sent = await send_news_broadcast(market_data)
        return {"status": "sent" if sent else "error", "items": len(all_news), "score": score}
    except Exception as e:
        import traceback
        print(f"[NEWS BROADCAST] ERRO:\n{traceback.format_exc()}")
        raise HTTPException(500, f"News broadcast error: {e}")


# ── Claude Brain endpoints ────────────────────────────────────────────────────

@app.get("/claude-brain/status")
async def claude_brain_status():
    from config import CLAUDE_BRAIN_BUDGET_USD
    usage = claude_brain.get_session_usage()
    remaining = max(0.0, CLAUDE_BRAIN_BUDGET_USD - usage["cost_usd"]) if CLAUDE_BRAIN_BUDGET_USD > 0 else None
    return {
        "enabled":       _claude_brain_enabled,
        "configured":    claude_brain.is_configured(),
        "model":         claude_brain._MODEL,
        "session_calls": usage["calls"],
        "session_cost":  usage["cost_usd"],
        "budget_usd":    CLAUDE_BRAIN_BUDGET_USD,
        "remaining_usd": remaining,
    }


@app.post("/claude-brain/toggle")
async def claude_brain_toggle():
    global _claude_brain_enabled, _sinais_claude_brain, _exec_claude_brain
    if not _claude_brain_enabled and not claude_brain.is_configured():
        raise HTTPException(400, "ANTHROPIC_API_KEY nao configurada no .env")

    _claude_brain_enabled = not _claude_brain_enabled
    # Sincroniza com as variáveis usadas nos canais do modo dual
    _sinais_claude_brain = _claude_brain_enabled
    _exec_claude_brain = _claude_brain_enabled
    
    # Salva o estado atualizado no banco de dados SQLite
    try:
        from database import save_setting
        await save_setting("claude_brain_enabled", str(_claude_brain_enabled))
    except Exception as e:
        print(f"[CLAUDE BRAIN] Erro ao persistir estado no DB: {e}")
        
    state = "ATIVADO" if _claude_brain_enabled else "DESATIVADO"
    print(f"[CLAUDE BRAIN] {state} via dashboard (sincronizado com canais dual e salvo no DB)")

    # Uso acumulado e budget
    usage = claude_brain.get_session_usage()
    from config import CLAUDE_BRAIN_BUDGET_USD
    asyncio.create_task(
        send_claude_brain_toggle_alert(_claude_brain_enabled, usage, CLAUDE_BRAIN_BUDGET_USD)
    )

    # Ao desativar: reseta contadores para a próxima sessão
    if not _claude_brain_enabled:
        claude_brain.reset_session_usage()

    return {"enabled": _claude_brain_enabled, "status": state}


# ── Dual-Mode endpoints (REMOVIDO — mantidos como stubs de compatibilidade) ───
# O dual mode foi removido: o bot roda 1 modo por vez (SINGLE-MODE). Estes
# endpoints continuam existindo só para não quebrar pollers/cache antigos do
# dashboard, mas /enable agora apenas ativa o modo único correspondente.

@app.get("/dual-mode/status")
async def get_dual_mode_status():
    return {
        "dual_mode_enabled": False,
        "single_mode": OPERATION_MODE,
        "profile": CURRENT_MODE,
        "deprecated": "dual mode removido — use /settings/mode",
    }


@app.post("/dual-mode/enable")
async def enable_dual_mode(
    exec_mode: str = "AUTONOMOUS",
    sinais_profile: str = "AGGRESSIVE",
    exec_profile: str = "NORMAL",
    sinais_brain: bool = False,
    exec_brain: bool = False,
):
    """COMPAT: dual removido. Ativa o modo ÚNICO `exec_mode` (sem canal paralelo)."""
    _alias = {
        "supervisao": "SUPERVISED", "supervisionado": "SUPERVISED",
        "autonomo": "AUTONOMOUS", "automatico": "AUTONOMOUS",
        "grid": "GRID", "sinais": "SINAIS",
    }
    mode = _alias.get(exec_mode.lower(), exec_mode.upper())
    if mode not in ("AUTONOMOUS", "SUPERVISED", "GRID", "SINAIS"):
        raise HTTPException(status_code=400, detail="Modo invalido")
    return await set_operation_mode(mode)


@app.post("/settings/sinais_enabled")
async def set_sinais_enabled(enabled: bool):
    """Ativa ou desativa canal de transmissão de sinais."""
    global SINAIS_ENABLED
    SINAIS_ENABLED = enabled
    status = "ATIVADO" if enabled else "DESATIVADO"
    msg = f"Canal de SINAIS {status} via dashboard."
    print(f"[SETTINGS] {msg}")
    await log_event("SETTINGS", f"Sinais canal: {status}")
    await send_alert(f"📡 Canal de Sinais {status}.")
    await save_global_state_to_db()
    return {"sinais_enabled": SINAIS_ENABLED, "message": msg}


@app.post("/dual-mode/disable")
async def disable_dual_mode_endpoint():
    """COMPAT: dual removido. Sem efeito além de garantir o canal de sinais coerente."""
    return {"status": "success", "message": f"Sistema single-mode. Modo atual: {OPERATION_MODE} (Perfil {CURRENT_MODE})"}


@app.post("/brain/sinais/toggle")
async def brain_sinais_toggle():
    global _sinais_claude_brain
    if not _sinais_claude_brain and not claude_brain.is_configured():
        raise HTTPException(400, "ANTHROPIC_API_KEY nao configurada no .env")
    _sinais_claude_brain = not _sinais_claude_brain
    state = "ATIVADO" if _sinais_claude_brain else "DESATIVADO"
    print(f"[BRAIN] Canal SINAIS: {state}")
    await save_global_state_to_db()
    return {"enabled": _sinais_claude_brain, "status": state}


@app.post("/brain/exec/toggle")
async def brain_exec_toggle():
    global _exec_claude_brain
    if not _exec_claude_brain and not claude_brain.is_configured():
        raise HTTPException(400, "ANTHROPIC_API_KEY nao configurada no .env")
    _exec_claude_brain = not _exec_claude_brain
    state = "ATIVADO" if _exec_claude_brain else "DESATIVADO"
    print(f"[BRAIN] Canal EXEC: {state}")
    await save_global_state_to_db()
    return {"enabled": _exec_claude_brain, "status": state}


# ── Trade Endpoints ───────────────────────────────────────────────────────────

@app.get("/trades/active")
async def active_trades():
    trades = await get_open_trades()
    # Enriquece com preço atual do cache
    for t in trades:
        cached = _active_trades_cache.get(t["id"], {})
        t["current_price"] = cached.get("current_price", t.get("entry_price", 0))
        t["pnl_usdt"]  = cached.get("pnl_usdt", 0)
        t["pnl_pct"]   = cached.get("pnl_pct", 0)
    return {"count": len(trades), "trades": trades}


@app.get("/trades/sync-binance")
async def sync_binance_positions():
    """
    Sincroniza posições abertas na Binance com o banco de dados.
    - Remove do DB trades que já foram fechados na Binance
    - Retorna posições abertas reais da Binance
    """
    try:
        def _fetch():
            client = _get_binance_client_synced()
            return client.futures_position_information()
        positions = await asyncio.to_thread(_fetch)
        open_pos  = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
    except Exception as e:
        return {"error": str(e), "open_binance": [], "closed": []}

    # Mapa symbol → direction real na Binance (considera Hedge Mode e One-Way)
    open_map: dict[str, str] = {}
    for p in open_pos:
        amt = float(p.get("positionAmt", 0))
        pos_side = p.get("positionSide", "BOTH")
        if pos_side in ("LONG", "SHORT"):
            # Hedge Mode: usa positionSide explícito
            open_map[f"{p['symbol']}_{pos_side}"] = pos_side
        else:
            # One-Way Mode: direção pelo sinal do amount
            direction = "LONG" if amt > 0 else "SHORT"
            open_map[f"{p['symbol']}_{direction}"] = direction

    open_symbols = {p["symbol"] for p in open_pos}

    # Marca como CLOSED no DB os trades que não estão mais abertos na Binance
    # Checa por SYMBOL + DIRECTION para detectar mismatch (ex: DB SHORT mas Binance LONG)
    db_trades = await get_open_trades()
    closed = []
    for t in db_trades:
        symbol    = t["asset"]
        db_dir    = str(t.get("direction", "LONG")).upper()
        key_exact = f"{symbol}_{db_dir}"
        # Fecha se: ativo não está na Binance OU direção não bate
        if symbol not in open_symbols or key_exact not in open_map:
            t["status"]    = "CLOSED"
            t["closed_at"] = datetime.utcnow().isoformat()
            await save_trade(t)
            _active_trades_cache.pop(t["id"], None)
            reason = "fechado na Binance" if symbol not in open_symbols else "direcao diferente na Binance (SL atingido?)"
            closed.append(f"{symbol} [{db_dir}]")
            print(f"[SYNC] {symbol} {db_dir} — {reason} → DB atualizado")

    # Monta lista de posições abertas com PnL atual
    result = []
    for p in open_pos:
        amt  = float(p["positionAmt"])
        epx  = float(p["entryPrice"])
        mark = float(p["markPrice"])
        pnl  = float(p["unRealizedProfit"])
        result.append({
            "symbol":     p["symbol"],
            "direction":  "LONG" if amt > 0 else "SHORT",
            "qty":        abs(amt),
            "entry":      epx,
            "mark_price": mark,
            "pnl_usdt":   round(pnl, 4),
            "pnl_pct":    round((mark - epx) / epx * 100 * (1 if amt > 0 else -1), 2),
            "leverage":   p.get("leverage", "?"),
        })

    await log_event("SYNC", f"Sync Binance: {len(result)} abertas, {len(closed)} fechadas", {"closed": closed})
    return {
        "open_binance": result,
        "closed_in_db": closed,
        "open_count": len(result),
    }


@app.get("/trades/history")
async def trade_history(limit: int = 50):
    trades = await get_all_trades(limit)
    return {"count": len(trades), "trades": trades}


@app.post("/trades/{trade_id}/close")
async def manual_close(trade_id: str):
    trades = await get_open_trades()
    target = next((t for t in trades if t["id"] == trade_id), None)
    if not target:
        # Tenta via sync Binance primeiro
        await _job_sync_binance()
        raise HTTPException(status_code=404, detail="Trade não encontrado")
    from models import Direction as Dir
    from binance_executor import _is_hedge_mode
    dir_val = Dir(target["direction"])

    def _do_manual_close() -> bool:
        client = _get_binance_client_synced()
        try:
            hedge = _is_hedge_mode(client)
            close_side  = "SELL" if dir_val == Dir.LONG else "BUY"
            pos_side    = "LONG" if dir_val == Dir.LONG else "SHORT"
            # Cancela todas as ordens pendentes do ativo
            client.futures_cancel_all_open_orders(symbol=target["asset"])
            # Fecha posição a mercado com a qty REAL da posicao
            qty = _get_position_qty(client, target["asset"], pos_side if hedge else "BOTH")
            if qty > 0:
                kwargs = {"positionSide": pos_side} if hedge else {"reduceOnly": True}
                client.futures_create_order(
                    symbol=target["asset"],
                    side=close_side,
                    type="MARKET",
                    quantity=qty,
                    **kwargs,
                )
            return True
        except Exception as e:
            print(f"[CLOSE] Erro: {e}")
            return close_position(target["asset"], dir_val, client)

    success = await asyncio.to_thread(_do_manual_close)

    if success:
        target["status"]    = "CLOSED"
        target["closed_at"] = datetime.utcnow().isoformat()
        await save_trade(target)
        _active_trades_cache.pop(trade_id, None)
        await send_trade_closed(target, "Fechado manualmente pelo usuário")
        if (target.get("pnl_usdt") or 0) > 0:
            _eff_mode = _resolve_effective_mode()
            asyncio.create_task(send_social_proof(target, _eff_mode))
    return {"success": success, "asset": target["asset"]}


def _get_position_qty(client, symbol: str, pos_side: str) -> float:
    """Retorna qty atual da posição para fechar no modo Hedge."""
    try:
        positions = client.futures_position_information(symbol=symbol)
        for p in positions:
            if p.get("positionSide") == pos_side:
                return abs(float(p.get("positionAmt", 0)))
    except Exception:
        pass
    return 0


# ── Performance ───────────────────────────────────────────────────────────────

@app.get("/performance")
async def performance():
    raw = await get_performance_stats()
    # Busca PnL por período direto do DB
    from datetime import timedelta
    now_iso = datetime.utcnow()
    today_str  = now_iso.date().isoformat()
    week_str   = (now_iso - timedelta(days=7)).isoformat()
    month_str  = (now_iso - timedelta(days=30)).isoformat()
    today_pnl = week_pnl = month_pnl = 0.0
    try:
        import aiosqlite as _aio
        from config import DB_PATH as _db
        async with _aio.connect(_db) as _db_conn:
            async with _db_conn.execute(
                "SELECT pnl_usdt FROM trades WHERE status='CLOSED' AND date(closed_at)=?",
                (today_str,)
            ) as c:
                today_pnl = sum(r[0] or 0 for r in await c.fetchall())
            async with _db_conn.execute(
                "SELECT pnl_usdt FROM trades WHERE status='CLOSED' AND closed_at>=?",
                (week_str,)
            ) as c:
                week_pnl = sum(r[0] or 0 for r in await c.fetchall())
            async with _db_conn.execute(
                "SELECT pnl_usdt FROM trades WHERE status='CLOSED' AND closed_at>=?",
                (month_str,)
            ) as c:
                month_pnl = sum(r[0] or 0 for r in await c.fetchall())
            # Avg R:R
            async with _db_conn.execute(
                "SELECT AVG(rr) FROM trades WHERE status='CLOSED'"
            ) as c:
                row = await c.fetchone()
                avg_rr = row[0] or 0
    except Exception:
        avg_rr = 0.0
    return {
        "total_trades":   raw.get("total", 0),
        "wins":           raw.get("wins", 0),
        "losses":         raw.get("losses", 0),
        "win_rate_pct":   raw.get("win_rate", 0),
        "total_pnl_usdt": raw.get("total_pnl", 0),
        "profit_factor":  raw.get("profit_factor", 0),
        "max_drawdown":   raw.get("max_drawdown", 0),
        "avg_rr":         round(avg_rr, 2),
        "today_pnl":      round(today_pnl, 2),
        "week_pnl":       round(week_pnl, 2),
        "month_pnl":      round(month_pnl, 2),
        "equity_curve":   raw.get("equity_curve", []),
        "data_quality":   raw.get("data_quality", "ok"),
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard/logo.png")
async def dashboard_logo():
    from pathlib import Path as _Path
    return FileResponse(_Path(__file__).parent / "dashboard" / "logo.png", media_type="image/png")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    # Caminho absoluto — funciona independente do cwd de onde o bot foi iniciado
    from pathlib import Path as _Path
    html_path = _Path(__file__).parent / "dashboard" / "index.html"
    with open(html_path, "r", encoding="utf-8") as f:
        # no-store: garante que o navegador (inclusive celular) sempre baixe a
        # versão mais recente do dashboard, sem ficar preso em cache antigo.
        return HTMLResponse(content=f.read(), headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        })


# ── Settings & Operation Mode Control ────────────────────────────────────────

@app.get("/settings")
async def get_settings():
    """Retorna todas as configurações atuais do bot."""
    cached = _balance_cache or {}
    open_trades = await get_open_trades()
    mode_cfg = MODE_SETTINGS.get(CURRENT_MODE, MODE_SETTINGS["NORMAL"])
    effective_banca = BANCA_USDT if BANCA_USDT > 0 else cached.get("available_balance", 0)
    daily_remaining = max(0, DAILY_TARGET_USDT - _daily_pnl) if DAILY_TARGET_USDT > 0 else 0
    # Tamanho por trade: banca ÷ nº de trades da sessão
    n_trades_eff = TRADES_PER_SESSION if TRADES_PER_SESSION > 0 else 5
    trade_size   = round(effective_banca / n_trades_eff, 2) if effective_banca > 0 else 0
    pct_each     = round(100.0 / n_trades_eff, 1)
    return {
        "operation_mode":    OPERATION_MODE,
        "trading_mode":      CURRENT_MODE,
        "banca_usdt":        round(BANCA_USDT, 2),
        "exposure_pct":      pct_each,
        "trade_size_usdt":   trade_size,
        "trades_per_session":TRADES_PER_SESSION,
        "session_trades":    _session_trades,
        "daily_target_usdt": DAILY_TARGET_USDT,
        "daily_pnl":         round(_daily_pnl, 2),
        "daily_remaining":   round(daily_remaining, 2),
        "wallet_balance":    round(cached.get("wallet_balance", 0), 2),
        "available_balance": round(cached.get("available_balance", 0), 2),
        "open_trades":       len(open_trades),
        "scan_interval_s":   _active_scan_interval_s(),   # intervalo REAL do scheduler
        "min_score":         mode_cfg["min_score"],
        "min_rr":            mode_cfg["min_rr"],
        "timeframes":        mode_cfg.get("timeframes", []),
        "signals_cached":    len(_latest_signals),
        "paper_trading":     PAPER_TRADING,
        "grid_pairs":        GRID_PAIRS,
        "grid_profit_target":GRID_PROFIT_TARGET_USDT,
        "grid_leverage":     GRID_LEVERAGE,
        "grid_max_concurrent":GRID_MAX_CONCURRENT,
        "grid_cycles":           _grid_cycles,
        "grid_total_profit":     round(_grid_profit_total, 2),
        "supervised_watchlist":  SUPERVISED_WATCHLIST,
        "autonomous_watchlist":  AUTONOMOUS_WATCHLIST,
        "sinais_watchlist":      SINAIS_WATCHLIST,
        "dual_mode_enabled":     DUAL_MODE_ENABLED,
        "sinais_profile":        SINAIS_PROFILE,
        "exec_mode":             EXEC_MODE,
        "sinais_brain_enabled":  _sinais_claude_brain,
        "exec_brain_enabled":    _exec_claude_brain,
        "claude_brain_enabled":  _claude_brain_enabled,
        "sinais_enabled":        SINAIS_ENABLED,
    }


@app.post("/settings/trades_per_session")
async def set_trades_per_session(count: int):
    global TRADES_PER_SESSION, _session_trades, _sinais_session_count
    if count < 0:
        raise HTTPException(400, "Use 0 para ilimitado ou valor > 0")
    TRADES_PER_SESSION = count
    _session_trades = 0
    _sinais_session_count = 0
    print(f"[SETTINGS] Trades/sessão: {count or 'ilimitado'}")
    await save_global_state_to_db()
    return {"trades_per_session": TRADES_PER_SESSION, "session_trades": _session_trades}


@app.post("/settings/sinais_rate_limit")
async def set_sinais_rate_limit(public_per_hour: int = None, vip_per_hour: int = None):
    """Define quantos sinais/hora cada canal pode receber. 0 = sem limite."""
    import notifier
    if public_per_hour is not None:
        if public_per_hour < 0:
            raise HTTPException(400, "Use 0 para ilimitado ou valor > 0")
        notifier.RATE_LIMIT_PUBLIC_PER_HOUR = public_per_hour
        await bot_state.save_key("sinais_max_hour_public", public_per_hour)
    if vip_per_hour is not None:
        if vip_per_hour < 0:
            raise HTTPException(400, "Use 0 para ilimitado ou valor > 0")
        notifier.RATE_LIMIT_VIP_PER_HOUR = vip_per_hour
        await bot_state.save_key("sinais_max_hour_vip", vip_per_hour)
    print(f"[SETTINGS] Limite sinais/hora — público: {notifier.RATE_LIMIT_PUBLIC_PER_HOUR or 'ilimitado'} "
          f"| VIP: {notifier.RATE_LIMIT_VIP_PER_HOUR or 'ilimitado'}")
    _pub_str = f"{notifier.RATE_LIMIT_PUBLIC_PER_HOUR}/h" if notifier.RATE_LIMIT_PUBLIC_PER_HOUR else "ilimitado"
    _vip_str = f"{notifier.RATE_LIMIT_VIP_PER_HOUR}/h" if notifier.RATE_LIMIT_VIP_PER_HOUR else "ilimitado"
    asyncio.create_task(send_alert(
        f"Limite de sinais/hora atualizado pelo dashboard:\n"
        f"📢 Canal público: {_pub_str}\n"
        f"💎 Canal VIP: {_vip_str}"
    ))
    return {
        "public_per_hour": notifier.RATE_LIMIT_PUBLIC_PER_HOUR,
        "vip_per_hour": notifier.RATE_LIMIT_VIP_PER_HOUR,
    }


@app.get("/settings/sinais_rate_limit")
async def get_sinais_rate_limit():
    import notifier
    return {
        "public_per_hour": notifier.RATE_LIMIT_PUBLIC_PER_HOUR,
        "vip_per_hour": notifier.RATE_LIMIT_VIP_PER_HOUR,
    }


@app.post("/settings/public_tier_pct")
async def set_public_tier_pct(p1: int = None, p2: int = None, p3: int = None, p4: int = None):
    """Define a % de cada nível de detalhe (1-4) do canal PÚBLICO. Não afeta o
    canal VIP em nada. Se a soma não der 100, normaliza proporcionalmente."""
    import notifier
    raw = {1: p1, 2: p2, 3: p3, 4: p4}
    if any(v is None for v in raw.values()):
        raise HTTPException(400, "Informe p1, p2, p3 e p4")
    if any(v < 0 for v in raw.values()):
        raise HTTPException(400, "Percentuais não podem ser negativos")
    total = sum(raw.values())
    if total <= 0:
        raise HTTPException(400, "A soma dos percentuais não pode ser 0")
    if total != 100:
        # Normaliza por maior resto, mesmo método de _pick_public_tier — garante soma exata 100
        scaled  = {k: v * 100.0 / total for k, v in raw.items()}
        floored = {k: int(v) for k, v in scaled.items()}
        falta   = 100 - sum(floored.values())
        restos  = sorted(raw.keys(), key=lambda k: scaled[k] - floored[k], reverse=True)
        for k in restos[:falta]:
            floored[k] += 1
        pct = floored
    else:
        pct = raw
    notifier.set_public_tier_pct(pct)
    await bot_state.save_key("public_tier_pct", json.dumps(pct))
    print(f"[SETTINGS] % por nível do canal público atualizado: {pct} (recebido: {raw})")
    return {"pct": pct, "normalized": total != 100}


@app.get("/settings/public_tier_pct")
async def get_public_tier_pct():
    import notifier
    return {
        "pct": notifier.get_public_tier_pct(),
        "today": notifier.get_public_tier_state(),
    }


@app.post("/signals/test_public_tiers")
async def test_public_tiers():
    """Dispara os 4 níveis de exemplo no canal público para revisão visual —
    NÃO usa o sorteio de produção, manda os 4 de uma vez, marcados como TESTE."""
    import notifier
    from config import TELEGRAM_CHANNEL_ID as _CH_ID
    if not _CH_ID:
        raise HTTPException(400, "TELEGRAM_CHANNEL_ID não configurado")
    result = await notifier.send_public_tier_test_batch()
    return result


@app.post("/settings/daily_target")
async def set_daily_target(target: float):
    global DAILY_TARGET_USDT
    if target < 0:
        raise HTTPException(400, "Meta deve ser >= 0 (0 = desativada)")
    DAILY_TARGET_USDT = round(target, 2)
    remaining = max(0, DAILY_TARGET_USDT - _daily_pnl)
    print(f"[SETTINGS] Meta diária: ${DAILY_TARGET_USDT:.2f} | PnL atual: ${_daily_pnl:.2f} | Falta: ${remaining:.2f}")
    await log_event("SETTINGS", f"Meta diária: ${DAILY_TARGET_USDT:.2f}")
    await save_global_state_to_db()
    return {"daily_target_usdt": DAILY_TARGET_USDT, "daily_pnl": round(_daily_pnl, 2), "remaining": round(remaining, 2)}


@app.post("/settings/reset_session")
async def reset_session():
    global _session_trades, _sinais_session_count
    _session_trades = 0
    _sinais_session_count = 0
    return {"session_trades": 0, "sinais_session_count": 0, "message": "Contadores de sessão resetados"}


@app.post("/settings/exposure")
async def set_exposure(pct: float):
    """Define % de exposição da banca por trade (5-50%)."""
    global EXPOSURE_PCT
    if pct < 1 or pct > 50:
        raise HTTPException(400, "Exposição deve ser entre 1% e 50%")
    EXPOSURE_PCT = round(pct, 1)
    trade_size = round(BANCA_USDT * EXPOSURE_PCT / 100, 2) if BANCA_USDT > 0 else 0
    print(f"[SETTINGS] Exposição: {EXPOSURE_PCT}% | Por trade: ${trade_size:.2f}")
    await save_global_state_to_db()
    return {"exposure_pct": EXPOSURE_PCT, "trade_size_usdt": trade_size}


@app.post("/settings/banca")
async def set_banca(banca: float):
    """Define a banca alocada para o bot (usada no modo AUTÔNOMO)."""
    global BANCA_USDT
    if banca < 0:
        raise HTTPException(400, "Banca deve ser >= 0 (0 = usa saldo disponível)")
    BANCA_USDT = round(banca, 2)
    n = TRADES_PER_SESSION if TRADES_PER_SESSION > 0 else 5
    trade_size = round(BANCA_USDT / n, 2) if BANCA_USDT > 0 else 0
    pct = round(100.0 / n, 1)
    print(f"[SETTINGS] Banca: ${BANCA_USDT:.2f} ÷ {n} trades = ${trade_size:.2f}/trade ({pct}%)")
    await log_event("SETTINGS", f"Banca atualizada: ${BANCA_USDT:.2f}")
    await save_global_state_to_db()
    return {
        "banca_usdt": BANCA_USDT,
        "trade_size_usdt": trade_size,
        "exposure_pct": pct,
        "message": f"Banca: ${BANCA_USDT:.2f} ÷ {n} trades = ${trade_size:.2f}/trade ({pct}%)"
    }


@app.post("/settings/mode")
async def set_operation_mode(mode: str):
    """Define modo de operação: AUTONOMOUS, SUPERVISED ou GRID (aceita slugs pt-BR)."""
    global OPERATION_MODE, EXEC_MODE, PAPER_TRADING, SINAIS_ENABLED, BOT_PAUSED
    _alias = {
        "supervisao": "SUPERVISED", "supervisionado": "SUPERVISED",
        "autonomo": "AUTONOMOUS", "automatico": "AUTONOMOUS",
        "grid": "GRID",
        "sinais": "SINAIS", "signal": "SINAIS", "signals": "SINAIS",
    }
    mode = _alias.get(mode.lower(), mode.upper())
    if mode not in ("AUTONOMOUS", "SUPERVISED", "GRID", "SINAIS"):
        raise HTTPException(400, "Modo invalido. Use: AUTONOMOUS, SUPERVISED, GRID ou SINAIS")
    # SINGLE-MODE: ativa exatamente 1 modo; o anterior é desligado por activate_mode.
    await bot_state.activate_mode(mode, profile=CURRENT_MODE)
    sync_state_to_globals()
    EXEC_MODE = OPERATION_MODE  # espelha o modo único ativo
    # Ativar modo retoma o bot (un-pausa). NÃO mexe em PAPER_TRADING — simulação
    # é controlada SÓ por /settings/paper_trading (correção do bug que abria ordem real).
    BOT_PAUSED = False
    # SINAIS só transmite em modo SINAIS; nos demais o canal de sinais fica off.
    SINAIS_ENABLED = (mode == "SINAIS")
    set_alerts_paused(False)  # garante Telegram liberado ao ativar qualquer modo
    # Aviso de ativação no Telegram (chat pessoal) — feedback IMEDIATO de que o
    # modo está no ar, mesmo antes de qualquer sinal qualificar nos filtros.
    _tps = TRADES_PER_SESSION if TRADES_PER_SESSION > 0 else "ilimitado"
    if mode == "AUTONOMOUS":
        _reset_auto_killswitch()  # sessão autônoma limpa: zera PnL e recaptura banca p/ o kill-switch -20%
        _exec_kind = "📝 SIMULADAS (PAPER)" if PAPER_TRADING else "🔴 REAIS"
        msg = "[BOT] Modo AUTONOMO ativado — bot executa trades automaticamente"
        aviso = (f"🤖 AUTÔNOMO ATIVADO (perfil {CURRENT_MODE})\n"
                 f"Banca ${BANCA_USDT:.2f} · {_tps} trades/sessão · alav. {GRID_LEVERAGE}x\n"
                 f"O bot vai ABRIR E FECHAR ordens {_exec_kind} sozinho quando um sinal passar nos filtros.")
        asyncio.create_task(job_scan_market())  # scan imediato → 1º sinal sai em segundos, não em até 60s
    elif mode == "GRID":
        pairs_str = " | ".join(GRID_PAIRS)
        msg = f"[GRID] Modo GRID ativado — pares: {pairs_str} | alvo: ${GRID_PROFIT_TARGET_USDT}/ciclo"
        aviso = (f"⚡ GRID ATIVADO (perfil {CURRENT_MODE})\n"
                 f"Pares: {pairs_str}\n"
                 f"Alvo ${GRID_PROFIT_TARGET_USDT}/ciclo · alav. {GRID_LEVERAGE}x — ciclos automáticos de compra/venda.")
        asyncio.create_task(job_grid_scan())
    elif mode == "SINAIS":
        wl_str = " | ".join(SINAIS_WATCHLIST) if SINAIS_WATCHLIST else "watchlist global"
        msg = f"[SINAIS] Modo SINAIS ativado — transmitindo sinais sem execucao | {wl_str}"
        aviso = (f"📡 SINAIS ATIVADO (perfil {CURRENT_MODE})\n"
                 f"Alertas de oportunidade aqui, SEM abrir ordens. Watchlist: {wl_str}.")
    else:
        msg = "[BOT] Modo SUPERVISIONADO ativado — bot envia sinais para aprovacao"
        aviso = (f"👤 SUPERVISIONADO ATIVADO (perfil {CURRENT_MODE})\n"
                 f"Banca ${BANCA_USDT:.2f} · {_tps} trades/sessão · alav. {GRID_LEVERAGE}x\n"
                 f"Vou te enviar cada sinal aqui com botões APROVAR/REJEITAR antes de operar.")
        asyncio.create_task(job_scan_market())  # scan imediato → 1º sinal de aprovação sai logo
    print(f"[SETTINGS] mode={mode}")
    await log_event("SETTINGS", f"Modo operacao: {mode}")
    asyncio.create_task(send_alert(aviso))   # aviso de ativação → Telegram pessoal
    await save_global_state_to_db() # Salva o estado atualizado no DB SQLite
    return {"operation_mode": OPERATION_MODE, "message": msg}


@app.get("/settings/grid")
async def get_grid_settings():
    return {
        "pairs": GRID_PAIRS,
        "profit_target_usdt": GRID_PROFIT_TARGET_USDT,
        "leverage": GRID_LEVERAGE,
        "max_concurrent": GRID_MAX_CONCURRENT,
        "cycles": _grid_cycles,
        "total_profit": round(_grid_profit_total, 2),
        "active": OPERATION_MODE == "GRID",
    }


@app.post("/settings/grid")
async def update_grid_settings(
    pairs: str = None,
    profit_target: float = None,
    leverage: int = None,
    max_concurrent: int = None,
):
    """Atualiza configurações do Grid Mode."""
    global GRID_PAIRS, GRID_PROFIT_TARGET_USDT, GRID_LEVERAGE, GRID_MAX_CONCURRENT
    if pairs:
        GRID_PAIRS = [p.strip().upper() for p in pairs.split(",") if p.strip()]
    if profit_target is not None and profit_target >= 0:   # 0 = ilimitado
        GRID_PROFIT_TARGET_USDT = profit_target
    if leverage is not None and 1 <= leverage <= 50:
        GRID_LEVERAGE = leverage
    if max_concurrent is not None and 1 <= max_concurrent <= 10:
        GRID_MAX_CONCURRENT = max_concurrent
    return await get_grid_settings()


@app.post("/settings/watchlist")
async def set_mode_watchlist(mode: str, symbols: str = ""):
    """
    Define watchlist por modo.
    mode: supervised | autonomous | grid
    symbols: "BTCUSDT,ETHUSDT,SOLUSDT" ou "all" para usar lista global
    """
    global SUPERVISED_WATCHLIST, AUTONOMOUS_WATCHLIST, GRID_PAIRS, SINAIS_WATCHLIST
    cleaned = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols and symbols.lower() != "all" else []
    # Garante sufixo USDT
    cleaned = [s if s.endswith("USDT") else s + "USDT" for s in cleaned]
    m = mode.lower()
    if m in ("supervised", "supervisionado"):
        SUPERVISED_WATCHLIST = cleaned
        label = "supervised"
    elif m in ("autonomous", "autonomo"):
        AUTONOMOUS_WATCHLIST = cleaned
        label = "autonomous"
    elif m == "grid":
        if cleaned:
            GRID_PAIRS = cleaned
        label = "grid"
    elif m in ("sinais", "signal", "signals"):
        SINAIS_WATCHLIST = cleaned
        label = "sinais"
    else:
        raise HTTPException(400, "mode deve ser: supervised | autonomous | grid | sinais")
    msg = f"Watchlist {label}: {', '.join(cleaned) if cleaned else 'todas (global)'}"
    print(f"[SETTINGS] {msg}")
    await send_alert(msg)
    return {"mode": label, "symbols": cleaned or "all", "message": msg}


@app.get("/grid/cycles")
async def get_grid_cycles():
    return {
        "cycles": _grid_cycles,
        "total_profit": round(_grid_profit_total, 2),
        "total_cycles": sum(_grid_cycles.values()),
        "pairs": GRID_PAIRS,
        "target_per_cycle": GRID_PROFIT_TARGET_USDT,
    }


@app.post("/bot/test")
async def bot_test():
    """Envia mensagem de teste ao Telegram com status atual do bot."""
    await _send_startup_test_notification()
    return {"message": "Notificacao de teste enviada ao Telegram"}


@app.get("/grid/status")
async def get_grid_status():
    """Alias compatível com o dashboard."""
    return {
        "total_cycles": sum(_grid_cycles.values()),
        "total_profit": round(_grid_profit_total, 2),
        "cycles": _grid_cycles,
        "pairs": GRID_PAIRS,
        "target_per_cycle": GRID_PROFIT_TARGET_USDT,
        "active": OPERATION_MODE == "GRID",
    }


# ── Configuração temporária (Pending Config) ──────────────────────────────────
_pending_config: dict = {}

@app.post("/config/propose")
async def propose_config(
    banca: float = None,
    risco: float = None,
    daily_target: float = None,
    leverage: int = None,
    perfil: str = None,
    mode: str = None
):
    """Propõe alterações de configuração sem aplicá-las de imediato."""
    global _pending_config
    if banca is not None and banca >= 0:
        _pending_config["banca_usdt"] = round(banca, 2)
    if risco is not None and 1 <= risco <= 50:
        _pending_config["exposure_pct"] = round(risco, 1)
    if daily_target is not None and daily_target >= 0:
        _pending_config["daily_target_usdt"] = round(daily_target, 2)
    if leverage is not None and 1 <= leverage <= 50:
        _pending_config["grid_leverage"] = leverage
    if perfil is not None:
        p = perfil.upper()
        if p in ("CONSERVATIVE", "NORMAL", "AGGRESSIVE"):
            _pending_config["trading_mode"] = p
    if mode is not None:
        _pending_config["operation_mode"] = mode

    return {"status": "pending", "pending": _pending_config}

@app.post("/config/approve")
async def approve_config():
    """Aplica de forma atômica e definitiva todas as configurações propostas e avisa no Telegram."""
    global _pending_config, BANCA_USDT, EXPOSURE_PCT, DAILY_TARGET_USDT, GRID_LEVERAGE, CURRENT_MODE, OPERATION_MODE
    if not _pending_config:
        return {"status": "no_changes", "message": "Nenhuma configuração pendente para aprovação."}

    applied = {}
    if "banca_usdt" in _pending_config:
        BANCA_USDT = _pending_config["banca_usdt"]
        applied["Banca"] = f"${BANCA_USDT:.2f}"
    if "exposure_pct" in _pending_config:
        EXPOSURE_PCT = _pending_config["exposure_pct"]
        applied["Exposição por Trade"] = f"{EXPOSURE_PCT}%"
    if "daily_target_usdt" in _pending_config:
        DAILY_TARGET_USDT = _pending_config["daily_target_usdt"]
        applied["Meta Diária"] = f"${DAILY_TARGET_USDT:.2f}"
    if "grid_leverage" in _pending_config:
        GRID_LEVERAGE = _pending_config["grid_leverage"]
        applied["Alavancagem Grid"] = f"{GRID_LEVERAGE}x"
    if "trading_mode" in _pending_config:
        CURRENT_MODE = _pending_config["trading_mode"]
        _update_scan_interval()
        applied["Perfil de Risco"] = CURRENT_MODE
    if "operation_mode" in _pending_config:
        # Se for mudar de modo enquanto já rodando, se auto_off/auto_on/etc forem ativados
        # passamos o profile ativo atual
        await bot_state.activate_mode(_pending_config["operation_mode"], profile=CURRENT_MODE)
        sync_state_to_globals()
        applied["Modo de Operação"] = OPERATION_MODE

    # Persiste no banco de dados SQLite e sincroniza
    await save_global_state_to_db()
    sync_state_to_globals()

    # Limpa propostas
    _pending_config.clear()

    # Envia notificação ao Telegram pessoal
    from datetime import datetime as _dt
    now = _dt.utcnow().strftime("%d/%m/%Y %H:%M UTC")
    lines = [f"• *{k}*: `{v}`" for k, v in applied.items()]
    msg = (
        "⚙️ *CONFIGURAÇÕES APROVADAS E APLICADAS*\n\n"
        + "\n".join(lines) + f"\n\nPaper Trading: `{'ON' if PAPER_TRADING else 'OFF'}`\n"
        f"Timestamp: {now}"
    )
    await send_alert(msg)

    return {"status": "approved", "applied": applied}


@app.get("/auto/status")
async def auto_status():
    """Status resumido enriquecido com dados de tempo de execução real do bot (compatibilidade com dashboard)."""
    settings = await get_settings()
    
    # Adiciona dados em tempo real
    settings["real_running_mode"] = _resolve_effective_mode()
    settings["active_strategy"] = CURRENT_MODE
    settings["running_state"] = "PAUSADO" if PAPER_TRADING else "LIVE"
    settings["telegram_state"] = "BLOQUEADO" if is_alerts_paused() else "LIBERADO"
    settings["pending_config"] = _pending_config

    return {**settings, "autonomous": OPERATION_MODE == "AUTONOMOUS", "last_scan_at": _last_scan_at, "operation_mode": OPERATION_MODE, "mode_started_at": _mode_started_at}


@app.post("/auto/on")
async def auto_on():
    await bot_state.activate_mode("AUTONOMOUS", profile=CURRENT_MODE)
    sync_state_to_globals()
    return {"operation_mode": OPERATION_MODE}


@app.post("/auto/off")
async def auto_off():
    await bot_state.activate_mode("SUPERVISED", profile=CURRENT_MODE)
    sync_state_to_globals()
    return {"operation_mode": OPERATION_MODE}


@app.post("/auto/grid")
async def auto_grid():
    await bot_state.activate_mode("GRID", profile=CURRENT_MODE)
    sync_state_to_globals()
    return {"operation_mode": OPERATION_MODE}


@app.post("/auto/sinais")
async def auto_sinais():
    await bot_state.activate_mode("SINAIS", profile=CURRENT_MODE)
    sync_state_to_globals()
    return {"operation_mode": OPERATION_MODE}


@app.api_route("/auto/force_scan", methods=["GET", "POST"])
async def force_scan():
    asyncio.create_task(job_scan_market())
    started_at = datetime.utcnow().strftime('%H:%M:%S')
    return {"message": "Scan iniciado", "operation_mode": OPERATION_MODE, "trading_mode": CURRENT_MODE, "started_at": started_at}


def _active_scan_interval_s() -> int:
    """Intervalo REAL de scan do perfil ativo (fonte única: MODE_SETTINGS)."""
    cfg = MODE_SETTINGS.get(CURRENT_MODE, MODE_SETTINGS["NORMAL"])
    # Piso de segurança absoluto: nunca < 45s (histórico de IP ban -1003)
    return max(45, int(cfg.get("scan_interval_s", 60)))


def _active_max_open() -> int:
    """Máximo de trades simultâneos do perfil ativo (fonte única: MODE_SETTINGS)."""
    cfg = MODE_SETTINGS.get(CURRENT_MODE, MODE_SETTINGS["NORMAL"])
    return int(cfg.get("max_open_trades", 5))


def _update_scan_interval():
    """Reajusta o intervalo de scan conforme o perfil ativo (lê de MODE_SETTINGS)."""
    seconds = _active_scan_interval_s()
    try:
        job = scheduler.get_job("scan")
        if job:
            scheduler.reschedule_job("scan", trigger="interval", seconds=seconds)
            print(f"[MODE] Scan reajustado para {seconds}s ({CURRENT_MODE})")
    except Exception as e:
        print(f"[MODE] Erro ao reajustar scan: {e}")


def _profile_alert(name_pt: str, key: str) -> str:
    _s = MODE_SETTINGS[key]
    return (f"Perfil {name_pt} ativado — Score>={_s['min_score']}, RR>={_s['min_rr']}, "
            f"risco {_s['risk_pct']}%, scan {_s['scan_interval_s']}s, TFs {'/'.join(_s['timeframes'])}")

@app.post("/mode/conservative")
async def set_conservative_mode():
    global CURRENT_MODE
    CURRENT_MODE = "CONSERVATIVE"
    _update_scan_interval()
    await save_global_state_to_db()
    await send_alert(_profile_alert("CONSERVADOR", "CONSERVATIVE"))
    asyncio.create_task(job_scan_market())
    return {"mode": "CONSERVATIVE", "settings": MODE_SETTINGS["CONSERVATIVE"]}


@app.post("/mode/normal")
async def set_normal_mode():
    global CURRENT_MODE
    CURRENT_MODE = "NORMAL"
    _update_scan_interval()
    await save_global_state_to_db()
    await send_alert(_profile_alert("NORMAL", "NORMAL"))
    asyncio.create_task(job_scan_market())
    return {"mode": "NORMAL", "settings": MODE_SETTINGS["NORMAL"]}


@app.post("/mode/aggressive")
async def set_aggressive_mode():
    global CURRENT_MODE
    CURRENT_MODE = "AGGRESSIVE"
    _update_scan_interval()
    await save_global_state_to_db()
    await send_alert(_profile_alert("AGRESSIVO", "AGGRESSIVE"))
    asyncio.create_task(job_scan_market())
    return {"mode": "AGGRESSIVE", "settings": MODE_SETTINGS["AGGRESSIVE"]}


@app.post("/settings/paper_trading")
async def set_paper_trading(enabled: bool):
    """Ativa/desativa Paper Trading (simulação sem dinheiro real)."""
    global PAPER_TRADING
    PAPER_TRADING = enabled
    status = "ATIVADO" if enabled else "DESATIVADO"
    print(f"[SETTINGS] Paper Trading {status}")
    await save_global_state_to_db()
    if enabled:
        await send_alert("[PAPER] PAPER TRADING ativado — Nenhuma ordem real sera enviada a Binance!")
    return {"paper_trading": PAPER_TRADING, "message": f"Paper Trading {status}"}


@app.get("/fear_greed")
async def get_fear_greed_endpoint():
    """Fear & Greed Index atual."""
    try:
        from fear_greed import get_fear_greed, fg_label_emoji
        fg = await get_fear_greed()
        fg["emoji"] = fg_label_emoji(fg["label"])
        return fg
    except Exception as e:
        return {"value": 50, "label": "Neutral", "error": str(e)}


@app.get("/trailing_stop/{trade_id}")
async def get_trailing_status(trade_id: str):
    """Retorna status do trailing stop de um trade."""
    trade = _active_trades_cache.get(trade_id)
    if not trade:
        raise HTTPException(404, "Trade não encontrado")
    return {
        "id":           trade_id,
        "asset":        trade.get("asset"),
        "entry":        trade.get("entry_price"),
        "current_stop": trade.get("stop_loss"),
        "current_price":trade.get("current_price"),
        "pnl_pct":      trade.get("pnl_pct", 0),
        "trailing_active": (trade.get("pnl_pct", 0) or 0) > 0,
    }


@app.get("/volume/zone")
async def get_volume_zones():
    """Retorna zona de volume (RED/YELLOW/GREEN) para os pares do watchlist e grid."""
    import aiohttp
    pairs = list(set(GRID_PAIRS + list(WATCHLIST)[:20]))
    zones = {}
    try:
        try:
            _resolver = aiohttp.AsyncResolver(nameservers=["8.8.8.8", "1.1.1.1"])
        except Exception:
            _resolver = None
        connector = aiohttp.TCPConnector(resolver=_resolver, ssl=True)
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.get(
                "https://fapi.binance.com/fapi/v1/ticker/24hr",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                tickers = await r.json()
        vol_map = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}
        for p in pairs:
            vol = vol_map.get(p, 0)
            vol_m = vol / 1_000_000
            if vol_m < 20:
                zone = "RED"
            elif vol_m < 200:
                zone = "YELLOW"
            else:
                zone = "GREEN"
            zones[p] = {"volume_m": round(vol_m, 1), "zone": zone}
    except Exception as e:
        print(f"[VOLUME ZONE] {e}")
    return zones


@app.get("/trending")
async def get_trending():
    """Retorna top movers da Binance Futuros."""
    if not _trending_cache:
        fresh = await get_trending_futures(10)
        return {"symbols": fresh}
    return {"symbols": _trending_cache}


# ── Backtesting ───────────────────────────────────────────────────────────────

@app.get("/backtest/strategies/list")
async def list_strategies():
    from backtest import STRATEGIES
    return STRATEGIES


@app.get("/backtest/run/full")
async def run_full_backtest_endpoint():
    """Roda benchmark completo em background e retorna resultados salvos."""
    import os
    if os.path.exists("backtest_results.json"):
        with open("backtest_results.json") as f:
            return {"cached": True, "results": json.load(f)}
    asyncio.create_task(_run_backtest_background())
    return {"message": "Backtest iniciado em background — aguarde ~2 min e chame novamente"}


@app.get("/backtest/{symbol}")
async def backtest_symbol(
    symbol: str,
    strategy: str = "EMA_CROSS_MOMENTUM",
    timeframe: str = "1h",
    direction: str = "BOTH",
    days: int = 120,
    leverage: int = 0,
):
    from backtest import run_backtest, STRATEGIES
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    if strategy not in STRATEGIES:
        raise HTTPException(400, f"Estratégia inválida. Opções: {list(STRATEGIES.keys())}")
    lev = leverage or (15 if sym in ("BTCUSDT","ETHUSDT","SOLUSDT") else 5)
    result = await run_backtest(sym, strategy, timeframe, direction, days, lev)
    return result


async def _run_backtest_background():
    from backtest import run_full_benchmark
    try:
        await run_full_benchmark()
    except Exception as e:
        print(f"[BACKTEST] Erro: {e}")


# ── Anomaly Detection ─────────────────────────────────────────────────────────

async def job_scan_anomalies():
    """Varre ativos em busca de movimentos atípicos a cada 3 minutos."""
    global _anomalies_cache
    try:
        found = await scan_anomalies()
        _anomalies_cache = found
        for item in found:
            print(f"[ANOMALY] {item['symbol']} {item['timeframe']}: {item['anomaly']}")
    except Exception as e:
        print(f"[ANOMALY] Erro: {e}")


@app.get("/anomalies")
async def get_anomalies():
    """Retorna movimentos atípicos detectados (volume spike, candle explosivo, etc.)"""
    return {"count": len(_anomalies_cache), "anomalies": _anomalies_cache}


@app.get("/signals/daily-report")
async def daily_signal_report(date: str = None):
    """
    Backtesting resumido do dia.
    - Sinais executados: usa PnL real do banco
    - Sinais rejeitados: compara preço atual vs TP1/SL para estimar acerto
    Parâmetro opcional: date=YYYY-MM-DD (padrão = hoje UTC)
    """
    from database import daily_signal_report as _report
    report = await _report(date)

    # Formata para leitura fácil
    lines = [f"📊 Relatório {report['date']}",
             f"Total sinais: {report['total']}",
             f"✅ Acertos: {report['wins']}  ❌ Erros: {report['losses']}  "
             f"⏳ Em aberto: {report['open']}  ❓ Sem dados: {report.get('unknown', 0)}",
             f"Win rate (decididos): {report['win_rate']}%",
             ""]

    for r in report.get("results", []):
        emoji = {"WIN": "✅", "LOSS": "❌", "OPEN": "⏳", "?": "❓"}.get(
            r["outcome"].split()[0], "🔸"
        )
        pnl_str = f" ({r['pnl_pct']:+.1f}%)" if r.get("pnl_pct") is not None else ""
        src = " [estimado]" if r.get("source") == "estimado" else ""
        lines.append(
            f"{emoji} {r['asset']} {r['direction']} {r['tf']} "
            f"conf={r['confidence']:.0f} → {r['outcome']}{pnl_str}{src}"
        )

    report["summary_text"] = "\n".join(lines)
    return report


@app.get("/signals/kpi")
async def signals_kpi():
    """KPIs do canal SINAIS para os cards do topo do dashboard."""
    return await get_signal_kpi_summary()


@app.get("/balance")
async def balance():
    """Retorna saldo — campos compatíveis com o dashboard."""
    cached = _balance_cache or {}
    return {
        "wallet_balance":    round(cached.get("wallet_balance", 0), 2),
        "available_balance": round(cached.get("available_balance", 0), 2),
        "unrealized_pnl":    round(cached.get("unrealized_pnl", 0), 2),
        "realized_pnl":      round(cached.get("realized_pnl", 0), 2),
        "margin_balance":    round(cached.get("margin_balance", 0), 2),
        "daily_pnl":         round(_daily_pnl, 2),
    }


# ── Balance detalhado (novo) ──────────────────────────────────────────────────

@app.get("/api/balance")
async def api_balance():
    """Saldo detalhado: wallet, available, unrealized PnL, realized PnL."""
    cached = _balance_cache or {}
    return {
        "wallet_balance": round(cached.get("wallet_balance", 0), 4),
        "available_balance": round(cached.get("available_balance", 0), 4),
        "unrealized_pnl": round(cached.get("unrealized_pnl", 0), 4),
        "realized_pnl": round(cached.get("realized_pnl", 0), 4),
        "margin_balance": round(cached.get("margin_balance", 0), 4),
    }


@app.get("/trades/history/binance")
async def binance_trade_history(hours: int = 24):
    """Histórico de PnL realizado nas últimas N horas direto da Binance."""
    try:
        result = await asyncio.to_thread(get_binance_trade_history, WATCHLIST, hours=hours)
        return result
    except Exception as e:
        return {"error": str(e), "trades": [], "total_pnl": 0}


# ── Bot Lifecycle / Dashboard Actions ─────────────────────────────────────────

@app.get("/sparkline/{symbol}")
async def get_sparkline(symbol: str, interval: str = "1h", limit: int = 48):
    """Retorna array de closes para sparkline charts."""
    import aiohttp
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    try:
        try:
            _resolver = aiohttp.AsyncResolver(nameservers=["8.8.8.8", "1.1.1.1"])
        except Exception:
            _resolver = None
        connector = aiohttp.TCPConnector(resolver=_resolver, ssl=True)
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": sym, "interval": interval, "limit": limit},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
        if isinstance(data, list):
            return {"symbol": sym, "prices": [float(k[4]) for k in data]}
        return {"symbol": sym, "prices": []}
    except Exception as e:
        return {"symbol": sym, "prices": [], "error": str(e)}


@app.post("/bot/pause")
async def pause_bot():
    """Pausa o bot e bloqueia imediatamente todo envio ao Telegram."""
    global BOT_PAUSED, SINAIS_ENABLED
    # Aviso ANTES de bloquear o Telegram, senão a própria notificação seria barrada.
    await send_alert(f"⏸️ BOT PAUSADO — sem novas ordens nem sinais (modo {OPERATION_MODE} preservado).")
    BOT_PAUSED = True          # pausa (separado de PAPER_TRADING — não altera simulação)
    SINAIS_ENABLED = False
    set_alerts_paused(True)   # bloqueia QUALQUER envio ao Telegram, inclusive tasks já agendadas
    print("[BOT] Pausado via dashboard — BOT_PAUSED=True, SINAIS_ENABLED=False, Telegram bloqueado")
    await save_global_state_to_db()
    return {"status": "paused", "bot_paused": True, "paper_trading": PAPER_TRADING, "sinais_enabled": False}


@app.post("/bot/resume")
async def resume_bot():
    """Retoma operacoes e libera envio ao Telegram. NÃO altera PAPER_TRADING (simulação preservada)."""
    global BOT_PAUSED, SINAIS_ENABLED
    BOT_PAUSED = False         # un-pausa (NÃO mexe em PAPER_TRADING — preserva simulação)
    # Só reativa sinais se o modo atual justifica — evita SINAIS=True com SUPERVISED/AUTONOMOUS ativo
    if OPERATION_MODE == "SINAIS":
        SINAIS_ENABLED = True
    set_alerts_paused(False)  # libera Telegram
    print(f"[BOT] Retomado via dashboard — BOT_PAUSED=False, PAPER_TRADING={PAPER_TRADING}, SINAIS_ENABLED={SINAIS_ENABLED}, modo={OPERATION_MODE}")
    _kind = "SIMULADO (paper)" if PAPER_TRADING else "REAL"
    await send_alert(f"▶️ BOT RETOMADO — modo {OPERATION_MODE} ({CURRENT_MODE}) · execução {_kind}.")
    await save_global_state_to_db()
    return {"status": "running", "bot_paused": False, "paper_trading": PAPER_TRADING, "sinais_enabled": SINAIS_ENABLED}


# IDs fixos do projeto/serviço no Railway (não são segredo, aparecem na própria URL
# do painel) — só o RAILWAY_PROJECT_TOKEN (env var) é sensível.
_RAILWAY_PROJECT_ID     = "cba184f2-5988-46c5-977f-4af22e443014"
_RAILWAY_SERVICE_ID     = "58815b1d-9515-460a-816c-f22d4aa21d27"
_RAILWAY_ENVIRONMENT_ID = "7d91ba84-8e1b-41ee-a45f-971c7ac65474"
_RAILWAY_GRAPHQL_URL    = "https://backboard.railway.com/graphql/v2"


@app.post("/bot/shutdown_railway")
async def shutdown_railway_service():
    """Desliga o SERVIÇO de verdade no Railway (para o container, não só o bot em
    memória) via API pública do Railway. Diferente de /bot/pause: isso economiza o
    custo do Railway enquanto desligado, mas é IRREVERSÍVEL por aqui — o dashboard
    fica inacessível até religar manualmente pelo painel do Railway (Deployments →
    Redeploy no último deployment)."""
    import aiohttp
    token = os.environ.get("RAILWAY_PROJECT_TOKEN")
    if not token:
        raise HTTPException(400, "RAILWAY_PROJECT_TOKEN não configurado nas variáveis do serviço.")

    headers = {"Project-Access-Token": token, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        # 1) Acha o deployment ativo (mais recente com status SUCCESS)
        q = {
            "query": "query deployments($input: DeploymentListInput!) { "
                     "deployments(input: $input, first: 5) { edges { node { id status } } } }",
            "variables": {"input": {
                "projectId": _RAILWAY_PROJECT_ID,
                "environmentId": _RAILWAY_ENVIRONMENT_ID,
                "serviceId": _RAILWAY_SERVICE_ID,
            }},
        }
        async with session.post(_RAILWAY_GRAPHQL_URL, json=q, headers=headers) as r:
            data = await r.json()
        edges = (data.get("data") or {}).get("deployments", {}).get("edges", [])
        active_id = next((e["node"]["id"] for e in edges if e["node"]["status"] == "SUCCESS"), None)
        if not active_id:
            raise HTTPException(500, f"Não achei o deployment ativo no Railway: {data}")

    # Avisa ANTES de desligar — depois disso o bot não consegue mais mandar nada.
    await send_alert(
        "🛑 *DESLIGANDO O SERVIÇO NO RAILWAY* — acionado pelo dashboard.\n"
        "O bot e o dashboard vão parar de responder em instantes.\n"
        "Pra religar: painel do Railway → Deployments → Redeploy no último deployment."
    )
    print(f"[RAILWAY] Desligando deployment {active_id} via API pública (acionado pelo dashboard)")

    async def _stop_after_delay():
        # Atraso curto pra essa resposta HTTP conseguir chegar no navegador antes
        # do container morrer de verdade.
        await asyncio.sleep(2)
        m = {
            "query": "mutation deploymentStop($id: String!) { deploymentStop(id: $id) }",
            "variables": {"id": active_id},
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s2:
                await s2.post(_RAILWAY_GRAPHQL_URL, json=m, headers=headers)
        except Exception as e:
            print(f"[RAILWAY] Erro ao chamar deploymentStop: {e}")

    asyncio.create_task(_stop_after_delay())
    return {
        "status": "shutting_down",
        "deployment_id": active_id,
        "message": "Serviço será desligado em ~2s. Religue pelo painel do Railway (Deployments → Redeploy).",
    }


@app.post("/bot/killswitch/reset")
async def killswitch_reset():
    """Reset manual do kill-switch -20% do modo AUTÔNOMO (libera novas entradas)."""
    res = _reset_auto_killswitch()
    await send_alert(
        f"✅ *Kill-switch -{AUTO_KILLSWITCH_PCT:.0f}% resetado* — sessão autônoma reiniciada. "
        f"O bot volta a abrir entradas no próximo ciclo."
    )
    await save_global_state_to_db()
    return {"status": "ok", **res}


@app.get("/bot/killswitch")
async def killswitch_status():
    """Estado atual do kill-switch -20% do modo AUTÔNOMO."""
    ref = _auto_killswitch_ref_banca()
    return {
        "tripped":       _auto_killswitch_tripped,
        "pct":           AUTO_KILLSWITCH_PCT,
        "ref_banca":     round(ref, 2),
        "session_pnl":   round(_auto_session_pnl, 2),
        "loss_limit":    round(ref * AUTO_KILLSWITCH_PCT / 100.0, 2),
        "entry_cadence_s": _entry_cadence_s(),
    }


@app.post("/bot/reset")
async def reset_bot():
    """Reseta cache, sinais e contadores. Preserva API keys e configuracoes."""
    global _latest_signals, _anomalies_cache, _session_trades, _sinais_session_count, _signal_cooldown, _trending_cache
    _latest_signals = []
    _anomalies_cache = []
    _session_trades = 0
    _sinais_session_count = 0
    _signal_cooldown.clear()
    _trending_cache = []
    msg = "[RESET] Cache, sinais e contadores resetados. Configuracoes e API keys preservadas."
    await log_event("RESET", "Bot resetado via dashboard")
    return {"status": "reset", "message": msg}


@app.post("/macro/broadcast")
async def broadcast_macro_today():
    """Envia todos os eventos macro do dia em uma unica mensagem para o Telegram."""
    try:
        import market_engine
        st = market_engine.get_market_state()
        events = st.get("macro_events", []) or []
        
        today_events = [ev for ev in events if ev.get("days_away", 99) == 0]
        
        if not today_events:
            return {"status": "empty", "message": "Nenhum evento macro programado para hoje."}
            
        msg = "📅 *EVENTOS MACRO HOJE*\n\n"
        for ev in today_events:
            impact = ev.get("impact", "")
            icon = "🚨" if impact == "HIGH" else ("⚠️" if impact == "MEDIUM" else "ℹ️")
            msg += f"{icon} `{ev.get('name', '?')}` ({ev.get('type', '')})\n"
            msg += f"   Impacto: {impact} | Horario: {ev.get('date', '')}\n\n"
            
        await send_alert(msg)
        return {"status": "sent", "count": len(today_events)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/config/notify")
async def notify_config_change():
    """Envia notificacao Telegram com a configuracao atual do bot."""
    from datetime import datetime as _dt
    now = _dt.utcnow().strftime("%d/%m/%Y %H:%M UTC")
    mode_map = {"AUTONOMOUS": "Autonomo", "SUPERVISED": "Supervisao", "GRID": "Grid"}
    profile_map = {"NORMAL": "Normal", "AGGRESSIVE": "Agressivo"}
    n = TRADES_PER_SESSION
    risk_est = round(100.0 / max(n, 5), 1) if n > 0 else 20.0
    msg = (
        "CONFIGURACAO ATUALIZADA\n\n"
        "Modo de Execucao: " + mode_map.get(OPERATION_MODE, OPERATION_MODE) + "\n"
        "Capital: $" + f"{BANCA_USDT:.2f}" + " USDT\n"
        "Trades por Sessao: " + (str(n) if n > 0 else "Ilimitado") + "\n"
        "Risco por Trade: ~" + str(risk_est) + "%\n"
        "Alavancagem: " + str(GRID_LEVERAGE) + "x\n"
        "Perfil: " + profile_map.get(CURRENT_MODE, CURRENT_MODE) + "\n"
        "Paper Trading: " + ("ON (simulacao)" if PAPER_TRADING else "OFF (real)") + "\n"
        "Timestamp: " + now
    )
    await send_alert(msg)
    return {"sent": True, "timestamp": now}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "bot": "TRADER 001",
        "autonomous": OPERATION_MODE == "AUTONOMOUS",
    }


@app.get("/cache/stats")
async def cache_stats_endpoint():
    """Estatísticas do klines cache — quantas entradas, idade, TTL."""
    from klines_cache import cache_stats
    return cache_stats()



# ── ML Engine endpoints ────────────────────────────────────────────────────────

@app.get("/ml/status")
async def get_ml_status():
    """Estado do ML Engine: modelos carregados, AUC, amostras."""
    return ml_engine.get_ml_status()


@app.post("/ml/retrain")
async def retrain_ml():
    """Força retreino do ML com dados atuais do banco."""
    global _ml_ready
    _ml_ready = False
    await ml_engine.train_all_models()
    _ml_ready = True
    return {"ok": True, "status": ml_engine.get_ml_status()}


# ── DCA Engine endpoints ───────────────────────────────────────────────────────

@app.get("/dca/status")
async def get_dca_status():
    """Estado atual das posições DCA abertas."""
    return dca_engine.get_dca_status()


@app.post("/dca/enable")
async def enable_dca_mode():
    """Ativa modo DCA para novas entradas."""
    dca_engine.enable_dca(True)
    return {"ok": True, "dca_enabled": True}


@app.post("/dca/disable")
async def disable_dca_mode():
    """Desativa modo DCA."""
    dca_engine.enable_dca(False)
    return {"ok": True, "dca_enabled": False}


# ── Monte Carlo endpoint ───────────────────────────────────────────────────────

@app.get("/monte_carlo")
async def run_monte_carlo(n: int = 1000):
    """
    Roda Monte Carlo sobre os trades fechados do banco.
    Parâmetro: n = número de simulações (padrão 1000).
    """
    from database import get_open_trades
    import aiosqlite
    from config import DB_PATH
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT pnl_pct FROM trades WHERE status='CLOSED' AND pnl_pct IS NOT NULL ORDER BY closed_at DESC LIMIT 500"
            ) as cur:
                rows = await cur.fetchall()
        returns = [float(r["pnl_pct"]) for r in rows if r["pnl_pct"] is not None]
        if len(returns) < 5:
            return {"error": "Trades insuficientes para Monte Carlo (mínimo 5)"}
        result = monte_carlo.run(returns, n_simulations=min(n, 2000))
        if not result:
            return {"error": "Falha ao rodar simulação"}
        interp = monte_carlo.interpret(result)
        return {
            "n_simulations":   result.n_simulations,
            "n_trades":        result.n_trades,
            "roi_mean":        result.roi_mean,
            "roi_median":      result.roi_median,
            "roi_p5":          result.roi_p5,
            "roi_p95":         result.roi_p95,
            "max_dd_mean":     result.max_dd_mean,
            "max_dd_p95":      result.max_dd_p95,
            "sharpe_mean":     result.sharpe_mean,
            "sortino_mean":    result.sortino_mean,
            "prob_profit":     result.prob_profit,
            "robustness_score":result.robustness_score,
            "verdict":         interp["verdict"],
            "interpretation":  interp["lines"],
            "roi_array":       result.roi_array,
            "max_dd_array":    result.max_dd_array,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Risk Metrics endpoint ──────────────────────────────────────────────────────

@app.get("/risk/metrics")
async def get_risk_metrics():
    """Sharpe e Sortino ao vivo da sessão atual."""
    metrics = _calc_risk_metrics()
    return {
        **metrics,
        "session_returns_count": len(_session_returns),
        "sortino_threshold":     SORTINO_PAUSE_THRESHOLD,
        "auto_paused":           _sortino_pause,
    }


@app.post("/risk/unpause")
async def unpause_sortino():
    """Reseta a pausa automática por Sortino (reset manual)."""
    global _sortino_pause
    _sortino_pause = False
    return {"ok": True, "message": "Pausa de Sortino resetada manualmente"}



# ── WebSocket Status ───────────────────────────────────────────────────────────

@app.get("/ws/status")
async def ws_status():
    return ws_feed.get_ws_status()


@app.get("/liquidations")
async def get_liquidations_status(symbol: str = None, window_s: int = 60):
    """Liquidações de mercado em tempo real. Com symbol: cascata específica."""
    if symbol:
        return {"symbol": symbol.upper(),
                "cascade": ws_feed.liquidation_cascade(symbol.upper())}
    return ws_feed.recent_market_liquidations(window_s=window_s)


@app.get("/correlation")
async def get_correlation_status():
    """Matriz de correlação dinâmica entre ativos (top pares correlacionados)."""
    import correlation_engine as _corr
    return _corr.get_correlation_status()


@app.get("/macro/guard")
async def get_macro_guard_status():
    """Estado do guard macro: pausa ativa e próximo evento HIGH impact."""
    import market_engine
    st = market_engine.get_market_state()
    return {
        "pause_active": _macro_pause_active(),
        "next_event":   st.get("next_macro_event"),
        "upcoming":     st.get("macro_events", [])[:5],
    }


# ── Regime Detection ───────────────────────────────────────────────────────────

@app.get("/regime")
async def get_regime(asset: str = "BTCUSDT"):
    data = regime_detector.get_cache(asset)
    return {"asset": asset.upper(), **data}


@app.get("/regime/all")
async def get_all_regimes():
    return regime_detector.get_all_regimes()


@app.get("/asset_memory")
async def get_asset_memory():
    """Retorna WR histórico + status de pausa por ativo."""
    return _asset_memory.get_all_stats()


@app.get("/asset_memory/{symbol}")
async def get_asset_memory_symbol(symbol: str):
    return _asset_memory.get_stats(symbol.upper())


# ── Fear & Greed ───────────────────────────────────────────────────────────────

@app.get("/fear_greed")
async def get_fear_greed_endpoint():
    from fear_greed import get_fear_greed
    return await get_fear_greed()


# ── Portfolio Risk ─────────────────────────────────────────────────────────────

@app.get("/portfolio/risk")
async def get_portfolio_risk_endpoint():
    open_trades = await get_open_trades()
    cached = _balance_cache or {}
    banca  = BANCA_USDT if BANCA_USDT > 0 else cached.get("available_balance", 0)
    trades_for_risk = [
        {
            "asset":         t.get("asset", ""),
            "direction":     t.get("direction", ""),
            "notional_usdt": float(t.get("notional_usdt", 10) or 10),
            "atr_pct":       2.0,
        }
        for t in open_trades
    ]
    return portfolio_risk.get_portfolio_summary(trades_for_risk, float(banca))


# ── Walk-Forward Analysis ──────────────────────────────────────────────────────

@app.get("/universe")
async def get_universe():
    """Retorna o universo dinâmico atual (ativos no radar AGGRESSIVE)."""
    stats = universe_builder.get_universe_stats()
    return {
        "total_active":   stats["total_active"],
        "symbols":        _dynamic_universe,
        "top_10":         stats["top_10"],
        "mode":           CURRENT_MODE,
        "using_dynamic":  bool(_dynamic_universe) and CURRENT_MODE == "AGGRESSIVE",
    }


@app.post("/universe/refresh")
async def refresh_universe():
    """Força rebuild imediato do universo dinâmico."""
    asyncio.create_task(_job_universe_builder())
    return {"ok": True, "message": "Universe builder iniciado em background"}


@app.get("/walk_forward")
async def get_walk_forward():
    """Retorna resultado do último walk-forward (atualizado a cada 12h)."""
    if not _walk_forward_result:
        return {"overall_status": "PENDING", "recommendation": "Análise em andamento..."}
    return _walk_forward_result


@app.post("/walk_forward/run")
async def run_walk_forward_now():
    """Força re-análise walk-forward imediata."""
    asyncio.create_task(_run_walk_forward_job())
    return {"ok": True, "message": "Walk-forward iniciado em background"}


# ── VIP Admin ────────────────────────────────────────────────────────────────

@app.post("/vip/link")
async def vip_generate_link(days: int = 30):
    """Gera link de convite único para o grupo VIP (1 uso, validade em dias)."""
    link = await create_vip_invite_link(expire_hours=days * 24, member_limit=1)
    if not link:
        raise HTTPException(status_code=500, detail="TELEGRAM_VIP_ID não configurado ou erro na API Telegram")
    return {"link": link, "expires_days": days}


@app.post("/vip/remove/{user_id}")
async def vip_remove(user_id: int):
    """Remove membro do grupo VIP pelo Telegram user_id numérico."""
    ok = await remove_vip_member(user_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Falha ao remover — verifique o user_id e se o bot é admin")
    return {"removed": True, "user_id": user_id}


@app.get("/vip/status")
async def vip_status():
    """Status do canal público e grupo VIP (membros, sinais do dia)."""
    from config import TELEGRAM_CHANNEL_ID, TELEGRAM_VIP_ID
    return {
        "canal_publico": {
            "id": TELEGRAM_CHANNEL_ID or "não configurado",
            "subscribers": await get_channel_subscriber_count(),
        },
        "grupo_vip": {
            "id": TELEGRAM_VIP_ID or "não configurado",
            "members": await get_vip_member_count(),
        },
    }


# ── Analytics Dashboard (nova versão) ─────────────────────────────────────────

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_dashboard():
    """Dashboard analítico avançado com equity curve, walk-forward, risk metrics."""
    from pathlib import Path as _Path
    html_path = _Path(__file__).parent / "dashboard_template.html"
    if html_path.exists():
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>dashboard_template.html não encontrado</h1>", status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
