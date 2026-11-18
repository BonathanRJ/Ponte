"""Captura de atalhos do Windows para a conexão remota.

Atalhos como Alt+Tab, tecla Windows e Ctrl+Shift+Esc são tratados pelo próprio
Windows antes de chegarem a qualquer programa — por isso a Ponte não os via.
Um gancho de teclado de baixo nível (WH_KEYBOARD_LL) recebe TODAS as teclas
antes do Windows. Enquanto uma janela de conexão da Ponte estiver em primeiro
plano, as teclas são enviadas ao PC remoto e "engolidas" aqui; com qualquer
outra janela em foco (ou a Ponte minimizada), passam direto e funcionam no PC
local como sempre.

O gancho só existe enquanto há uma janela de conexão aberta, e é chamado na
thread da interface (que já processa as mensagens do Windows).
"""
from __future__ import annotations

import sys
from typing import Callable

from .log import log, log_exc

KeyCallback = Callable[[int, bool], None]  # (código virtual da tecla, pressionada?)


class KeyboardGrabber:
    """Liga janelas (por HWND) a um destino para as teclas."""

    def __init__(self):
        self.targets: dict[int, KeyCallback] = {}
        self._hook = None
        self._proc = None

    def add(self, hwnd: int, callback: KeyCallback) -> None:
        self.targets[int(hwnd)] = callback
        self._ensure_hook()

    def remove(self, hwnd: int) -> None:
        self.targets.pop(int(hwnd), None)
        if not self.targets:
            self._unhook()

    def handle(self, foreground_hwnd: int, vk: int, down: bool, injected: bool = False) -> bool:
        """Decide o destino de uma tecla. True = foi para o PC remoto (engolir aqui)."""
        if injected:
            return False  # teclas geradas por programas não são do usuário
        target = self.targets.get(int(foreground_hwnd or 0))
        if target is None:
            return False  # outra janela em foco (ou Ponte minimizada): tecla fica local
        target(vk, down)
        return True

    @property
    def active(self) -> bool:
        return self._hook is not None

    # implementação: só no Windows
    def _ensure_hook(self) -> None:
        if self._hook is not None or sys.platform != "win32":
            return
        try:
            self._install()
            log("captura de atalhos do Windows ligada")
        except Exception as e:  # noqa: BLE001
            self._hook = None
            log_exc("não consegui ligar a captura de atalhos", e)

    def _unhook(self) -> None:
        if self._hook is not None and sys.platform == "win32":
            try:
                _user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            log("captura de atalhos do Windows desligada")
        self._hook = None
        self._proc = None

    if sys.platform == "win32":
        def _install(self) -> None:
            def proc(n_code, w_param, l_param):
                try:
                    if n_code == HC_ACTION:
                        k = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        if self.handle(_user32.GetForegroundWindow() or 0, int(k.vkCode),
                                       w_param in (WM_KEYDOWN, WM_SYSKEYDOWN),
                                       bool(k.flags & LLKHF_INJECTED)):
                            return 1  # não deixa o Windows local tratar a tecla
                except Exception:  # noqa: BLE001  (nunca deixar exceção sair do gancho)
                    pass
                return _user32.CallNextHookEx(None, n_code, w_param, l_param)

            self._proc = HOOKPROC(proc)  # referência guardada: o ctypes não pode liberar
            self._hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc,
                                                   _kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                raise ctypes.WinError(ctypes.get_last_error())


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    WH_KEYBOARD_LL = 13
    HC_ACTION = 0
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
    LLKHF_INJECTED = 0x10
    LRESULT = ctypes.c_ssize_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
    _user32.SetWindowsHookExW.restype = wintypes.HHOOK
    _user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    _user32.CallNextHookEx.restype = LRESULT
    _user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE


grabber = KeyboardGrabber()
