"""Privilégios de administrador.

O Windows não deixa um programa comum controlar janelas de administrador
(proteção chamada UIPI): o Gerenciador de Tarefas, instaladores, Regedit…
Mouse e teclado enviados pela Ponte simplesmente não chegam nelas. Para
controlá-las, a Ponte do PC acessado precisa rodar como administrador.
"""
from __future__ import annotations

import os
import sys

from .log import log, log_exc


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(extra_args: list[str] | None = None) -> bool:
    """Abre outra Ponte como administrador (o Windows pergunta). True se abriu."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        args = " ".join(f'"{a}"' for a in [main, "--elevated", *(extra_args or [])])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args,
                                                 os.path.dirname(main), 1)
        ok = rc > 32
        log("Ponte reaberta como administrador" if ok else f"reabrir como administrador cancelado ({rc})")
        return ok
    except Exception as e:  # noqa: BLE001
        log_exc("não consegui reabrir como administrador", e)
        return False


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TokenElevation = 20

    def foreground_is_elevated() -> tuple[bool, int]:
        """(a janela em primeiro plano é de administrador?, hwnd dela)."""
        hwnd = _user32.GetForegroundWindow() or 0
        if not hwnd:
            return False, 0
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return False, hwnd
        proc = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not proc:
            return True, hwnd  # nem consultar deixa: processo protegido/administrador
        try:
            token = wintypes.HANDLE()
            if not _advapi32.OpenProcessToken(proc, TOKEN_QUERY, ctypes.byref(token)):
                return True, hwnd
            try:
                elevated = wintypes.DWORD()
                size = wintypes.DWORD()
                if not _advapi32.GetTokenInformation(token, TokenElevation, ctypes.byref(elevated),
                                                     ctypes.sizeof(elevated), ctypes.byref(size)):
                    return False, hwnd
                return bool(elevated.value), hwnd
            finally:
                _kernel32.CloseHandle(token)
        finally:
            _kernel32.CloseHandle(proc)
else:
    def foreground_is_elevated() -> tuple[bool, int]:
        return False, 0
