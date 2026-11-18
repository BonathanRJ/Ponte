"""Ícones da Ponte, todos em SVG (renderizados pelo QtSvg em qualquer tamanho/DPI).

Os ícones de interface são traços em grade 24x24 com "currentColor", que é
trocado pela cor pedida na hora de desenhar.
"""
from __future__ import annotations

ACCENT = "#e5484d"
TEXT = "#1f2328"

LOGO = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect x="4" y="4" width="56" height="56" rx="14" fill="{ACCENT}"/>
  <path d="M24 47V17h10.5a9.5 9.5 0 0 1 0 19H24" fill="none" stroke="#fff"
        stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

_PATHS = {
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-2.6 3.6"/>'
               '<path d="M6.6 6.6C3.7 8.5 2 12 2 12s3.6 7 10 7c1.9 0 3.6-.6 5-1.5"/>'
               '<path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M3 3l18 18"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9v11a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9"/>',
    "panel": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>'
             '<path d="M5.5 8h1.5M5.5 11h1.5"/>',
    "audio": '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>'
             '<path d="M18.5 5.5a9 9 0 0 1 0 13"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/>'
           '<path d="M12 18v3"/>',
    "fit": '<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4'
           'M9 20H5a1 1 0 0 1-1-1v-4"/><rect x="8" y="8" width="8" height="8" rx="1"/>',
    "fullscreen": '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
    "monitor": '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "monitors": '<rect x="2" y="3" width="13" height="9" rx="1.5"/>'
                '<rect x="9" y="11" width="13" height="9" rx="1.5"/>',
    "windows": '<rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1"/>'
               '<rect x="3" y="13" width="8" height="8" rx="1"/><rect x="13" y="13" width="8" height="8" rx="1"/>',
    "activity": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "switch": '<rect x="3" y="8" width="13" height="13" rx="2"/><path d="M8 4h11a2 2 0 0 1 2 2v11"/>',
    "power": '<path d="M18.4 6.6a9 9 0 1 1-12.8 0"/><path d="M12 2v10"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
    "trash": '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
    "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
}


def svg(name: str, color: str = TEXT, stroke: float = 2.0) -> str:
    """Texto SVG de um ícone (LOGO para o logotipo)."""
    if name == "logo":
        return LOGO
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round">{_PATHS[name]}</svg>')


def _render(text: str, size: int, ratio: float = 1.0):
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer
    px = max(1, round(size * ratio))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    r = QSvgRenderer(QByteArray(text.encode("utf-8")))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p)
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


_cache: dict = {}


def icon(name: str, color: str = TEXT):
    """QIcon nítido em qualquer tamanho/escala de tela."""
    key = (name, color)
    if key not in _cache:
        from PySide6.QtGui import QIcon
        ic = QIcon()
        text = svg(name, color)
        for size in (16, 20, 24, 32, 48, 64, 128, 256):
            ic.addPixmap(_render(text, size))
        _cache[key] = ic
    return _cache[key]


def app_icon():
    return icon("logo")


def save_ico(path: str) -> bool:
    """Gera um .ico (exigido pelo Mixer de volume do Windows) a partir do logotipo SVG."""
    try:
        import io

        from PIL import Image
        from PySide6.QtCore import QBuffer, QIODevice
        pm = _render(LOGO, 256)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        pm.toImage().save(buf, "PNG")
        img = Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")
        img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        return True
    except Exception:
        return False
