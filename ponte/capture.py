"""Captura de tela.

Usa `mss` (rápido, multi-monitor). Se não estiver instalado, cai para
PIL.ImageGrab. O quadro é sempre um array numpy HxWx4 (uint8), contíguo,
com o "rawmode" do PIL que descreve a ordem dos canais.

Importante: no Windows o objeto mss precisa ser criado na mesma thread
que captura, por isso ScreenCapture deve ser instanciado na thread de envio.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


ALL_MONITORS = 255  # "todos os monitores": a área de trabalho virtual inteira


@dataclass
class Rect:
    left: int
    top: int
    width: int
    height: int


class ScreenCapture:
    def __init__(self):
        self._sct = None
        try:
            import mss  # noqa: F401
            self._sct = mss.mss()
        except Exception:
            self._sct = None

    def monitors(self) -> list[Rect]:
        if self._sct is not None:
            return [Rect(m["left"], m["top"], m["width"], m["height"])
                    for m in self._sct.monitors[1:]] or [self._all_rect()]
        from PIL import ImageGrab
        w, h = ImageGrab.grab().size
        return [Rect(0, 0, w, h)]

    def _all_rect(self) -> Rect:
        m = self._sct.monitors[0]
        return Rect(m["left"], m["top"], m["width"], m["height"])

    def all_rect(self) -> Rect:
        """Retângulo que envolve todos os monitores."""
        return bounding_rect(self.monitors())

    def grab(self, monitor: int = 0) -> tuple[np.ndarray, str, Rect]:
        """Retorna (frame HxWx4, rawmode PIL, retângulo capturado).

        monitor = ALL_MONITORS captura todos os monitores numa imagem só.
        """
        mons = self.monitors()
        if monitor == ALL_MONITORS:
            rect = self.all_rect()
        else:
            rect = mons[monitor] if 0 <= monitor < len(mons) else mons[0]
        if self._sct is not None:
            shot = self._sct.grab({"left": rect.left, "top": rect.top,
                                   "width": rect.width, "height": rect.height})
            frame = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
            return frame, "BGRX", rect
        from PIL import ImageGrab
        img = ImageGrab.grab().convert("RGBA")
        frame = np.asarray(img, dtype=np.uint8)
        return np.ascontiguousarray(frame), "RGBX", Rect(0, 0, img.width, img.height)

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass


def bounding_rect(rects: list[Rect]) -> Rect:
    left = min(r.left for r in rects)
    top = min(r.top for r in rects)
    right = max(r.left + r.width for r in rects)
    bottom = max(r.top + r.height for r in rects)
    return Rect(left, top, right - left, bottom - top)
