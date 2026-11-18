"""Janela de visualização/controle do computador remoto (PySide6)."""
from __future__ import annotations

import sys
import time

import numpy as np

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import (QComboBox, QLabel, QMainWindow, QMenu, QMessageBox,
                               QScrollArea, QSizePolicy, QSlider, QToolBar, QToolButton,
                               QVBoxLayout, QWidget)

from . import audio as A
from . import icons
from . import protocol as P
from .winkbd import grabber
from .client import RemoteClient
from .log import log
from .protocol import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, BTN_X1, BTN_X2

# nome -> (qualidade JPEG, fps, texto nítido 4:4:4)
QUALITY_PRESETS = {
    "Baixa (internet lenta)": (40, 12, False),
    "Média": (65, 20, False),
    "Alta": (80, 30, True),
    "Máxima": (92, 30, True),
}

_QT_TO_VK = {
    Qt.Key_Backspace: 0x08, Qt.Key_Tab: 0x09, Qt.Key_Backtab: 0x09,
    Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D,
    Qt.Key_Shift: 0x10, Qt.Key_Control: 0x11, Qt.Key_Alt: 0x12, Qt.Key_AltGr: 0xA5,
    Qt.Key_Pause: 0x13, Qt.Key_CapsLock: 0x14, Qt.Key_Escape: 0x1B, Qt.Key_Space: 0x20,
    Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22, Qt.Key_End: 0x23, Qt.Key_Home: 0x24,
    Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
    Qt.Key_Print: 0x2C, Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E,
    Qt.Key_Meta: 0x5B, Qt.Key_Menu: 0x5D, Qt.Key_NumLock: 0x90, Qt.Key_ScrollLock: 0x91,
    Qt.Key_Semicolon: 0xBA, Qt.Key_Equal: 0xBB, Qt.Key_Comma: 0xBC, Qt.Key_Minus: 0xBD,
    Qt.Key_Period: 0xBE, Qt.Key_Slash: 0xBF, Qt.Key_QuoteLeft: 0xC0,
    Qt.Key_BracketLeft: 0xDB, Qt.Key_Backslash: 0xDC, Qt.Key_BracketRight: 0xDD,
    Qt.Key_Apostrophe: 0xDE,
}


def qt_key_to_vk(key: int) -> int:
    """Usado quando o visualizador não roda no Windows (sem nativeVirtualKey)."""
    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        return int(key)
    if Qt.Key_F1 <= key <= Qt.Key_F24:
        return 0x70 + int(key) - int(Qt.Key_F1)
    return _QT_TO_VK.get(key, 0)


_BUTTONS = {
    Qt.LeftButton: BTN_LEFT, Qt.RightButton: BTN_RIGHT, Qt.MiddleButton: BTN_MIDDLE,
    Qt.BackButton: BTN_X1, Qt.ForwardButton: BTN_X2,
}


class _Bridge(QObject):
    """Leva eventos da thread de rede para a thread da interface."""
    frame = Signal(int, int, object)
    clipboard = Signal(str)
    audio_status = Signal(str)
    closed = Signal(object)
    monitors = Signal(object)
    notice = Signal(str)
    notice_local = Signal(str)  # mensagem informativa daqui mesmo (ex.: qual microfone abriu)


class FrameStore(QObject):
    """A imagem da tela remota, compartilhada por todas as janelas que a mostram."""
    changed = Signal()
    dirty = Signal(QRect)  # trecho da imagem que mudou (para redesenhar só ele)
    resized = Signal()

    def __init__(self):
        super().__init__()
        self.image = QImage()

    def apply_tiles(self, w: int, h: int, tiles) -> None:
        if self.image.isNull() or self.image.size() != QSize(w, h):
            self.image = QImage(w, h, QImage.Format_RGB32)
            self.image.fill(Qt.black)
            self.resized.emit()
        p = QPainter(self.image)
        area = QRect()
        for x, y, img in tiles:
            p.drawImage(x, y, img)
            area = area.united(QRect(x, y, img.width(), img.height()))
        p.end()
        if not area.isEmpty():
            self.dirty.emit(area)
        self.changed.emit()


class RemoteCanvas(QWidget):
    """Mostra a tela remota (ou só um pedaço dela: `source`) e repassa mouse/teclado."""

    def __init__(self, client: RemoteClient, store: FrameStore, parent=None):
        super().__init__(parent)
        self.client = client
        self.store = store
        self.source: QRect | None = None  # pedaço da imagem (um monitor remoto) ou None = tudo
        self.fit = True
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        store.dirty.connect(self._on_dirty)
        store.resized.connect(self._update_size)
        store.resized.connect(self.update)
        self._pending_move = None
        self._move_timer = QTimer(self, interval=15, timeout=self._flush_move)
        self._move_timer.start()

    @property
    def image(self) -> QImage:
        return self.store.image

    # ----- imagem -----
    def src_rect(self) -> QRect:
        img = self.store.image
        if img.isNull():
            return QRect()
        if self.source is not None and img.rect().contains(self.source):
            return self.source
        return img.rect()

    def set_source(self, source: QRect | None) -> None:
        self.source = source
        self._update_size()
        self.update()

    def set_fit(self, fit: bool) -> None:
        self.fit = fit
        self._update_size()
        self.update()

    def _update_size(self) -> None:
        src = self.src_rect()
        if not self.fit and not src.isNull():
            self.setFixedSize(src.size())
        else:
            self.setMinimumSize(1, 1)
            self.setMaximumSize(16777215, 16777215)

    def _on_dirty(self, area: QRect) -> None:
        """Redesenha só o pedaço da janela que mostra o trecho que mudou.

        Antes a janela inteira era redesenhada (e reescalada) a cada quadro, mesmo
        quando só o cursor piscava — em tela cheia isso pesa e dá travadinhas.
        """
        src, r = self.src_rect(), self.target_rect()
        if src.isNull() or r.isNull():
            self.update()
            return
        part = area.intersected(src)
        if part.isEmpty():
            return
        sx, sy = r.width() / src.width(), r.height() / src.height()
        x0 = r.x() + (part.x() - src.x()) * sx
        y0 = r.y() + (part.y() - src.y()) * sy
        self.update(QRect(int(x0) - 2, int(y0) - 2,
                          int(part.width() * sx) + 5, int(part.height() * sy) + 5))

    def target_rect(self) -> QRect:
        src = self.src_rect()
        if src.isNull():
            return QRect()
        iw, ih = src.width(), src.height()
        if not self.fit:
            return QRect(0, 0, iw, ih)
        scale = min(self.width() / iw, self.height() / ih)
        tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
        return QRect((self.width() - tw) // 2, (self.height() - th) // 2, tw, th)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        img = self.store.image
        if not img.isNull():
            r, src = self.target_rect(), self.src_rect()
            if r.size() != src.size():
                p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(r, img, src)
        else:
            p.setPen(Qt.white)
            p.drawText(self.rect(), Qt.AlignCenter, "Aguardando a tela remota…")
        p.end()

    # ----- mouse -----
    def _norm(self, pos) -> tuple[int, int]:
        """Posição na janela -> coordenada 0..65535 na imagem remota INTEIRA."""
        r, src, img = self.target_rect(), self.src_rect(), self.store.image
        if r.isNull() or img.isNull():
            return 0, 0
        fx = min(1.0, max(0.0, (pos.x() - r.x()) / max(1, r.width())))
        fy = min(1.0, max(0.0, (pos.y() - r.y()) / max(1, r.height())))
        x = src.x() + fx * (src.width() - 1)
        y = src.y() + fy * (src.height() - 1)
        return (round(x / max(1, img.width() - 1) * 65535), round(y / max(1, img.height() - 1) * 65535))

    def _flush_move(self):
        if self._pending_move is not None:
            self.client.mouse_move(*self._pending_move)
            self._pending_move = None

    def mouseMoveEvent(self, e):
        self._pending_move = self._norm(e.position())

    def mousePressEvent(self, e):
        b = _BUTTONS.get(e.button())
        if b is not None:
            self._pending_move = None
            self.client.mouse_button(b, True, *self._norm(e.position()))

    def mouseDoubleClickEvent(self, e):
        # Qt entrega: press, release, doubleclick, release -> o doubleclick vira o 2º press
        self.mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        b = _BUTTONS.get(e.button())
        if b is not None:
            self.client.mouse_button(b, False, *self._norm(e.position()))

    def wheelEvent(self, e):
        d = e.angleDelta()
        if d.y():
            self.client.wheel(d.y(), False)
        if d.x():
            self.client.wheel(-d.x(), True)

    # ----- teclado -----
    def event(self, e):
        t = e.type()
        if t == QEvent.ShortcutOverride:
            e.accept()  # impede que atalhos locais "roubem" teclas como Ctrl+C
            return True
        if t in (QEvent.KeyPress, QEvent.KeyRelease):
            self._handle_key(e, t == QEvent.KeyPress)
            return True  # inclui Tab, que normalmente trocaria o foco
        return super().event(e)

    def _handle_key(self, e, down: bool):
        if not down and e.isAutoRepeat():
            return
        vk = e.nativeVirtualKey() if sys.platform == "win32" else 0
        if not vk:
            vk = qt_key_to_vk(e.key())
        if vk:
            self.client.key(vk, down)

    def focusOutEvent(self, e):
        self.client.release_all()  # evita Ctrl/Alt "presos" no remoto
        super().focusOutEvent(e)

    def send_combo(self, *vks: int):
        for vk in vks:
            self.client.key(vk, True)
        for vk in reversed(vks):
            self.client.key(vk, False)


def place_on_screen(widget: QWidget, screen) -> None:
    """Coloca a janela no monitor local indicado (antes de showFullScreen)."""
    if widget.isFullScreen() or widget.isMaximized():
        widget.showNormal()
    widget.winId()  # garante a QWindow nativa
    handle = widget.windowHandle()
    if handle is not None:
        handle.setScreen(screen)
    g = screen.availableGeometry()
    widget.setGeometry(g.x() + 40, g.y() + 40, max(400, g.width() - 80), max(300, g.height() - 80))


class ExtraScreenWindow(QWidget):
    """Janela em tela cheia num monitor local extra, mostrando outro monitor remoto."""

    def __init__(self, client: RemoteClient, store: FrameStore, source: QRect, screen, title: str):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setWindowIcon(icons.app_icon())
        self.canvas = RemoteCanvas(client, store, self)
        self.canvas.set_source(source)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.canvas)
        self._screen = screen
        self._hwnd = int(self.winId())
        grabber.add(self._hwnd, client.key)  # atalhos do Windows vão para o remoto

    def closeEvent(self, e):
        grabber.remove(self._hwnd)
        super().closeEvent(e)

    def show_on_screen(self):
        place_on_screen(self, self._screen)
        self.showFullScreen()


class ViewerWindow(QMainWindow):
    def __init__(self, client: RemoteClient, title: str, quality: str = "Média", on_quality=None,
                 audio_on: bool = True, volume: int = 100, on_audio=None, on_toggle_panel=None,
                 mic_id: str = "", on_mic_device=None):
        super().__init__()
        self.mic_id = mic_id or ""
        self.on_mic_device = on_mic_device or (lambda mic_id: None)
        self.client = client
        self.on_quality = on_quality
        self.on_audio = on_audio or (lambda on, vol: None)
        self.on_toggle_panel = on_toggle_panel
        self.player: A.AudioPlayer | None = None
        self.mic: A.MicStreamer | None = None
        self.setWindowTitle(f"Ponte — {title}")
        self.setWindowIcon(icons.app_icon())
        self.resize(1280, 800)
        self._frames = 0
        self._bytes = 0
        self._audio_in = 0
        self._audio_peak = 0
        self._audio_err = ""
        self._audio_logged = False
        self._last_remote_clip = None
        self._closing = False
        self._extra_windows: list[ExtraScreenWindow] = []
        self._pre_fs_monitor = 0
        self._span = False          # "usar todos os meus monitores"
        self._pre_span_geometry = None
        self._pre_span_flags = None
        self._monitor_rects: list[tuple[int, int, int, int]] = list(client.monitor_rects)

        self.bridge = _Bridge()
        self.bridge.frame.connect(self._on_frame)
        self.bridge.clipboard.connect(self._on_remote_clipboard)
        self.bridge.closed.connect(self._on_closed)
        self.bridge.audio_status.connect(self._on_audio_status)
        self.bridge.monitors.connect(self._on_monitors)
        self.bridge.notice.connect(self._on_notice)
        self.bridge.notice_local.connect(lambda t: self.statusBar().showMessage(t, 10000))

        self.store = FrameStore()
        self.canvas = RemoteCanvas(client, self.store)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setStyleSheet("QScrollArea { background: black; border: none; }")
        self.setCentralWidget(self.scroll)

        self._build_toolbar(quality, audio_on, volume)
        self.stats = QLabel()
        self.statusBar().addPermanentWidget(self.stats)
        self.statusBar().showMessage(f"Conectado a {title}")
        QTimer(self, interval=1000, timeout=self._update_stats).start()

        QGuiApplication.clipboard().dataChanged.connect(self._on_local_clipboard)

        # liga os callbacks de rede e começa a receber
        client.on_frame = self._decode_frame
        client.on_clipboard = self.bridge.clipboard.emit
        client.on_closed = self.bridge.closed.emit
        client.on_audio = self._on_audio_data
        client.on_audio_status = self.bridge.audio_status.emit
        client.on_monitors = self.bridge.monitors.emit
        client.on_notice = self.bridge.notice.emit
        client.manual_ack = True  # confirma o quadro só depois de desenhado (ver _on_frame)
        client.start()
        self._refresh_monitor_box(max(client.monitors, len(self._monitor_rects)))
        self._apply_quality(quality)
        self._toggle_audio(self.audio_action.isChecked())
        self.canvas.setFocus()
        # Com esta janela em primeiro plano, Alt+Tab, tecla Windows, Ctrl+Shift+Esc
        # etc. vão para o PC remoto (ver winkbd.py). Fora dela, ficam no PC local.
        self._hwnd = int(self.winId())
        grabber.add(self._hwnd, self.client.key)

    # ----- barra de ferramentas -----
    def _action(self, icon_name: str, text: str, tip: str, slot=None, checkable=False,
                checked=False, show_text=False) -> QAction:
        act = QAction(icons.icon(icon_name), text, self, checkable=checkable)
        if checkable:
            act.setChecked(checked)
            act.toggled.connect(slot)
        elif slot is not None:
            act.triggered.connect(slot)
        act.setToolTip(tip)
        self.toolbar.addAction(act)
        btn = self.toolbar.widgetForAction(act)
        if btn is not None:
            btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if show_text else Qt.ToolButtonIconOnly)
        return act

    def _build_toolbar(self, quality: str, audio_on: bool, volume: int):
        tb = self.toolbar = QToolBar("Sessão")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)

        self._action("home", "Painel", "Mostrar/ocultar a janela principal da Ponte",
                     slot=lambda: self.on_toggle_panel and self.on_toggle_panel())
        tb.addWidget(QLabel(" Qualidade: "))
        self.quality_box = QComboBox()
        self.quality_box.addItems(list(QUALITY_PRESETS))
        if quality in QUALITY_PRESETS:
            self.quality_box.setCurrentText(quality)
        self.quality_box.currentTextChanged.connect(self._apply_quality)
        tb.addWidget(self.quality_box)

        # monitores remotos (só aparece se o PC remoto tiver mais de um)
        self.monitor_label = QLabel("  Tela: ")
        self.monitor_label_act = tb.addWidget(self.monitor_label)
        self.monitor_box = QComboBox()
        self.monitor_box.currentIndexChanged.connect(self._on_monitor_selected)
        self.monitor_box_act = tb.addWidget(self.monitor_box)
        self.next_monitor_act = self._action("monitors", "Próximo monitor",
                                             "Alternar para o próximo monitor do PC remoto",
                                             slot=self._next_monitor)

        tb.addSeparator()
        self.audio_action = self._action("audio", "Áudio", "Ouvir o som do computador remoto",
                                         slot=self._toggle_audio, checkable=True, checked=audio_on,
                                         show_text=True)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(max(0, min(100, volume)))
        self.volume.setFixedWidth(90)
        self.volume.setToolTip("Volume")
        self.volume.valueChanged.connect(self._set_volume)
        tb.addWidget(self.volume)
        self.mic_action = self._action("mic", "Microfone",
                                       "Enviar seu microfone para o PC remoto (para calls).\n"
                                       "Use fone de ouvido para não dar eco.",
                                       slot=self._toggle_mic, checkable=True, show_text=True)
        self._build_mic_menu()

        tb.addSeparator()
        self.fit_action = self._action("fit", "Ajustar à janela",
                                       "Ajustar à janela (desligado = tamanho real, com rolagem)",
                                       slot=self._toggle_fit, checkable=True, checked=True)
        self.fs_action = self._action("fullscreen", "Tela cheia",
                                      "Tela cheia — com dois monitores aqui e lá, "
                                      "cada monitor remoto ocupa um monitor seu",
                                      slot=self._toggle_fullscreen, checkable=True)
        self._build_fullscreen_menu()

        tb.addSeparator()
        self._action("windows", "Tecla Windows", "Tecla Windows",
                     slot=lambda: self.canvas.send_combo(0x5B))
        self._action("activity", "Ctrl+Shift+Esc", "Ctrl+Shift+Esc (Gerenciador de Tarefas)",
                     slot=lambda: self.canvas.send_combo(0x11, 0x10, 0x1B))
        self._action("switch", "Alt+Tab", "Alt+Tab", slot=lambda: self.canvas.send_combo(0x12, 0x09))

        tb.addSeparator()
        self._action("power", "Desconectar", "Encerrar a conexão", slot=self.close, show_text=True)

    def _build_fullscreen_menu(self):
        """Com mais de um monitor AQUI, a tela cheia pode ir para o monitor escolhido."""
        screens = list(QGuiApplication.screens())
        btn = self.toolbar.widgetForAction(self.fs_action)
        if len(screens) < 2 or btn is None:
            return
        menu = QMenu(self)
        for i, scr in enumerate(screens):
            act = menu.addAction(icons.icon("monitor"), f"Tela cheia no meu monitor {i + 1}")
            act.triggered.connect(lambda _=False, s=scr: self._enter_fullscreen(s))
        menu.addSeparator()
        span = menu.addAction(icons.icon("monitors"), "Usar todos os meus monitores")
        span.setToolTip("Uma janela só, ocupando todos os seus monitores "
                        "(como na Área de Trabalho Remota do Windows)")
        span.triggered.connect(self._enter_span)
        menu.setToolTipsVisible(True)
        btn.setMenu(menu)
        btn.setPopupMode(QToolButton.MenuButtonPopup)

    def _build_mic_menu(self):
        """Setinha do botão Microfone: escolher qual microfone deste PC enviar."""
        btn = self.toolbar.widgetForAction(self.mic_action)
        if btn is None:
            return
        self.mic_menu = QMenu(self)
        self.mic_menu.aboutToShow.connect(self._fill_mic_menu)
        btn.setMenu(self.mic_menu)
        btn.setPopupMode(QToolButton.MenuButtonPopup)

    def _fill_mic_menu(self):
        menu = self.mic_menu
        menu.clear()
        mics, default = A.list_microphones()
        auto = menu.addAction("Automático (padrão do Windows, ignorando microfones virtuais)")
        auto.setCheckable(True)
        auto.setChecked(not self.mic_id)
        auto.triggered.connect(lambda _=False: self._choose_mic(""))
        menu.addSeparator()
        if not mics:
            menu.addAction("Nenhum microfone encontrado").setEnabled(False)
        for mic_id, name in mics:
            label = name + ("  (padrão do Windows)" if mic_id == default else "")
            if A._is_virtual_mic(name):
                label += "  — virtual, não capta sua voz"
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(mic_id == self.mic_id)
            act.triggered.connect(lambda _=False, i=mic_id: self._choose_mic(i))

    def _choose_mic(self, mic_id: str):
        self.mic_id = mic_id
        self.on_mic_device(mic_id)
        if self.mic is not None:  # já está enviando: troca na hora
            self.mic.close()
            self._start_mic_stream()

    def _start_mic_stream(self):
        self.mic = A.MicStreamer(self.client.mic_data, on_error=self.bridge.audio_status.emit,
                                 mic_id=self.mic_id or None,
                                 on_open=lambda name: self.bridge.notice_local.emit(
                                     f"Enviando o microfone: {name}"))

    def _on_notice(self, text: str):
        log(f"aviso do PC remoto: {text}")
        self.statusBar().showMessage("⚠ " + text, 20000)

    # ----- monitores -----
    def _on_monitors(self, rects):
        self._monitor_rects = list(rects)
        self._refresh_monitor_box(len(rects))

    def _refresh_monitor_box(self, count: int):
        current = self.monitor_box.currentIndex()
        self.monitor_box.blockSignals(True)
        self.monitor_box.clear()
        for i in range(count):
            self.monitor_box.addItem(icons.icon("monitor"), f"Monitor {i + 1}")
        if count > 1:
            self.monitor_box.addItem(icons.icon("monitors"), "Todos os monitores")
        self.monitor_box.setCurrentIndex(current if 0 <= current < self.monitor_box.count() else 0)
        self.monitor_box.blockSignals(False)
        many = count > 1
        for act in (self.monitor_label_act, self.monitor_box_act, self.next_monitor_act):
            act.setVisible(many)

    def _monitor_count(self) -> int:
        return max(1, len(self._monitor_rects) or self.client.monitors)

    def _selected_monitor(self) -> int:
        i = max(0, self.monitor_box.currentIndex())
        return P.ALL_MONITORS if i >= self._monitor_count() else i

    def _on_monitor_selected(self, _index: int):
        self._apply_quality(self.quality_box.currentText())

    def _next_monitor(self):
        n = self.monitor_box.count()
        if n:
            self.monitor_box.setCurrentIndex((self.monitor_box.currentIndex() + 1) % n)

    # ----- tela cheia (um monitor remoto por monitor local) -----
    def _toggle_fullscreen(self, on: bool):
        if on:
            self._enter_fullscreen(self.screen())
        else:
            self._leave_fullscreen()

    def _enter_fullscreen(self, screen):
        if self._span:
            self._leave_span()
        self._close_extra_windows()
        self._pre_fs_monitor = self.monitor_box.currentIndex()
        local = list(QGuiApplication.screens())
        rects = self._monitor_rects
        if len(rects) > 1 and len(local) > 1:
            # Mostra o PC remoto inteiro ("todos") e distribui: monitor remoto 1 na
            # tela escolhida, o 2 no próximo monitor local, e assim por diante.
            self.monitor_box.setCurrentIndex(self.monitor_box.count() - 1)  # "Todos"
            order = [screen] + [s for s in local if s != screen]
            self.canvas.set_source(QRect(*rects[0]))
            for i, scr in enumerate(order[1:len(rects)], start=1):
                w = ExtraScreenWindow(self.client, self.store, QRect(*rects[i]), scr,
                                      f"{self.windowTitle()} — monitor {i + 1}")
                w.show_on_screen()
                self._extra_windows.append(w)
            log(f"tela cheia em {1 + len(self._extra_windows)} monitores")
        if not self.fit_action.isChecked():
            self.fit_action.setChecked(True)
        if screen is not None and screen != self.screen():
            place_on_screen(self, screen)
        self.showFullScreen()
        self.fs_action.blockSignals(True)
        self.fs_action.setChecked(True)
        self.fs_action.blockSignals(False)
        self.canvas.setFocus()

    def _enter_span(self):
        """Uma janela sem bordas cobrindo TODOS os monitores locais.

        Se o PC remoto tiver vários monitores, mostra todos ("Todos os monitores"),
        lado a lado — com o mesmo número de monitores dos dois lados, cada monitor
        remoto cai num monitor seu. Com menos monitores lá, a imagem fica centralizada.
        """
        if self._span:
            return
        if self.isFullScreen() or self._extra_windows:
            self._leave_fullscreen()
        screens = list(QGuiApplication.screens())
        if not screens:
            return
        area = QRect(screens[0].geometry())
        for scr in screens[1:]:
            area = area.united(scr.geometry())
        self._pre_fs_monitor = self.monitor_box.currentIndex()
        if self._monitor_count() > 1:
            self.monitor_box.setCurrentIndex(self.monitor_box.count() - 1)  # "Todos"
        self.canvas.set_source(None)
        if not self.fit_action.isChecked():
            self.fit_action.setChecked(True)
        self._pre_span_geometry = self.geometry()
        self._pre_span_flags = self.windowFlags()
        self._span = True
        self.showNormal()
        self.setWindowFlags(self._pre_span_flags | Qt.FramelessWindowHint)
        self.setGeometry(area)
        self.show()
        self._rebind_keyboard()  # trocar as bordas recria a janela nativa (novo HWND)
        self.raise_()
        self.activateWindow()
        self.fs_action.blockSignals(True)
        self.fs_action.setChecked(True)
        self.fs_action.blockSignals(False)
        self.canvas.setFocus()
        log(f"tela cheia em todos os monitores locais ({area.width()}x{area.height()})")

    def _leave_span(self):
        if not self._span:
            return
        self._span = False
        self.setWindowFlags(self._pre_span_flags)
        if self._pre_span_geometry is not None:
            self.setGeometry(self._pre_span_geometry)
        self.showNormal()
        self._rebind_keyboard()
        self.monitor_box.setCurrentIndex(self._pre_fs_monitor)

    def _rebind_keyboard(self):
        grabber.remove(getattr(self, "_hwnd", 0))
        self._hwnd = int(self.winId())
        grabber.add(self._hwnd, self.client.key)

    def _close_extra_windows(self):
        for w in self._extra_windows:
            grabber.remove(w._hwnd)
            w.close()
            w.deleteLater()
        self._extra_windows = []

    def _leave_fullscreen(self):
        if self._span:
            self._leave_span()
            self.fs_action.blockSignals(True)
            self.fs_action.setChecked(False)
            self.fs_action.blockSignals(False)
            return
        had_extra = bool(self._extra_windows)
        self._close_extra_windows()
        self.canvas.set_source(None)
        if had_extra:
            self.monitor_box.setCurrentIndex(self._pre_fs_monitor)
        self.showNormal()
        self.fs_action.blockSignals(True)
        self.fs_action.setChecked(False)
        self.fs_action.blockSignals(False)

    # ----- rede -> interface -----
    def _decode_frame(self, w, h, tiles):
        # roda na thread "ponte-decode": decodifica o JPEG fora da interface e da rede
        decoded = []
        for x, y, _tw, _th, data in tiles:
            img = QImage.fromData(data, "JPG")
            if not img.isNull():
                decoded.append((x, y, img))
            self._bytes += len(data)
        self.bridge.frame.emit(w, h, decoded)

    def _on_frame(self, w, h, tiles):
        try:
            self._frames += 1
            self.store.apply_tiles(w, h, tiles)
        finally:
            self.client.ack()

    def _update_stats(self):
        text = f"{self._frames} qps · {self._bytes / 1024:.0f} KB/s"
        if self.audio_action.isChecked():
            p = self.player
            if self._audio_err:
                audio = f"⚠ {self._audio_err}"
            elif p is not None and p.error:
                audio = f"⚠ {p.error}"
            elif self._audio_in == 0:
                audio = "nenhum áudio chegando do PC remoto"
            elif self._audio_peak < 30:
                audio = f"chegando em silêncio ({self._audio_in}/s) — nada tocando lá?"
            else:
                audio = f"{self._audio_in}/s → tocando em {p.device_name if p else '?'}"
            text += f"  ·  Áudio: {audio}"
            if self._audio_in and not self._audio_logged:
                log(f"áudio chegando ({self._audio_in} blocos/s, pico {self._audio_peak}), "
                    f"tocando em {p.device_name if p else '?'}")
                self._audio_logged = True
        if self.mic is not None:
            peak = self.mic.take_peak()
            if not self.mic.name:
                text += "  ·  Mic: abrindo…"
            elif peak < 30:
                text += f"  ·  Mic: {self.mic.name} — sem som (é o microfone certo?)"
            else:
                bars = "▮" * min(5, 1 + peak // 6000)
                text += f"  ·  Mic: {bars} {self.mic.name}"
        self.stats.setText(text + " ")
        self._frames = 0
        self._bytes = 0
        self._audio_in = 0
        self._audio_peak = 0

    def _on_audio_status(self, msg: str):
        log(f"aviso de áudio: {msg}")
        if "ligado" in msg:  # mensagens informativas (ex.: microfone ligado)
            self.statusBar().showMessage(msg, 10000)
        else:
            self._audio_err = msg  # fica visível até religar o áudio

    def _apply_quality(self, name: str):
        q, fps, sharp = QUALITY_PRESETS.get(name, QUALITY_PRESETS["Média"])
        self.client.settings(q, fps, self._selected_monitor(), sharp)
        if self.on_quality:
            self.on_quality(name)

    # ----- áudio -----
    def _toggle_audio(self, on: bool):
        self._audio_err = ""
        self._audio_logged = False
        log(f"áudio {'ligado' if on else 'desligado'} na janela remota")
        if on:
            ok, why = A.available()
            if not ok:
                self.statusBar().showMessage(f"Áudio indisponível neste PC: {why}", 10000)
                self.audio_action.blockSignals(True)
                self.audio_action.setChecked(False)
                self.audio_action.blockSignals(False)
                return
            if self.player is None:
                self.player = A.AudioPlayer(on_error=self.bridge.audio_status.emit)
                self.player.volume = self.volume.value() / 100
        else:
            if self.player is not None:
                self.player.close()
                self.player = None
        self.client.audio(on)
        self.volume.setEnabled(on)
        self.on_audio(on, self.volume.value())

    def _toggle_mic(self, on: bool):
        if on:
            ok, why = A.available()
            if not ok:
                self.statusBar().showMessage(f"Microfone indisponível neste PC: {why}", 10000)
                self.mic_action.blockSignals(True)
                self.mic_action.setChecked(False)
                self.mic_action.blockSignals(False)
                return
            self.client.mic(True)
            if self.mic is None:
                self._start_mic_stream()
            self.mic_action.setText("Microfone ●")
        else:
            if self.mic is not None:
                self.mic.close()
                self.mic = None
            self.client.mic(False)
            self.mic_action.setText("Microfone")

    def _set_volume(self, value: int):
        if self.player is not None:
            self.player.volume = value / 100
        self.on_audio(self.audio_action.isChecked(), value)

    def _on_audio_data(self, data: bytes):
        self._audio_in += 1  # roda na thread de rede
        if self._audio_in % 10 == 1:  # amostra o volume sem pesar
            a = np.frombuffer(data, dtype="<i2")
            if a.size:
                self._audio_peak = max(self._audio_peak, int(np.abs(a.astype(np.int32)).max()))
        player = self.player
        if player is not None:
            player.feed(data)

    def _toggle_fit(self, fit: bool):
        self.scroll.setWidgetResizable(fit)
        self.canvas.set_fit(fit)

    # ----- clipboard -----
    def _on_local_clipboard(self):
        text = QGuiApplication.clipboard().text()
        if text and text != self._last_remote_clip:
            self.client.clipboard(text)

    def _on_remote_clipboard(self, text: str):
        self._last_remote_clip = text
        QGuiApplication.clipboard().setText(text)

    # ----- fim de sessão -----
    def _on_closed(self, reason):
        if self._closing:
            return
        self._closing = True
        if reason:
            QMessageBox.warning(self, "Ponte", reason)
        self.close()

    def changeEvent(self, e):
        # minimizou ou perdeu o foco: solta as teclas que estavam apertadas no remoto
        if e.type() in (QEvent.WindowStateChange, QEvent.ActivationChange) and not self.isActiveWindow():
            self.client.release_all()
        super().changeEvent(e)

    def closeEvent(self, e):
        self._closing = True
        grabber.remove(getattr(self, "_hwnd", 0))
        self._close_extra_windows()
        try:
            QGuiApplication.clipboard().dataChanged.disconnect(self._on_local_clipboard)
        except (RuntimeError, TypeError):
            pass
        if self.player is not None:
            self.player.close()
            self.player = None
        if self.mic is not None:
            self.mic.close()
            self.mic = None
        self.client.close()
        super().closeEvent(e)
