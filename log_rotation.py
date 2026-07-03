# -*- coding: utf-8 -*-
"""
Rotação de log do Trader 001 (2026-07-02).

Antes: todo o output (prints + uvicorn) ia pro bot_console.log via redirect do
shell (>>), que crescia sem limite. Agora, `install()` — chamado no topo do
main.py — redireciona stdout/stderr para logs/bot.log com rotação automática
(10MB x 5 backups = máx ~50MB pra sempre).

O redirect externo (>> bot_console.log) continua inofensivo: só captura o que
sai antes do install() (praticamente nada).
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_PATH = os.path.join(LOG_DIR, "bot.log")

# Erros internos do logging (ex: rename bloqueado por leitor externo no Windows)
# não podem explodir nem recursar no nosso stderr substituído.
logging.raiseExceptions = False


class _RotatingStream:
    """File-like que alimenta um RotatingFileHandler linha a linha.
    Substitui sys.stdout/sys.stderr — o handler cuida de lock e rollover."""

    def __init__(self, handler: RotatingFileHandler, echo=None):
        self._h = handler
        self._buf = ""
        self._echo = echo  # stream original (console), se quisermos eco

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        if self._echo is not None:
            try:
                self._echo.write(s)
            except Exception:
                pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            try:
                rec = logging.LogRecord(
                    "bot", logging.INFO, "", 0, line, None, None
                )
                self._h.emit(rec)
            except Exception:
                pass
        return len(s)

    def flush(self):
        try:
            self._h.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"

    def fileno(self):
        # uvicorn/asyncio às vezes pedem fileno; devolve o do arquivo atual.
        return self._h.stream.fileno()


_installed = False


def install(max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> None:
    """Redireciona stdout/stderr pro log rotativo. Idempotente."""
    global _installed
    if _installed:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=max_bytes, backupCount=backups,
        encoding="utf-8", delay=False,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    stream = _RotatingStream(handler)
    sys.stdout = stream
    sys.stderr = stream
    _installed = True
    print(f"[LOG] Rotação ativa: {LOG_PATH} ({max_bytes // 1048576}MB x {backups})")
