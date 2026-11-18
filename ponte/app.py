"""Janela principal da Ponte: 'Este computador' + 'Acessar outro computador'."""
from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import APP_NAME, __version__, icons, netinfo
from . import protocol as P
from .client import RemoteClient
from .config import Config, generate_password
from .host import HostServer
from .viewer import QUALITY_PRESETS, ViewerWindow
from .winput import make_injector

ACCENT = "#e5484d"
FALLBACK_PORTS = [7070, 17070, 27070, 37070, 41070, 45070, 8765, 9443]

STYLE = f"""
QWidget {{ color: #1f2328; }}
QMainWindow, QWidget#root {{ background: #f4f5f7; }}
QCheckBox {{ spacing: 8px; }}
QComboBox QAbstractItemView {{ background: white; color: #1f2328;
    selection-background-color: {ACCENT}; selection-color: white; }}
QFrame#card {{ background: white; border: 1px solid #e2e4e8; border-radius: 10px; }}
QLabel#cardTitle {{ font-size: 15px; font-weight: 600; color: #1f2328; }}
QLabel#address {{ font-size: 22px; font-weight: 600; color: {ACCENT}; }}
QLabel#muted {{ color: #6b7280; }}
QPushButton {{ padding: 6px 12px; border-radius: 6px; border: 1px solid #d0d4da; background: white; }}
QPushButton:hover {{ background: #f0f1f3; }}
QPushButton#primary {{ background: {ACCENT}; color: white; border: none; font-weight: 600; padding: 8px 18px; }}
QPushButton#primary:hover {{ background: #d13c41; }}
QPushButton#primary:disabled {{ background: #f0a3a5; }}
QLineEdit, QComboBox {{ padding: 6px; border: 1px solid #d0d4da; border-radius: 6px; background: white; }}
"""


def make_icon() -> QIcon:
    return icons.app_icon()


PERMISSION_TIMEOUT = 60  # segundos até recusar sozinho


class PermissionRequest:
    """Pedido de acesso esperando resposta (criado na thread da sessão)."""

    def __init__(self, peer: str, device_id: str, name: str):
        self.peer, self.device_id, self.name = peer, device_id, name
        self.accepted = False
        self.trust = False
        self.done = threading.Event()

    def answer(self, accepted: bool, trust: bool = False):
        if not self.done.is_set():
            self.accepted, self.trust = accepted, trust
            self.done.set()


class PermissionDialog(QWidget):
    """'Fulano quer acessar este computador' — Aceitar / Recusar."""

    def __init__(self, req: PermissionRequest, icon: QIcon):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint | Qt.CustomizeWindowHint
                         | Qt.WindowTitleHint)
        self.req = req
        self.setWindowTitle(f"{APP_NAME} — pedido de acesso")
        self.setWindowIcon(icon)
        self.setStyleSheet(STYLE + "QWidget#perm { background: white; }")
        self.setObjectName("perm")
        self.setAttribute(Qt.WA_DeleteOnClose)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 18)
        v.setSpacing(10)
        title = QLabel(f"<b>{req.name or 'Um computador'}</b> quer acessar este computador",
                       objectName="cardTitle")
        title.setWordWrap(True)
        v.addWidget(title)
        v.addWidget(QLabel(f"Endereço: {req.peer}\nA senha informada está correta.", objectName="muted"))
        self.trust_box = QCheckBox("Permitir este computador sem pedir confirmação nas próximas vezes")
        v.addWidget(self.trust_box)
        row = QHBoxLayout()
        self.countdown = QLabel("", objectName="muted")
        row.addWidget(self.countdown, 1)
        refuse = QPushButton("Recusar")
        refuse.clicked.connect(lambda: self._finish(False))
        row.addWidget(refuse)
        accept = QPushButton("Aceitar", objectName="primary")
        accept.clicked.connect(lambda: self._finish(True))
        accept.setDefault(True)
        row.addWidget(accept)
        v.addLayout(row)
        self._left = PERMISSION_TIMEOUT
        self._tick()
        self._timer = QTimer(self, interval=1000, timeout=self._tick)
        self._timer.start()
        self.adjustSize()
        self.setFixedWidth(max(self.width(), 460))

    def _tick(self):
        if self.req.done.is_set():  # a sessão desistiu (cliente fechou / tempo esgotado)
            self.close()
            return
        if self._left <= 0:
            self._finish(False)
            return
        self.countdown.setText(f"Recusa automática em {self._left} s")
        self._left -= 1

    def _finish(self, accepted: bool):
        self.req.answer(accepted, accepted and self.trust_box.isChecked())
        self.close()

    def closeEvent(self, e):
        self.req.answer(False)  # fechar no X = recusar
        super().closeEvent(e)

    def show_centered(self):
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.show()
        self.move(geo.center().x() - self.width() // 2, geo.center().y() - self.height() // 2)
        self.raise_()
        self.activateWindow()


class _HostBridge(QObject):
    event = Signal(str, object)
    connect_done = Signal(object, object, str)  # (client | None, erro | None, endereço)
    permission = Signal(object)                  # PermissionRequest
    waiting = Signal(str)                        # cliente: aguardando aceitarem


class SessionBanner(QWidget):
    """Aviso enquanto alguém controla este computador.

    Pode ser arrastado para qualquer lugar (a posição é lembrada), recolhido
    para uma bolinha (–) ou escondido (×). O ícone da bandeja continua
    indicando a sessão.
    """

    def __init__(self, on_disconnect, cfg):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.cfg = cfg
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setCursor(Qt.SizeAllCursor)
        self.setToolTip("Arraste para mover")
        self.setStyleSheet(f"QWidget {{ background: {ACCENT}; color: white; }}"
                           "QPushButton { background: white; color: #b42318; border: none;"
                           " border-radius: 4px; padding: 4px 10px; font-weight: 600; }"
                           "QPushButton#flat { background: transparent; color: white;"
                           " padding: 2px 6px; font-size: 14px; }"
                           "QPushButton#flat:hover { background: rgba(255,255,255,0.25); }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 4, 4)
        lay.setSpacing(6)
        self.label = QLabel()
        lay.addWidget(self.label)
        self.kick = QPushButton("Desconectar")
        self.kick.setCursor(Qt.ArrowCursor)
        self.kick.clicked.connect(on_disconnect)
        lay.addWidget(self.kick)
        self.toggle_btn = QPushButton("–", objectName="flat")
        self.toggle_btn.setCursor(Qt.ArrowCursor)
        self.toggle_btn.setToolTip("Recolher / expandir")
        self.toggle_btn.clicked.connect(lambda: self._set_compact(not self.compact))
        lay.addWidget(self.toggle_btn)
        close = QPushButton("×", objectName="flat")
        close.setCursor(Qt.ArrowCursor)
        close.setToolTip("Esconder aviso (a sessão continua; veja o ícone na bandeja)")
        close.clicked.connect(self.hide)
        lay.addWidget(close)
        self.peer = ""
        self.compact = bool(cfg["banner_compact"])
        self._drag = None

    def show_for(self, peer: str):
        if not self.cfg["show_banner"]:
            return
        self.peer = peer
        self._set_compact(self.compact, save=False)
        pos = self.cfg["banner_pos"]
        geo = QGuiApplication.primaryScreen().availableGeometry()
        if pos and any(s.availableGeometry().contains(pos[0] + 10, pos[1] + 10)
                       for s in QGuiApplication.screens()):
            self.move(pos[0], pos[1])
        else:  # padrão: canto inferior direito, longe do conteúdo
            self.move(geo.right() - self.width() - 16, geo.bottom() - self.height() - 16)
        self.show()

    def _set_compact(self, compact: bool, save: bool = True):
        self.compact = compact
        self.kick.setVisible(not compact)
        self.label.setText("●" if compact else f"●  Acesso remoto: {self.peer}")
        self.label.setToolTip(f"Este computador está sendo acessado por {self.peer}")
        self.toggle_btn.setText("+" if compact else "–")
        self.adjustSize()
        if save:
            self.cfg["banner_compact"] = compact

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.cfg["banner_pos"] = [self.x(), self.y()]

    def mouseDoubleClickEvent(self, e):
        self._set_compact(not self.compact)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = Config()
        self.server: HostServer | None = None
        self.viewers: list[ViewerWindow] = []
        self._last_remote_clip = None
        self._really_quit = False

        self.bridge = _HostBridge()
        self.bridge.event.connect(self._on_host_event)
        self.bridge.connect_done.connect(self._on_connect_done)
        self.bridge.permission.connect(self._show_permission)
        self.bridge.waiting.connect(lambda msg: self.connect_status.setText(msg))

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(make_icon())
        self.setStyleSheet(STYLE)
        self.banner = SessionBanner(self._disconnect_guest, self.cfg)

        root = QWidget(objectName="root")
        lay = QHBoxLayout(root)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(16)
        lay.addWidget(self._build_this_pc(), 1)
        lay.addWidget(self._build_remote(), 1)
        self.setCentralWidget(root)
        self.resize(900, 460)

        self._build_tray()
        QGuiApplication.clipboard().dataChanged.connect(self._on_local_clipboard)
        QTimer.singleShot(0, self._refresh_addresses)
        from .winsession import ICON_PATH, OutputRouter, start_presence
        icons.save_ico(ICON_PATH)  # ícone do Mixer de volume, gerado do logotipo SVG
        self.output_router = OutputRouter(self.cfg)
        start_presence()  # "Ponte" no Mixer de volume do Windows

        def recover():  # se a Ponte fechou com o som desviado para o cabo, devolve
            try:
                from . import audio
                if audio.available()[0]:
                    audio._soundcard()  # COM nesta thread
                    self.output_router.recover()
            except Exception:
                pass
        threading.Thread(target=recover, name="ponte-recover", daemon=True).start()
        if self.cfg["allow_on_start"]:
            self.allow_box.setChecked(True)

    # ================= Este computador =================
    def _card(self, title: str):
        card = QFrame(objectName="card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)
        v.addWidget(QLabel(title, objectName="cardTitle"))
        return card, v

    def _build_this_pc(self):
        card, v = self._card("Este computador")

        v.addWidget(QLabel("Endereço para acessar este PC:", objectName="muted"))
        self.addr_label = QLabel("…", objectName="address")
        self.addr_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.addr_label)
        self.addr_detail = QLabel("", objectName="muted")
        self.addr_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.addr_detail.setWordWrap(True)
        v.addWidget(self.addr_detail)

        v.addWidget(QLabel("Senha de acesso:", objectName="muted"))
        row = QHBoxLayout()
        self.pw_edit = QLineEdit(self.cfg["password"])
        self.pw_edit.setEchoMode(QLineEdit.Password)
        self.pw_edit.editingFinished.connect(self._save_password)
        row.addWidget(self.pw_edit, 1)
        show = QPushButton(icons.icon("eye"), "Mostrar", checkable=True)
        show.toggled.connect(lambda on: (self.pw_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password),
                                         show.setText("Ocultar" if on else "Mostrar")))
        row.addWidget(show)
        copy = QPushButton(icons.icon("copy"), "Copiar")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.pw_edit.text()))
        row.addWidget(copy)
        new = QPushButton(icons.icon("refresh"), "Nova")
        new.clicked.connect(self._new_password)
        row.addWidget(new)
        v.addLayout(row)

        self.allow_box = QCheckBox("Permitir que outros computadores acessem este")
        self.allow_box.toggled.connect(self._toggle_hosting)
        v.addWidget(self.allow_box)
        self.view_only_box = QCheckBox("Somente visualização (bloquear mouse e teclado remotos)")
        self.view_only_box.setChecked(bool(self.cfg["view_only"]))
        self.view_only_box.toggled.connect(lambda on: self.cfg.__setitem__("view_only", on))
        v.addWidget(self.view_only_box)
        # Marcada sozinha quando este PC recebe uma conexão: da próxima vez que a
        # Ponte abrir, ele já fica disponível para ser acessado.
        self.start_box = QCheckBox("Permitir acesso automaticamente ao abrir a Ponte")
        self.start_box.setChecked(bool(self.cfg["allow_on_start"]))
        self.start_box.toggled.connect(lambda on: self.cfg.__setitem__("allow_on_start", on))
        v.addWidget(self.start_box)
        banner_box = QCheckBox("Mostrar aviso na tela enquanto alguém estiver acessando")
        banner_box.setChecked(bool(self.cfg["show_banner"]))
        banner_box.toggled.connect(self._toggle_banner_setting)
        v.addWidget(banner_box)
        silent_box = QCheckBox("Silenciar este PC durante o acesso (o som vai só para quem acessa)")
        silent_box.setToolTip("Durante o acesso, o som do Windows vai para o cabo virtual (VB-CABLE):\n"
                              "nada sai nas caixas deste PC, e quem acessa ouve normalmente.\n"
                              "A saída de som volta ao normal quando a conexão termina.\n"
                              "Com o microfone remoto ligado, o som volta para a saída normal.")
        silent_box.setChecked(bool(self.cfg["silent_host"]))
        silent_box.toggled.connect(lambda on: self.cfg.__setitem__("silent_host", on))
        v.addWidget(silent_box)
        from .winadmin import is_admin
        admin_now = is_admin()
        self.admin_box = QCheckBox("Abrir a Ponte como administrador (para controlar o Gerenciador "
                                   "de Tarefas e programas de administrador)"
                                   + ("  — ativo" if admin_now else ""))
        self.admin_box.setToolTip("O Windows não deixa um programa comum controlar janelas de "
                                  "administrador.\nCom esta opção, ao abrir a Ponte o Windows pede "
                                  "permissão (UAC) uma vez.")
        self.admin_box.setChecked(bool(self.cfg["run_as_admin"]))
        self.admin_box.toggled.connect(self._toggle_run_as_admin)
        v.addWidget(self.admin_box)
        self.cable_label = QLabel("", objectName="muted")  # só aparece com aviso importante
        self.cable_label.setWordWrap(True)
        self.cable_label.hide()
        v.addWidget(self.cable_label)
        trow = QHBoxLayout()
        self.trusted_label = QLabel("", objectName="muted")
        self.trusted_label.setWordWrap(True)
        trow.addWidget(self.trusted_label, 1)
        self.forget_trusted_btn = QPushButton("Pedir de novo")
        self.forget_trusted_btn.setToolTip("Voltar a pedir confirmação para todos os computadores liberados")
        self.forget_trusted_btn.clicked.connect(self._forget_trusted)
        trow.addWidget(self.forget_trusted_btn)
        v.addLayout(trow)
        self._update_trusted_label()

        v.addStretch(1)
        status_row = QHBoxLayout()
        self.status_label = QLabel("", objectName="muted")  # só aparece com algo a dizer
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        status_row.addWidget(self.status_label, 1)
        self.kick_btn = QPushButton(icons.icon("power"), "Desconectar")
        self.kick_btn.clicked.connect(self._disconnect_guest)
        self.kick_btn.hide()
        status_row.addWidget(self.kick_btn)
        v.addLayout(status_row)
        return card

    def _refresh_addresses(self):
        def work():
            ts = netinfo.tailscale_ip()
            lan = netinfo.lan_ip()
            self.bridge.event.emit("_addresses", (ts, lan, netinfo.hostname()))
            from . import audio
            ok, _ = audio.available()
            self.bridge.event.emit("_cable", audio.find_virtual_cable() if ok else None)
            if ok and audio.default_output_is_cable():
                self.bridge.event.emit("_cable_default", None)
        threading.Thread(target=work, daemon=True).start()

    def _save_password(self):
        pw = self.pw_edit.text()
        if len(pw) < 6:
            QMessageBox.warning(self, APP_NAME, "A senha precisa ter pelo menos 6 caracteres.")
            self.pw_edit.setText(self.cfg["password"])
            return
        self.cfg["password"] = pw

    def _new_password(self):
        self.pw_edit.setText(generate_password())
        self._save_password()

    def _toggle_hosting(self, on: bool):
        if on:
            preferred = int(self.cfg["port"])
            # O Windows reserva faixas de portas (Hyper-V/WSL/Docker); se a preferida
            # estiver bloqueada, tenta outras e passa a usar a que funcionar.
            candidates = [preferred] + [p for p in FALLBACK_PORTS if p != preferred]
            last_error = None
            for port in candidates:
                server = HostServer(
                    get_password=lambda: self.cfg["password"],
                    injector=make_injector(),
                    port=port,
                    on_event=self.bridge.event.emit,
                    view_only=self.view_only_box.isChecked,
                    is_trusted=self.cfg.is_trusted,
                    ask_permission=self._ask_permission,
                    silent_mode=lambda: bool(self.cfg["silent_host"]),
                    output_router=self.output_router,
                )
                # Reaberta como administrador: a Ponte anterior pode estar fechando e
                # ainda segurando a porta. Espera um pouco para não mudar o endereço.
                tries = 10 if ("--elevated" in sys.argv and port == preferred) else 1
                for attempt in range(tries):
                    try:
                        server.start()
                        last_error = None
                        break
                    except OSError as e:
                        last_error = e
                        if attempt + 1 < tries:
                            time.sleep(0.3)
                if last_error is not None:
                    continue
                self.server = server
                if port != preferred:
                    self.cfg["port"] = port
                break
            if self.server is None:
                QMessageBox.critical(self, APP_NAME,
                                     "Não consegui abrir nenhuma porta "
                                     f"({', '.join(map(str, candidates))}):\n{last_error}")
                self.allow_box.blockSignals(True)
                self.allow_box.setChecked(False)
                self.allow_box.blockSignals(False)
            else:
                self._refresh_addresses()
        else:
            if self.server:
                self.server.stop()
                self.server = None
            self.banner.hide()
            self.kick_btn.hide()
            self.status_label.hide()
        self._update_tray_tip()

    # ----- pedido de permissão (roda na thread da sessão) -----
    def _ask_permission(self, peer: str, device_id: str, name: str) -> bool:
        req = PermissionRequest(peer, device_id, name)
        self.bridge.permission.emit(req)
        answered = req.done.wait(PERMISSION_TIMEOUT + 5)
        if not answered:
            req.answer(False)  # fecha o diálogo se ainda estiver aberto
        if req.accepted and req.trust:
            self.cfg.trust(device_id, name)
            self.bridge.event.emit("_trusted_changed", None)
        return req.accepted

    def _show_permission(self, req: PermissionRequest):
        dlg = PermissionDialog(req, self.windowIcon())
        dlg.show_centered()
        self.tray.showMessage(APP_NAME, f"{req.name or req.peer} quer acessar este computador.",
                              self.windowIcon())

    def _update_trusted_label(self):
        n = len(self.cfg["trusted"])
        if n:
            names = ", ".join(v.get("name", "?") for v in self.cfg["trusted"].values())
            self.trusted_label.setText(f"Entram sem pedir confirmação: {names}")
            self.trusted_label.show()
            self.forget_trusted_btn.show()
        else:
            self.trusted_label.hide()
            self.forget_trusted_btn.hide()

    def _forget_trusted(self):
        if QMessageBox.question(self, APP_NAME, "Voltar a pedir confirmação para todos os "
                                "computadores liberados?") == QMessageBox.Yes:
            self.cfg.untrust_all()
            self._update_trusted_label()

    def _toggle_run_as_admin(self, on: bool):
        self.cfg["run_as_admin"] = on
        from .winadmin import is_admin, relaunch_as_admin
        if not on or is_admin():
            return
        ans = QMessageBox.question(self, APP_NAME, "Reabrir a Ponte agora como administrador?\n\n"
                                   "O Windows vai pedir permissão. Se alguém estiver conectado, "
                                   "a conexão cai e pode ser refeita em seguida.")
        if ans == QMessageBox.Yes:
            if self.server:
                self.server.stop()  # libera a porta para a nova Ponte
                self.server = None
            if relaunch_as_admin():
                self._quit()
            else:
                self.allow_box.setChecked(bool(self.cfg["allow_on_start"]))

    def _toggle_banner_setting(self, on: bool):
        self.cfg["show_banner"] = on
        peer = self.server.connected_label if self.server else None
        if on and peer:
            self.banner.show_for(peer)
        elif not on:
            self.banner.hide()

    def _disconnect_guest(self):
        if self.server:
            self.server.disconnect()

    def _on_host_event(self, kind: str, data):
        if kind == "_addresses":
            ts, lan, host = data
            port = int(self.cfg["port"])
            suffix = "" if port == P.DEFAULT_PORT else f":{port}"
            self.addr_label.setText((ts or lan or host) + suffix)
            parts = []
            if ts:
                parts.append(f"Tailscale: {ts}  ·  nome: {host}")
            else:
                parts.append("Tailscale não encontrado — use o IP da rede local.")
            if lan:
                parts.append(f"Rede local: {lan}")
            parts.append(f"Porta: {self.cfg['port']}")
            self.addr_detail.setText("\n".join(parts))
        elif kind == "_cable":
            self.cable_label.setToolTip(
                "Microfone remoto: pronto (VB-CABLE encontrado)" if data else
                "Microfone remoto: instale o VB-CABLE (vb-audio.com/Cable) para usar em calls")
        elif kind == "_cable_default":
            self.cable_label.setText("⚠ A saída de som do Windows está no 'CABLE Input', por isso "
                                     "nenhum som sai neste PC. Clique no ícone de som da barra de "
                                     "tarefas e escolha seu fone ou caixa de som.")
            self.cable_label.setStyleSheet("color: #b42318; font-weight: 600;")
            self.cable_label.show()
        elif kind == "_trusted_changed":
            self._update_trusted_label()
        elif kind == "listening":
            self.status_label.hide()  # aguardando conexões: estado normal, sem mensagem
        elif kind == "connected":
            if not self.cfg["allow_on_start"]:  # recebeu conexão: abrir já permitindo
                self.cfg["allow_on_start"] = True
                self.start_box.setChecked(True)
            self.status_label.setText(f"● Conectado: {data}")
            self.status_label.setStyleSheet(f"color: {ACCENT}; font-weight: 600;")
            self.status_label.show()
            self.kick_btn.show()
            self.banner.show_for(data)
            self.tray.showMessage(APP_NAME, f"{data} está acessando este computador.", self.windowIcon())
        elif kind == "disconnected":
            self.status_label.hide()
            self.status_label.setStyleSheet("")
            self.kick_btn.hide()
            self.banner.hide()
        elif kind == "auth_failed":
            self.status_label.setText(f"Tentativa com senha errada de {data}.")
            self.status_label.show()
        elif kind == "clipboard":
            self._last_remote_clip = data
            QGuiApplication.clipboard().setText(data)
        elif kind == "error":
            self.status_label.setText(f"Erro na sessão: {data}")
            self.status_label.show()
        self._update_tray_tip()

    def _on_local_clipboard(self):
        text = QGuiApplication.clipboard().text()
        if self.server and text and text != self._last_remote_clip:
            self.server.send_clipboard(text)

    # ================= Acessar outro computador =================
    def _build_remote(self):
        card, v = self._card("Acessar outro computador")
        v.addWidget(QLabel("Endereço (IP do Tailscale ou nome do PC):", objectName="muted"))
        self.addr_box = QComboBox()
        self.addr_box.setEditable(True)
        self.addr_box.addItems(self.cfg["recents"])
        self.addr_box.setCurrentText(self.cfg["recents"][0] if self.cfg["recents"] else "")
        self.addr_box.lineEdit().setPlaceholderText("ex.: 100.101.102.103 ou pc-do-escritorio")
        v.addWidget(self.addr_box)

        v.addWidget(QLabel("Senha do computador remoto:", objectName="muted"))
        pw_row = QHBoxLayout()
        self.remote_pw = QLineEdit()
        self.remote_pw.setEchoMode(QLineEdit.Password)
        self.remote_pw.returnPressed.connect(self._connect)
        # "olhinho" dentro do campo: mostra/oculta a senha (e a salva, se houver)
        self.pw_eye = self.remote_pw.addAction(icons.icon("eye"), QLineEdit.TrailingPosition)
        self.pw_eye.setToolTip("Mostrar senha")
        self.pw_eye.triggered.connect(self._toggle_remote_pw_visible)
        pw_row.addWidget(self.remote_pw, 1)
        self.forget_pw_btn = QPushButton(icons.icon("trash"), "Esquecer")
        self.forget_pw_btn.setToolTip("Apagar a senha salva deste endereço")
        self.forget_pw_btn.clicked.connect(self._forget_saved_password)
        pw_row.addWidget(self.forget_pw_btn)
        v.addLayout(pw_row)
        self.remember_box = QCheckBox("Lembrar a senha neste computador")
        self.remember_box.setChecked(bool(self.cfg["remember_password"]))
        self.remember_box.toggled.connect(lambda on: self.cfg.__setitem__("remember_password", on))
        v.addWidget(self.remember_box)
        self.addr_box.currentTextChanged.connect(self._update_saved_password_hint)

        grid = QGridLayout()
        grid.addWidget(QLabel("Qualidade inicial:", objectName="muted"), 0, 0)
        self.quality_box = QComboBox()
        self.quality_box.addItems(list(QUALITY_PRESETS))
        self.quality_box.setCurrentText(self.cfg["quality"])
        grid.addWidget(self.quality_box, 0, 1)
        v.addLayout(grid)

        self.connect_btn = QPushButton(icons.icon("arrow-right", "#ffffff"), "Conectar", objectName="primary")
        self.connect_btn.setLayoutDirection(Qt.RightToLeft)  # seta depois do texto
        self.connect_btn.clicked.connect(self._connect)
        v.addWidget(self.connect_btn, 0, Qt.AlignLeft)
        self.connect_status = QLabel("", objectName="muted")
        self.connect_status.setWordWrap(True)
        v.addWidget(self.connect_status)
        v.addStretch(1)
        self._update_saved_password_hint()
        return card

    def _update_saved_password_hint(self, *_):
        saved = self.cfg.saved_password(self.addr_box.currentText()) is not None
        self.remote_pw.setPlaceholderText("Senha salva — é só clicar em Conectar" if saved else "")
        self.forget_pw_btn.setVisible(saved)

    def _toggle_remote_pw_visible(self):
        show = self.remote_pw.echoMode() == QLineEdit.Password
        if show and not self.remote_pw.text():
            saved = self.cfg.saved_password(self.addr_box.currentText())
            if saved:
                self.remote_pw.setText(saved)  # ver a senha que está salva
        self.remote_pw.setEchoMode(QLineEdit.Normal if show else QLineEdit.Password)
        self.pw_eye.setIcon(icons.icon("eye-off" if show else "eye"))
        self.pw_eye.setToolTip("Ocultar senha" if show else "Mostrar senha")

    def _forget_saved_password(self):
        self.cfg.forget_password(self.addr_box.currentText())
        self._update_saved_password_hint()
        self.connect_status.setText("Senha salva apagada para este endereço.")

    def _connect(self):
        text = self.addr_box.currentText().strip()
        typed = self.remote_pw.text()
        if not text:
            self.connect_status.setText("Digite o endereço do computador remoto.")
            return
        pw = typed or self.cfg.saved_password(text)
        if not pw:
            self.connect_status.setText("Digite a senha do computador remoto.")
            return
        self._pending_pw = (text, pw, bool(typed))
        host, port = netinfo.parse_address(text, int(self.cfg["port"]))
        self.connect_btn.setEnabled(False)
        self.connect_status.setText(f"Conectando a {host}:{port}…")

        def work():
            client = RemoteClient(host, port, pw, on_frame=lambda *a: None,
                                  device_id=self.cfg["device_id"], device_name=self.cfg.device_name,
                                  on_waiting=lambda: self.bridge.waiting.emit(
                                      "Senha aceita. Aguardando alguém aceitar no outro computador…"))
            try:
                client.connect()
                self.bridge.connect_done.emit(client, None, text)
            except Exception as e:  # noqa: BLE001
                self.bridge.connect_done.emit(None, e, text)
        threading.Thread(target=work, daemon=True).start()

    def _on_connect_done(self, client, error, address):
        self.connect_btn.setEnabled(True)
        addr, pw, typed = getattr(self, "_pending_pw", (address, "", False))
        if error is not None:
            if isinstance(error, P.AuthError) and not typed and self.cfg.saved_password(address):
                # a senha salva não vale mais (trocaram a senha no outro PC)
                self.cfg.forget_password(address)
                self._update_saved_password_hint()
                self.connect_status.setText("A senha salva não funciona mais (foi trocada no outro PC). "
                                            "Digite a senha nova.")
                self.remote_pw.setFocus()
                return
            if isinstance(error, (P.AuthError, P.BusyError, P.DeniedError, P.ProtocolError)):
                msg = str(error)
            elif isinstance(error, TimeoutError) or "timed out" in str(error):
                msg = "Tempo esgotado. O outro PC está ligado, com a Ponte aberta e o acesso permitido?"
            elif isinstance(error, ConnectionRefusedError):
                msg = "Conexão recusada: a Ponte do outro PC não está com o acesso permitido."
            else:
                msg = f"Não foi possível conectar: {error}"
            self.connect_status.setText(msg)
            return
        self.connect_status.setText("")
        self.remote_pw.clear()
        if self.remote_pw.echoMode() == QLineEdit.Normal:
            self._toggle_remote_pw_visible()
        if self.remember_box.isChecked() and pw:
            self.cfg.save_password(address, pw)
        elif typed:
            self.cfg.forget_password(address)
        self.cfg.add_recent(address)
        if self.addr_box.findText(address) < 0:
            self.addr_box.insertItem(0, address)
        self._update_saved_password_hint()
        quality = self.quality_box.currentText()
        def save_audio(on, vol):
            self.cfg.data["audio"] = on
            self.cfg.data["volume"] = vol
            self.cfg.save()
        # O áudio sempre começa ligado: se ficasse salvo como "desligado" (um clique
        # sem querer no botão), todas as conexões seguintes abriam mudas.
        viewer = ViewerWindow(client, address, quality,
                              on_quality=lambda q: self.cfg.__setitem__("quality", q),
                              audio_on=True, volume=int(self.cfg["volume"]),
                              on_audio=save_audio, on_toggle_panel=self._toggle_panel,
                              mic_id=str(self.cfg["mic_device"] or ""),
                              on_mic_device=lambda i: self.cfg.__setitem__("mic_device", i))
        viewer.setWindowIcon(self.windowIcon())
        viewer.setAttribute(Qt.WA_DeleteOnClose)
        viewer.destroyed.connect(lambda *_: self._viewer_closed(viewer))
        self.viewers.append(viewer)
        viewer.show()
        # Enquanto conectado, só a janela do PC remoto fica na barra de tarefas;
        # o botão "Painel" da janela remota mostra/oculta esta janela.
        self.hide()

    def _toggle_panel(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._show_main()

    def _viewer_closed(self, viewer):
        if viewer in self.viewers:
            self.viewers.remove(viewer)
        if not self.viewers:
            self._show_main()  # terminou a conexão: a janela principal volta

    # ================= bandeja =================
    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        menu.addAction(QAction("Abrir Ponte", self, triggered=self._show_main))
        menu.addAction(QAction("Mostrar aviso de acesso", self, triggered=self._reshow_banner))
        menu.addAction(QAction("Desconectar visitante", self, triggered=self._disconnect_guest))
        menu.addSeparator()
        menu.addAction(QAction("Sair", self, triggered=self._quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._show_main() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self._update_tray_tip()

    def _reshow_banner(self):
        peer = self.server.connected_label if self.server else None
        if peer:
            self.banner.show_for(peer)

    def _update_tray_tip(self):
        if not self.server:
            tip = "Ponte — acesso desligado"
        elif self.server.connected_label:
            tip = f"Ponte — {self.server.connected_label} conectado"
        else:
            tip = "Ponte — aguardando conexões"
        self.tray.setToolTip(tip)

    def _show_main(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._really_quit = True
        self.close()

    def closeEvent(self, e):
        if self.viewers and not self._really_quit:
            e.ignore()  # há uma conexão aberta: só esconde (volta pelo botão Painel)
            self.hide()
            return
        if self.server and not self._really_quit and QSystemTrayIcon.isSystemTrayAvailable():
            # Continua aceitando conexões em segundo plano, como o AnyDesk.
            e.ignore()
            self.hide()
            self.tray.showMessage(APP_NAME, "A Ponte continua aberta na bandeja aceitando conexões.",
                                  self.windowIcon())
            return
        for v in list(self.viewers):
            v.close()
        if self.server:
            self.server.stop()
        try:
            self.output_router.restore()  # nunca sair com o som desviado
        except Exception:
            pass
        self.banner.close()
        self.tray.hide()
        e.accept()
        QApplication.quit()
