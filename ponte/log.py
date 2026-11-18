"""Log simples em ponte.log (na pasta da Ponte) para diagnosticar problemas."""
from __future__ import annotations

import os
import threading
import time
import traceback

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ponte.log")
_lock = threading.Lock()
MAX_SIZE = 512 * 1024


def log(msg: str) -> None:
    line = f"{time.strftime('%d/%m %H:%M:%S')} [{threading.current_thread().name}] {msg}\n"
    try:
        with _lock:
            if os.path.exists(_PATH) and os.path.getsize(_PATH) > MAX_SIZE:
                os.replace(_PATH, _PATH + ".old")
            with open(_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def describe(e: BaseException) -> str:
    """Mensagem curta e nunca vazia (AssertionError, por exemplo, vem sem texto)."""
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def log_exc(context: str, e: BaseException) -> str:
    """Registra o traceback completo e devolve a mensagem curta para a interface."""
    log(f"{context}: {describe(e)}\n{traceback.format_exc()}")
    return f"{context}: {describe(e)}"
