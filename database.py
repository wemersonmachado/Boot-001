import aiosqlite
import json
from datetime import datetime
from config import DB_PATH


# ── Schema base ───────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT,
    event_type TEXT,
    message    TEXT,
    data_json  TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id          TEXT PRIMARY KEY,
    asset       TEXT,
    direction   TEXT,
    entry_price REAL,
    exit_price  REAL,
    stop_loss   REAL,
    tp1 REAL, tp2 REAL, tp3 REAL,
    rr          REAL,
    leverage    INTEGER,
    size_usdt   REAL,
    pnl_pct     REAL DEFAULT 0,
    pnl_usdt    REAL DEFAULT 0,
    status      TEXT DEFAULT 'OPEN',
    reason      TEXT,
    confidence  REAL,
    timeframe   TEXT,
    score_json  TEXT,
    opened_at   TEXT,
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset       TEXT,
    direction   TEXT,
    entry       REAL,
    stop_loss   REAL,
    tp1 REAL, tp2 REAL, tp3 REAL,
    rr          REAL,
    confidence  REAL,
    score_total REAL,
    reason      TEXT,
    timeframe   TEXT,
    executed    INTEGER DEFAULT 0,
    timestamp   TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    data_json TEXT,
    timestamp TEXT
);

-- ── Adaptive Memory tables ────────────────────────────────────────────────────

-- Performance histórica por ativo × timeframe × hora × dia da semana
CREATE TABLE IF NOT EXISTS asset_profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset         TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL,
    hour_utc      INTEGER NOT NULL,
    weekday       INTEGER NOT NULL,
    total         INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    total_pnl_pct REAL    DEFAULT 0,
    last_updated  TEXT,
    UNIQUE(asset, timeframe, hour_utc, weekday)
);

-- Padrões de confluência (tags) → win rate por ativo
CREATE TABLE IF NOT EXISTS confluence_patterns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT NOT NULL,
    tag_combo    TEXT NOT NULL,
    total        INTEGER DEFAULT 0,
    wins         INTEGER DEFAULT 0,
    avg_pnl_pct  REAL    DEFAULT 0,
    last_updated TEXT,
    UNIQUE(asset, tag_combo)
);

-- Resultado de cada sinal enviado → base do aprendizado
CREATE TABLE IF NOT EXISTS signal_outcomes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_db_id INTEGER,
    asset        TEXT,
    direction    TEXT,
    timeframe    TEXT,
    entry        REAL,
    exit_price   REAL,
    pnl_pct      REAL,
    outcome      TEXT,
    hour_utc     INTEGER,
    weekday      INTEGER,
    tags         TEXT,
    rsi_val      REAL,
    recorded_at  TEXT
);

-- Resumo diário de performance
CREATE TABLE IF NOT EXISTS daily_stats (
    date          TEXT PRIMARY KEY,
    total_trades  INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    win_rate      REAL    DEFAULT 0,
    pnl_usdt      REAL    DEFAULT 0,
    signals_sent  INTEGER DEFAULT 0,
    updated_at    TEXT
);

-- Registro de quais sinais foram para qual destino Telegram
CREATE TABLE IF NOT EXISTS telegram_sent (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_db_id INTEGER,
    asset        TEXT,
    destination  TEXT,
    sent_at      TEXT
);

-- Padrões de candlestick detectados por ativo × timeframe (MTF)
CREATE TABLE IF NOT EXISTS candlestick_patterns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT    NOT NULL,
    timeframe    TEXT    NOT NULL,
    pattern_name TEXT    NOT NULL,
    pattern_pt   TEXT,
    signal       TEXT,            -- bullish | bearish | neutral
    strength     INTEGER,         -- 1=fraco | 2=moderado | 3=forte
    bias         TEXT,            -- viés MTF global no momento da detecção
    detected_at  TEXT    NOT NULL
);

-- Configurações persistentes do bot (ex: Claude Brain ativo)
CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT,
    updated_at   TEXT
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_signals_ts        ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_asset     ON signals(asset);
CREATE INDEX IF NOT EXISTS idx_signals_executed  ON signals(executed);
CREATE INDEX IF NOT EXISTS idx_trades_status     ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_opened     ON trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_asset      ON trades(asset);
CREATE INDEX IF NOT EXISTS idx_logs_type         ON logs(event_type);
CREATE INDEX IF NOT EXISTS idx_logs_ts           ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_outcomes_asset    ON signal_outcomes(asset);
CREATE INDEX IF NOT EXISTS idx_profiles_asset    ON asset_profiles(asset, timeframe);
CREATE INDEX IF NOT EXISTS idx_cp_asset_ts       ON candlestick_patterns(asset, detected_at);
CREATE INDEX IF NOT EXISTS idx_cp_signal         ON candlestick_patterns(signal, detected_at);
"""


async def _configure_db(db) -> None:
    """
    Aplica PRAGMAs de performance em toda conexão aberta.
    WAL permite leituras e escritas concorrentes sem bloqueio mútuo.
    """
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")   # mais rápido; WAL garante durabilidade
    await db.execute("PRAGMA cache_size=10000")      # ~10MB de cache em memória
    await db.execute("PRAGMA temp_store=MEMORY")     # tabelas temporárias em RAM
    await db.execute("PRAGMA mmap_size=268435456")   # memory-mapped I/O 256MB


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        for stmt in _DDL.strip().split(";"):
            s = stmt.strip()
            if s:
                await db.execute(s)
        for stmt in _INDEXES.strip().split(";"):
            s = stmt.strip()
            if s:
                try:
                    await db.execute(s)
                except Exception:
                    pass
        await db.commit()


# ── Trades ────────────────────────────────────────────────────────────────────

async def save_trade(trade: dict):
    """Insere ou substitui trade completo (usado na abertura)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        await db.execute(
            """INSERT OR REPLACE INTO trades
               (id, asset, direction, entry_price, exit_price, stop_loss, tp1, tp2, tp3,
                rr, leverage, size_usdt, pnl_pct, pnl_usdt, status, reason, confidence,
                timeframe, score_json, opened_at, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade["id"], trade["asset"], trade["direction"],
                trade["entry_price"], trade.get("exit_price"),
                trade["stop_loss"], trade["tp1"], trade["tp2"], trade["tp3"],
                trade["rr"], trade["leverage"], trade["size_usdt"],
                trade.get("pnl_pct", 0), trade.get("pnl_usdt", 0),
                trade["status"], trade["reason"], trade["confidence"],
                trade.get("timeframe", ""), trade.get("score_json", "{}"),
                trade["opened_at"], trade.get("closed_at"),
            ),
        )
        await db.commit()


async def update_trade_close(trade_id: str, exit_price: float,
                              pnl_usdt: float, pnl_pct: float):
    """FIX: atualiza exit_price e PnL real no fechamento do trade."""
    closed_at = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        await db.execute(
            """UPDATE trades
               SET status='CLOSED', exit_price=?, pnl_usdt=?, pnl_pct=?, closed_at=?
               WHERE id=?""",
            (exit_price, pnl_usdt, pnl_pct, closed_at, trade_id),
        )
        await db.commit()


# ── Signals ───────────────────────────────────────────────────────────────────

async def save_signal(signal: dict) -> int:
    """Salva sinal e retorna o ID gerado para rastreamento posterior."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        cur = await db.execute(
            """INSERT INTO signals
               (asset, direction, entry, stop_loss, tp1, tp2, tp3, rr, confidence,
                score_total, reason, timeframe, executed, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal["asset"], signal["direction"], signal["entry"],
                signal["stop_loss"], signal["tp1"], signal["tp2"], signal["tp3"],
                signal["rr"], signal["confidence"], signal["score_total"],
                signal["reason"], signal["timeframe"], signal.get("executed", 0),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def mark_signal_executed(signal_db_id: int, asset: str = "",
                                destination: str = "personal"):
    """Marca sinal como enviado e registra o destino Telegram."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE signals SET executed=1 WHERE id=?", (signal_db_id,)
        )
        await db.execute(
            "INSERT INTO telegram_sent (signal_db_id, asset, destination, sent_at) VALUES (?,?,?,?)",
            (signal_db_id, asset, destination, now),
        )
        await db.commit()


# ── Adaptive Memory ───────────────────────────────────────────────────────────

async def upsert_asset_profile(asset: str, timeframe: str,
                                hour_utc: int, weekday: int,
                                is_win: bool, pnl_pct: float):
    """Atualiza o perfil de performance do ativo para a hora/dia específicos."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO asset_profiles (asset, timeframe, hour_utc, weekday,
                   total, wins, total_pnl_pct, last_updated)
               VALUES (?,?,?,?,1,?,?,?)
               ON CONFLICT(asset, timeframe, hour_utc, weekday) DO UPDATE SET
                   total         = total + 1,
                   wins          = wins + ?,
                   total_pnl_pct = total_pnl_pct + ?,
                   last_updated  = ?""",
            (asset, timeframe, hour_utc, weekday, 1 if is_win else 0, pnl_pct, now,
             1 if is_win else 0, pnl_pct, now),
        )
        await db.commit()


async def upsert_confluence_pattern(asset: str, tag_combo: str,
                                     is_win: bool, pnl_pct: float):
    """Atualiza estatísticas de uma combinação de confluências."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO confluence_patterns (asset, tag_combo, total, wins, avg_pnl_pct, last_updated)
               VALUES (?,?,1,?,?,?)
               ON CONFLICT(asset, tag_combo) DO UPDATE SET
                   total       = total + 1,
                   wins        = wins + ?,
                   avg_pnl_pct = (avg_pnl_pct * total + ?) / (total + 1),
                   last_updated = ?""",
            (asset, tag_combo, 1 if is_win else 0, pnl_pct, now,
             1 if is_win else 0, pnl_pct, now),
        )
        await db.commit()


async def record_signal_outcome(signal_db_id: int, asset: str, direction: str,
                                  timeframe: str, entry: float, exit_price: float,
                                  pnl_pct: float, outcome: str,
                                  tags: str = "", rsi_val: float = 0.0):
    """Registra o resultado final de um sinal enviado."""
    now = datetime.utcnow().isoformat()
    dt  = datetime.utcnow()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO signal_outcomes
               (signal_db_id, asset, direction, timeframe, entry, exit_price,
                pnl_pct, outcome, hour_utc, weekday, tags, rsi_val, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_db_id, asset, direction, timeframe, entry, exit_price,
             pnl_pct, outcome, dt.hour, dt.weekday(), tags, rsi_val, now),
        )
        await db.commit()


async def get_score_adjustment(asset: str, timeframe: str,
                                hour_utc: int, weekday: int) -> float:
    """
    Retorna ajuste de score baseado na memória histórica do ativo.
    Range: -15 a +15 pontos. Requer mínimo 5 trades para ajustar.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT total, wins, total_pnl_pct
               FROM asset_profiles
               WHERE asset=? AND timeframe=? AND hour_utc=? AND weekday=?""",
            (asset, timeframe, hour_utc, weekday),
        ) as cur:
            row = await cur.fetchone()

    if not row or row[0] < 5:
        return 0.0

    total, wins, total_pnl = row
    win_rate = wins / total
    avg_pnl  = total_pnl / total

    # Ajuste baseado em win rate histórico nessa condição
    if win_rate >= 0.70:
        boost = +12.0
    elif win_rate >= 0.60:
        boost = +7.0
    elif win_rate >= 0.50:
        boost = +3.0
    elif win_rate >= 0.40:
        boost = -5.0
    elif win_rate >= 0.30:
        boost = -10.0
    else:
        boost = -15.0

    # Penalidade extra se PnL médio negativo mesmo com WR ok
    if avg_pnl < -1.0:
        boost -= 5.0

    return max(-15.0, min(15.0, boost))


async def get_asset_stats(asset: str) -> dict:
    """Retorna resumo de performance do ativo para exibir no Telegram."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT SUM(total), SUM(wins), AVG(total_pnl_pct / NULLIF(total,0))
               FROM asset_profiles WHERE asset=?""",
            (asset,),
        ) as cur:
            row = await cur.fetchone()

        async with db.execute(
            """SELECT tag_combo, total, wins, avg_pnl_pct
               FROM confluence_patterns WHERE asset=? AND total >= 3
               ORDER BY (wins * 1.0 / total) DESC LIMIT 3""",
            (asset,),
        ) as cur:
            patterns = await cur.fetchall()

    total = row[0] or 0
    wins  = row[1] or 0
    return {
        "asset":    asset,
        "total":    total,
        "wins":     wins,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "avg_pnl":  round(row[2] or 0, 2),
        "top_patterns": [
            {"combo": p[0], "total": p[1], "wr": round(p[2]/p[1]*100, 1), "avg_pnl": round(p[3], 2)}
            for p in patterns
        ],
    }


async def upsert_daily_stats(pnl_usdt: float, is_win: bool, signals_delta: int = 0):
    """Atualiza o resumo do dia atual."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    now   = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO daily_stats (date, total_trades, wins, losses, pnl_usdt, signals_sent, updated_at)
               VALUES (?,1,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                   total_trades = total_trades + 1,
                   wins         = wins + ?,
                   losses       = losses + ?,
                   pnl_usdt     = pnl_usdt + ?,
                   signals_sent = signals_sent + ?,
                   win_rate     = (wins + ?) * 100.0 / (total_trades + 1),
                   updated_at   = ?""",
            (today, 1 if is_win else 0, 0 if is_win else 1, pnl_usdt, signals_delta, now,
             1 if is_win else 0, 0 if is_win else 1, pnl_usdt, signals_delta,
             1 if is_win else 0, now),
        )
        await db.commit()


# ── Existing functions (unchanged) ────────────────────────────────────────────

async def get_open_trades() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM trades WHERE status='OPEN'") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_all_trades(limit: int = 100) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_performance_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, pnl_usdt, pnl_pct FROM trades WHERE status='CLOSED'"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "profit_factor": 1, "max_drawdown": 0}

    wins         = [r[1] for r in rows if r[1] > 0]
    losses       = [r[1] for r in rows if r[1] <= 0]
    total_pnl    = sum(r[1] for r in rows)
    gross_profit = sum(wins)   if wins   else 0
    gross_loss   = abs(sum(losses)) if losses else 0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999

    cumulative = []; running = 0; peak = 0; max_dd = 0
    for r in rows:
        running += r[1]
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd
        cumulative.append(running)

    return {
        "total":        len(rows),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(len(wins) / len(rows) * 100, 1) if rows else 0,
        "total_pnl":    round(total_pnl, 2),
        "profit_factor": profit_factor,
        "max_drawdown": round(max_dd, 2),
        "equity_curve": cumulative[-50:],
    }


async def log_event(event_type: str, message: str, data: dict = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs (timestamp, event_type, message, data_json) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), event_type, message,
             json.dumps(data) if data else None),
        )
        await db.commit()


async def save_snapshot(data: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO market_snapshots (data_json, timestamp) VALUES (?,?)",
            (json.dumps(data), datetime.utcnow().isoformat()),
        )
        await db.commit()


async def daily_signal_report(date_str: str = None) -> dict:
    from datetime import timezone
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signals WHERE timestamp LIKE ? ORDER BY timestamp ASC",
            (f"{date_str}%",),
        ) as cur:
            signals = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT * FROM trades WHERE opened_at LIKE ? AND status='CLOSED'",
            (f"{date_str}%",),
        ) as cur:
            closed_trades = {r["asset"]: dict(r) for r in await cur.fetchall()}

        async with db.execute(
            "SELECT * FROM trades WHERE opened_at LIKE ? AND status='OPEN'",
            (f"{date_str}%",),
        ) as cur:
            open_trades = {r["asset"]: dict(r) for r in await cur.fetchall()}

    if not signals:
        return {"date": date_str, "total": 0, "message": "Nenhum sinal registrado hoje."}

    price_cache: dict = {}
    try:
        from klines_cache import get_klines_cached
        assets_to_check = {
            s["asset"] for s in signals
            if s["asset"] not in closed_trades and s["asset"] not in open_trades
        }
        import asyncio as _asyncio
        async def _get_price(sym):
            try:
                df = await get_klines_cached(sym, "5m", limit=5)
                return sym, float(df["close"].iloc[-1]) if df is not None and len(df) > 0 else None
            except Exception:
                return sym, None
        prices = await _asyncio.gather(*[_get_price(a) for a in assets_to_check])
        price_cache = {sym: px for sym, px in prices if px is not None}
    except Exception:
        pass

    results = []; wins = 0; losses = 0; open_count = 0; unknown = 0
    for sig in signals:
        asset = sig["asset"]; direction = sig["direction"]
        entry = sig["entry"]; tp1 = sig["tp1"]; sl = sig["stop_loss"]
        conf = sig["confidence"]; tf = sig["timeframe"]

        if asset in closed_trades:
            t = closed_trades[asset]; pnl = t.get("pnl_pct", 0)
            outcome = "WIN" if pnl > 0 else "LOSS"
            wins += 1 if pnl > 0 else 0
            losses += 0 if pnl > 0 else 1
            results.append({"asset": asset, "direction": direction, "tf": tf,
                             "confidence": conf, "outcome": outcome,
                             "pnl_pct": round(pnl, 2), "source": "trade_real"})
            continue

        if asset in open_trades:
            open_count += 1
            results.append({"asset": asset, "direction": direction, "tf": tf,
                             "confidence": conf, "outcome": "OPEN",
                             "pnl_pct": None, "source": "trade_aberto"})
            continue

        current_price = price_cache.get(asset)
        if current_price is None or entry <= 0:
            unknown += 1
            results.append({"asset": asset, "direction": direction, "tf": tf,
                             "confidence": conf, "outcome": "?",
                             "pnl_pct": None, "source": "sem_preco"})
            continue

        is_long = "LONG" in str(direction).upper()
        hit_tp1 = current_price >= tp1 if is_long else current_price <= tp1
        hit_sl  = current_price <= sl  if is_long else current_price >= sl
        move_pct = ((current_price - entry) / entry * 100) if is_long else \
                   ((entry - current_price) / entry * 100)

        if hit_tp1:   outcome = "WIN";  wins   += 1
        elif hit_sl:  outcome = "LOSS"; losses += 1
        else:         outcome = f"EM ABERTO ({move_pct:+.1f}%)"; open_count += 1

        results.append({"asset": asset, "direction": direction, "tf": tf,
                         "confidence": conf, "outcome": outcome,
                         "pnl_pct": round(move_pct, 2), "source": "estimado",
                         "current_price": current_price, "tp1": tp1, "sl": sl})

    total_decided = wins + losses
    return {
        "date": date_str, "total": len(signals),
        "wins": wins, "losses": losses, "open": open_count, "unknown": unknown,
        "win_rate": round(wins / total_decided * 100, 1) if total_decided > 0 else 0,
        "results": results,
    }


# ── Candlestick Patterns ───────────────────────────────────────────────────────

async def save_candle_patterns(asset: str, mtf_results: dict, bias: str):
    """
    Salva padrões MTF detectados no DB.
    mtf_results: dict[timeframe, List[CandlePattern]]
    """
    now = datetime.utcnow().isoformat()
    rows = []
    for tf, patterns in mtf_results.items():
        for p in patterns:
            rows.append((asset, tf, p.name, p.name_pt, p.signal, p.strength, bias, now))
    if not rows:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO candlestick_patterns
               (asset, timeframe, pattern_name, pattern_pt, signal, strength, bias, detected_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )
        await db.commit()


async def get_recent_candle_patterns(asset: str, limit: int = 50) -> list:
    """Retorna os padrões mais recentes detectados para um ativo."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM candlestick_patterns
               WHERE asset = ?
               ORDER BY detected_at DESC LIMIT ?""",
            (asset, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ── Persistência de Settings ──────────────────────────────────────────────────

async def save_setting(key: str, value: str):
    """Salva ou atualiza uma configuração genérica no banco de dados."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        await db.execute(
            """INSERT OR REPLACE INTO settings (key, value, updated_at)
               VALUES (?, ?, ?)""",
            (key, value, now)
        )
        await db.commit()


async def get_setting(key: str, default: str = None) -> str:
    """Recupera o valor de uma configuração no banco de dados."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row["value"]
    return default
