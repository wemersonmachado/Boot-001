"""
Notificador Telegram — TRADER 001
Envia sinais com botões APROVAR/REJEITAR, projeções de lucro e ajuste de alavancagem.
"""
import asyncio
import aiohttp
import json
import time
from datetime import datetime
from typing import Callable, Optional

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHANNEL_ID, TELEGRAM_VIP_ID

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

_pending_approvals: dict = {}
_approval_callbacks: dict = {}
_last_update_id = 0
_alerts_paused = False

def set_alerts_paused(paused: bool):
    global _alerts_paused
    _alerts_paused = paused
    print(f"[NOTIFIER] Alertas do Telegram {'pausados' if paused else 'liberados'}")

def is_alerts_paused() -> bool:
    """Getter público do estado de pausa (evita referência ao símbolo privado de fora do módulo)."""
    return _alerts_paused

# ── Session HTTP persistente — reutiliza conexão TCP/TLS ─────────────────────
_tg_session: Optional[aiohttp.ClientSession] = None
_tg_session_lock: Optional[asyncio.Lock] = None


def _get_tg_lock() -> asyncio.Lock:
    """Retorna lock singleton para criação segura da session."""
    global _tg_session_lock
    if _tg_session_lock is None:
        _tg_session_lock = asyncio.Lock()
    return _tg_session_lock


async def _get_tg_session() -> aiohttp.ClientSession:
    """Session persistente para Telegram — cria apenas uma vez, reutiliza sempre."""
    global _tg_session
    async with _get_tg_lock():
        if _tg_session is None or _tg_session.closed:
            connector = aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver(),
                ssl=True,            # SSL habilitado (Telegram usa certificado válido)
                limit=20,            # máx 20 conexões simultâneas ao Telegram
                keepalive_timeout=30,
            )
            _tg_session = aiohttp.ClientSession(
                connector=connector,
                connector_owner=True,
                timeout=aiohttp.ClientTimeout(total=12, connect=5),
            )
        return _tg_session


async def close_tg_session():
    """Fecha a session ao encerrar o bot (chamar no shutdown do FastAPI)."""
    global _tg_session
    if _tg_session and not _tg_session.closed:
        await _tg_session.close()
        _tg_session = None

# ── Roteamento canal público — limites diários ────────────────────────────────
_channel_counter: dict    = {"date": "", "count": 0}   # sinais normais
_channel_pd_counter: dict = {"date": "", "count": 0}   # pump/dump

_CHANNEL_DAILY_LIMIT    = 10   # sinais long/short por dia (máx 10 por dia)
_CHANNEL_PD_DAILY_LIMIT = 4   # pump/dump por dia

_last_channel_send_time = 0.0


def _reset_daily(counter: dict) -> None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if counter["date"] != today:
        counter["date"]  = today
        counter["count"] = 0


def _channel_ok(conf_label: str) -> bool:
    """True se o sinal deve ir para o canal público (Alta + cooldown + abaixo do limite)."""
    global _last_channel_send_time
    if not TELEGRAM_CHANNEL_ID:
        return False
    # Apenas sinais de Alta qualidade
    if conf_label != "Alta":
        return False
    # Cooldown de 10 minutos (600 segundos)
    now = time.time()
    if now - _last_channel_send_time < 600:
        return False
    _reset_daily(_channel_counter)
    if _channel_counter["count"] >= _CHANNEL_DAILY_LIMIT:
        return False
    _channel_counter["count"] += 1
    _last_channel_send_time = now
    return True


def _channel_pd_ok() -> bool:
    """True se o alerta de pump/dump deve ir para o canal público (máx 4/dia)."""
    if not TELEGRAM_CHANNEL_ID:
        return False
    _reset_daily(_channel_pd_counter)
    if _channel_pd_counter["count"] >= _CHANNEL_PD_DAILY_LIMIT:
        return False
    _channel_pd_counter["count"] += 1
    return True


def get_pending_assets() -> set:
    """Returns set of asset symbols currently awaiting Telegram approval."""
    return {v.get("asset") for v in _pending_approvals.values() if v.get("asset")}


def _is_configured() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


async def _post(endpoint: str, data: dict) -> dict:
    """POST para Telegram usando session persistente (reutiliza conexão TCP)."""
    if not _is_configured():
        return {}
    try:
        session = await _get_tg_session()
        async with session.post(
            f"{TELEGRAM_API}/{endpoint}",
            json=data,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return await r.json()
    except aiohttp.ClientConnectorError:
        # Reconecta e tenta uma vez mais se a conexão foi perdida
        global _tg_session
        _tg_session = None
        try:
            session = await _get_tg_session()
            async with session.post(
                f"{TELEGRAM_API}/{endpoint}", json=data,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                return await r.json()
        except Exception as e:
            print(f"[TELEGRAM] Erro após reconect: {e}")
            return {}
    except Exception as e:
        print(f"[TELEGRAM] Erro: {e}")
        return {}



def _clean_direction(direction) -> str:
    """Converte Direction.SHORT / direction.SHORT / SHORT → SHORT."""
    return str(direction).split(".")[-1].strip().upper()


def _binance_link(symbol: str) -> str:
    """Retorna URL do chart Binance Futures para o símbolo."""
    clean = symbol.replace("USDT", "").upper()
    return f"https://www.binance.com/futures/{clean}USDT"


def _calc_projections(entry: float, direction: str, leverage: int) -> str:
    """Calcula projeções de lucro para 1%, 2%, 3%, 4%, 5% de variação no preço."""
    lines = []
    pcts = [1, 2, 3, 4, 5]
    for p in pcts:
        if "LONG" in direction:
            tp = entry * (1 + p / 100)
        else:
            tp = entry * (1 - p / 100)
        lucro_pct = p * leverage
        lines.append(f"  +{p}% movimento → *+{lucro_pct}% lucro* (`${tp:,.4f}`)")
    return "\n".join(lines)


def _build_signal_keyboard(sig_id: str, leverage: int) -> dict:
    """Teclado inline com APROVAR/REJEITAR + botões de alavancagem."""
    lev_row = [
        {"text": f"{'✅' if leverage == lv else ''}{lv}x", "callback_data": f"lev_{sig_id}_{lv}"}
        for lv in [5, 10, 15, 20]
    ]
    return {
        "inline_keyboard": [
            [
                {"text": "✅ APROVAR — OPERAR", "callback_data": f"approve_{sig_id}"},
                {"text": "❌ REJEITAR",         "callback_data": f"reject_{sig_id}"},
            ],
            lev_row,
        ]
    }


def _build_trade_keyboard(trade_id: str) -> dict:
    """Teclado inline para trade aberto — botão Fechar."""
    return {
        "inline_keyboard": [[
            {"text": "🔴 Fechar Operação Agora", "callback_data": f"close_{trade_id}"},
        ]]
    }


async def send_signal_alert(signal: dict, on_approve: Callable, on_reject: Callable) -> bool:
    if not _is_configured():
        await on_approve(signal)
        return True

    sig_id = f"{signal['asset']}_{int(time.time())}"
    _pending_approvals[sig_id] = signal
    _approval_callbacks[sig_id] = {"approve": on_approve, "reject": on_reject}

    asset     = signal['asset']
    direcao   = signal.get("direction", "")
    dir_clean = _clean_direction(direcao)
    dir_emoji = "🟢 LONG" if "LONG" in dir_clean else "🔴 SHORT"
    tf        = signal.get("timeframe", "?")
    entry     = float(signal.get("entry", 0))
    sl        = float(signal.get("stop_loss", 0))
    tp1       = float(signal.get("tp1", 0))
    tp2       = float(signal.get("tp2", 0))
    rr        = signal.get("rr", 0)
    score     = signal.get("confidence", signal.get("score_total", 0))
    reason    = signal.get("reason", "")
    trade_type = signal.get("trade_type", "DAY_TRADE")
    anomaly   = signal.get("anomaly", "")
    leverage  = signal.get("leverage", 10)

    tipo_map   = {"SCALP": "⚡ SCALP", "DAY_TRADE": "📅 DAY TRADE", "SWING": "🌊 SWING"}
    tipo_label = tipo_map.get(trade_type, trade_type)
    conf_emoji = "🔥" if score >= 90 else "✅" if score >= 80 else "⚠️"

    sl_pct  = abs(entry - sl)  / entry * 100
    tp1_pct = abs(tp1 - entry) / entry * 100
    tp2_pct = abs(tp2 - entry) / entry * 100

    anomaly_line = f"\n🚨 *Anomalia:* _{anomaly}_" if anomaly else ""
    proj = _calc_projections(entry, dir_clean, leverage)

    # Padrões detectados pelo CandlePatternEngine
    _sup_pats  = signal.get("patterns_detected", [])
    _sup_mtf   = signal.get("patterns_mtf", {})
    _SIG_IC    = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
    _SUP_PAT_LINES = []
    for p in sorted(_sup_pats, key=lambda x: -x.get("strength", 1))[:3]:
        _SUP_PAT_LINES.append(
            f"{_SIG_IC.get(p.get('signal','neutral'),'⚪')} [{tf.upper()}] {p['name_pt']} {'★'*p.get('strength',1)}"
        )
    for _stf in ["1h", "4h", "1d"]:
        if _stf.upper() == str(tf).upper():
            continue
        for p in sorted(_sup_mtf.get(_stf, []), key=lambda x: -x.get("strength", 1))[:1]:
            if p.get("strength", 1) >= 2:
                _SUP_PAT_LINES.append(
                    f"{_SIG_IC.get(p.get('signal','neutral'),'⚪')} [{_stf.upper()}] {p['name_pt']} {'★'*p.get('strength',1)}"
                )
    patterns_sup_line = ("\n🕯️ *Padrões:* " + " | ".join(_SUP_PAT_LINES)) if _SUP_PAT_LINES else ""

    msg = f"""{conf_emoji} *TRADER 001 — NOVO SINAL*

*{asset}* | {dir_emoji} | `{tf}` | {tipo_label}

💰 *Entrada:* `${entry:,.4f}`
🛑 *Stop Loss:* `${sl:,.4f}` _(-{sl_pct:.2f}%)_
🎯 *TP1:* `${tp1:,.4f}` _(+{tp1_pct:.2f}%)_
🎯 *TP2:* `${tp2:,.4f}` _(+{tp2_pct:.2f}%)_

📊 *R:R:* `{rr:.1f}:1` | *Score:* `{score:.0f}/100`
⚡ *Alavancagem atual:* `{leverage}x`
📝 _{reason}_{anomaly_line}{patterns_sup_line}

📈 *Projeção de Lucro ({leverage}x):*
{proj}

⏰ `{datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC`""".strip()

    # Gera gráfico com dados reais da Binance
    _chart_signal = {
        "entry":       entry,
        "stop_loss":   sl,
        "tp1":         tp1,
        "tp2":         tp2,
        "direction":   dir_clean,
        "confidence":  float(score),
        "rr":          float(rr),
        "conf_label":  "Alta" if float(score) >= 80 else "Média" if float(score) >= 65 else "Baixa",
    }
    chart_bytes = await _generate_signal_chart(asset, str(tf).lower(), _chart_signal)

    keyboard = _build_signal_keyboard(sig_id, leverage)
    result = await _send_photo_with_caption_or_split(str(TELEGRAM_CHAT_ID), msg, keyboard, chart_bytes)
    ok = bool(result.get("ok"))
    if ok:
        msg_id = result.get("result", {}).get("message_id")
        if msg_id and sig_id in _pending_approvals:
            _pending_approvals[sig_id]["message_id"] = msg_id
    return ok


async def send_trade_opened(trade: dict, operation_mode: str = "SUPERVISED"):
    if not _is_configured():
        return

    import re as _re

    asset      = trade.get("asset", "?")
    dir_clean  = _clean_direction(trade.get("direction", ""))
    is_long    = "LONG" in dir_clean
    dir_icon   = "🟢" if is_long else "🔴"
    entry      = float(trade.get("entry_price", 0))
    sl         = float(trade.get("stop_loss", 0))
    tp1        = float(trade.get("tp1", 0))
    tp2        = float(trade.get("tp2", 0))
    lev        = int(trade.get("leverage", 1))
    size       = float(trade.get("size_usdt", 0))
    score      = float(trade.get("confidence", 0))
    rr         = float(trade.get("rr", 0))
    reason_raw = trade.get("reason", "")
    tf         = trade.get("timeframe", "15m")
    trade_id   = trade.get("id", "")
    paper      = trade.get("paper", False)

    sl_pct  = abs(entry - sl)  / entry * 100 if entry else 0
    tp1_pct = abs(tp1 - entry) / entry * 100 if entry else 0
    tp2_pct = abs(tp2 - entry) / entry * 100 if entry else 0
    margin  = round(size / lev, 2) if lev else 0

    mode_map   = {"AUTONOMOUS": "Autônomo", "SUPERVISED": "Supervisão", "GRID": "Grid"}
    mode_label = mode_map.get(operation_mode.upper(), operation_mode)
    paper_tag  = "  🔸 PAPER" if paper else ""

    # Score bar
    filled    = min(10, int(score / 10))
    score_bar = "█" * filled + "░" * (10 - filled)

    # V6 entry reason
    v6_m = _re.search(r'\[V6:([^\]]+)\]', reason_raw)
    if v6_m:
        raw = v6_m.group(1)
        bonus_m = _re.search(r'\+(\d+[\d.]*)pt', raw)
        v6_bonus_str = f" `+{bonus_m.group(1)}pt`" if bonus_m else ""
        tags = _re.sub(r'\s*\+\d+[\d.]*pt', '', raw).strip()
        if "OB" in tags:
            v6_why = f"Reteste de Order Block (OB){v6_bonus_str}"
        elif "FVG" in tags:
            v6_why = f"Tap em Fair Value Gap (FVG){v6_bonus_str}"
        elif "BOS" in tags:
            v6_why = f"Breakout de Estrutura (BOS){v6_bonus_str}"
        elif "sweep" in tags:
            v6_why = f"Sweep de liquidez → reversão{v6_bonus_str}"
        else:
            v6_why = f"Estrutura V6 confirmada{v6_bonus_str}"
    else:
        v6_why = "Sinal técnico V4"

    # V4 summary (remove V6 tag, limit length)
    v4_txt = _re.sub(r'\[V6:[^\]]+\]\s*', '', reason_raw).strip()
    if len(v4_txt) > 65:
        v4_txt = v4_txt[:65] + "…"

    # Price levels visual
    ar = "▲" if is_long else "▼"
    if is_long:
        levels = (
            f"  🎯 TP2 `${tp2:,.2f}` _+{tp2_pct:.2f}%_ {ar}\n"
            f"  🎯 TP1 `${tp1:,.2f}` _+{tp1_pct:.2f}%_ {ar}\n"
            f"  ▶  ENT `${entry:,.4f}` ← entrada\n"
            f"  🛑 SL  `${sl:,.2f}` _-{sl_pct:.2f}%_ ▼"
        )
    else:
        levels = (
            f"  🛑 SL  `${sl:,.2f}` _+{sl_pct:.2f}%_ ▲\n"
            f"  ▶  ENT `${entry:,.4f}` ← entrada\n"
            f"  🎯 TP1 `${tp1:,.2f}` _-{tp1_pct:.2f}%_ {ar}\n"
            f"  🎯 TP2 `${tp2:,.2f}` _-{tp2_pct:.2f}%_ {ar}"
        )

    # V6 Grid zone line (only for GRID mode)
    gz = trade.get("v6_grid_zones", {})
    zone_line = ""
    if gz and gz.get("found") and operation_mode.upper() == "GRID":
        lo, hi  = gz.get("lower", 0), gz.get("upper", 0)
        lt, ut  = gz.get("lower_type", "?"), gz.get("upper_type", "?")
        dl, dh  = gz.get("dist_lo_pct", 0), gz.get("dist_hi_pct", 0)
        zone_line = (
            f"\n📐 *Zona V6 do Grid*\n"
            f"  Suporte `${lo:,.2f}` [{lt}] -{dl:.2f}%\n"
            f"  Resist  `${hi:,.2f}` [{ut}] +{dh:.2f}%"
        )

    brt = datetime.utcnow().strftime('%d/%m %H:%M')

    msg = (
        f"{dir_icon} *{dir_clean} — {asset}*{paper_tag}\n"
        f"`{mode_label}` | `{tf}` | `{brt} UTC`\n\n"
        f"📍 *Motivo de entrada*\n"
        f"  {v6_why}\n"
        f"  Score `{score_bar}` {score:.0f}/100\n"
        f"  _{v4_txt}_\n\n"
        f"📊 *Níveis de preço*\n"
        f"{levels}"
        f"{zone_line}\n\n"
        f"⚡ *Execução*\n"
        f"  `{lev}x` alavancagem | R:R `{rr:.1f}:1`\n"
        f"  Margem `${margin:.2f}` | Nocional `${size:.2f}`"
    )

    # Gera e envia gráfico com dados reais
    _chart_sig = {
        "entry":      entry,
        "stop_loss":  sl,
        "tp1":        tp1,
        "tp2":        tp2,
        "direction":  dir_clean,
        "confidence": score,
        "rr":         rr,
        "conf_label": "Alta" if score >= 80 else "Média" if score >= 65 else "Baixa",
    }
    _chart_bytes = await _generate_signal_chart(asset, tf, _chart_sig)
    await _send_photo_with_caption_or_split(
        str(TELEGRAM_CHAT_ID), msg, _build_trade_keyboard(trade_id), _chart_bytes
    )


async def send_trade_closed(trade: dict, reason: str):
    if not _is_configured():
        return
    asset   = trade.get("asset", "?")
    pnl     = trade.get("pnl_usdt", 0)
    pnl_pct = trade.get("pnl_pct", 0)
    emoji   = "+" if float(pnl) >= 0 else "-"
    sign_   = "+" if float(pnl) >= 0 else ""
    msg = f"""*TRADE FECHADO* — *{asset}* — _{reason}_

PnL: `{sign_}${float(pnl):.2f} USDT` ({sign_}{float(pnl_pct):.2f}%)
`{datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC`""".strip()
    await _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


async def send_trailing_update(asset: str, new_stop: float, profit_pct: float):
    if not _is_configured():
        return
    msg = f"*TRAILING STOP* — *{asset}*\nNovo SL: `${new_stop:,.4f}` | Lucro travado: `+{profit_pct:.1f}%`"
    await _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


async def send_daily_summary(stats: dict):
    if not _is_configured():
        return
    pnl  = stats.get("total_pnl", 0)
    sign_ = "+" if pnl >= 0 else ""
    msg = f"""📊 *RESUMO DIÁRIO — TRADER 001*

Trades: `{stats.get('total', 0)}` | Acertos: `{stats.get('wins', 0)}` | Erros: `{stats.get('losses', 0)}`
Taxa de Acerto: `{stats.get('win_rate', 0):.1f}%`
PnL do Dia: `{sign_}${pnl:.2f} USDT`
Fator de Lucro: `{stats.get('profit_factor', 0):.2f}`""".strip()
    # Resumo diário → apenas ao bot/admin (não enviar aos canais VIP)
    daily_targets = [str(TELEGRAM_CHAT_ID)]
    await asyncio.gather(
        *[_post("sendMessage", {"chat_id": t, "text": msg, "parse_mode": "Markdown"})
          for t in daily_targets],
        return_exceptions=True,
    )


async def send_alert(msg: str):
    if not _is_configured():
        print(f"[ALERTA] {msg}")
        return
    await _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ {msg}"})


async def send_macro_decision(msg: str):
    """
    Alerta de evento macro com botões Pausar/Continuar.
    O bot CONTINUA operando até o usuário clicar em Pausar.
    """
    if not _is_configured():
        print(f"[MACRO] {msg}")
        return
    keyboard = {"inline_keyboard": [[
        {"text": "⏸️ Pausar trades", "callback_data": "macro_pause"},
        {"text": "▶️ Continuar operando", "callback_data": "macro_continue"},
    ]]}
    await _post("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID, "text": f"📅 {msg}",
        "parse_mode": "Markdown", "reply_markup": keyboard,
    })


# ── Command Handler — controle do bot via texto no Telegram ──────────────────

_command_handler = None   # injetado pelo main.py no startup
_close_handler   = None   # injetado pelo main.py — fecha trade pelo ID


def set_command_handler(handler):
    global _command_handler
    _command_handler = handler


def set_close_handler(handler):
    """main.py registra função async que fecha um trade pelo ID."""
    global _close_handler
    _close_handler = handler


def _build_control_keyboard() -> dict:
    """Painel de controle: escolher modo e perfil com um toque.

    Modos seguros (Sinais/Supervisionado) ativam direto.
    Modos de dinheiro REAL (Autônomo/Grid) pedem confirmação em 2 toques.
    """
    return {"inline_keyboard": [
        [{"text": "── MODO ──", "callback_data": "noop"}],
        [{"text": "📡 Sinais",         "callback_data": "setmode_sinais"},
         {"text": "👤 Supervisionado", "callback_data": "setmode_off"}],
        [{"text": "🤖 Autônomo 💰",    "callback_data": "setmode_on"},
         {"text": "⚡ Grid 💰",         "callback_data": "setmode_grid"}],
        [{"text": "── PERFIL ──", "callback_data": "noop"}],
        [{"text": "🟢 Conservador", "callback_data": "setprofile_conservador"},
         {"text": "🟡 Normal",      "callback_data": "setprofile_normal"},
         {"text": "🔴 Agressivo",   "callback_data": "setprofile_agressivo"}],
        [{"text": "📊 Status", "callback_data": "panel_status"},
         {"text": "🔄 Atualizar", "callback_data": "panel_refresh"}],
    ]}


async def send_control_panel(chat_id: str = None):
    """Envia o painel de controle (texto de status + botões)."""
    chat_id = chat_id or TELEGRAM_CHAT_ID
    header = "*🎛️ TRADER 001 — Painel de Controle*\n\nToque para escolher *modo* e *perfil*."
    if _command_handler:
        try:
            header = await _command_handler("/menu")
        except Exception:
            pass
    await _post("sendMessage", {
        "chat_id": chat_id, "text": header, "parse_mode": "Markdown",
        "reply_markup": _build_control_keyboard(),
    })


async def _handle_text_command(text: str, chat_id: str):
    if _command_handler is None:
        await _post("sendMessage", {"chat_id": chat_id,
            "text": "⚠️ Bot ainda inicializando, tente em alguns segundos."})
        return
    # /menu, /painel e /start abrem o painel de botões (modo + perfil)
    first = text.strip().lower().split()[0] if text.strip() else ""
    if first in ("/menu", "/painel", "/start"):
        await send_control_panel(chat_id)
        return
    try:
        response = await _command_handler(text.strip())
        if response:
            await _post("sendMessage", {
                "chat_id": chat_id, "text": response, "parse_mode": "Markdown"
            })
    except Exception as e:
        await _post("sendMessage", {"chat_id": chat_id, "text": f"❌ Erro: {e}"})


# ── Polling de callbacks (approve/reject/leverage/close) + comandos de texto ──

_poll_session: aiohttp.ClientSession = None


async def _get_poll_session() -> aiohttp.ClientSession:
    global _poll_session
    if _poll_session is None or _poll_session.closed:
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ssl=False)
        _poll_session = aiohttp.ClientSession(connector=connector)
    return _poll_session


async def prune_expired_approvals(timeout_seconds: int = 300):
    """Remove pending approvals that have timed out and notify Telegram."""
    now = time.time()
    expired_ids = []
    for sig_id, signal in list(_pending_approvals.items()):
        parts = sig_id.split("_")
        try:
            timestamp = int(parts[-1])
        except ValueError:
            timestamp = int(now)
        if now - timestamp > timeout_seconds:
            expired_ids.append(sig_id)

    for sig_id in expired_ids:
        signal = _pending_approvals.pop(sig_id, None)
        cbs = _approval_callbacks.pop(sig_id, {})
        if signal:
            print(f"[TELEGRAM] Expirado por timeout: {signal.get('asset')}")
            if "reject" in cbs:
                try:
                    asyncio.create_task(cbs["reject"](signal))
                except Exception as e:
                    print(f"[TELEGRAM] Erro ao chamar reject no timeout de {sig_id}: {e}")
            msg_id = signal.get("message_id")
            if msg_id:
                try:
                    await _post("editMessageReplyMarkup", {
                        "chat_id": TELEGRAM_CHAT_ID,
                        "message_id": msg_id,
                        "reply_markup": {
                            "inline_keyboard": [[
                                {"text": "⚠️ Expirado (Sem Resposta)", "callback_data": "done"}
                            ]]
                        }
                    })
                except Exception as e:
                    print(f"[TELEGRAM] Erro ao editar mensagem expirada: {e}")


async def poll_telegram_responses():
    global _last_update_id
    if not _is_configured():
        return
    await prune_expired_approvals()
    try:
        sess = await _get_poll_session()
        async with sess.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"timeout": 1, "offset": _last_update_id + 1},
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            result = await r.json()

        for update in result.get("result", []):
            _last_update_id = update["update_id"]

            # ── Mensagem de texto / comando ───────────────────────────────────
            msg = update.get("message") or update.get("edited_message")
            if msg:
                text    = msg.get("text", "").strip()
                chat_id = str(msg["chat"]["id"])
                chat_title = msg["chat"].get("title", "")
                # /chatid funciona de qualquer chat — responde com o ID do grupo
                if text.lower().startswith("/chatid"):
                    asyncio.create_task(_post("sendMessage", {
                        "chat_id": chat_id,
                        "text": (
                            f"📋 *Chat ID deste grupo:*\n"
                            f"`{chat_id}`\n\n"
                            f"Título: {chat_title}\n\n"
                            f"Cole esse valor em `TELEGRAM_VIP_ID` no `.env`"
                        ),
                        "parse_mode": "Markdown",
                    }))
                    continue
                if chat_id == str(TELEGRAM_CHAT_ID) and text.startswith("/"):
                    asyncio.create_task(_handle_text_command(text, chat_id))
                continue

            # ── Callback de botão ─────────────────────────────────────────────
            cb = update.get("callback_query")
            if not cb:
                continue

            data_str = cb.get("data", "")
            cb_id    = cb["id"]
            msg_id   = cb["message"]["message_id"]
            chat_id  = cb["message"]["chat"]["id"]

            await _post("answerCallbackQuery", {"callback_query_id": cb_id})

            # ── APROVAR ───────────────────────────────────────────────────────
            if data_str.startswith("approve_"):
                sig_id = data_str[8:]
                if sig_id in _pending_approvals:
                    signal = _pending_approvals.pop(sig_id)
                    cbs    = _approval_callbacks.pop(sig_id, {})
                    print(f"[TELEGRAM] Aprovado: {signal.get('asset')}")
                    if "approve" in cbs:
                        asyncio.create_task(cbs["approve"](signal))
                    await _post("editMessageReplyMarkup", {
                        "chat_id": chat_id, "message_id": msg_id,
                        "reply_markup": {"inline_keyboard": [[
                            {"text": "Aprovado — Operando", "callback_data": "done"}
                        ]]}
                    })

            # ── REJEITAR ──────────────────────────────────────────────────────
            elif data_str.startswith("reject_"):
                sig_id = data_str[7:]
                if sig_id in _pending_approvals:
                    signal = _pending_approvals.pop(sig_id)
                    cbs    = _approval_callbacks.pop(sig_id, {})
                    print(f"[TELEGRAM] Rejeitado: {signal.get('asset')}")
                    if "reject" in cbs:
                        asyncio.create_task(cbs["reject"](signal))
                    await _post("editMessageReplyMarkup", {
                        "chat_id": chat_id, "message_id": msg_id,
                        "reply_markup": {"inline_keyboard": [[
                            {"text": "Rejeitado", "callback_data": "done"}
                        ]]}
                    })

            # ── MACRO: Pausar / Continuar ─────────────────────────────────────
            elif data_str in ("macro_pause", "macro_continue"):
                escolha = "pausar" if data_str == "macro_pause" else "continuar"
                label   = "⏸️ Trades PAUSADOS" if escolha == "pausar" else "▶️ Operando normalmente"
                await _post("editMessageReplyMarkup", {
                    "chat_id": chat_id, "message_id": msg_id,
                    "reply_markup": {"inline_keyboard": [[
                        {"text": label, "callback_data": "done"}
                    ]]}
                })
                if _command_handler:
                    resp = await _command_handler(f"/macro {escolha}")
                    if resp:
                        await _post("sendMessage", {"chat_id": chat_id, "text": resp,
                                                    "parse_mode": "Markdown"})

            # ── PAINEL: ignora rótulos (cabeçalhos não-clicáveis) ─────────────
            elif data_str == "noop":
                pass

            # ── PAINEL: escolher PERFIL (seguro, ativa direto) ────────────────
            elif data_str.startswith("setprofile_"):
                perfil = data_str[len("setprofile_"):]   # conservador/normal/agressivo
                if _command_handler:
                    resp = await _command_handler(f"/modo {perfil}")
                    if resp:
                        await _post("sendMessage", {"chat_id": chat_id, "text": resp,
                                                    "parse_mode": "Markdown"})

            # ── PAINEL: escolher MODO ─────────────────────────────────────────
            elif data_str.startswith("setmode_"):
                val = data_str[len("setmode_"):]   # sinais/off/on/grid
                # Sinais e Supervisionado são seguros → ativam direto
                if val in ("sinais", "off"):
                    if _command_handler:
                        resp = await _command_handler(f"/auto {val}")
                        if resp:
                            await _post("sendMessage", {"chat_id": chat_id, "text": resp,
                                                        "parse_mode": "Markdown"})
                # Autônomo e Grid mexem com dinheiro REAL → pedem confirmação
                else:
                    nome = "🤖 AUTÔNOMO" if val == "on" else "⚡ GRID"
                    await _post("sendMessage", {
                        "chat_id": chat_id,
                        "text": (f"⚠️ *{nome}* opera com *dinheiro REAL*.\n"
                                 f"Confirma a ativação?"),
                        "parse_mode": "Markdown",
                        "reply_markup": {"inline_keyboard": [[
                            {"text": f"✅ Confirmar {nome}", "callback_data": f"confmode_{val}"},
                            {"text": "❌ Cancelar",          "callback_data": "panel_refresh"},
                        ]]},
                    })

            # ── PAINEL: confirmação de modo REAL (2º toque) ───────────────────
            elif data_str.startswith("confmode_"):
                val = data_str[len("confmode_"):]   # on/grid
                await _post("editMessageReplyMarkup", {
                    "chat_id": chat_id, "message_id": msg_id,
                    "reply_markup": {"inline_keyboard": [[
                        {"text": "Ativando...", "callback_data": "done"}
                    ]]},
                })
                if _command_handler:
                    resp = await _command_handler(f"/auto {val} confirmar")
                    if resp:
                        await _post("sendMessage", {"chat_id": chat_id, "text": resp,
                                                    "parse_mode": "Markdown"})

            # ── PAINEL: botão Status ──────────────────────────────────────────
            elif data_str == "panel_status":
                if _command_handler:
                    resp = await _command_handler("/status")
                    if resp:
                        await _post("sendMessage", {"chat_id": chat_id, "text": resp,
                                                    "parse_mode": "Markdown"})

            # ── PAINEL: botão Atualizar (reabre o painel) ─────────────────────
            elif data_str == "panel_refresh":
                await send_control_panel(str(chat_id))

            # ── FECHAR TRADE (botão na mensagem de trade aberto) ──────────────
            elif data_str.startswith("close_"):
                trade_id = data_str[6:]
                print(f"[TELEGRAM] Fechar solicitado: {trade_id}")
                # Atualiza botão imediatamente para feedback visual
                await _post("editMessageReplyMarkup", {
                    "chat_id": chat_id, "message_id": msg_id,
                    "reply_markup": {"inline_keyboard": [[
                        {"text": "Fechando...", "callback_data": "done"}
                    ]]}
                })
                if _close_handler:
                    asyncio.create_task(_close_handler(trade_id, msg_id, chat_id))

            # ── MUDAR ALAVANCAGEM ─────────────────────────────────────────────
            elif data_str.startswith("lev_"):
                parts = data_str.split("_")
                if len(parts) >= 3:
                    new_lev = int(parts[-1])
                    sig_id  = "_".join(parts[1:-1])
                    if sig_id in _pending_approvals:
                        _pending_approvals[sig_id]["leverage"] = new_lev
                        await _post("editMessageReplyMarkup", {
                            "chat_id": chat_id, "message_id": msg_id,
                            "reply_markup": _build_signal_keyboard(sig_id, new_lev),
                        })
                        await _post("answerCallbackQuery", {
                            "callback_query_id": cb_id,
                            "text": f"Alavancagem → {new_lev}x", "show_alert": False,
                        })
                        print(f"[TELEGRAM] Alavancagem {_pending_approvals[sig_id].get('asset')} → {new_lev}x")

    except Exception:
        pass


async def _post_photo(photo_bytes: bytes, caption: str, reply_markup: dict = None,
                      chat_id: str = None, parse_mode: str = "Markdown") -> dict:
    """Envia foto ao Telegram via multipart form."""
    if not _is_configured():
        return {}
    target = chat_id or str(TELEGRAM_CHAT_ID)
    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", target)
        data.add_field("photo", photo_bytes, filename="chart.png", content_type="image/png")
        if caption:
            data.add_field("caption", caption)
            data.add_field("parse_mode", parse_mode)
        if reply_markup:
            data.add_field("reply_markup", json.dumps(reply_markup))
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ssl=False)
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.post(
                f"{TELEGRAM_API}/sendPhoto", data=data,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                res_json = await r.json()
                if not res_json.get("ok"):
                    print(f"[TELEGRAM] sendPhoto respondeu erro para {target}: {res_json}")
                return res_json
    except Exception as e:
        print(f"[TELEGRAM] sendPhoto erro de conexão/processamento ({target}): {e}")
        return {}


async def _send_photo_with_caption_or_split(chat_id: str, msg: str, keyboard: dict | None,
                                           photo_bytes: bytes | None) -> dict:
    """
    Envia foto com a legenda (caption) ou dividida se for muito longa.
    Retorna o dict de resposta do Telegram do post que contém os botões.
    """
    if not photo_bytes:
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "Markdown",
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        return await _post("sendMessage", payload)

    # Se tem foto, tentamos enviar como caption
    if len(msg) <= 1024:
        return await _post_photo(photo_bytes, msg, reply_markup=keyboard, chat_id=chat_id)
    else:
        # Divide a mensagem. Procuramos o segundo separador '━━━━━━━━━━━━━━━━━━'
        sep = "━━━━━━━━━━━━━━━━━━"
        parts = msg.split(sep)
        if len(parts) >= 3:
            part1 = sep.join(parts[:2]) + sep
            part2 = sep.join(parts[2:])
        else:
            split_idx = msg.rfind("\n", 0, 1000)
            if split_idx == -1:
                split_idx = 1000
            part1 = msg[:split_idx]
            part2 = msg[split_idx:]

        res_photo = await _post_photo(photo_bytes, part1, reply_markup=keyboard, chat_id=chat_id)
        await _post("sendMessage", {
            "chat_id": chat_id,
            "text": part2,
            "parse_mode": "Markdown",
        })
        return res_photo


async def _send_signal_message(chat_id: str, msg: str, keyboard: dict,
                                chart_bytes: bytes | None) -> bool:
    """Envia sinal: foto com legenda (caption) ou dividida se for muito longa."""
    res = await _send_photo_with_caption_or_split(chat_id, msg, keyboard, chart_bytes)
    return bool(res.get("ok"))


def _ema_series(closes: list, period: int) -> list:
    """EMA simples sobre lista de floats."""
    k = 2 / (period + 1)
    prev = sum(closes[:period]) / period if len(closes) >= period else closes[0]
    out = []
    for v in closes:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def _rsi_series(closes: list, period: int = 14) -> list:
    import pandas as pd
    if len(closes) <= period:
        return [50.0] * len(closes)
    s = pd.Series(closes)
    delta = s.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    gain = gain.values.copy()
    loss = loss.values.copy()
    for i in range(period, len(closes)):
        gain[i] = (gain[i - 1] * (period - 1) + (closes[i] - closes[i - 1] if closes[i] > closes[i - 1] else 0)) / period
        loss[i] = (loss[i - 1] * (period - 1) + (closes[i - 1] - closes[i] if closes[i - 1] > closes[i] else 0)) / period
    
    rsi = []
    for i in range(len(closes)):
        if i < period:
            rsi.append(50.0)
        else:
            if loss[i] == 0:
                rsi.append(100.0)
            else:
                rs = gain[i] / loss[i]
                rsi.append(100.0 - (100.0 / (1.0 + rs)))
    return rsi


def _chart_swings(vals, kind, w=3):
    """Detecta swing highs/lows (extremos locais) para traçar tendência."""
    out = []
    for i in range(w, len(vals) - w):
        seg = vals[i - w:i + w + 1]
        if kind == "high" and vals[i] == max(seg):
            out.append(i)
        elif kind == "low" and vals[i] == min(seg):
            out.append(i)
    return out


def _chart_linreg(xs, ys):
    """Regressão linear (mínimos quadrados) — tendência robusta, não 2 pontos."""
    k = len(xs)
    if k < 2:
        return 0.0, (ys[0] if ys else 0.0)
    sx = sum(xs); sy = sum(ys); sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    d = k * sxx - sx * sx
    if d == 0:
        return 0.0, sy / k
    m = (k * sxy - sx * sy) / d
    return m, (sy - m * sx) / k


def _chart_fit_boundary(vals, pivots, kind, start, tolerance, max_slope):
    """Escolhe a linha com mais toques e menos rompimentos na estrutura recente."""
    points = [i for i in pivots if i >= start][-10:]
    if len(points) < 2:
        return None

    # O pivô final ('b') precisa estar nos 40% mais recentes da janela — senão
    # a linha "morre" no meio do gráfico e parece flutuando, desconectada do
    # candle atual.
    _recent_floor = start + (len(vals) - start) * 0.6

    best = None
    for a_pos in range(len(points) - 1):
        for b_pos in range(a_pos + 1, len(points)):
            a, b = points[a_pos], points[b_pos]
            if b < _recent_floor:
                continue
            span = b - a
            if span < 5:
                continue
            slope = (vals[b] - vals[a]) / span
            if abs(slope) > max_slope:
                continue
            intercept = vals[a] - slope * a
            pivot_dist = [abs(vals[i] - (slope * i + intercept)) for i in points]
            touches = sum(d <= tolerance for d in pivot_dist)

            violations = 0
            severe = 0
            for i in range(a, len(vals)):
                delta = vals[i] - (slope * i + intercept)
                crossed = delta < -tolerance if kind == "low" else delta > tolerance
                badly_crossed = delta < -2.5 * tolerance if kind == "low" else delta > 2.5 * tolerance
                violations += int(crossed)
                severe += int(badly_crossed)

            # Prioriza ajuste real à estrutura recente: recência do pivô 'b'
            # pesa mais que cobertura (uma linha que nasce longe no passado e
            # some no meio do caminho parece "flutuando" desconectada) e
            # rompimentos custam mais caro — a linha deve realmente respeitar
            # os candles pelos quais passa, não só tocar os 2 pivôs extremos.
            recency_b = b / max(len(vals) - 1, 1)
            coverage = span / max(len(vals) - start, 1)
            mean_error = sum(min(d / tolerance, 3.0) for d in pivot_dist) / len(pivot_dist)
            score = (touches * 4.0 + coverage * 1.2 + recency_b * 4.5
                     - violations * 3.5 - severe * 7.0 - mean_error * 1.5)
            candidate = (score, touches, span, slope, intercept, [a, b])
            if best is None or candidate[:3] > best[:3]:
                best = candidate

    if best is None or best[1] < 2:
        return None
    return best[3], best[4], best[5], best[0]


def _chart_horizontal_level(vals, pivots, start, tolerance, cur_price, side):
    """Agrupa pivôs (highs ou lows) em zonas de preço próximas e devolve a
    zona mais forte (mais toques) do lado certo do preço atual — isso é
    suporte/resistência HORIZONTAL 'de verdade' (nível testado várias vezes),
    diferente da linha diagonal de tendência.
    side='res' só aceita zonas ACIMA do preço atual; side='sup' só ABAIXO.
    """
    pts = [vals[i] for i in pivots if i >= start]
    if len(pts) < 2:
        return None
    clusters = []
    for p in sorted(pts):
        for c in clusters:
            if abs(p - c["avg"]) <= tolerance:
                c["prices"].append(p)
                c["avg"] = sum(c["prices"]) / len(c["prices"])
                break
        else:
            clusters.append({"prices": [p], "avg": p})
    strong = [c for c in clusters if len(c["prices"]) >= 2]
    if side == "res":
        strong = [c for c in strong if c["avg"] > cur_price]
    else:
        strong = [c for c in strong if c["avg"] < cur_price]
    if not strong:
        return None
    best = max(strong, key=lambda c: (len(c["prices"]), -abs(c["avg"] - cur_price)))
    return best["avg"], len(best["prices"])


def _chart_classify_figure(ms, mr, price):
    """Classifica a figura pela inclinação por vela (normalizada em % do preço)."""
    if price <= 0:
        return "Consolidação / Range"
    sm = ms / price; sr = mr / price          # inclinação por vela, fração do preço
    flat = 0.0004                              # < ~0.04%/vela = praticamente lateral
    up_s, dn_s = sm > flat, sm < -flat         # suporte subindo / descendo
    up_r, dn_r = sr > flat, sr < -flat         # resistência subindo / descendo
    if up_s and dn_r:
        return "Triângulo Simétrico"
    if up_s and not up_r and not dn_r:
        return "Triângulo Ascendente"
    if dn_r and not up_s and not dn_s:
        return "Triângulo Descendente"
    if up_s and up_r:
        return "Canal de Alta"
    if dn_s and dn_r:
        return "Canal de Baixa"
    return "Consolidação / Range"


async def _generate_signal_chart(asset: str, timeframe: str, signal: dict) -> bytes | None:
    """
    Gera gráfico de velas REAIS da Binance Futures com overlays do sinal:
    EMA 21/50/200, painel RSI, zona OB, Entry/SL/TP1/TP2, seta direcional,
    linhas de tendência, figura gráfica, alvos futuros projetados,
    watermark e caixa de estratégia.
    """
    try:
        import io
        import re as _re
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        # Normaliza o timeframe (ex: 3 -> 3m, 60 -> 1h, 15m -> 15m)
        tf_normal = str(timeframe).lower().strip()
        if tf_normal.isdigit():
            tf_minutes = int(tf_normal)
            if tf_minutes == 60:
                tf_normal = "1h"
            elif tf_minutes == 240:
                tf_normal = "4h"
            elif tf_minutes == 1440:
                tf_normal = "1d"
            else:
                tf_normal = f"{tf_minutes}m"
        elif not tf_normal.endswith(("m", "h", "d")):
            tf_normal = f"{tf_normal}m"

        # Usa o CACHE de klines (já populado pelo scan de mercado) em vez de uma
        # chamada fapi nova — no Railway a fapi sofre rate-limit/timeout e a chamada
        # extra só p/ o gráfico falhava, mandando o sinal SEM gráfico. Fallback p/
        # get_klines direto se o cache estiver frio/indisponível.
        df = None
        try:
            from klines_cache import get_klines_cached
            df = await get_klines_cached(asset, tf_normal, limit=100)
        except Exception as _kc_ex:
            print(f"[CHART] cache klines falhou ({_kc_ex}); tentando fapi direto")
        if df is None or len(df) < 15:
            try:
                from data_fetcher import get_klines
                df = await get_klines(asset, tf_normal, limit=100)
            except Exception as _gk_ex:
                print(f"[CHART] get_klines fapi falhou: {_gk_ex}")
                return None
        if df is None or len(df) < 15:
            print(f"[CHART] Klines insuficientes para {asset} {tf_normal}: df={df is not None and len(df) or 'None'}")
            return None

        # ── Dados reais ────────────────────────────────────────────────────────
        opens  = df["open"].values.tolist()
        highs  = df["high"].values.tolist()
        lows   = df["low"].values.tolist()
        closes = df["close"].values.tolist()
        vols   = df["volume"].values.tolist() if "volume" in df.columns else [1.0] * len(closes)
        times  = df.index.tolist() if hasattr(df.index, 'strftime') else list(range(len(closes)))

        # ── Parâmetros do sinal ────────────────────────────────────────────────
        dir_clean  = str(signal.get("direction", "LONG")).split(".")[-1].strip().upper()
        is_long    = "LONG" in dir_clean
        entry      = float(signal.get("entry", closes[-1]))
        sl         = float(signal.get("stop_loss", 0))
        tp1        = float(signal.get("tp1", 0))
        tp2        = float(signal.get("tp2", 0))
        rr         = float(signal.get("rr", 0))
        score      = float(signal.get("confidence", signal.get("score_total", 0)))
        conf_label = signal.get("conf_label") or ("Alta" if score >= 80 else "Média" if score >= 65 else "Baixa")
        tf_disp    = timeframe.upper()

        # Em testes ou sinais antigos, o mercado pode ja ter batido TP/SL.
        # Para o chart parecer um sinal real, corta no candle mais proximo
        # da entrada e projeta o futuro a partir dali.
        n_raw = len(closes)
        signal_idx = n_raw - 1
        if entry > 0 and n_raw >= 40:
            current = closes[-1]
            target_hit = (
                (is_long and ((tp1 and current >= tp1) or (sl and current <= sl))) or
                ((not is_long) and ((tp1 and current <= tp1) or (sl and current >= sl)))
            )
            moved_far = abs(current - entry) / entry >= 0.006
            if target_hit or moved_far:
                recent_start = max(0, n_raw - 90)
                candidates = []
                for i in range(recent_start, n_raw):
                    touched_entry = lows[i] <= entry <= highs[i]
                    dist = 0.0 if touched_entry else abs(closes[i] - entry)
                    candidates.append((dist, -i, i))
                signal_idx = max(24, min(candidates)[2])

        opens  = opens[:signal_idx + 1]
        highs  = highs[:signal_idx + 1]
        lows   = lows[:signal_idx + 1]
        closes = closes[:signal_idx + 1]
        vols   = vols[:signal_idx + 1]
        times  = times[:signal_idx + 1]
        n      = len(closes)

        rsi_vals = _rsi_series(closes, 14)

        # ── Binance-style colors ───────────────────────────────────────────────
        BG    = "#181a20"   # Binance dark background
        UP    = "#0ecb81"   # Binance green
        DOWN  = "#f6465d"   # Binance red
        GRID  = "#2b2f36"   # Binance grid
        TEXT  = "#eaecef"   # Binance main text
        MUTED = "#848e9c"   # Binance muted text
        SIG   = "#f0b90b" if is_long else "#f6465d"
        OB_C  = UP if is_long else DOWN
        ob_label = "OB Bullish" if is_long else "OB Bearish"

        fig = plt.figure(figsize=(16, 9), facecolor=BG)
        gs  = fig.add_gridspec(3, 1, height_ratios=[6.2, 1.0, 1.25], hspace=0.025)
        ax  = fig.add_subplot(gs[0])
        axv = fig.add_subplot(gs[1], sharex=ax)
        axr = fig.add_subplot(gs[2], sharex=ax)

        for a in [ax, axv, axr]:
            a.set_facecolor(BG)
            a.tick_params(colors=MUTED, labelsize=8, which="both",
                          top=False, bottom=False, left=False, right=True,
                          labelleft=False, labelright=True)
            for sp in a.spines.values():
                sp.set_edgecolor(GRID)
                sp.set_linewidth(0.6)
            a.yaxis.grid(True, color=GRID, linewidth=0.4, alpha=0.5)
            a.xaxis.grid(True, color=GRID, linewidth=0.3, alpha=0.3)

        # ── Candlesticks ────────────────────────────────────────────────────────
        W = 0.64
        price_range = max(highs) - min(lows) if highs else 1.0
        for i in range(n):
            col = UP if closes[i] >= opens[i] else DOWN
            ax.plot([i, i], [lows[i], highs[i]], color=col, linewidth=0.7, zorder=2)
            body = max(abs(closes[i] - opens[i]), price_range * 0.0005)
            rect = mpatches.FancyBboxPatch(
                (i - W/2, min(opens[i], closes[i])), W, body,
                boxstyle="square,pad=0", facecolor=col, edgecolor=col,
                linewidth=0, zorder=3)
            ax.add_patch(rect)
            axv.bar(i, vols[i] / 1e6, color=col, alpha=0.6, width=0.64)

        xs = list(range(n))

        # ── Zona OB ─────────────────────────────────────────────────────────────
        if sl and entry:
            risk = abs(entry - sl)
            ob_bot = (sl + risk * 0.15)  if is_long else (entry - risk * 0.12)
            ob_top = (entry + risk * 0.12) if is_long else (sl - risk * 0.15)
            ob_x0  = max(0, n - 28)
            ob_rect = mpatches.FancyBboxPatch(
                (ob_x0, min(ob_bot, ob_top)), n - ob_x0 - 0.5, abs(ob_top - ob_bot),
                boxstyle="square,pad=0", facecolor=OB_C, alpha=0.12,
                edgecolor=OB_C, linewidth=0.8, linestyle="--", zorder=2)
            ax.add_patch(ob_rect)
            ax.text(ob_x0 + 0.5, max(ob_bot, ob_top) + price_range * 0.004, ob_label,
                    color=OB_C, fontsize=7, fontweight="bold", va="bottom")

        # ════════ tendência (regressão) + figura + alvos projetados ══════════
        FUTURE  = 16                       # espaço à direita (menor → menos exagero)
        EXT     = 9                        # avanço da linha de tendência rumo ao possível final do movimento
        RES_C   = "#ff5f6d"                # resistência — tom de venda (alinhado ao DOWN)
        SUP_C   = "#2ecc71"                # suporte — tom de compra (alinhado ao UP)
        PROJ_C  = "#36c5f0"                # azul claro do caminho projetado
        import matplotlib.patheffects as _pe
        _GLOW = [_pe.withStroke(linewidth=3.4, foreground=BG, alpha=0.55)]
        figura  = ""
        WIN = min(n, 80)                   # janela ampla — acompanha o início real do movimento
        _i0 = n - WIN
        _sh  = [i for i in _chart_swings(highs, "high") if i >= _i0]
        _slw = [i for i in _chart_swings(lows,  "low")  if i >= _i0]
        # Linhas estruturais: pares de pivôs com mais toques e menos rompimentos.
        # A tolerância acompanha a volatilidade recente para não ajustar ruído.
        _max_slope = (price_range / max(WIN, 1)) * 1.2
        recent_ranges = [highs[i] - lows[i] for i in range(max(0, n - 20), n)]
        _tol = max(
            (sum(recent_ranges) / max(len(recent_ranges), 1)) * 0.32,
            price_range * 0.006,
        )
        resistance = _chart_fit_boundary(highs, _sh, "high", _i0, _tol, _max_slope)
        support = _chart_fit_boundary(lows, _slw, "low", _i0, _tol, _max_slope)

        # Sanidade: a linha ajustada pode ter "touches" ok nos pivôs esparsos mas
        # ainda assim se afastar muito do preço atual ao alcançar o candle mais
        # recente (ex: fit numa perna antiga do movimento, extrapolado por cima da
        # consolidação toda) — descarta nesse caso em vez de desenhar uma diagonal
        # desconectada da estrutura visível.
        _local_avg_range = sum(recent_ranges) / max(len(recent_ranges), 1)
        _max_dev = max(_local_avg_range * 5.0, price_range * 0.10)
        _cur_price = closes[-1]
        if resistance and abs((resistance[0] * (n - 1) + resistance[1]) - _cur_price) > _max_dev:
            resistance = None
        if support and abs((support[0] * (n - 1) + support[1]) - _cur_price) > _max_dev:
            support = None

        # ── Suporte/Resistência HORIZONTAL forte (nível testado ≥2x) ─────────
        # Diferente da linha diagonal de tendência: aqui é uma faixa de preço
        # fixa onde o mercado já reagiu mais de uma vez — cor neutra pra não
        # confundir com as linhas diagonais (vermelho/verde) nem com Entry/SL/TP.
        LVL_RES_C = "#ffb454"   # âmbar — resistência horizontal
        LVL_SUP_C = "#5b9bd5"   # azul aço — suporte horizontal
        strong_res = _chart_horizontal_level(highs, _sh, _i0, _tol, _cur_price, "res")
        strong_sup = _chart_horizontal_level(lows, _slw, _i0, _tol, _cur_price, "sup")
        _lvl_x0 = max(0, n - WIN)
        if strong_res:
            _lvl_price, _lvl_touches = strong_res
            ax.plot([_lvl_x0, n - 1], [_lvl_price, _lvl_price],
                    color=LVL_RES_C, linewidth=1.3, linestyle="-", alpha=0.75, zorder=3)
            ax.text(_lvl_x0, _lvl_price + price_range * 0.006,
                    f"Resistência forte ({_lvl_touches}x)", color=LVL_RES_C,
                    fontsize=7.3, fontweight="bold", va="bottom", ha="left")
        if strong_sup:
            _lvl_price, _lvl_touches = strong_sup
            ax.plot([_lvl_x0, n - 1], [_lvl_price, _lvl_price],
                    color=LVL_SUP_C, linewidth=1.3, linestyle="-", alpha=0.75, zorder=3)
            ax.text(_lvl_x0, _lvl_price - price_range * 0.006,
                    f"Suporte forte ({_lvl_touches}x)", color=LVL_SUP_C,
                    fontsize=7.3, fontweight="bold", va="top", ha="left")

        if resistance and support:
            mr, br, rx, _ = resistance
            ms, bs, sx, _ = support
            figura = _chart_classify_figure(ms, mr, entry)
            x_lo = min(rx[0], sx[0])
            # Limita a extensão futura a um avanço curto (EXT) — a linha não
            # invade a zona de projeção/alvos, onde já há texto e setas.
            x_hi = n + EXT
            if abs(ms - mr) > 1e-12:
                cross_x = (br - bs) / (ms - mr)
                if n - 2 <= cross_x < x_hi:
                    x_hi = cross_x
            ax.plot([x_lo, x_hi], [mr * x_lo + br, mr * x_hi + br],
                    color=RES_C, linewidth=2.0, alpha=0.95, zorder=4,
                    path_effects=_GLOW)
            ax.plot([x_lo, x_hi], [ms * x_lo + bs, ms * x_hi + bs],
                    color=SUP_C, linewidth=2.0, alpha=0.95, zorder=4,
                    path_effects=_GLOW)
            ax.scatter(rx, [highs[i] for i in rx], s=22, color=RES_C,
                       edgecolors=BG, linewidths=0.7, alpha=0.95, zorder=6)
            ax.scatter(sx, [lows[i] for i in sx], s=22, color=SUP_C,
                       edgecolors=BG, linewidths=0.7, alpha=0.95, zorder=6)

        # zona-alvo + caminho projetado SUAVE (espalhado por todo o futuro)
        if entry and (tp1 or tp2):
            tgt = tp2 or tp1
            same_tp = bool(tp1 and tp2 and abs(tp1 - tp2) <= entry * 1e-6)
            z_lo, z_hi = (entry, tgt) if is_long else (tgt, entry)
            ax.add_patch(mpatches.Rectangle(
                (n - 0.5, z_lo), FUTURE, z_hi - z_lo,
                facecolor=(UP if is_long else DOWN), alpha=0.05,
                edgecolor="none", zorder=1))
            _dip = (sl - entry) * 0.20 if sl else entry * (-0.0015 if is_long else 0.0015)
            if same_tp or not tp1:
                _px = [n - 1, n + 3, n + 8, n + 12, n + FUTURE]
                _py = [entry, entry + _dip, entry + (tgt - entry) * 0.55,
                       entry + (tgt - entry) * 0.42, tgt]
            else:
                _px = [n - 1, n + 3, n + 8, n + 11, n + FUTURE]
                _py = [entry, entry + _dip, tp1, tp1 - (tp1 - entry) * 0.25, tp2]
            ax.plot(_px, _py, color=PROJ_C, linewidth=2.1,
                    linestyle=(0, (4, 3)), zorder=6)
            ax.annotate("", xy=(_px[-1], _py[-1]), xytext=(_px[-2], _py[-2]),
                        arrowprops=dict(arrowstyle="-|>", color=PROJ_C, lw=1.6,
                                        mutation_scale=14), zorder=6)
            # Rótulo no início do caminho (perto do "dip"). O offset fixo de 5%
            # do price_range falhava quando Entry/SL/TP ficam comprimidos numa
            # faixa estreita (range total inflado por um movimento antigo no
            # histórico) — em vez disso, empurra o rótulo pra longe de qualquer
            # nível Entry/SL/TP1/TP2 que esteja realmente por perto.
            _occupied_lvls = sorted({y for y in [entry, sl, tp1, tp2] if y})
            _label_gap = price_range * 0.035
            projection_x = _px[1]
            _proj_dir = 1 if is_long else -1
            projection_y = _py[1] + _proj_dir * price_range * 0.05
            _tries = 0
            while any(abs(projection_y - lvl) < _label_gap for lvl in _occupied_lvls) and _tries < 8:
                projection_y += _proj_dir * _label_gap
                _tries += 1
            ax.text(
                projection_x, projection_y, "Projeção esperada",
                color=PROJ_C, fontsize=8.2, fontweight="bold",
                ha="left", va="bottom" if is_long else "top",
                bbox=dict(facecolor=BG, alpha=0.78, edgecolor=PROJ_C, linewidth=0.6, pad=2.0),
            )

        # ── Linhas Entry / SL / TP (full-width, label no lado direito) ───────────
        x1 = n + FUTURE + 0.3
        px_off = price_range * 0.006

        def hline_full(y, color, ls, lw, label):
            ax.axhline(y, color=color, linewidth=lw, linestyle=ls, zorder=5, alpha=0.9)
            ax.text(x1 - 0.5, y + px_off, label, color=color,
                    fontsize=9.2, fontweight="bold", va="bottom", ha="right")

        dir_label = "LONG" if is_long else "SHORT"
        # % de distância do nível em relação ao ENTRY (mostra se o alvo é realista)
        def _pct(y):
            return (y - entry) / entry * 100 if entry else 0.0
        if entry: hline_full(entry, SIG,  "--", 1.5, f"Entry  ${entry:,.4f}")
        if sl:    hline_full(sl,    DOWN, "-",  1.2, f"SL  ${sl:,.4f}  ({_pct(sl):+.1f}%)")
        # TP1==TP2 é por design (alvo único) → desenha UMA linha "TP" em vez de
        # duas sobrepostas. Quando forem distintos, mostra TP1 e TP2 separados.
        if tp1 and tp2 and abs(tp1 - tp2) <= entry * 1e-6:
            hline_full(tp1, UP, "--", 1.3, f"TP  ${tp1:,.4f}  ({_pct(tp1):+.1f}%)")
        else:
            if tp1: hline_full(tp1, UP, "--", 1.2, f"TP1  ${tp1:,.4f}  ({_pct(tp1):+.1f}%)")
            if tp2: hline_full(tp2, UP, ":",  1.0, f"TP2  ${tp2:,.4f}  ({_pct(tp2):+.1f}%)")

        # Faixas risco/retorno
        if sl and entry:
            ax.axhspan(min(sl, entry), max(sl, entry), alpha=0.05, color=DOWN, zorder=1)
        if entry and tp2:
            ax.axhspan(min(entry, tp2), max(entry, tp2), alpha=0.05, color=UP, zorder=1)

        # ── Caixa com Nome da Estratégia ───────────────────────────────────────
        strategy_label = ""
        v6_match = _re.search(r'\[V6:([^\]]+)\]', signal.get("reason", ""))
        if v6_match:
            strategy_label = f"Strategy: V6 ({v6_match.group(1)})"
        elif "PUMP" in signal.get("reason", "").upper():
            strategy_label = "Strategy: Pump Monitor"
        elif signal.get("trade_type"):
            strategy_label = f"Strategy: {signal.get('trade_type')}"
        
        if strategy_label:
            ax.text(0.02, 0.94, strategy_label, color="#f0b90b", fontsize=8.5,
                    fontweight="bold", transform=ax.transAxes,
                    bbox=dict(facecolor="#181a20", alpha=0.75, edgecolor="#2b2f36", boxstyle="round,pad=0.4"))

        # ── Seta direcional na última vela ─────────────────────────────────────
        sig_i  = n - 1
        tip_y  = lows[sig_i]  - price_range * 0.022 if is_long else highs[sig_i] + price_range * 0.022
        base_y = tip_y - price_range * 0.044 if is_long else tip_y + price_range * 0.044
        ax.annotate("", xy=(sig_i, tip_y), xytext=(sig_i, base_y),
                    arrowprops=dict(arrowstyle="-|>", color=SIG, lw=2.0,
                                    mutation_scale=18))
        ax.text(sig_i, base_y - price_range * 0.018 if is_long else base_y + price_range * 0.018,
                dir_label, color=SIG, fontsize=11, fontweight="bold",
                ha="center", va="top" if is_long else "bottom")

        # ── Watermark ───────────────────────────────────────────────────────────
        ax.text(0.5, 0.5, "@mestressinais_br", color=TEXT, fontsize=28,
                alpha=0.04, transform=ax.transAxes, ha="center", va="center",
                rotation=20, fontweight="bold")

        # ── Badge da figura gráfica (topo, centralizado) ─────────────────────────
        if figura:
            ax.text(0.5, 0.965, f"◈ {figura}", transform=ax.transAxes,
                    color=PROJ_C, fontsize=9.5, fontweight="bold",
                    va="top", ha="center",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="#10243a",
                              edgecolor=PROJ_C, linewidth=1.0, alpha=0.92))

        # ── Eixo X com datas reais ──────────────────────────────────────────────
        step = max(1, n // 8)
        ticks = list(range(0, n, step))
        tf_fmt = {"1m": "%H:%M", "3m": "%H:%M", "5m": "%H:%M",
                  "15m": "%d/%m %Hh", "1h": "%d/%m %Hh", "4h": "%d/%m",
                  "1d": "%d/%m"}.get(timeframe.lower(), "%d/%m %Hh")
        try:
            labels = [times[i].strftime(tf_fmt) for i in ticks]
        except Exception:
            labels = [str(i) for i in ticks]
        
        ax.set_xticks([])
        axv.set_xticks([])
        
        axr.set_xticks(ticks)
        axr.set_xticklabels(labels, fontsize=7.5, color=MUTED, rotation=0)
        axr.tick_params(axis="x", colors=MUTED, labelsize=7.5)

        # ── Painel de RSI (Subplot inferior) ──────────────────────────────────
        axr.axhline(70, color="#f6465d", linewidth=0.8, linestyle="--", alpha=0.6)
        axr.axhline(30, color="#0ecb81", linewidth=0.8, linestyle="--", alpha=0.6)
        axr.axhline(50, color=MUTED, linewidth=0.5, linestyle=":", alpha=0.4)
        axr.axhspan(30, 70, facecolor="#2b2f36", alpha=0.15)
        axr.plot(range(n), rsi_vals, color="#7a5cff", linewidth=1.6, label="RSI 14")
        axr.set_ylim(15, 85)
        axr.set_yticks([30, 50, 70])
        axr.set_ylabel("RSI", color=MUTED, fontsize=7, labelpad=2)
        axr.yaxis.set_label_position("right")

        # ── Eixo Y — preços à direita ────────────────────────────────────────────
        all_prices = highs + lows + ([tp2] if tp2 else []) + ([sl] if sl else [])
        ymin = min(all_prices) - price_range * 0.08
        ymax = max(all_prices) + price_range * 0.14
        ax.set_ylim(ymin, ymax)
        price_fmt = (lambda x, _: f"{x:,.4f}") if entry < 10 else \
                    (lambda x, _: f"{x:,.2f}") if entry < 1000 else \
                    (lambda x, _: f"{x:,.1f}")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(price_fmt))
        ax.yaxis.set_label_position("right")
        ax.yaxis.tick_right()
        ax.tick_params(axis="y", labelsize=8, colors=MUTED)

        # ── Eixo Y — volume ──────────────────────────────────────────────────────
        axv.set_yticks([])
        axv.set_ylabel("Vol", color=MUTED, fontsize=7, labelpad=2)
        axv.yaxis.set_label_position("right")

        # ── Legenda inline (estilo Binance) ─────────────────────────────────────
        # ── Cabeçalho estilo Binance ─────────────────────────────────────────────
        ax.set_title(
            f"{asset} / USDT  ·  {tf_disp}  ·  Binance Futures     "
            f"{dir_label}  ▪  Score {score:.0f}  ▪  R:R 1:{rr:.1f}  ▪  {conf_label}",
            color=TEXT, fontsize=15, pad=13, loc="left", fontweight="bold")
        ax.set_xlim(-1, n + FUTURE + 1.5)

        fig.subplots_adjust(left=0.018, right=0.89, top=0.93, bottom=0.055, hspace=0.025)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=135, facecolor=BG, bbox_inches=None)
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        import traceback
        print(f"[CHART] Erro ao gerar grafico {asset} {timeframe}: {e}")
        print(f"[CHART] Traceback: {traceback.format_exc()}")
        return None




# Explicações curtas para cada tag estrutural detectada no V6
_TAG_EXPLAIN: dict = {
    "BEAR-TREND":   "EMA50 abaixo da EMA200 — tendência de baixa confirmada",
    "BULL-TREND":   "EMA50 acima da EMA200 — tendência de alta confirmada",
    "DEATH-X":      "EMA50 cruzou abaixo da EMA200 — cruzamento da morte bearish",
    "GOLDEN-X":     "EMA50 cruzou acima da EMA200 — cruzamento dourado bullish",
    "MACRO-BEAR":   "Preço abaixo das EMAs longas — ciclo macro de baixa",
    "MACRO-BULL":   "Preço acima das EMAs longas — ciclo macro de alta",
    "MACRO-CONTRA": "Operando contra o ciclo macro — risco elevado",
    "DIV-REG":      "Divergência RSI regular — possível reversão de tendência",
    "DIV-HID":      "Divergência RSI oculta — confirmação de continuação",
    "OB/FVG":       "Order Block ou Fair Value Gap — zona institucional relevante",
    "sweep":        "Stop hunt detectado — possível reversão de liquidez",
    "struct":       "Estrutura de mercado favorável à direção",
    "BOS":          "Break of Structure — rompimento da estrutura atual",
    "FIB618":       "Confluência Fibonacci 61.8% — nível de retração dourado",
    "FIB":          "Confluência Fibonacci — retração em nível relevante",
    "RGM-TRE":      "Regime de tendência — operar a favor do fluxo direcional",
    "RGM-RNG":      "Regime lateral — alvos menores, cautela no tamanho",
    "RGM-BRK":      "Regime de breakout — alta volatilidade, risco elevado",
}


async def send_sinais_alert(signal: dict, pump_info: dict = None) -> bool:
    """Modo SINAIS — envia sinal formatado com leitura de mercado completa."""
    from state import state
    if state.operation_mode in ("SUPERVISED", "AUTONOMOUS", "GRID") and not state.dual_mode_enabled:
        print(f"[NOTIFIER] send_sinais_alert cancelado: bot em modo operacional exclusivo ({state.operation_mode}) sem modo Dual.")
        return True

    if not _is_configured():
        return False

    from datetime import timedelta
    import re as _re

    SEP = "━━━━━━━━━━"

    asset      = signal.get("asset", "")
    direcao    = signal.get("direction", "")
    dir_clean  = _clean_direction(direcao)
    is_long    = "LONG" in dir_clean
    tf         = signal.get("timeframe", "15m").upper()
    entry      = float(signal.get("entry", 0))
    sl         = float(signal.get("stop_loss", 0))
    tp1        = float(signal.get("tp1", 0))
    tp2        = float(signal.get("tp2", 0))
    rr         = float(signal.get("rr", 0))
    score      = float(signal.get("confidence", signal.get("score_total", 0)))
    reason     = signal.get("reason", "")
    trade_type = signal.get("trade_type", "DAY_TRADE")
    body_pct   = float(signal.get("body_pct", 0.0))
    vol_ratio  = float(signal.get("vol_ratio", 1.0))
    rsi_val    = float(signal.get("rsi_val", 50.0))
    confirmed_sigs = signal.get("confirmed_signals", [])
    recommendation = signal.get("recommendation", "")
    sug_lev    = int(signal.get("suggested_leverage", 0))
    leverage   = sug_lev if sug_lev else int(signal.get("leverage", 5))
    conf_label = signal.get("conf_label") or ("Alta" if score >= 80 else "Média" if score >= 65 else "Baixa")

    perfil_raw   = signal.get("perfil", "NORMAL")
    perfil_label = "AGRESSIVO" if perfil_raw == "AGGRESSIVE" else "CONSERVADOR" if perfil_raw == "CONSERVATIVE" else "NORMAL"
    mode_label   = signal.get("mode", "SINAIS").upper()

    tipo_map   = {"SCALP": "📅 SCALP", "DAY_TRADE": "📅 DAY TRADE", "SWING": "🌊 SWING"}
    tipo_label = tipo_map.get(trade_type, "📅 DAY TRADE")

    sl_pct  = abs(entry - sl)  / entry * 100 if entry else 0
    tp1_pct = abs(tp1 - entry) / entry * 100 if entry else 0
    tp2_pct = abs(tp2 - entry) / entry * 100 if entry else 0

    brt     = datetime.utcnow() - timedelta(hours=3)
    brt_str = brt.strftime("%d/%m/%Y • %H:%M BRT")

    # ── Direção ───────────────────────────────────────────────────────────────
    dir_icon    = "🟢" if is_long else "🔴"
    dir_label   = "LONG" if is_long else "SHORT"
    trend_arrow = "🔺" if is_long else "🔻"
    trend_label = "ALTA" if is_long else "BAIXA"

    # ── Estrutura / EMAs ──────────────────────────────────────────────────────
    if "UPTREND" in reason or "uptrend" in reason.lower():
        estrutura = "UPTREND"
    elif "DOWNTREND" in reason or "downtrend" in reason.lower():
        estrutura = "DOWNTREND"
    elif "RANGING" in reason:
        estrutura = "RANGING"
    else:
        estrutura = "UPTREND" if is_long else "DOWNTREND"

    ema_pos     = "ACIMA" if is_long else "ABAIXO"
    ema21_line  = f"{trend_arrow} EMA21 {ema_pos}"
    ema200_line = f"{trend_arrow} EMA200 {ema_pos}"
    if "BULL-TREND" in reason or "GOLDEN-X" in reason or "MACRO-BULL" in reason:
        ema200_line = "🔺 EMA200 ACIMA"
    elif "BEAR-TREND" in reason or "DEATH-X" in reason or "MACRO-BEAR" in reason:
        ema200_line = "🔻 EMA200 ABAIXO"
    ema200_pos  = "ACIMA" if "ACIMA" in ema200_line else "ABAIXO"

    # ── RSI label ─────────────────────────────────────────────────────────────
    if rsi_val < 30:
        rsi_label = "Sobrevendido"
    elif rsi_val > 70:
        rsi_label = "Sobrecomprado"
    elif rsi_val < 45:
        rsi_label = "Neutro baixo"
    elif rsi_val > 55:
        rsi_label = "Neutro alto"
    else:
        rsi_label = "Neutro"

    # ── Barra de força da vela ────────────────────────────────────────────────
    body_filled = int(body_pct * 10)
    body_bar    = "█" * body_filled + "░" * (10 - body_filled)

    # ── Confirmações ──────────────────────────────────────────────────────────
    conf_items = []
    v6_m = _re.search(r'\[V6:([^\]]+)\]', reason)
    if v6_m:
        raw_tags = v6_m.group(1)
        for tag, explain in _TAG_EXPLAIN.items():
            if tag in raw_tags:
                conf_items.append(explain)
    for s in confirmed_sigs:
        if not s.startswith("Padrão estrutural:"):
            conf_items.append(s)
    if not conf_items:
        conf_items = [f"Score técnico {score:.0f}/100 — múltiplos fatores alinhados"]
    conf_lines = "\n".join(f"• {c}" for c in conf_items[:3])

    # ── Recomendação (fallback) ───────────────────────────────────────────────
    if not recommendation:
        # Recomendação CONTEXTUAL — reflete estrutura, RSI, tipo e nível de stop reais
        _conv = "Alta convicção" if score >= 80 else "Convicção média" if score >= 65 else "Convicção baixa"
        if trade_type == "SCALP":
            _horiz = "alvo rápido, não segure. Realize TP1 e proteja no break-even"
        elif trade_type == "SWING":
            _horiz = "movimento mais longo. Use trailing após TP1 e deixe correr até TP2"
        else:
            _horiz = "realize parcial em TP1 e mova o stop para o break-even"
        if estrutura == "RANGING":
            _struct = "Estrutura em range"
        elif (estrutura == "UPTREND" and is_long) or (estrutura == "DOWNTREND" and not is_long):
            _struct = "A favor da tendência"
        elif estrutura in ("UPTREND", "DOWNTREND"):
            _struct = "Contra a tendência — cuidado redobrado"
        else:
            _struct = "Operar a favor do fluxo"
        if is_long and rsi_val >= 68:
            _rsi_note = f"RSI {rsi_val:.0f} esticado — acima de 70 não persiga"
        elif (not is_long) and rsi_val <= 32:
            _rsi_note = f"RSI {rsi_val:.0f} esticado — abaixo de 30 não persiga"
        else:
            _rsi_note = f"RSI {rsi_val:.0f} dá espaço"
        _rr_note = "R:R favorável" if rr >= 2 else "R:R apertado, seja seletivo"
        recommendation = (
            f"{_struct}: {_horiz}. {_rsi_note}. "
            f"Invalida se {'perder' if is_long else 'recuperar'} ${sl:,.4f}. "
            f"{_conv} (score {score:.0f}), {_rr_note}. Sem subir alavancagem."
        )

    # ── Pump/Dump note ────────────────────────────────────────────────────────
    pd_note = ""
    if pump_info:
        pd_type = pump_info.get("type", "PUMP/DUMP")
        pd_vol  = float(pump_info.get("rel_volume", 1.0))
        pd_ts   = pump_info.get("ts", 0)
        if pd_ts:
            pd_brt  = datetime.utcfromtimestamp(pd_ts) - timedelta(hours=3)
            pd_time = pd_brt.strftime("%H:%Mhrs")
            pd_note = f"🚨 {pd_type} detectado as {pd_time} BRT — Volume: {pd_vol:.1f}x\n"
        else:
            pd_note = f"🚨 {pd_type} detectado — Volume: {pd_vol:.1f}x\n"

    # ── Projeção ──────────────────────────────────────────────────────────────
    proj_lines = "  ".join(f"+{p}%→+{p * leverage}%" for p in [1, 2, 3, 4, 5])

    # ── Volume 24h absoluto (USDT) ────────────────────────────────────────────
    vol_24h_str = ""
    try:
        from data_fetcher import fetch, BINANCE_BASE
        ticker = await fetch(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", {"symbol": asset})
        vol_24h_usdt = float(ticker.get("quoteVolume", 0))
        if vol_24h_usdt >= 1_000_000_000:
            vol_24h_str = f"{vol_24h_usdt / 1_000_000_000:.1f}B"
        elif vol_24h_usdt >= 1_000_000:
            vol_24h_str = f"{vol_24h_usdt / 1_000_000:.0f}M"
        elif vol_24h_usdt >= 1_000:
            vol_24h_str = f"{vol_24h_usdt / 1_000:.0f}K"
        else:
            vol_24h_str = f"{vol_24h_usdt:.0f}"
    except Exception:
        vol_24h_str = "—"

    # ── Eventos CoinMarketCal ─────────────────────────────────────────────────
    event_line = ""
    try:
        from coinmarketcal_client import get_global_events, get_events_for_symbol, format_event_note
        sig_events = get_events_for_symbol(asset, get_global_events(), hours_ahead=48)
        if sig_events:
            event_line = f"\n{SEP}\n\n{format_event_note(sig_events)}"
    except Exception:
        pass

    # ── Padrões de candlestick (CandlePatternEngine) ──────────────────────────
    _pat_sig  = signal.get("patterns_detected", [])  # padrões no TF do sinal
    _pat_mtf  = signal.get("patterns_mtf", {})       # padrões em TFs maiores
    _SIG_ICONS = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
    _STR_STARS = {1: "★", 2: "★★", 3: "★★★"}

    _pat_lines = []
    # Padrões no TF do próprio sinal
    for p in sorted(_pat_sig, key=lambda x: -x.get("strength", 1))[:3]:
        icon  = _SIG_ICONS.get(p.get("signal", "neutral"), "⚪")
        stars = _STR_STARS.get(p.get("strength", 1), "★")
        _pat_lines.append(f"  {icon} [{tf}] {p['name_pt']} {stars}")
    # Padrões nos TFs maiores (excluindo o TF do sinal)
    for _mtf_tf in ["1h", "4h", "1d"]:
        if _mtf_tf.upper() == tf.upper():
            continue
        _mtf_pats = _pat_mtf.get(_mtf_tf, [])
        for p in sorted(_mtf_pats, key=lambda x: -x.get("strength", 1))[:2]:
            if p.get("strength", 1) >= 2:
                icon  = _SIG_ICONS.get(p.get("signal", "neutral"), "⚪")
                stars = _STR_STARS.get(p.get("strength", 1), "★")
                label = _mtf_tf.upper()
                _pat_lines.append(f"  {icon} [{label}] {p['name_pt']} {stars}")

    patterns_section = ""
    if _pat_lines:
        patterns_section = (
            f"{SEP}\n"
            f"🕯️ PADRÕES GRÁFICOS\n\n"
            + "\n".join(_pat_lines) + "\n"
        )

    # ── Monta mensagem final ──────────────────────────────────────────────────
    msg = (
        f"MESTRE DO SINAIS -MODO: {mode_label} | PERFIL {perfil_label}\n\n"
        f"{asset} | {dir_icon} {dir_label} | {tf} | {tipo_label}\n"
        f"{SEP}\n"
        f"📊 Score: {score:.0f}/100   ⚖️ R:R: {rr:.1f}:1\n"
        f"🎯 Confiança: {conf_label}   💹 Vol 24h: {vol_24h_str}\n"
        f"💰 Entrada: ${entry:,.4f}\n"
        f"🛑 Stop: ${sl:,.4f} (-{sl_pct:.2f}%)\n"
        f"🎯 TP1: ${tp1:,.4f} (+{tp1_pct:.2f}%)   🎯 TP2: ${tp2:,.4f} (+{tp2_pct:.2f}%)\n"
        f"⚡️ Alavancagem: {leverage}x\n"
        + (f"{pd_note}" if pd_note else "")
        + f"{SEP}\n"
        f"📈 LEITURA DO MERCADO\n"
        f"{trend_arrow} Tendência: {trend_label} | Estrutura: {estrutura}\n"
        f"{trend_arrow} EMA21 {ema_pos} | EMA200 {ema200_pos}\n"
        f"📊 RSI: {rsi_val:.1f} - {rsi_label}\n"
        f"🕯️ Vela: {body_bar} {body_pct*100:.0f}%   📦 Vol: {vol_ratio:.1f}x média\n"
        f"{SEP}\n"
        f"✅ CONFIRMAÇÕES\n"
        f"{conf_lines}\n"
        f"{SEP}\n"
        f"💡 RECOMENDAÇÃO\n"
        f"{recommendation}\n"
        f"{SEP}\n"
        f"📈 PROJEÇÃO ({leverage}x)\n"
        f"{proj_lines}\n"
        f"{SEP}\n"
        f"⏰ {brt_str}\n"
        f"OPERE SEMPRE COM MUITA ATENÇÃO!"
    )

    msg += event_line
    # Salvaguarda CAIXA ÚNICA: legenda de foto do Telegram tem limite de 1024 chars.
    if len(msg) > 1024:
        msg = msg[:1021].rstrip() + "..."

    chart_bytes = await _generate_signal_chart(asset, tf.lower(), signal)
    if chart_bytes:
        print(f"[CHART] Gráfico gerado com sucesso: {asset} {tf} ({len(chart_bytes):,} bytes)")
    else:
        print(f"[CHART] ⚠ Gráfico NÃO gerado para {asset} {tf} — sinal enviado sem imagem")

    # ── Mensagem pública limpa (sem seções internas do VIP) ──────────────────
    msg_public = (
        f"MESTRE DO SINAIS -MODO: {mode_label} | PERFIL {perfil_label}\n\n"
        f"{asset} | {dir_icon} {dir_label} | {tf} | {tipo_label}\n"
        f"{SEP}\n"
        f"📊 Score: {score:.0f}/100   ⚖️ R:R: {rr:.1f}:1\n"
        f"🎯 Confiança: {conf_label}   💹 Vol 24h: {vol_24h_str}\n"
        f"💰 Entrada: ${entry:,.4f}\n"
        f"🛑 Stop: ${sl:,.4f} (-{sl_pct:.2f}%)\n"
        f"🎯 TP1: ${tp1:,.4f} (+{tp1_pct:.2f}%)   🎯 TP2: ${tp2:,.4f} (+{tp2_pct:.2f}%)\n"
        f"⚡️ Alavancagem: {leverage}x\n"
        f"{SEP}\n"
        f"📈 LEITURA DO MERCADO\n"
        f"{trend_arrow} Tendência: {trend_label} | Estrutura: {estrutura}\n"
        f"📊 RSI: {rsi_val:.1f} - {rsi_label}\n"
        f"🕯️ Vela: {body_bar} {body_pct*100:.0f}%   📦 Vol: {vol_ratio:.1f}x média\n"
        f"{SEP}\n"
        f"⚠️ Não é conselho financeiro. Gerencie seu risco."
    )

    # ── Roteamento ────────────────────────────────────────────────────────────
    # VIP sempre — mensagem completa
    # Canal público — mensagem limpa se Alta/Média e abaixo do limite diário
    vip_targets:     list[str] = []
    channel_targets: list[str] = []

    if TELEGRAM_VIP_ID:
        vip_targets.append(str(TELEGRAM_VIP_ID))
    else:
        vip_targets.append(str(TELEGRAM_CHAT_ID))  # fallback: chat pessoal

    if _channel_ok(conf_label):
        channel_targets.append(str(TELEGRAM_CHANNEL_ID))

    send_coros = [_send_signal_message(t, msg, {}, chart_bytes) for t in vip_targets]
    send_coros += [_send_signal_message(t, msg_public, {}, chart_bytes) for t in channel_targets]

    results = await asyncio.gather(*send_coros, return_exceptions=True)
    return any(r is True for r in results)


async def send_social_proof(trade: dict, mode: Optional[str] = None) -> bool:
    """Broadcast resultado de trade bem-sucedido como prova social (canal público + VIP)."""
    if not _is_configured():
        return False
    # Sinais e resultados dos modos GRID, AUTONOMOUS e SUPERVISED ficam apenas no bot (chat pessoal)
    from state import state
    if mode in ("GRID", "AUTONOMOUS", "SUPERVISED") or (state.operation_mode in ("SUPERVISED", "AUTONOMOUS", "GRID") and not state.dual_mode_enabled):
        return True
    if not TELEGRAM_CHANNEL_ID:
        return False

    asset      = trade.get("asset", "???")
    direction  = trade.get("direction", "LONG")
    entry      = float(trade.get("entry_price", 0) or 0)
    exit_p     = float(trade.get("exit_price", 0) or 0)
    pnl_pct    = float(trade.get("pnl_pct", 0) or 0)
    pnl_usdt   = float(trade.get("pnl_usdt", 0) or 0)
    leverage   = int(trade.get("leverage", 1) or 1)
    closed_at  = trade.get("closed_at", "")[:16].replace("T", " ") if trade.get("closed_at") else ""

    dir_icon  = "🟢" if direction == "LONG" else "🔴"
    dir_label = "LONG" if direction == "LONG" else "SHORT"
    trophy    = "🏆" if pnl_pct >= 5 else "✅"
    sign      = "+" if pnl_usdt >= 0 else ""

    SEP = "─" * 28
    msg = (
        f"{trophy} TRADE FECHADO COM LUCRO!\n"
        f"{SEP}\n"
        f"{dir_icon} {asset} | {dir_label} | {leverage}x\n\n"
        f"💰 Entrada:  ${entry:,.4f}\n"
        f"🎯 Saída:    ${exit_p:,.4f}\n\n"
        f"📈 Resultado: {sign}{pnl_pct:.2f}%\n"
        f"💵 PnL:       {sign}${pnl_usdt:.2f} USDT\n"
        f"{SEP}\n"
        f"🤖 Bot operando em tempo real\n"
        f"⏱️ Fechado: {closed_at} UTC\n\n"
        f"⚠️ Resultados passados não garantem ganhos futuros."
    )

    targets: list[str] = [str(TELEGRAM_CHANNEL_ID)]
    if TELEGRAM_VIP_ID:
        targets.append(str(TELEGRAM_VIP_ID))
    elif TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID) not in targets:
        targets.append(str(TELEGRAM_CHAT_ID))

    results = await asyncio.gather(
        *[_post("sendMessage", {"chat_id": t, "text": msg}) for t in targets],
        return_exceptions=True,
    )
    return any(isinstance(r, dict) and r.get("ok") for r in results)


async def send_daily_target_reached(pnl: float, target: float) -> bool:
    """Notificacao quando meta diaria e atingida — enviada uma vez por dia."""
    if not _is_configured():
        return False
    msg = (
        f"🎯 META DIARIA ATINGIDA!\n\n"
        f"PnL do dia: +${pnl:.2f} USDT\n"
        f"Meta: ${target:.2f} USDT\n\n"
        f"Bot pausando novos trades por hoje.\n"
        f"Use /auto retomar para continuar operando."
    )
    result = await _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    return bool(result.get("ok"))


async def send_grid_trend_alert(symbol: str, direction: str, rsi: float, reason: str = "") -> bool:
    """Alerta quando grid e pausado por tendencia forte."""
    if not _is_configured():
        return False
    dir_label = "ALTA" if "LONG" in direction.upper() else "BAIXA"
    msg = (
        f"⚠️ GRID PAUSADO — {symbol}\n\n"
        f"Tendencia forte detectada: {dir_label}\n"
        f"RSI atual: {rsi:.1f}\n"
        f"{reason}\n\n"
        f"Grid nao entra contra a tendencia.\n"
        f"Aguardando mercado lateral para retomar."
    )
    result = await _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    return bool(result.get("ok"))


async def send_grid_stale_alert(symbol: str, hours: float) -> bool:
    """Alerta quando par grid fica mais de 2h sem completar ciclo."""
    if not _is_configured():
        return False
    msg = (
        f"⏰ GRID SEM CICLO — {symbol}\n\n"
        f"Este par esta sem completar ciclo ha {hours:.1f}h.\n"
        f"Pode indicar mercado sem liquidez ou trend forte.\n\n"
        f"Verifique a posicao e considere fechar manualmente."
    )
    result = await _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    return bool(result.get("ok"))


async def send_news_broadcast(market_data: dict) -> bool:
    """Envia RADAR DE MERCADO formatado — novo padrão visual completo."""
    if not _is_configured():
        return False
    from datetime import timedelta

    SEP = "━━━━━━━━━━━━━━━━━━"
    brt     = datetime.utcnow() - timedelta(hours=3)
    brt_str = brt.strftime("%d/%m/%Y • %H:%M BRT")

    # ── Dados base ────────────────────────────────────────────────────────────
    fg        = market_data.get("fear_greed", {})
    fg_val    = fg.get("value", "--")
    fg_class  = fg.get("value_classification", "--")
    btc_price = market_data.get("btc_price", "--")
    btc_chg   = market_data.get("btc_change_24h", "--")
    btc_dom   = market_data.get("btc_dominance", "--")
    eth_chg   = market_data.get("eth_change_24h", 0)
    sol_chg   = market_data.get("sol_change_24h", 0)
    score     = market_data.get("score", 5)
    trending  = market_data.get("trending", [])
    gainers   = market_data.get("top_gainers", [])
    losers    = market_data.get("top_losers", [])
    news_list = market_data.get("news", [])
    calendar  = market_data.get("calendar", [])

    # ── Parse numérico ────────────────────────────────────────────────────────
    try:
        fgi = int(fg_val)
    except Exception:
        fgi = 50
    try:
        btc_chg_f = float(str(btc_chg).replace(",", "").replace("+", ""))
    except Exception:
        btc_chg_f = 0.0
    try:
        eth_chg_f = float(eth_chg)
    except Exception:
        eth_chg_f = 0.0

    # ── Sentimento ────────────────────────────────────────────────────────────
    if fgi < 25:
        fg_emoji = "😱"; fg_label = "Medo Extremo"
    elif fgi < 45:
        fg_emoji = "😰"; fg_label = "Medo"
    elif fgi < 55:
        fg_emoji = "😐"; fg_label = "Neutro"
    elif fgi < 75:
        fg_emoji = "😊"; fg_label = "Ganância"
    else:
        fg_emoji = "🤑"; fg_label = "Ganância Extrema"

    btc_chg_str = f"+{btc_chg_f:.2f}%" if btc_chg_f >= 0 else f"{btc_chg_f:.2f}%"

    # ── Cenário Atual ─────────────────────────────────────────────────────────
    if fgi < 25 and btc_chg_f > 2:
        cenario_emoji = "🟡"; cenario = "Recuperação / Alta Contrária"
    elif fgi >= 75 and btc_chg_f > 3:
        cenario_emoji = "🟡"; cenario = "Ganância Extrema / Cautela"
    elif fgi >= 55 and btc_chg_f > 1:
        cenario_emoji = "🟢"; cenario = "Altista / Favorável"
    elif btc_chg_f < -3:
        cenario_emoji = "🔴"; cenario = "Baixista / Risco"
    elif fgi < 45:
        cenario_emoji = "🟡"; cenario = "Neutro / Volátil"
    else:
        cenario_emoji = "🟡"; cenario = "Neutro / Observação"

    # ── Leitura interpretativa ─────────────────────────────────────────────────
    if fgi < 25 and btc_chg_f > 2:
        leitura = "Mercado em medo extremo, mas BTC demonstra recuperação. Oportunidade seletiva para quem tem disciplina."
    elif fgi < 45:
        leitura = "Mercado ainda opera sob medo, mas o Bitcoin demonstra força no curto prazo."
    elif fgi >= 75 and btc_chg_f > 5:
        leitura = "Ganância extrema com BTC em alta forte. Cautela com novos longs — risco de correção elevado."
    elif btc_chg_f > 3:
        leitura = "Bitcoin puxando o mercado. Altcoins seletivas acompanhando. Viés comprador no curto prazo."
    elif btc_chg_f < -3:
        leitura = "Bitcoin em queda relevante. Reduzir exposição e aguardar estabilização antes de novos trades."
    else:
        leitura = "Mercado lateral buscando direção. Aguardar confirmação antes de grandes posições."

    # ── Fluxo de Capital ──────────────────────────────────────────────────────
    btc_flow = (
        "🟢 BTC recebendo fluxo comprador" if btc_chg_f > 1
        else "🔴 BTC sob pressão vendedora" if btc_chg_f < -1
        else "🟡 BTC lateralizando — fluxo neutro"
    )
    eth_sign     = "+" if eth_chg_f >= 0 else ""
    eth_label    = "forte desempenho" if abs(eth_chg_f) > 3 else "variação moderada"
    eth_flow_em  = "🟢" if eth_chg_f > 0 else "🔴"
    eth_flow     = f"{eth_flow_em} ETH com {eth_label} ({eth_sign}{eth_chg_f:.1f}%)"

    n_gainers = len([g for g in gainers if float(g.get("change", 0)) > 3])
    alt_flow  = (
        "🟢 Altcoins em alta generalizada" if n_gainers >= 4
        else "🟢 Altcoins seguem seletivas" if n_gainers >= 2
        else "🟡 Altcoins com desempenho misto" if n_gainers >= 1
        else "🔴 Altcoins em queda"
    )
    cap_summary = (
        "📊 Dinheiro concentrado em BTC e ETH" if btc_chg_f > 2 and eth_chg_f > 2
        else "📊 Rotação de capital para altcoins seletivas" if n_gainers >= 3
        else "📊 Capital defensivo em USDT / BTC"
    )

    # ── Ativos em Destaque ────────────────────────────────────────────────────
    highlights = list(dict.fromkeys(
        [str(s).replace("USDT","").upper() for s in (trending or [])] +
        [g.get("symbol","").replace("USDT","").upper() for g in gainers[:3]]
    ))[:5]
    if not highlights:
        highlights = ["BTC", "ETH", "SOL"]
    highlight_lines = "\n".join(f"• {s}" for s in highlights)

    # ── Destaques do Dia (sem links, emoji de sentimento) ─────────────────────
    sent_map   = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}
    news_lines = []
    for n in news_list[:4]:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title", ""))[:100].strip()
        if not title:
            continue
        emoji = sent_map.get(n.get("sentiment", "neutral"), "🟡")
        news_lines.append(f"{emoji} {title}")
    if not news_lines:
        news_lines = ["🟡 Sem destaques recentes no momento"]

    # ── Eventos Importantes ───────────────────────────────────────────────────
    flags    = {"USD":"🇺🇸","EUR":"🇪🇺","GBP":"🇬🇧","JPY":"🇯🇵",
                "CNY":"🇨🇳","CAD":"🇨🇦","AUD":"🇦🇺","BRL":"🇧🇷"}
    cal_lines = []
    for ev in calendar[:4]:
        if not isinstance(ev, dict):
            continue
        date  = ev.get("date","")
        flag  = flags.get(ev.get("country",""), ev.get("country",""))
        title = str(ev.get("title",""))[:55].strip()
        if date and title:
            cal_lines.append(f"📅 {date} {flag} {title}")
    has_events = len(cal_lines) > 0
    if has_events:
        cal_lines.append("⚡ Possível aumento de volatilidade nas próximas 48h")
    else:
        cal_lines = ["📅 Sem eventos de alto impacto nas próximas 48h"]

    # ── Viés Operacional ──────────────────────────────────────────────────────
    def _vies(chg: float) -> str:
        if chg > 5:  return "🟢 ALTISTA FORTE"
        if chg > 2:  return "🟢 LEVEMENTE ALTISTA"
        if chg > 0:  return "🟡 LEVEMENTE ALTISTA"
        if chg > -2: return "🟡 NEUTRO"
        if chg > -5: return "🔴 LEVEMENTE BAIXISTA"
        return "🔴 BAIXISTA"

    btc_vies = _vies(btc_chg_f)
    eth_vies = _vies(eth_chg_f)
    alt_vies = "🟢 ALTISTA" if n_gainers >= 3 else "🟡 NEUTRO" if n_gainers >= 1 else "🔴 BAIXISTA"
    mkt_geral = "🟢 FAVORÁVEL" if score >= 7 else "🟡 CAUTELOSO" if score >= 5 else "🔴 DESFAVORÁVEL"

    # ── Recomendações dinâmicas ───────────────────────────────────────────────
    recs = []
    if score >= 7:
        recs.append("✅ Priorizar operações com Score acima de 80")
        recs.append("✅ Preferir ativos com volume crescente")
    else:
        recs.append("⚠️ Reduzir tamanho de posições — cenário incerto")
        recs.append("✅ Aguardar sinais com Score acima de 85")
    if has_events:
        recs.append("✅ Reduzir exposição antes dos eventos macroeconômicos")
    if fgi >= 75:
        recs.append("⚠️ Evitar perseguir movimentos após pumps fortes")
    elif fgi < 30:
        recs.append("✅ Mercado com medo = possível oportunidade — operar com cautela")
    if btc_chg_f > 5:
        recs.append("⚠️ BTC em alta forte — cuidado com alts que não acompanham")
    rec_block = "\n".join(recs[:4])

    # ── Oportunidades em Observação ───────────────────────────────────────────
    opps = list(dict.fromkeys(
        [g.get("symbol","").replace("USDT","").upper() for g in gainers[:5]] +
        [str(s).replace("USDT","").upper() for s in (trending or [])]
    ))[:5]
    if not opps:
        opps = ["ETH", "BTC", "SOL"]
    nums = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣"]
    opp_lines = "\n".join(f"{nums[i]} {sym}" for i,sym in enumerate(opps))

    # ── Monta mensagem ────────────────────────────────────────────────────────
    msg = (
        f"🚨 TRADER 001 — RADAR DE MERCADO\n\n"
        f"⏰ {brt_str}\n\n"
        f"{SEP}\n\n"
        f"🧠 SENTIMENTO GERAL\n\n"
        f"{fg_emoji} Fear & Greed: {fg_val}/100 ({fg_label})\n"
        f"{cenario_emoji} Cenário Atual: {cenario}\n"
        f"📈 BTC: ${btc_price} ({btc_chg_str})\n"
        f"👑 Dominância BTC: {btc_dom}%\n"
        f"💡 Leitura:\n{leitura}\n\n"
        f"{SEP}\n\n"
        f"💰 FLUXO DE CAPITAL\n\n"
        f"{btc_flow}\n"
        f"{eth_flow}\n"
        f"{alt_flow}\n"
        f"{cap_summary}\n\n"
        f"{SEP}\n\n"
        f"🔥 ATIVOS EM DESTAQUE\n\n"
        f"{highlight_lines}\n\n"
        f"📈 Maior força relativa nas últimas 24h\n\n"
        f"{SEP}\n\n"
        f"📰 DESTAQUES DO DIA\n\n"
        + "\n".join(news_lines) + "\n\n"
        + f"{SEP}\n\n"
        f"⚠️ EVENTOS IMPORTANTES\n\n"
        + "\n".join(cal_lines) + "\n\n"
        + f"{SEP}\n\n"
        f"🎯 VIÉS OPERACIONAL\n\n"
        f"BTC: {btc_vies}\n"
        f"ETH: {eth_vies}\n"
        f"ALTCOINS: {alt_vies}\n"
        f"Mercado Geral: {mkt_geral}\n\n"
        f"{SEP}\n\n"
        f"📌 RECOMENDAÇÃO\n\n"
        f"{rec_block}\n\n"
        f"{SEP}\n\n"
        f"🏆 OPORTUNIDADES EM OBSERVAÇÃO\n\n"
        f"{opp_lines}\n\n"
        f"{SEP}\n\n"
        f"🤖 TRADER 001 MARKET INTELLIGENCE\n"
        f"Atualização automática a cada 60 minutos"
    )

    if len(msg) > 4000:
        msg = msg[:3990] + "\n\n_(truncado)_"

    # Radar vai para: chat pessoal + VIP
    radar_targets = [str(TELEGRAM_CHAT_ID)]
    if TELEGRAM_VIP_ID:
        radar_targets.append(str(TELEGRAM_VIP_ID))

    results = await asyncio.gather(
        *[_post("sendMessage", {"chat_id": t, "text": msg, "disable_web_page_preview": True})
          for t in radar_targets],
        return_exceptions=True,
    )
    return any(isinstance(r, dict) and r.get("ok") for r in results)


async def send_weekly_sinais_stats(stats: dict) -> bool:
    """Envia resumo semanal de sinais do modo SINAIS."""
    if not _is_configured():
        return False
    total    = stats.get("total_sent", 0)
    alta     = stats.get("alta", 0)
    media    = stats.get("media", 0)
    baixa    = stats.get("baixa", 0)
    period   = stats.get("period", "semana")
    win_rate = stats.get("win_rate", "--")
    avg_rr   = stats.get("avg_rr", "--")
    msg = (
        f"📊 SINAIS — RESUMO DA {period.upper()}\n\n"
        f"Total de sinais transmitidos: {total}\n"
        f"  Alta confianca: {alta}\n"
        f"  Media confianca: {media}\n"
        f"  Baixa confianca: {baixa}\n\n"
        f"Taxa de acerto estimada: {win_rate}%\n"
        f"R:R medio dos sinais: {avg_rr}\n\n"
        f"Use /sinais estatisticas para mais detalhes."
    )
    # Relatório semanal vai para canal público e VIP (não conta no limite diário)
    targets: list[str] = []
    if TELEGRAM_CHANNEL_ID:
        targets.append(str(TELEGRAM_CHANNEL_ID))
    if TELEGRAM_VIP_ID:
        targets.append(str(TELEGRAM_VIP_ID))
    if not targets:
        targets.append(str(TELEGRAM_CHAT_ID))

    results = await asyncio.gather(
        *[_post("sendMessage", {"chat_id": t, "text": msg}) for t in targets],
        return_exceptions=True,
    )
    return any(isinstance(r, dict) and r.get("ok") for r in results)


async def send_pd_monitor_alert(alert: dict) -> bool:
    """
    Alerta dedicado de Pump/Dump Monitor — enviado sempre que detectado no modo SINAIS.
    Independente do canal de sinais regulares.
    """
    if not _is_configured():
        print(f"[PD-MONITOR] {alert.get('type')} {alert.get('symbol')} — Telegram nao configurado")
        return False

    from datetime import timedelta

    sym        = alert.get("symbol", "?")
    pd_type    = alert.get("type", "PUMP")
    intensity  = alert.get("intensity", "MODERADO")
    confidence = int(alert.get("confidence", 0))
    rel_vol    = float(alert.get("rel_volume", 1.0))
    rsi_val    = float(alert.get("rsi", 50))
    price_acc  = float(alert.get("price_acc", 0.0))   # variacao 3 velas
    price_1c   = float(alert.get("price_1c", 0.0))    # variacao 1 vela
    price      = float(alert.get("price", 0.0))
    consec_up  = int(alert.get("consec_up", 0))
    consec_dn  = int(alert.get("consec_down", 0))
    big_candle   = bool(alert.get("big_candle", False))
    oi_signal    = alert.get("oi_signal", "PENDING")
    oi_chg_pct   = float(alert.get("oi_change_pct", 0.0))
    rsi_delta    = float(alert.get("rsi_delta", 0.0))
    vol_sustained = bool(alert.get("vol_sustained", False))
    signals      = alert.get("signals", [])
    rec          = alert.get("recommendation", "")
    ts           = alert.get("ts", 0)

    brt = datetime.utcnow() - timedelta(hours=3)
    brt_str = brt.strftime("%H:%M BRT")

    # Icones por tipo e intensidade
    if pd_type == "PUMP":
        type_icon = "🚀"
        price_sign = "+"
    else:
        type_icon = "💣"
        price_sign = ""

    if intensity == "EXTREMO":
        intensity_icon = "🔴🔴🔴"
    elif intensity == "FORTE":
        intensity_icon = "🟠🟠"
    else:
        intensity_icon = "🟡"

    big_candle_line = f"\n  🕯️ *VELA {price_1c:+.1f}%* — valorização absoluta ≥5%" if big_candle else ""

    # OI direction line
    _oi_icons = {"ORGANIC": "🟢 ORGÂNICO", "SQUEEZE": "⚡ SQUEEZE", "EXHAUSTION": "⚠️ EXAUSTÃO", "NEUTRAL": "⚪ NEUTRO", "PENDING": "⏳"}
    oi_label = _oi_icons.get(oi_signal, oi_signal)
    oi_chg_str = f" ({oi_chg_pct:+.1f}%)" if oi_signal not in ("PENDING",) else ""
    oi_line = f"\n  OI: {oi_label}{oi_chg_str}"

    # RSI delta e vol sustentado
    rsi_delta_str = f" | ΔRSI {rsi_delta:.0f}" if rsi_delta >= 8 else ""
    vol_sust_str  = "  ✅ Volume sustentado (v-1 acima da média)\n" if vol_sustained else ""

    # Linha de sinais detectados
    signals_str = "\n".join(f"  • {s}" for s in signals[:5]) if signals else "  • Volume anormal"

    # Sequencia de velas
    consec_str = ""
    if pd_type == "PUMP" and consec_up >= 3:
        consec_str = f"\n🕯 {consec_up} velas altas consecutivas"
    elif pd_type == "DUMP" and consec_dn >= 3:
        consec_str = f"\n🕯 {consec_dn} velas baixas consecutivas"

    # Qualidade da vela (corpo/range)
    body_pct     = float(alert.get("body_pct_rng", 0.0))
    body_bar     = "█" * int(body_pct * 10) + "░" * (10 - int(body_pct * 10))
    vol_accel    = float(alert.get("vol_accel", 1.0))
    move_in_atrs = float(alert.get("move_in_atrs", 0.0))
    close_pos    = float(alert.get("close_pos", 0.5))
    pre_accum    = bool(alert.get("pre_accum", False))
    session_name = alert.get("session", "")

    # Posicao do fechamento dentro do range (barra visual)
    cp_idx        = min(9, int(close_pos * 10))
    close_pos_bar = "░" * cp_idx + "█" + "░" * (9 - cp_idx)

    # Acumulacao pre-spike
    pre_accum    = bool(alert.get("pre_accum", False))
    pre_line     = "\n  ✅ Acumulacao pre-spike confirmada" if pre_accum else ""

    # Sessao de liquidez
    session_line = f"\n  Sessao: *{session_name}*" if session_name else ""

    # Sustentação
    sust = alert.get("sustentation", "FRESH")
    sust_icons = {"FRESH": "🆕", "CONTINUATION": "🔥", "DISTRIBUTION": "⚠️"}
    sust_labels = {
        "FRESH":        "FRESCO — spike acontecendo agora",
        "CONTINUATION": "CONTINUACAO — momentum ativo",
        "DISTRIBUTION": "DISTRIBUICAO — movimento pode estar exausto",
    }
    sust_icon  = sust_icons.get(sust, "")
    sust_label = sust_labels.get(sust, sust)

    # Timeframes onde foi detectado (MTF confluence)
    tf_labels   = alert.get("tf_labels", [alert.get("tf", "?")])
    mtf_count   = alert.get("mtf_count", 1)
    tf_str      = " + ".join(tf_labels)
    mtf_line    = f"\n  TFs ativos: *{tf_str}*" + (f"  🔀 MTF x{mtf_count}" if mtf_count > 1 else "")

    # Indicador de intensidade visual (barra)
    conf_bar = "█" * (confidence // 10) + "░" * (10 - confidence // 10)

    SEP = "━━━━━━━━━━"

    tf_display = alert.get("tf", "5M").upper()

    # Padrão definitivo (caixa única): cabeçalho compacto com Conf; sem Sessão/MTF/OI/
    # Fechamento. Linhas condicionais sem espaço inicial (alinham com o layout aprovado).
    _vol_sust = " ✅ Volume sustentado (v-1 acima da média)\n" if vol_sustained else ""
    _pre_line = "✅ Acumulacao pre-spike confirmada\n" if pre_accum else ""
    if pd_type == "PUMP" and consec_up >= 3:
        _consec = f"🕯 {consec_up} velas altas consecutivas\n"
    elif pd_type == "DUMP" and consec_dn >= 3:
        _consec = f"🕯 {consec_dn} velas baixas consecutivas\n"
    else:
        _consec = ""
    _signals = "\n".join(f"• {s}" for s in signals[:5]) if signals else "• Volume anormal"

    msg = (
        f"{'🔴' if pd_type == 'DUMP' else '🚀'}{pd_type}{'🔴' if pd_type == 'DUMP' else '🚀'}"
        f" {sym} •  Conf {confidence} • {tf_display}| 📅 DAY TRADE\n\n"
        f"Intensidade: {intensity_icon} {intensity}\n"
        f"Score: {conf_bar} {confidence}/100\n\n"
        f"{SEP}\n"
        f"📊 Movimento:{big_candle_line}\n"
        f" Ultima vela:    {price_sign}{price_1c:.2f}%\n"
        f" Ultimas 3 vel:  {price_sign}{price_acc:.2f}%\n"
        f" Preco atual:    ${price:,.6g}\n"
        f"📈 Volume:\n"
        f" Volume:   {rel_vol:.1f}x acima da media\n"
        f" Aceleracao vol:  {vol_accel:.1f}x vs vela anterior\n"
        f" RSI atual: {rsi_val:.0f}{rsi_delta_str}\n"
        f"{_vol_sust}\n"
        f"{SEP}\n"
        f"🕯 Qualidade da vela:\n"
        f" Corpo/Range: {body_bar} {body_pct*100:.0f}%\n"
        f"{sust_icon} {sust_label}\n"
        f"{_pre_line}"
        f"{_consec}\n"
        f"{SEP}\n"
        f"⚡️ Sinais confirmados:\n{_signals}\n\n"
        f"{SEP}\n"
        f"💡 Recomendacao:\n{rec}"
    )

    # Garantia de CAIXA ÚNICA: a legenda de foto do Telegram tem limite de 1024 chars.
    if len(msg) > 1024:
        msg = msg[:1021].rstrip() + "..."

    # Gera gráfico com dados reais + níveis estimados para pump/dump
    _pd_is_long = pd_type == "PUMP"
    _pd_chart_sig = {
        "entry":      price,
        "stop_loss":  price * (0.97 if _pd_is_long else 1.03),
        "tp1":        price * (1.03 if _pd_is_long else 0.97),
        "tp2":        price * (1.06 if _pd_is_long else 0.94),
        "direction":  "LONG" if _pd_is_long else "SHORT",
        "confidence": float(confidence),
        "rr":         2.0,
        "conf_label": "Alta" if confidence >= 80 else "Média",
    }
    _pd_tf = alert.get("tf", "5m").lower()
    _pd_chart_bytes = await _generate_signal_chart(sym, _pd_tf, _pd_chart_sig)

    # Roteamento: VIP sempre, canal público máx 4/dia (intensidade FORTE ou EXTREMO)
    targets: list[str] = []
    if TELEGRAM_VIP_ID:
        targets.append(str(TELEGRAM_VIP_ID))
    if not targets:
        targets.append(str(TELEGRAM_CHAT_ID))
    if intensity in ("FORTE", "EXTREMO") and _channel_pd_ok():
        targets.append(str(TELEGRAM_CHANNEL_ID))

    async def _send_pd_to(chat_id: str):
        # CAIXA ÚNICA: chart + mensagem inteira como legenda (1 só bolha).
        if _pd_chart_bytes:
            return await _post_photo(_pd_chart_bytes, msg, chat_id=chat_id)
        # Sem chart: cai para texto puro.
        return await _post("sendMessage", {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})

    results = await asyncio.gather(
        *[_send_pd_to(t) for t in targets],
        return_exceptions=True,
    )
    return any(isinstance(r, dict) and r.get("ok") for r in results)


async def send_claude_brain_toggle_alert(enabled: bool, usage: dict, budget_usd: float = 0.0) -> bool:
    """
    Notificação quando Claude Brain é ativado ou desativado.
    usage: {"calls": int, "cost_usd": float, "input_tokens": int, "output_tokens": int}
    budget_usd: limite configurado (0 = sem limite)
    """
    if not _is_configured():
        return False

    cost      = float(usage.get("cost_usd", 0.0))
    calls     = int(usage.get("calls", 0))
    in_tok    = int(usage.get("input_tokens", 0))
    out_tok   = int(usage.get("output_tokens", 0))
    remaining = max(0.0, budget_usd - cost) if budget_usd > 0 else None

    from datetime import datetime
    import pytz
    brt = pytz.timezone("America/Sao_Paulo")
    now_str = datetime.now(brt).strftime("%d/%m %H:%M")

    if enabled:
        budget_line = ""
        if budget_usd > 0:
            bar_filled = int((cost / budget_usd) * 10) if budget_usd > 0 else 0
            bar_filled = min(bar_filled, 10)
            budget_bar = "█" * bar_filled + "░" * (10 - bar_filled)
            budget_line = (
                f"\n\n💰 *Budget da Sessão:*\n"
                f"  Gasto:    `${cost:.4f}` / `${budget_usd:.2f}`\n"
                f"  Barra:    `{budget_bar}` {bar_filled*10}%\n"
                f"  Restante: `${remaining:.4f}`"
            )
        else:
            budget_line = f"\n\n💰 *Custo até agora:* `${cost:.4f}` (sem limite definido)"

        msg = (
            f"🧠 *Claude Brain ATIVADO*  ⏰ {now_str}\n\n"
            f"Modelo: `claude-haiku-4-5`\n"
            f"Cada sinal será analisado pela API do Claude antes de executar.\n"
            f"{budget_line}"
        )
    else:
        cost_line = f"`${cost:.4f}`" if cost > 0 else "`$0.0000` (nenhuma análise realizada)"
        tok_line  = f"{in_tok:,} in / {out_tok:,} out" if (in_tok + out_tok) > 0 else "—"
        msg = (
            f"🔧 *Claude Brain DESATIVADO*  ⏰ {now_str}\n\n"
            f"*Resumo da Sessão:*\n"
            f"  Análises realizadas: `{calls}`\n"
            f"  Tokens:              `{tok_line}`\n"
            f"  Custo total:         {cost_line}\n\n"
            f"_Modo tradicional V4+V6 restaurado._"
        )

    result = await _post("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
    })
    return bool(result.get("ok"))


# ── Administração VIP ─────────────────────────────────────────────────────────

async def create_vip_invite_link(expire_hours: int = 720, member_limit: int = 1) -> str:
    """Cria link de convite único para o grupo VIP. Padrão: 1 uso, 30 dias."""
    if not TELEGRAM_VIP_ID:
        return ""
    import time as _time
    result = await _post("createChatInviteLink", {
        "chat_id":      str(TELEGRAM_VIP_ID),
        "expire_date":  int(_time.time()) + expire_hours * 3600,
        "member_limit": member_limit,
    })
    return result.get("result", {}).get("invite_link", "") if result.get("ok") else ""


async def remove_vip_member(user_id: int) -> bool:
    """Remove membro do grupo VIP (kick sem block permanente)."""
    if not TELEGRAM_VIP_ID:
        return False
    ban = await _post("banChatMember", {"chat_id": str(TELEGRAM_VIP_ID), "user_id": user_id})
    if ban.get("ok"):
        await _post("unbanChatMember", {
            "chat_id": str(TELEGRAM_VIP_ID), "user_id": user_id, "only_if_banned": True,
        })
        return True
    return False


async def get_vip_member_count() -> int:
    """Retorna número de membros no grupo VIP."""
    if not TELEGRAM_VIP_ID:
        return 0
    result = await _post("getChatMemberCount", {"chat_id": str(TELEGRAM_VIP_ID)})
    return result.get("result", 0) if result.get("ok") else 0


async def get_channel_subscriber_count() -> int:
    """Retorna número de inscritos no canal público."""
    if not TELEGRAM_CHANNEL_ID:
        return 0
    result = await _post("getChatMemberCount", {"chat_id": str(TELEGRAM_CHANNEL_ID)})
    return result.get("result", 0) if result.get("ok") else 0


async def send_pattern_analysis(symbol: str, mtf_results: dict,
                                bias_info: dict, chat_id: str = None) -> bool:
    """
    Envia análise de padrões MTF ao Telegram.
    Chamado pelo job_pattern_scan() em main.py após detecção de padrões relevantes.
    """
    if not _is_configured():
        return False

    from candle_pattern_engine import format_mtf_telegram
    msg = format_mtf_telegram(symbol, mtf_results, bias_info)

    dest = chat_id or TELEGRAM_CHAT_ID
    result = await _post("sendMessage", {
        "chat_id": dest,
        "text":    msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    })
    return bool(result.get("ok"))


async def send_pattern_approval_preview(symbol: str, mtf_results: dict,
                                        bias_info: dict) -> bool:
    """
    Envia preview da análise MTF para aprovação — inclui botão de feedback.
    Usado na primeira execução para demonstrar o novo recurso.
    """
    if not _is_configured():
        return False

    from candle_pattern_engine import format_mtf_telegram
    msg = format_mtf_telegram(symbol, mtf_results, bias_info)

    header = (
        "🆕 *NOVO RECURSO — ANÁLISE DE PADRÕES GRÁFICOS MTF*\n"
        "41 padrões implementados em 9 timeframes (3m→1S)\n"
        "─────────────────────────────────────\n"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ APROVADO — ATIVAR",   "callback_data": "pattern_approve"},
            {"text": "❌ AJUSTAR",              "callback_data": "pattern_reject"},
        ]]
    }

    result = await _post("sendMessage", {
        "chat_id":      TELEGRAM_CHAT_ID,
        "text":         header + msg,
        "parse_mode":   "Markdown",
        "reply_markup": json.dumps(keyboard),
        "disable_web_page_preview": True,
    })
    return bool(result.get("ok"))


def _startup_message() -> str:
    """Mensagem de inicio do bot — sessao e qualidade calculadas pela hora BRT."""
    from datetime import datetime, timedelta
    brt = datetime.utcnow() - timedelta(hours=3)
    h = brt.hour
    if   5 <= h < 9:   sess, ql, qi, qs = "Abertura Londres", "Alta",  "🟢", 8
    elif 9 <= h < 10:  sess, ql, qi, qs = "Pre-NY",           "Media", "🟡", 6
    elif 10 <= h < 14: sess, ql, qi, qs = "NY + overlap",     "Alta",  "🟢", 9
    elif 14 <= h < 17: sess, ql, qi, qs = "NY tarde",         "Media", "🟡", 6
    elif 17 <= h < 21: sess, ql, qi, qs = "Noite",            "Baixa", "🔴", 4
    elif 21 <= h < 24: sess, ql, qi, qs = "Asia",             "Media", "🟡", 5
    else:              sess, ql, qi, qs = "Madrugada",        "Baixa", "🔴", 3
    return (
        "🤖 Mestre dos Sinais acordou!\n\n"
        f"⏰ {brt.strftime('%H:%M')} BRT | Sessao: {sess} | Qualidade: {ql} {qi} ({qs}/10)\n\n"
        "Melhores janelas BRT:\n\n"
        "  🟢 05h-09h  Abertura Londres\n"
        "  🟢 10h-14h  NY + overlap\n"
        "  🟡 14h-17h  NY tarde\n"
        "  🔴 00h-05h  Liquidez baixa\n\n"
        "Motor V6+V4 ativo | Pump/Dump monitor ativo!"
    )


async def test_connection() -> bool:
    if not _is_configured():
        return False
    result = await _post("getMe", {})
    if result.get("ok"):
        name = result["result"].get("username", "?")
        print(f"[TELEGRAM] Conectado: @{name}")
        # NOTA: a mensagem de boot é enviada UMA única vez por
        # _send_startup_test_notification() no main.py ("TRADER 001 Online!").
        # Não enviar _startup_message() aqui para evitar msg duplicada no boot.
        return True
    return False


async def register_bot_commands():
    """Registra o menu '/' no Telegram (aparece ao digitar /)."""
    if not _is_configured():
        return
    commands = [
        {"command": "menu",        "description": "🎛️ Painel de botoes: escolher modo e perfil"},
        {"command": "status",      "description": "Estado atual: modo, saldo, trades abertos"},
        {"command": "resumo",      "description": "Snapshot rapido: saldo, PnL, sinais, modo"},
        {"command": "sinais",      "description": "Ultimos sinais gerados com scores"},
        {"command": "trades",      "description": "Trades abertos com PnL em tempo real"},
        {"command": "performance", "description": "Metricas: taxa de acerto, RR medio, resultado"},
        {"command": "pnl",         "description": "PnL detalhado: hoje, semana e mes"},
        {"command": "risco",       "description": "Metricas de risco: exposicao, sortino, pausa"},
        {"command": "posicao",     "description": "Detalhe de posicao — /posicao BTCUSDT"},
        {"command": "mercado",     "description": "Market Intelligence: vies, funding, OI, noticias"},
        {"command": "scan",        "description": "Forcar varredura de mercado agora"},
        {"command": "auto",        "description": "Modo: /auto on|off|grid|sinais [confirmar]"},
        {"command": "brain",       "description": "Claude Brain: /brain on|off|sinais|exec"},
        {"command": "modo",        "description": "Perfil: /modo normal|agressivo|conservador"},
        {"command": "banca",       "description": "Definir banca: /banca 500"},
        {"command": "ntrades",     "description": "Limite de trades por sessao: /ntrades 3"},
        {"command": "fechar",      "description": "Fechar posicao: /fechar BTCUSDT ou /fechar tudo"},
        {"command": "paper",       "description": "Paper trading: /paper on|off"},
        {"command": "macro",       "description": "Eventos macro: /macro pausar|continuar|status"},
        {"command": "grid",        "description": "Config grid: /grid alvo 10 | pares BTC ETH"},
        {"command": "ajuda",       "description": "Menu completo de comandos"},
    ]
    result = await _post("setMyCommands", {"commands": commands})
    if result.get("ok"):
        print(f"[TELEGRAM] {len(commands)} comandos registrados no menu '/'")
    else:
        print(f"[TELEGRAM] Aviso: nao foi possivel registrar comandos: {result.get('description','')}")
