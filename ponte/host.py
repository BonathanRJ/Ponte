"""Servidor: roda no computador que será acessado.

Aceita uma sessão por vez. Cada sessão usa duas threads:
  * envio: captura -> diferença por blocos -> JPEG -> FRAME (com controle
    de fluxo: no máximo MAX_INFLIGHT quadros sem confirmação)
  * leitura: confirmações, mouse, teclado, clipboard e configurações

Eventos para a interface chegam pelo callback on_event(tipo, dados):
  "listening" porta | "connected" endereço | "disconnected" endereço |
  "auth_failed" endereço | "clipboard" texto | "error" mensagem
"""
from __future__ import annotations

import socket
import threading
import time
from collections import deque
from typing import Callable

from . import protocol as P
from .capture import Rect, ScreenCapture
from .encoder import TileEncoder
from .log import log, log_exc
from .winput import BaseInjector

MAX_INFLIGHT = 2
# Cada quadro vai em mensagens de até ~FRAME_CHUNK bytes. Enquanto uma mensagem
# grande está sendo enviada, ninguém mais usa a conexão: com partes menores o
# áudio (e os avisos) entram entre elas em vez de esperar o quadro inteiro.
FRAME_CHUNK = 192 * 1024
MAX_FAILS = 5
FAIL_WINDOW = 300.0   # segundos
LOCKOUT = 300.0


class HostServer:
    def __init__(self,
                 get_password: Callable[[], str],
                 injector: BaseInjector,
                 port: int = P.DEFAULT_PORT,
                 bind: str = "0.0.0.0",
                 capture_factory: Callable[[], ScreenCapture] = ScreenCapture,
                 on_event: Callable[[str, object], None] | None = None,
                 view_only: Callable[[], bool] = lambda: False,
                 audio_factory: Callable[[], object] | None = None,
                 mic_sink_factory: Callable[[Callable[[str], None]], object] | None = None,
                 is_trusted: Callable[[str], bool] = lambda device_id: True,
                 ask_permission: Callable[[str, str, str], bool] = lambda peer, dev_id, name: True,
                 silent_mode: Callable[[], bool] = lambda: False,
                 output_router=None,
                 default_output_id: Callable[[], str | None] | None = None,
                 elevated_check: Callable[[], tuple[bool, int]] | None = None):
        self.get_password = get_password
        self.injector = injector
        self.port = port
        self.bind = bind
        self.capture_factory = capture_factory
        self.on_event = on_event or (lambda kind, data: None)
        self.view_only = view_only
        if audio_factory is None:
            from .audio import LoopbackSource
            audio_factory = LoopbackSource
        self.audio_factory = audio_factory
        self.mic_sink_factory = mic_sink_factory or _default_mic_sink
        # is_trusted: computador já liberado para entrar sem confirmação.
        # ask_permission: bloqueia a thread da sessão até a pessoa aceitar/recusar.
        self.is_trusted = is_trusted
        self.ask_permission = ask_permission
        # silent_mode: mandar o som deste PC só para quem acessa (via cabo virtual)
        self.silent_mode = silent_mode
        self.output_router = output_router
        if default_output_id is None:
            from .audio import default_speaker_id
            default_output_id = default_speaker_id
        self.default_output_id = default_output_id
        # A janela em foco é de administrador e esta Ponte não? (o Windows bloqueia o controle)
        if elevated_check is None:
            from .winadmin import foreground_is_elevated, is_admin
            elevated_check = (lambda: (False, 0)) if is_admin() else foreground_is_elevated
        self.elevated_check = elevated_check
        self._sock: socket.socket | None = None
        self._running = False
        self._session: _Session | None = None
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    # ----- ciclo de vida -----
    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: impede outro programa de "sequestrar" a porta
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.bind, self.port))
        except OSError:
            s.close()
            raise
        s.listen(4)
        self.port = s.getsockname()[1]
        self._sock = s
        self._running = True
        threading.Thread(target=self._accept_loop, name="ponte-accept", daemon=True).start()
        self.on_event("listening", self.port)

    def stop(self) -> None:
        self._running = False
        self.disconnect()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def disconnect(self) -> None:
        with self._lock:
            sess = self._session
        if sess:
            sess.stop()

    @property
    def connected_peer(self) -> str | None:
        with self._lock:
            return self._session.peer if self._session else None

    @property
    def connected_label(self) -> str | None:
        """'NOME-DO-PC (ip)' da sessão ativa, ou None (inclui quem ainda aguarda permissão)."""
        with self._lock:
            s = self._session
            return s.label if s and s.authenticated else None

    def send_clipboard(self, text: str) -> None:
        with self._lock:
            sess = self._session
        if sess and sess.authenticated:
            sess.send_clipboard(text)

    # ----- aceitação / bloqueio por tentativas -----
    def _accept_loop(self) -> None:
        while self._running:
            try:
                client, addr = self._sock.accept()
            except OSError:
                break
            ip = addr[0]
            conn = P.Connection(client)
            if self._is_locked(ip):
                threading.Thread(target=self._reject, args=(conn, P.T.AUTH_FAIL), daemon=True).start()
                continue
            threading.Thread(target=self._admit, args=(conn, ip), daemon=True).start()

    def _admit(self, conn: P.Connection, ip: str) -> None:
        # Se a sessão anterior está terminando (ex.: reconexão rápida), espera ela sair.
        deadline = time.monotonic() + 3.0
        while True:
            with self._lock:
                sess = self._session
                if sess is None:
                    self._session = sess = _Session(self, conn, ip)
                    break
                dying = not sess.running
            if not dying or time.monotonic() > deadline:
                self._reject(conn, P.T.BUSY)
                return
            time.sleep(0.05)
        sess.run()

    @staticmethod
    def _reject(conn: P.Connection, kind: int) -> None:
        try:
            conn.sock.settimeout(5)
            conn.send(P.T.HELLO, P.pack_hello(P.new_nonce()))
            conn.recv()
            if kind == P.T.AUTH_FAIL:
                time.sleep(1.5)
            conn.send(kind)
        except Exception:
            pass
        conn.close()

    def _is_locked(self, ip: str) -> bool:
        return self._locked_until.get(ip, 0) > time.monotonic()

    def _register_fail(self, ip: str) -> None:
        now = time.monotonic()
        fails = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW] + [now]
        self._fails[ip] = fails
        if len(fails) >= MAX_FAILS:
            self._locked_until[ip] = now + LOCKOUT
            self._fails[ip] = []

    def _session_ended(self, sess: "_Session") -> None:
        with self._lock:
            if self._session is sess:
                self._session = None


def _split_tiles(tiles, limit: int) -> list[list]:
    """Agrupa os trechos de um quadro em partes de até ~`limit` bytes (mín. 1 trecho)."""
    parts, cur, size = [], [], 0
    for t in tiles:
        n = len(t[4])
        if cur and size + n > limit:
            parts.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        parts.append(cur)
    return parts


def _peak16(data: bytes) -> int:
    import numpy as np
    a = np.frombuffer(data, dtype="<i2")
    return int(np.abs(a.astype(np.int32)).max()) if a.size else 0


def _default_mic_sink(on_error):
    """Toca a voz recebida no cabo virtual (VB-CABLE), que o app de call usa como microfone.

    Não toca nas caixas normais de propósito: o som voltaria pela captura do
    áudio do sistema e você ouviria a própria voz com atraso.
    """
    from .audio import AudioPlayer, available, find_virtual_cable
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    cable = find_virtual_cable()
    if not cable:
        raise RuntimeError("instale o VB-CABLE no PC remoto (vb-audio.com/Cable) e reabra a Ponte lá")
    return AudioPlayer(on_error=on_error, speaker_name=cable)


class _Session:
    def __init__(self, server: HostServer, conn: P.Connection, peer: str):
        self.server = server
        self.conn = conn
        self.peer = peer
        self.device_id = ""
        self.device_name = ""
        self.authenticated = False
        self.running = True
        self.quality = 65
        self.fps = 20
        self.monitor = 0
        self.sharp_text = False
        self.settings_changed = False
        self.rect = Rect(0, 0, 1, 1)
        # Quadros enviados e ainda não confirmados: quantas partes faltam de cada um.
        self._pending: deque[int] = deque()
        self._cond = threading.Condition()
        self.audio_on = False
        self._audio_thread: threading.Thread | None = None
        self.mic_sink = None
        self._silenced = False
        self._silence_warned = False
        self._elev_checked_at = 0.0
        self._elev_warned_hwnd = None

    def _check_admin_window(self) -> None:
        """Avisa quem acessa quando o clique/tecla foi numa janela de administrador."""
        now = time.monotonic()
        if now - self._elev_checked_at < 1.5:
            return
        self._elev_checked_at = now
        try:
            elevated, hwnd = self.server.elevated_check()
        except Exception:
            return
        if not elevated:
            self._elev_warned_hwnd = None
            return
        if hwnd != self._elev_warned_hwnd:
            self._elev_warned_hwnd = hwnd
            log("controle bloqueado: janela de administrador em foco no PC acessado")
            try:
                self.conn.send(P.T.NOTICE, (
                    "A janela em foco no PC remoto é de administrador (ex.: Gerenciador de "
                    "Tarefas) e o Windows bloqueia o controle dela. No PC remoto, marque "
                    "\"Abrir a Ponte como administrador\" — ou clique em outra janela."
                ).encode("utf-8"))
            except Exception:
                pass

    # ----- microfone do cliente -----
    def _status(self, msg: str) -> None:
        try:
            self.conn.send(P.T.AUDIO_STATUS, msg.encode("utf-8"))
        except Exception:
            pass

    def set_mic(self, on: bool) -> None:
        if on and self.mic_sink is None:
            try:
                self.mic_sink = self.server.mic_sink_factory(
                    lambda m: self._status(f"Microfone no PC remoto: {m}"))
                self._status("Microfone ligado — no app de call do PC remoto, "
                             "escolha 'CABLE Output' como microfone.")
            except Exception as e:  # noqa: BLE001
                self._status(log_exc("Microfone indisponível no PC remoto", e))
        elif not on and self.mic_sink is not None:
            self.mic_sink.close()
            self.mic_sink = None

    # ----- áudio -----
    def set_audio(self, on: bool) -> None:
        self.audio_on = on
        if on and (self._audio_thread is None or not self._audio_thread.is_alive()):
            self._audio_thread = threading.Thread(target=self._audio_loop, name="ponte-audio", daemon=True)
            self._audio_thread.start()

    def _update_silence(self) -> None:
        """Liga/desliga o 'silenciar este PC' conforme a opção e o microfone."""
        router = self.server.output_router
        if router is None:
            return
        want = bool(self.server.silent_mode()) and self.audio_on and self.running
        if want and self.mic_sink is not None:
            # Com um único cabo virtual, o som da call e a voz se misturariam no
            # mesmo cabo e as pessoas da call ouviriam a si mesmas.
            want = False
            if self._silenced:
                self._status("Microfone ligado: o PC remoto voltou a tocar o som pela saída normal "
                             "enquanto o microfone estiver ligado (com um só cabo virtual, as "
                             "pessoas da call se ouviriam).")
        if want and not self._silenced:
            err = router.silence()
            if err:
                if not self._silence_warned:
                    self._status(f"Não deu para silenciar o PC remoto: {err}")
                    self._silence_warned = True
            else:
                self._silenced = True
        elif not want and self._silenced:
            router.restore()
            self._silenced = False

    def _open_source(self):
        src = self.server.audio_factory()
        dev = getattr(src, "device_id", None)
        log(f"captura do som em: {getattr(src, 'name', '?')}")
        return src, dev

    def _audio_loop(self) -> None:
        log(f"áudio pedido por {self.label}")
        src = None
        try:
            self._update_silence()  # troca a saída ANTES de abrir a captura
            try:
                src, src_dev = self._open_source()
            except Exception as e:  # noqa: BLE001
                self.audio_on = False
                self._status(log_exc("Sem áudio no PC remoto", e))
                return
            sent, peak, t_report, t_check = 0, 0, time.monotonic(), time.monotonic()
            while self.running and self.audio_on:
                data = src.read()
                if data and self.audio_on:
                    self.conn.send(P.T.AUDIO, data)
                    sent += 1
                    peak = max(peak, _peak16(data))
                now = time.monotonic()
                if now - t_check > 1.0:
                    t_check = now
                    self._update_silence()
                    # Seguir a saída padrão: se trocarem no meio da sessão, o som
                    # muda de dispositivo e a captura precisa ir junto.
                    cur = self.server.default_output_id()
                    if src_dev is not None and cur and cur != src_dev:
                        log(f"saída padrão mudou ({src_dev} -> {cur}); reabrindo a captura")
                        src.close()
                        src, src_dev = self._open_source()
                if now - t_report > 10:
                    log(f"áudio: {sent} blocos enviados nos últimos 10 s, volume máx. {peak / 32768:.3f}"
                        + ("  (SILÊNCIO — nada tocando na saída padrão?)" if peak < 30 else ""))
                    sent, peak, t_report = 0, 0, now
            log("áudio desligado")
        except Exception as e:  # noqa: BLE001
            if self.running:
                self._status(log_exc("O áudio parou no PC remoto", e))
        finally:
            if src is not None:
                try:
                    src.close()
                except Exception:
                    pass
            if self._silenced and self.server.output_router is not None:
                self.server.output_router.restore()
                self._silenced = False

    def stop(self) -> None:
        self.running = False
        with self._cond:
            self._cond.notify_all()
        try:
            self.conn.send(P.T.BYE)
        except Exception:
            pass
        self.conn.close()

    def send_clipboard(self, text: str) -> None:
        data = text.encode("utf-8")[:P.MAX_CLIPBOARD]
        try:
            self.conn.send(P.T.CLIPBOARD, data)
        except Exception:
            pass

    # ----- sessão -----
    def run(self) -> None:
        srv = self.server
        try:
            if not self._handshake():
                return
            self.authenticated = True
            srv.on_event("connected", self.label)
            reader = threading.Thread(target=self._reader, name="ponte-reader", daemon=True)
            reader.start()
            self._sender()
        except Exception as e:  # noqa: BLE001
            if self.authenticated and self.running:
                srv.on_event("error", f"{type(e).__name__}: {e}")
        finally:
            self.running = False
            self.conn.close()
            try:
                srv.injector.release_all()
            except Exception:
                pass
            srv._session_ended(self)
            if self.authenticated:
                srv.on_event("disconnected", self.label)

    def _handshake(self) -> bool:
        srv = self.server
        self.conn.sock.settimeout(15)
        nonce = P.new_nonce()
        self.conn.send(P.T.HELLO, P.pack_hello(nonce))
        mtype, payload = self.conn.recv()
        password = srv.get_password() or ""
        digest, self.device_id, self.device_name = P.unpack_auth(payload)
        if mtype != P.T.AUTH or not password or not P.check_auth(password, nonce, digest):
            srv._register_fail(self.peer)
            srv.on_event("auth_failed", self.peer)
            time.sleep(1.5)  # atrasa tentativas de força bruta
            try:
                self.conn.send(P.T.AUTH_FAIL)
            except Exception:
                pass
            return False
        # Senha certa. Computador ainda não confiável -> a pessoa daqui precisa aceitar.
        if not srv.is_trusted(self.device_id):
            self.conn.send(P.T.WAITING)
            try:
                accepted = srv.ask_permission(self.peer, self.device_id, self.device_name)
            except Exception:
                accepted = False
            if not accepted:
                try:
                    self.conn.send(P.T.DENIED)
                except Exception:
                    pass
                return False
        self.conn.sock.settimeout(None)
        return True

    @property
    def label(self) -> str:
        return f"{self.device_name} ({self.peer})" if self.device_name else self.peer

    def _sender(self) -> None:
        cap = self.server.capture_factory()
        try:
            enc = TileEncoder()
            mons = cap.monitors()
            first = mons[0]
            self.conn.send(P.T.AUTH_OK, P.pack_auth_ok(first.width, first.height, len(mons)))
            # Onde fica cada monitor dentro da imagem "todos os monitores":
            # o cliente usa isso para mostrar um monitor remoto por monitor local.
            from .capture import bounding_rect
            allr = bounding_rect(mons)
            self.conn.send(P.T.MONITORS, P.pack_monitors(
                [(m.left - allr.left, m.top - allr.top, m.width, m.height) for m in mons]))
            while self.running:
                with self._cond:
                    while self.running and len(self._pending) >= MAX_INFLIGHT:
                        self._cond.wait(1.0)
                if not self.running:
                    break
                t0 = time.monotonic()
                if self.settings_changed:
                    self.settings_changed = False
                    enc.reset()
                frame, rawmode, rect = cap.grab(self.monitor)
                self.rect = rect
                tiles = enc.encode(frame, rawmode, self.quality, self.sharp_text)
                if tiles:
                    h, w = frame.shape[:2]
                    parts = _split_tiles(tiles, FRAME_CHUNK)
                    with self._cond:
                        self._pending.append(len(parts))
                    for part in parts:
                        if not self.running:
                            break
                        self.conn.send(P.T.FRAME, P.pack_frame(w, h, part))
                delay = 1.0 / max(1, self.fps) - (time.monotonic() - t0)
                if delay > 0:
                    time.sleep(delay)
        finally:
            cap.close()

    def _reader(self) -> None:
        inj = self.server.injector
        try:
            while self.running:
                mtype, p = self.conn.recv()
                if mtype == P.T.FRAME_ACK:  # o cliente confirma cada parte
                    with self._cond:
                        if self._pending:
                            self._pending[0] -= 1
                            if self._pending[0] <= 0:
                                self._pending.popleft()
                        self._cond.notify_all()
                    continue
                if mtype == P.T.BYE:
                    break
                if mtype == P.T.CLIPBOARD:
                    self.server.on_event("clipboard", p[:P.MAX_CLIPBOARD].decode("utf-8", "replace"))
                    continue
                if mtype == P.T.MIC:
                    sink = self.mic_sink
                    if sink is not None:
                        sink.feed(p)
                    continue
                if mtype == P.T.MIC_CTRL:
                    self.set_mic(bool(p[:1]) and p[0] == 1)
                    continue
                if mtype == P.T.AUDIO_CTRL:
                    self.set_audio(bool(p[:1]) and p[0] == 1)
                    continue
                if mtype == P.T.SETTINGS:
                    q, f, m, s = P.unpack_settings(p)
                    self.quality = max(10, min(95, q))
                    self.fps = max(1, min(60, f))
                    self.sharp_text = s
                    if m != self.monitor:
                        self.monitor = m
                    self.settings_changed = True
                    continue
                if self.server.view_only():
                    continue
                if mtype == P.T.MOUSE_MOVE:
                    inj.mouse_move(*P.unpack_mouse_move(p), self.rect)
                elif mtype == P.T.MOUSE_BTN:
                    b, d, x, y = P.unpack_mouse_btn(p)
                    inj.mouse_button(b, d, x, y, self.rect)
                    if not d:
                        self._check_admin_window()
                elif mtype == P.T.MOUSE_WHEEL:
                    inj.wheel(*P.unpack_wheel(p))
                elif mtype == P.T.KEY:
                    vk, down = P.unpack_key(p)
                    inj.key(vk, down)
                    if not down:
                        self._check_admin_window()
                elif mtype == P.T.RELEASE_ALL:
                    inj.release_all()
        except Exception:
            pass
        finally:
            self.running = False
            self.audio_on = False
            self.set_mic(False)
            with self._cond:
                self._cond.notify_all()
            self.conn.close()
