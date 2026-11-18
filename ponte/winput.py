"""Injeção de mouse e teclado no Windows via SendInput (ctypes, sem dependências).

As coordenadas chegam normalizadas (0..65535) em relação ao monitor que está
sendo transmitido e são convertidas para coordenadas absolutas da área de
trabalho virtual (todos os monitores).

Limitação do próprio Windows (UIPI): para controlar janelas executadas como
administrador, a Ponte também precisa estar rodando como administrador.
"""
from __future__ import annotations

import sys

from .capture import Rect
from .protocol import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, BTN_X1, BTN_X2


class BaseInjector:
    """Interface comum (também usada como injetor 'nulo' fora do Windows)."""

    def mouse_move(self, nx: int, ny: int, rect: Rect) -> None: ...
    def mouse_button(self, button: int, down: bool, nx: int, ny: int, rect: Rect) -> None: ...
    def wheel(self, delta: int, horizontal: bool) -> None: ...
    def key(self, vk: int, down: bool) -> None: ...
    def release_all(self) -> None: ...


class RecordingInjector(BaseInjector):
    """Guarda os eventos recebidos (testes / modo somente visualização)."""

    def __init__(self):
        self.events = []

    def mouse_move(self, nx, ny, rect):
        self.events.append(("move", nx, ny))

    def mouse_button(self, button, down, nx, ny, rect):
        self.events.append(("btn", button, down, nx, ny))

    def wheel(self, delta, horizontal):
        self.events.append(("wheel", delta, horizontal))

    def key(self, vk, down):
        self.events.append(("key", vk, down))

    def release_all(self):
        self.events.append(("release_all",))


def make_injector() -> BaseInjector:
    if sys.platform == "win32":
        return WindowsInjector()
    return RecordingInjector()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    user32.MapVirtualKeyW.restype = wintypes.UINT

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_HWHEEL = 0x1000
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

    _BUTTON_FLAGS = {
        BTN_LEFT: (0x0002, 0x0004, 0),
        BTN_RIGHT: (0x0008, 0x0010, 0),
        BTN_MIDDLE: (0x0020, 0x0040, 0),
        BTN_X1: (0x0080, 0x0100, 1),
        BTN_X2: (0x0080, 0x0100, 2),
    }

    # Teclas que precisam da flag "extended" para não virarem teclas do numpad.
    _EXTENDED_VK = {
        0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # PgUp PgDn End Home setas
        0x2C, 0x2D, 0x2E,  # PrintScreen Insert Delete
        0x5B, 0x5C, 0x5D,  # Win esq/dir, Menu
        0x6F, 0x90,        # / do numpad, NumLock
        0xA3, 0xA5,        # Ctrl direito, Alt direito (AltGr)
    }

    def _try_dpi_aware():
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

    class WindowsInjector(BaseInjector):
        def __init__(self):
            _try_dpi_aware()
            self._keys_down: set[int] = set()
            self._buttons_down: set[int] = set()

        @staticmethod
        def _send(*inputs):
            arr = (INPUT * len(inputs))(*inputs)
            user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

        @staticmethod
        def _abs(nx: int, ny: int, rect: Rect) -> tuple[int, int]:
            px = rect.left + nx * (rect.width - 1) / 65535
            py = rect.top + ny * (rect.height - 1) / 65535
            vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            vw = max(2, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
            vh = max(2, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
            return (round((px - vx) * 65535 / (vw - 1)), round((py - vy) * 65535 / (vh - 1)))

        def _move_input(self, nx, ny, rect):
            ax, ay = self._abs(nx, ny, rect)
            i = INPUT(type=INPUT_MOUSE)
            i.u.mi = MOUSEINPUT(ax, ay, 0,
                                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, 0)
            return i

        def mouse_move(self, nx, ny, rect):
            self._send(self._move_input(nx, ny, rect))

        def mouse_button(self, button, down, nx, ny, rect):
            flags = _BUTTON_FLAGS.get(button)
            if flags is None:
                return
            down_f, up_f, data = flags
            i = INPUT(type=INPUT_MOUSE)
            i.u.mi = MOUSEINPUT(0, 0, data, down_f if down else up_f, 0, 0)
            self._send(self._move_input(nx, ny, rect), i)
            (self._buttons_down.add if down else self._buttons_down.discard)(button)

        def wheel(self, delta, horizontal):
            i = INPUT(type=INPUT_MOUSE)
            i.u.mi = MOUSEINPUT(0, 0, delta & 0xFFFFFFFF,
                                MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, 0, 0)
            self._send(i)

        def key(self, vk, down):
            if not 0 < vk < 256:
                return
            flags = 0 if down else KEYEVENTF_KEYUP
            if vk in _EXTENDED_VK:
                flags |= KEYEVENTF_EXTENDEDKEY
            scan = user32.MapVirtualKeyW(vk, 0)
            i = INPUT(type=INPUT_KEYBOARD)
            i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
            self._send(i)
            (self._keys_down.add if down else self._keys_down.discard)(vk)

        def release_all(self):
            """Solta teclas/botões presos (ex.: conexão caiu com Ctrl apertado)."""
            for vk in list(self._keys_down):
                self.key(vk, False)
            for b in list(self._buttons_down):
                down_f, up_f, data = _BUTTON_FLAGS[b]
                i = INPUT(type=INPUT_MOUSE)
                i.u.mi = MOUSEINPUT(0, 0, data, up_f, 0, 0)
                self._send(i)
            self._buttons_down.clear()
