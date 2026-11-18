"""Codificação incremental da tela.

A tela é dividida em blocos de TILE x TILE pixels. Só os blocos que mudaram
desde o último quadro enviado são recodificados em JPEG. Blocos vizinhos
alterados na mesma linha são agrupados em um retângulo só, o que reduz muito
o overhead de cabeçalhos JPEG (ex.: ao rolar uma página ou digitar).
"""
from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

TILE = 64
# Retângulos altos viram faixas de no máximo BAND linhas. Assim uma tela inteira
# (1º quadro, troca de janela, rolagem) não vira um JPEG único de ~1 MB: as faixas
# são codificadas em paralelo e enviadas em partes, sem segurar o áudio e o resto.
BAND = 4 * TILE

# O Pillow libera o GIL enquanto codifica JPEG, então threads aceleram de verdade.
_POOL = ThreadPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1)),
                           thread_name_prefix="ponte-jpeg")


class TileEncoder:
    def __init__(self, tile: int = TILE):
        self.tile = tile
        self.prev: np.ndarray | None = None

    def reset(self) -> None:
        self.prev = None

    def changed_rects(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        h, w = frame.shape[:2]
        prev = self.prev
        if prev is None or prev.shape != frame.shape:
            return [(0, 0, w, h)]

        # Compara pixels como uint32 (1 comparação por pixel em vez de 4).
        cur32 = frame.view(np.uint32).reshape(h, w)
        prev32 = prev.view(np.uint32).reshape(h, w)
        diff = cur32 != prev32
        if not diff.any():
            return []

        t = self.tile
        th, tw = -(-h // t), -(-w // t)
        padded = np.zeros((th * t, tw * t), dtype=bool)
        padded[:h, :w] = diff
        tiles = padded.reshape(th, t, tw, t).any(axis=(1, 3))

        rects = []
        for r in range(th):
            row = tiles[r]
            if not row.any():
                continue
            y0 = r * t
            hh = min(y0 + t, h) - y0
            c = 0
            while c < tw:
                if row[c]:
                    c0 = c
                    while c < tw and row[c]:
                        c += 1
                    x0 = c0 * t
                    rects.append((x0, y0, min(c * t, w) - x0, hh))
                else:
                    c += 1
        return _merge_vertical(rects)

    def encode(self, frame: np.ndarray, rawmode: str, quality: int = 65,
               sharp_text: bool = False) -> list[tuple[int, int, int, int, bytes]]:
        """Retorna a lista de (x, y, w, h, jpeg) dos trechos que mudaram."""
        rects = _split_bands(self.changed_rects(frame), BAND)

        def one(rect):
            x, y, w, h = rect
            region = np.ascontiguousarray(frame[y:y + h, x:x + w])
            img = Image.frombuffer("RGB", (w, h), region, "raw", rawmode, 0, 1)
            if img.mode != "RGB":  # Pillow mantém "RGBX" quando o rawmode é RGBX
                img = img.convert("RGB")
            buf = io.BytesIO()
            # subsampling=0 (4:4:4) deixa texto colorido nítido, mas pesa mais.
            img.save(buf, "JPEG", quality=quality, subsampling=0 if sharp_text else 2)
            return (x, y, w, h, buf.getvalue())

        # Poucos pixels: em série (o custo de agendar nas threads não compensa).
        area = sum(w * h for _, _, w, h in rects)
        if len(rects) > 1 and area > 256 * 256:
            out = list(_POOL.map(one, rects))
        else:
            out = [one(r) for r in rects]
        # Guarda cópia: alguns backends reutilizam o buffer da captura.
        self.prev = frame.copy()
        return out


def _split_bands(rects, band: int):
    """Corta retângulos mais altos que `band` em faixas horizontais."""
    out = []
    for x, y, w, h in rects:
        if h <= band:
            out.append((x, y, w, h))
            continue
        for yy in range(y, y + h, band):
            out.append((x, yy, w, min(band, y + h - yy)))
    return out


def _merge_vertical(rects):
    """Junta retângulos empilhados com o mesmo x/largura (ex.: uma coluna inteira mudando)."""
    if not rects:
        return rects
    merged = []
    open_by_span = {}
    for x, y, w, h in rects:
        key = (x, w)
        prev = open_by_span.get(key)
        if prev is not None and prev[1] + prev[3] == y:
            prev[3] += h
        else:
            item = [x, y, w, h]
            merged.append(item)
            open_by_span[key] = item
    return [tuple(m) for m in merged]
