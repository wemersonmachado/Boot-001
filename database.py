import aiosqlite
import json
from datetime import datetime, timedelta
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
    closed_at   TEXT,
    mode        TEXT,
    paper       INTEGER DEFAULT 1,
    execution_status TEXT,
    order_id    TEXT,
    signal_db_id INTEGER DEFAULT 0
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

-- Fase 4 (2026-06-29): memória de estratégias — vencedoras, rejeitadas e seus
-- parâmetros/métricas, para não retestar o que já falhou e reusar o que funcionou.
CREATE TABLE IF NOT EXISTS strategy_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    version         TEXT,
    status          TEXT    NOT NULL,   -- WINNING | REJECTED | TESTING
    timeframe_ideal TEXT,
    params_json      TEXT,              -- parâmetros otimizados (json)
    market_conditions TEXT,             -- condições em que funciona (texto livre)
    backtest_metrics_json  TEXT,        -- PF, WR, Sharpe, expectancy etc. (json)
    validation_metrics_json TEXT,       -- métricas em produção/out-of-sample (json)
    score_final     REAL,
    risk_avg_pct     REAL,
    best_sl_pct      REAL,
    best_tp_pct      REAL,
    best_rr          REAL,
    rejection_reason TEXT,
    trained_at       TEXT,
    created_at       TEXT    NOT NULL
);

-- Shadow book (2026-07-05): sinais BLOQUEADOS pelos filtros + o que TERIA
-- acontecido (TP ou SL primeiro). Único jeito de auditar se cada gate salva
-- dinheiro ou joga lucro fora — sem isso, só medimos o que foi enviado.
CREATE TABLE IF NOT EXISTS shadow_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    asset        TEXT,
    direction    TEXT,
    timeframe    TEXT,
    entry        REAL,
    sl           REAL,
    tp           REAL,
    block_reason TEXT,
    outcome      TEXT,            -- NULL=pendente | WIN | LOSS | TIMEOUT
    exit_price   REAL,
    pnl_pct      REAL,
    resolved_at  TEXT
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
CREATE INDEX IF NOT EXISTS idx_outcomes_signal    ON signal_outcomes(signal_db_id);
CREATE INDEX IF NOT EXISTS idx_telegram_signal    ON telegram_sent(signal_db_id, destination);
CREATE INDEX IF NOT EXISTS idx_profiles_asset    ON asset_profiles(asset, timeframe);
CREATE INDEX IF NOT EXISTS idx_cp_asset_ts       ON candlestick_patterns(asset, detected_at);
CREATE INDEX IF NOT EXISTS idx_cp_signal         ON candlestick_patterns(signal, detected_at);
CREATE INDEX IF NOT EXISTS idx_registry_status    ON strategy_registry(status, name);
CREATE INDEX IF NOT EXISTS idx_shadow_outcome     ON shadow_signals(outcome, ts);
CREATE INDEX IF NOT EXISTS idx_shadow_asset       ON shadow_signals(asset, timeframe, direction, ts);
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
        # Migração: garante a coluna 'mode' em bancos antigos (ignora se já existe).
        try:
            await db.execute("ALTER TABLE trades ADD COLUMN mode TEXT")
        except Exception:
            pass
        for stmt in (
            "ALTER TABLE trades ADD COLUMN paper INTEGER DEFAULT 1",
            "ALTER TABLE trades ADD COLUMN execution_status TEXT",
            "ALTER TABLE trades ADD COLUMN order_id TEXT",
            # FIX 2026-07-08: liga o trade de volta ao sinal que o originou —
            # sem isto, o resultado (ganho/perda) de trades Autônomo/Supervisionado
            # nunca linkava ao sinal original (só o canal Sinais linkava).
            "ALTER TABLE trades ADD COLUMN signal_db_id INTEGER DEFAULT 0",
            # 2026-07-11: ATR gravado na abertura — habilita trailing por ATR
            # em risk_manager.check_trailing_stop (trades antigos ficam com 0 =
            # trailing por ATR desligado pra eles, só a tabela de milestones).
            "ALTER TABLE trades ADD COLUMN atr REAL DEFAULT 0",
            # 2026-07-12 (pedido do usuário): sinais de pump/dump (engines
            # BREAKOUT/FADE) estavam diluindo a taxa de acerto dos sinais
            # estruturais — precisa metrificar separado. Coluna gravada em
            # signals e shadow_signals, calculada a partir do prefixo do
            # reason ('[BREAKOUT|...]'/'[FADE|...]'/'[CASCADE:BREAKOUT|...]'
            # etc.) — ver _classify_pump_dump() em main.py.
            "ALTER TABLE signals ADD COLUMN is_pump_dump INTEGER DEFAULT 0",
            "ALTER TABLE shadow_signals ADD COLUMN is_pump_dump INTEGER DEFAULT 0",
            # FIX 2026-07-13 (INCIDENTE CRÍTICO — dinheiro real): `trade_type`
            # era escrito em trade_dict (main.py, pairs_trading_engine.py,
            # job_update_trades) e usado para decidir lógica real (ex.:
            # pairs_trading_engine filtrava open_trades_db por
            # trade_type=="PAIRS_ARB" para saber se um par JÁ estava aberto),
            # mas a coluna NUNCA existiu na tabela `trades` e NUNCA foi
            # inserida em save_trade() — toda leitura de volta do banco
            # (get_open_trades) retornava trade_type ausente. Resultado: o
            # motor de arbitragem de pares SEMPRE via a lista de posições
            # ativas como vazia, nunca reconhecia pares já abertos, e abria
            # posições novas a cada ciclo de 15min indefinidamente — o bug de
            # ordenação de chave (ver pairs_trading_engine.py) era secundário;
            # esta é a causa raiz real e mais profunda do mesmo incidente.
            "ALTER TABLE trades ADD COLUMN trade_type TEXT DEFAULT ''",
            "ALTER TABLE shadow_signals ADD COLUMN block_code TEXT DEFAULT ''",
        ):
            try:
                await db.execute(stmt)
            except Exception:
                pass
        # Backfill (2026-07-13, mesmo incidente): trades PAIRS_ARB abertos ANTES
        # deste fix foram gravados com trade_type vazio (a coluna não existia).
        # Sem isto, mesmo com a coluna e o dedup corrigidos, essas posições já
        # abertas continuariam invisíveis para run_pairs_trading_cycle() e o
        # bot abriria pares NOVOS por cima delas de novo. Idempotente e seguro
        # (só corrige metadado no banco — nenhuma posição real é tocada).
        try:
            await db.execute(
                "UPDATE trades SET trade_type='PAIRS_ARB' "
                "WHERE (trade_type IS NULL OR trade_type='') AND reason LIKE 'PAIR:%'"
            )
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
                timeframe, score_json, opened_at, closed_at, mode, paper, execution_status, order_id,
                signal_db_id, atr, trade_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade["id"], trade["asset"], trade["direction"],
                trade["entry_price"], trade.get("exit_price"),
                trade["stop_loss"], trade["tp1"], trade["tp2"], trade["tp3"],
                trade["rr"], trade["leverage"], trade["size_usdt"],
                trade.get("pnl_pct", 0), trade.get("pnl_usdt", 0),
                trade["status"], trade["reason"], trade["confidence"],
                trade.get("timeframe", ""), trade.get("score_json", "{}"),
                trade["opened_at"], trade.get("closed_at"), trade.get("mode"),
                1 if trade.get("paper", True) else 0,
                trade.get("execution_status"),
                str(trade.get("order_id")) if trade.get("order_id") is not None else None,
                int(trade.get("signal_db_id") or 0),
                float(trade.get("atr") or 0.0),
                str(trade.get("trade_type") or ""),
            ),
        )
        await db.commit()


async def prune_old_rows(days: int = 30) -> dict:
    """Limpeza de retenção (2026-07-02): apaga linhas com mais de N dias das
    tabelas de alto volume (signals, market_snapshots, logs, telegram_sent).
    signal_outcomes NUNCA é apagada — é a base histórica do auto-tune e dos
    filtros de performance. trades também fica (volume baixo, valor alto)."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    deleted: dict = {}
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        for table, col in (
            ("signals", "timestamp"),
            ("market_snapshots", "timestamp"),
            ("logs", "timestamp"),
            ("telegram_sent", "sent_at"),
        ):
            cur = await db.execute(
                f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)
            )
            deleted[table] = cur.rowcount
        await db.commit()
    return deleted


async def get_recent_trade_stats(limit: int = 30) -> dict:
    """Estatística dos últimos N trades fechados (base do auto-tune do score).

    FIX 2026-07-09: trades fechados praticamente no zero a zero (ex.: SL
    movido pro entry) antes contavam no denominador SEM contar como vitória
    — isso diluía a taxa de acerto artificialmente (ex.: 57% -> 44% só por
    dois trades de $0.00 entrarem na amostra) e empurrava o auto-tune de
    volta pro aperto máximo mesmo com performance real estável. Breakeven
    (|pnl| < BREAKEVEN_EPS) agora é excluído tanto do numerador quanto do
    denominador — só entram na conta os trades que de fato ganharam ou
    perderam dinheiro."""
    BREAKEVEN_EPS = 0.01  # USDT — abaixo disso é considerado "zero a zero"
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        cur = await db.execute(
            "SELECT pnl_usdt FROM trades WHERE status='CLOSED' AND pnl_usdt IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT ?", (int(limit),)
        )
        rows = await cur.fetchall()
    decisive = [float(r[0] or 0) for r in rows if abs(float(r[0] or 0)) >= BREAKEVEN_EPS]
    n    = len(decisive)
    wins = sum(1 for pnl in decisive if pnl > 0)
    wr   = (wins / n * 100.0) if n > 0 else 0.0
    return {"n": n, "wins": wins, "win_rate": round(wr, 1)}


async def get_performance_window(start_iso: str = None, end_iso: str = None) -> dict:
    """TRAVA DE AUDITORIA (2026-07-11): estatísticas de trades fechados numa
    janela de tempo — usada para comparar performance ANTES vs DEPOIS de uma
    mudança de estratégia (ver /performance/strategy_audit em main.py e o
    marcador persistido `strategy_v2_started_at`). Sem isto, "a mudança deu
    resultado?" ficava só na impressão — agora é uma consulta objetiva."""
    q = "SELECT pnl_usdt, timeframe, asset FROM trades WHERE status='CLOSED' AND pnl_usdt IS NOT NULL"
    params = []
    if start_iso:
        q += " AND closed_at >= ?"
        params.append(start_iso)
    if end_iso:
        q += " AND closed_at < ?"
        params.append(end_iso)
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        cur = await db.execute(q, params)
        rows = await cur.fetchall()
    BREAKEVEN_EPS = 0.01
    decisive = [float(r[0] or 0) for r in rows if abs(float(r[0] or 0)) >= BREAKEVEN_EPS]
    n = len(decisive)
    wins = [p for p in decisive if p > 0]
    losses = [p for p in decisive if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n_total": len(rows),
        "n_decisive": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100.0, 1) if n else 0.0,
        "net_pnl_usdt": round(sum(decisive), 4),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if gross_win == 0 else float("inf")),
        "avg_trade_usdt": round(sum(decisive) / n, 4) if n else 0.0,
    }


async def get_recent_signal_stats(limit: int = 30) -> dict:
    """Acerto dos últimos N sinais SINAIS resolvidos (base do auto-tune do SINAIS).
    Considera WIN/LOSS (TIMEOUT entra como acerto se pnl_pct>0)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        cur = await db.execute(
            "SELECT outcome, pnl_pct FROM signal_outcomes "
            "WHERE outcome IS NOT NULL ORDER BY id DESC LIMIT ?", (int(limit),)
        )
        rows = await cur.fetchall()
    n    = len(rows)
    wins = sum(1 for o, p in rows
               if str(o).upper() == "WIN" or (str(o).upper() == "TIMEOUT" and float(p or 0) > 0))
    wr   = (wins / n * 100.0) if n > 0 else 0.0
    return {"n": n, "wins": wins, "win_rate": round(wr, 1)}


async def get_score_calibration_ok(limit: int = 30, min_n: int = 20) -> bool:
    """Checagem de sanidade do auto-tune (auditoria 06/07/2026): compara o WR
    da metade de MAIOR confiança vs a metade de MENOR confiança dos últimos
    `limit` sinais resolvidos. Se o score realmente prevê acerto, a metade de
    cima deve ganhar IGUAL ou MAIS que a de baixo. Se estiver invertido (achado
    real: dentro do mesmo engine, score 80+ teve WR 10.3% contra 66.7% do <70),
    apertar o corte de score só pioraria — então retorna False e o auto-tune
    não aperta (mas ainda pode afrouxar, que é sempre seguro)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        cur = await db.execute(
            """SELECT o.outcome, o.pnl_pct, s.confidence
               FROM signal_outcomes o JOIN signals s ON s.id = o.signal_db_id
               WHERE o.outcome IS NOT NULL ORDER BY o.id DESC LIMIT ?""",
            (int(limit),),
        )
        rows = await cur.fetchall()
    if len(rows) < min_n:
        return True  # amostra pequena demais — não trava o auto-tune de propósito
    rows_sorted = sorted(rows, key=lambda r: r[2] or 0)
    mid = len(rows_sorted) // 2
    lower, upper = rows_sorted[:mid], rows_sorted[mid:]

    def _wr(chunk):
        n = len(chunk)
        if n == 0:
            return 0.0
        w = sum(1 for o, p, _ in chunk
                if str(o).upper() == "WIN" or (str(o).upper() == "TIMEOUT" and float(p or 0) > 0))
        return w / n * 100.0

    return _wr(upper) >= _wr(lower)


async def get_unresolved_sinais_signals(max_age_h: float, limit: int = 200) -> list:
    """Sinais SINAIS (destino 'vip') ainda sem outcome — lidos direto do banco em vez
    de uma lista em memória, para que o rastreio sobreviva a reinícios do bot local.
    Janela de busca = max_age_h*4 (margem de segurança p/ não perder sinal nenhum)."""
    cutoff = (datetime.utcnow() - timedelta(hours=max_age_h * 4)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        cur = await db.execute(
            """SELECT s.id, s.asset, s.direction, s.entry, s.stop_loss, s.tp1,
                      s.timeframe, s.timestamp, s.reason
               FROM signals s
               JOIN telegram_sent t ON t.signal_db_id = s.id AND t.destination = 'vip'
               WHERE s.id NOT IN (SELECT signal_db_id FROM signal_outcomes
                                   WHERE signal_db_id IS NOT NULL)
                 AND s.entry > 0 AND s.stop_loss > 0 AND s.tp1 > 0
                 AND s.timestamp >= ?
               ORDER BY s.id DESC LIMIT ?""",
            (cutoff, int(limit)),
        )
        rows = await cur.fetchall()
    return [
        {
            "db_id": r[0], "asset": r[1], "direction": r[2],
            "entry": r[3], "sl": r[4], "tp": r[5],
            "timeframe": r[6] or "15m", "timestamp": r[7], "tags": (r[8] or "")[:120],
        }
        for r in rows
    ]


async def register_strategy(
    name: str, status: str, version: str = "1.0", timeframe_ideal: str = "",
    params: dict | None = None, market_conditions: str = "",
    backtest_metrics: dict | None = None, validation_metrics: dict | None = None,
    score_final: float = 0.0, risk_avg_pct: float = 0.0,
    best_sl_pct: float = 0.0, best_tp_pct: float = 0.0, best_rr: float = 0.0,
    rejection_reason: str = "",
) -> int:
    """Grava (ou atualiza histórico de) uma estratégia no registry — WINNING,
    REJECTED ou TESTING. Cada chamada cria um novo registro (histórico
    versionado), não faz upsert — para manter rastro de evolução/tentativas."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        cur = await db.execute(
            """INSERT INTO strategy_registry
               (name, version, status, timeframe_ideal, params_json, market_conditions,
                backtest_metrics_json, validation_metrics_json, score_final, risk_avg_pct,
                best_sl_pct, best_tp_pct, best_rr, rejection_reason, trained_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name, version, status, timeframe_ideal,
                json.dumps(params or {}), market_conditions,
                json.dumps(backtest_metrics or {}), json.dumps(validation_metrics or {}),
                score_final, risk_avg_pct, best_sl_pct, best_tp_pct, best_rr,
                rejection_reason, now, now,
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_strategy_registry(status: str | None = None) -> list[dict]:
    """Lista estratégias do registry, mais recentes primeiro. status=None retorna todas."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute(
                "SELECT * FROM strategy_registry WHERE status = ? ORDER BY id DESC", (status,)
            )
        else:
            cur = await db.execute("SELECT * FROM strategy_registry ORDER BY id DESC")
        rows = await cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("params_json", "backtest_metrics_json", "validation_metrics_json"):
            try:
                d[k] = json.loads(d[k]) if d[k] else {}
            except Exception:
                d[k] = {}
        out.append(d)
    return out


async def get_signal_kpi_summary(window_h: float = 24.0) -> dict:
    """KPIs de sinais para o dashboard, recortados nas últimas `window_h`
    horas: total enviado (signals + telegram_sent, qualquer destino) e
    positivos/negativos/% acerto resolvidos (signal_outcomes), ambos filtrados
    pelo horário ORIGINAL do sinal (não pelo horário em que foi resolvido).
    Atualiza sozinho conforme os sinais são resolvidos por TP1/TP2/SL.

    FIX 2026-07-08: antes só contava destino='vip' (canal Sinais) — agora
    mark_signal_executed() é chamado por TODOS os modos (Autônomo grava
    destino 'autonomous', Supervisionado 'supervised', Sinais 'vip'), então
    o card "Sinais Enviados (24h)" passa a refletir qualquer modo ativo, não
    só o canal Sinais. COUNT(DISTINCT s.id) já evita contar duas vezes um
    sinal que por acaso tenha mais de um registro em telegram_sent."""
    cutoff = (datetime.utcnow() - timedelta(hours=window_h)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        cur = await db.execute(
            """SELECT COUNT(DISTINCT s.id) FROM signals s
               JOIN telegram_sent t ON t.signal_db_id = s.id
               WHERE s.timestamp >= ?""",
            (cutoff,),
        )
        total_sent = (await cur.fetchone())[0] or 0
        # 2026-07-12: total enviado SEPARADO por pump/dump (engines BREAKOUT/
        # FADE) vs "limpo" (demais engines) — usuário reportou que o volume de
        # sinais pump/dump estava diluindo a % de acerto real dos sinais
        # estruturais no card do dashboard.
        cur = await db.execute(
            """SELECT COUNT(DISTINCT s.id) FROM signals s
               JOIN telegram_sent t ON t.signal_db_id = s.id
               WHERE s.timestamp >= ? AND s.is_pump_dump = 1""",
            (cutoff,),
        )
        pump_dump_sent = (await cur.fetchone())[0] or 0
        cur = await db.execute(
            """SELECT o.outcome, o.pnl_pct, s.is_pump_dump FROM signal_outcomes o
               JOIN signals s ON s.id = o.signal_db_id
               WHERE o.outcome IS NOT NULL AND s.timestamp >= ?""",
            (cutoff,),
        )
        rows = await cur.fetchall()
    def _is_pos(o, p): return str(o).upper() == "WIN" or (str(o).upper() == "TIMEOUT" and float(p or 0) > 0)
    def _is_neg(o, p): return str(o).upper() == "LOSS" or (str(o).upper() == "TIMEOUT" and float(p or 0) <= 0)
    positive = sum(1 for o, p, _pd in rows if _is_pos(o, p))
    negative = sum(1 for o, p, _pd in rows if _is_neg(o, p))
    resolved = positive + negative
    win_rate = (positive / resolved * 100.0) if resolved > 0 else 0.0
    pd_pos = sum(1 for o, p, pd in rows if pd and _is_pos(o, p))
    pd_neg = sum(1 for o, p, pd in rows if pd and _is_neg(o, p))
    pd_resolved = pd_pos + pd_neg
    pd_wr = (pd_pos / pd_resolved * 100.0) if pd_resolved > 0 else 0.0
    clean_pos = positive - pd_pos
    clean_neg = negative - pd_neg
    clean_resolved = clean_pos + clean_neg
    clean_wr = (clean_pos / clean_resolved * 100.0) if clean_resolved > 0 else 0.0
    return {
        "total_sent": int(total_sent),
        "positive":   positive,
        "negative":   negative,
        "resolved":   resolved,
        "win_rate_pct": round(win_rate, 1),
        "pump_dump_sent":       int(pump_dump_sent),
        "pump_dump_positive":   pd_pos,
        "pump_dump_negative":   pd_neg,
        "pump_dump_win_rate_pct": round(pd_wr, 1),
        "clean_sent":           int(total_sent) - int(pump_dump_sent),
        "clean_positive":       clean_pos,
        "clean_negative":       clean_neg,
        "clean_win_rate_pct":   round(clean_wr, 1),
    }


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
                score_total, reason, timeframe, executed, timestamp, is_pump_dump)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal["asset"], signal["direction"], signal["entry"],
                signal["stop_loss"], signal["tp1"], signal["tp2"], signal["tp3"],
                signal["rr"], signal["confidence"], signal["score_total"],
                signal["reason"], signal["timeframe"], signal.get("executed", 0),
                datetime.utcnow().isoformat(),
                1 if signal.get("is_pump_dump") else 0,
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


# ── Shadow book — sinais bloqueados e seus resultados hipotéticos ────────────

async def save_shadow_signal(asset: str, direction: str, timeframe: str,
                             entry: float, sl: float, tp: float,
                             block_reason: str, dedup_min: int = 30,
                             is_pump_dump: bool = False, block_code: str = "") -> bool:
    """Registra um sinal bloqueado pelos filtros para rastreio hipotético.
    Dedup: ignora se o MESMO asset+tf+direction já foi registrado (pendente)
    nos últimos dedup_min minutos — o scan roda a cada 60s e re-bloquearia o
    mesmo setup dezenas de vezes, inflando a amostra com duplicatas."""
    now = datetime.utcnow()
    cutoff = (now - timedelta(minutes=dedup_min)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM shadow_signals
               WHERE asset=? AND timeframe=? AND direction=? AND ts>=? LIMIT 1""",
            (asset, timeframe, direction, cutoff),
        ) as cur:
            if await cur.fetchone():
                return False
        await db.execute(
            """INSERT INTO shadow_signals
               (ts, asset, direction, timeframe, entry, sl, tp, block_reason, is_pump_dump, block_code)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (now.isoformat(), asset, direction, timeframe, entry, sl, tp, block_reason[:120],
             1 if is_pump_dump else 0, block_code[:40]),
        )
        await db.commit()
    return True


async def get_unresolved_shadow_signals(max_age_h: float, limit: int = 200) -> list:
    """Sinais bloqueados ainda sem resultado, dentro da janela de resolução."""
    cutoff = (datetime.utcnow() - timedelta(hours=max_age_h)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, ts, asset, direction, timeframe, entry, sl, tp, block_reason
               FROM shadow_signals WHERE outcome IS NULL AND ts>=?
               ORDER BY ts ASC LIMIT ?""",
            (cutoff, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def resolve_shadow_signal(row_id: int, outcome: str, exit_price: float, pnl_pct: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shadow_signals SET outcome=?, exit_price=?, pnl_pct=?, resolved_at=? WHERE id=?",
            (outcome, exit_price, pnl_pct, datetime.utcnow().isoformat(), row_id),
        )
        await db.commit()


async def get_recent_shadow_detail(limit: int = 30) -> list:
    """Últimos N sinais descartados (shadow book), linha a linha, para a tabela
    em tempo real do dashboard — ativo, motivo do descarte e resultado hipotético."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT asset, direction, timeframe, block_reason, outcome, pnl_pct, ts, is_pump_dump
               FROM shadow_signals ORDER BY id DESC LIMIT ?""",
            (int(limit),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_recent_sinais_detail(limit: int = 30) -> list:
    """Últimos N sinais SINAIS (destino 'vip') enviados, linha a linha, para a
    tabela em tempo real do dashboard — ativo, motivo/tag e resultado real."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _configure_db(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.asset, s.direction, s.timeframe, s.confidence, s.reason,
                      o.outcome, o.pnl_pct, s.timestamp, s.is_pump_dump
               FROM signals s
               JOIN telegram_sent t ON t.signal_db_id = s.id AND t.destination = 'vip'
               LEFT JOIN signal_outcomes o ON o.signal_db_id = s.id
               ORDER BY s.id DESC LIMIT ?""",
            (int(limit),),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_shadow_stats(hours: float = 24.0) -> dict:
    """Resumo das últimas N horas do shadow book: quantos sinais foram
    bloqueados, quantos TERIAM ganho/perdido, e o ranking por motivo de
    bloqueio — a nota de cada filtro (WR alto dos bloqueados = filtro ruim)."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT outcome, COUNT(*) n, COALESCE(AVG(pnl_pct),0) avg_pnl
               FROM shadow_signals WHERE ts>=? GROUP BY outcome""",
            (cutoff,),
        ) as cur:
            by_outcome = {(r["outcome"] or "PENDENTE"): {"n": r["n"], "avg_pnl": round(r["avg_pnl"], 2)}
                          for r in await cur.fetchall()}
        # 2026-07-12: mesmo recorte pump/dump (engines BREAKOUT/FADE) aplicado
        # ao shadow book — quantos DESCARTES eram pump/dump e o que teriam feito.
        async with db.execute(
            """SELECT outcome, COUNT(*) n
               FROM shadow_signals WHERE ts>=? AND is_pump_dump=1 GROUP BY outcome""",
            (cutoff,),
        ) as cur:
            pd_by_outcome = {(r["outcome"] or "PENDENTE"): r["n"] for r in await cur.fetchall()}
        pd_total  = sum(pd_by_outcome.values())
        pd_wins   = pd_by_outcome.get("WIN", 0)
        pd_losses = pd_by_outcome.get("LOSS", 0)
        pd_resolved = pd_wins + pd_losses
        async with db.execute(
            """SELECT block_reason,
                      COUNT(*) total,
                      SUM(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END) wins,
                      SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) losses
               FROM shadow_signals WHERE ts>=?
               GROUP BY block_reason ORDER BY total DESC LIMIT 10""",
            (cutoff,),
        ) as cur:
            reasons = []
            for r in await cur.fetchall():
                resolved = (r["wins"] or 0) + (r["losses"] or 0)
                reasons.append({
                    "reason":   r["block_reason"],
                    "total":    r["total"],
                    "wins":     r["wins"] or 0,
                    "losses":   r["losses"] or 0,
                    "would_wr": round((r["wins"] or 0) / resolved * 100, 1) if resolved else None,
                })
    total   = sum(v["n"] for v in by_outcome.values())
    wins    = by_outcome.get("WIN",  {}).get("n", 0)
    losses  = by_outcome.get("LOSS", {}).get("n", 0)
    resolved = wins + losses
    return {
        "hours":       hours,
        "blocked":     total,
        "resolved":    resolved,
        "would_win":   wins,
        "would_lose":  losses,
        "pending":     by_outcome.get("PENDENTE", {}).get("n", 0),
        "timeout":     by_outcome.get("TIMEOUT", {}).get("n", 0),
        "would_wr":    round(wins / resolved * 100, 1) if resolved else None,
        "by_reason":   reasons,
        "pump_dump_total":    pd_total,
        "pump_dump_wins":     pd_wins,
        "pump_dump_losses":   pd_losses,
        "pump_dump_would_wr": round(pd_wins / pd_resolved * 100, 1) if pd_resolved else None,
        "clean_total":        total - pd_total,
    }


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
    # Limite inferior de Wilson: reduz overfitting em amostras pequenas e
    # impede que uma sequência curta de vitórias aumente o score demais.
    import math
    z = 1.96
    denom = 1.0 + z * z / total
    centre = win_rate + z * z / (2.0 * total)
    spread = z * math.sqrt((win_rate * (1.0 - win_rate) + z * z / (4.0 * total)) / total)
    win_rate = max(0.0, (centre - spread) / denom)
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


async def upsert_daily_stats(pnl_usdt: float = 0.0, is_win: bool | None = None, signals_delta: int = 0):
    """Atualiza o resumo diario.

    `is_win is None` registra apenas sinais enviados, sem incrementar trades
    nem perdas. Isso evita classificar sinal SINAIS como trade perdido.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    now   = datetime.utcnow().isoformat()
    trade_delta = 1 if is_win is not None else 0
    win_delta   = 1 if is_win is True else 0
    loss_delta  = 1 if is_win is False else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO daily_stats (date, total_trades, wins, losses, pnl_usdt, signals_sent, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                   total_trades = total_trades + ?,
                   wins         = wins + ?,
                   losses       = losses + ?,
                   pnl_usdt     = pnl_usdt + ?,
                   signals_sent = signals_sent + ?,
                   win_rate     = CASE
                                      WHEN (total_trades + ?) > 0
                                      THEN (wins + ?) * 100.0 / (total_trades + ?)
                                      ELSE 0
                                  END,
                   updated_at   = ?""",
            (today, trade_delta, win_delta, loss_delta, pnl_usdt, signals_delta, now,
             trade_delta, win_delta, loss_delta, pnl_usdt, signals_delta,
             trade_delta, win_delta, trade_delta, now),
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
            "SELECT status, pnl_usdt, pnl_pct, exit_price FROM trades WHERE status='CLOSED'"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "profit_factor": 1, "max_drawdown": 0}

    valid_rows = [r for r in rows if r[1] is not None and r[2] is not None and r[3] is not None]
    if not valid_rows:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "profit_factor": 1, "max_drawdown": 0,
                "data_quality": "no_realized_pnl"}

    wins         = [r[1] for r in valid_rows if r[1] > 0]
    losses       = [r[1] for r in valid_rows if r[1] <= 0]
    total_pnl    = sum(r[1] for r in valid_rows)
    gross_profit = sum(wins)   if wins   else 0
    gross_loss   = abs(sum(losses)) if losses else 0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999

    cumulative = []; running = 0; peak = 0; max_dd = 0
    for r in valid_rows:
        running += r[1]
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd
        cumulative.append(running)

    return {
        "total":        len(valid_rows),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(len(wins) / len(valid_rows) * 100, 1) if valid_rows else 0,
        "total_pnl":    round(total_pnl, 2),
        "profit_factor": profit_factor,
        "max_drawdown": round(max_dd, 2),
        "equity_curve": cumulative[-50:],
        "data_quality": "ok",
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
