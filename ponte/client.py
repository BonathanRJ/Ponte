"""Cliente: conecta no computador remoto, recebe a tela e envia mouse/teclado.

Sem dependência de interface; o visualizador (viewer.py) só fornece callbacks.
"""
from __future__ import annotations

import queue
import socket
import threading
from typing import Callable

from . import protocol as P


PERMISSION_WAIT = 90.0  # o servidor recusa sozinho após 60 s sem resposta


class RemoteClient:
    def __init__(self, host: str, port: int, password: str,
                 on_frame: Callable[[int, int, list], None],
                 on_clipboard: Callable[[str], None] = lambda t: None,
                 on_closed: Callable[[str | None], None] = lambda reason: None,
                 on_audio: Callable[[bytes], None] = lambda data: None,
                 on_audio_status: Callable[[str], None] = lambda msg: None,
                 device_id: str = "", device_name: str = "",
                 on_waiting: Callable[[], None] = lambda: None):
        self.host = host
        self.device_id = device_id
        self.device_name = device_name
        self.on_waiting = on_waiting
        self.port = port
        self.password = password
        self.on_frame = on_frame
        self.on_clipboard = on_clipboard
        self.on_closed = on_closed
        self.on_audio = on_audio
        self.on_audio_status = on_audio_status
        self.conn: P.Connection | None = None
        self.width = self.height = 0
        self.monitors = 1
        self.monitor_rects: list[tuple[int, int, int, int]] = []
        self.on_monitors: Callable[[list], None] = lambda rects: None
        self.on_notice: Callable[[str], None] = lambda text: None
        self._running = False
        self._closed_by_user = False
        # manual_ack=True: quem desenha a tela chama ack() depois de aplicar o quadro.
        # Assim o PC remoto só manda mais quando a imagem anterior já apareceu —
        # se a interface atrasar, o remoto espera em vez de acumular atraso.
        self.manual_ack = False
        self._frames: queue.Queue[bytes | None] = queue.Queue()

    def connect(self, timeout: float = 10.0) -> None:
        """Conecta e autentica. Levanta AuthError, BusyError, OSError..."""
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        conn = P.Connection(sock)
        try:
            mtype, payload = conn.recv()
            if mtype != P.T.HELLO:
                raise P.ProtocolError("resposta inesperada do servidor")
            version, nonce = P.unpack_hello(payload)
            if version != P.PROTOCOL_VERSION:
                raise P.ProtocolError("as versões da Ponte nos dois PCs são diferentes "
                                      f"(remoto: {version}, este: {P.PROTOCOL_VERSION}) — atualize os dois")
            conn.send(P.T.AUTH, P.pack_auth(P.auth_digest(self.password, nonce),
                                            self.device_id, self.device_name))
            mtype, payload = conn.recv()
            if mtype == P.T.WAITING:
                # Senha certa; agora alguém no PC remoto precisa clicar em "Aceitar".
                self.on_waiting()
                sock.settimeout(PERMISSION_WAIT)
                mtype, payload = conn.recv()
            if mtype == P.T.AUTH_FAIL:
                raise P.AuthError("senha incorreta (ou muitas tentativas, aguarde alguns minutos)")
            if mtype == P.T.BUSY:
                raise P.BusyError("o computador remoto já está em outra sessão")
            if mtype == P.T.DENIED:
                raise P.DeniedError("a conexão foi recusada no computador remoto "
                                    "(ou ninguém aceitou a tempo)")
            if mtype != P.T.AUTH_OK:
                raise P.ProtocolError("resposta inesperada do servidor")
            self.width, self.height, self.monitors = P.unpack_auth_ok(payload)
        except Exception:
            conn.close()
            raise
        sock.settimeout(None)
        self.conn = conn

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._reader, name="ponte-client", daemon=True).start()
        threading.Thread(target=self._decoder, name="ponte-decode", daemon=True).start()

    def _decoder(self) -> None:
        """Abre os quadros fora da thread de rede: o áudio não fica esperando o JPEG."""
        while True:
            p = self._frames.get()
            if p is None:
                return
            ok = False
            try:
                w, h, tiles = P.unpack_frame(p)
                self.width, self.height = w, h
                self.on_frame(w, h, tiles)
                ok = True
            except Exception:
                pass
            finally:
                if not (ok and self.manual_ack):
                    self.ack()

    def ack(self) -> None:
        """Confirma uma mensagem de quadro (libera o PC remoto a mandar a próxima)."""
        self._send(P.T.FRAME_ACK)

    def _reader(self) -> None:
        reason = None
        try:
            while self._running:
                mtype, p = self.conn.recv()
                if mtype == P.T.FRAME:
                    self._frames.put(p)
                elif mtype == P.T.NOTICE:
                    self.on_notice(p.decode("utf-8", "replace"))
                elif mtype == P.T.MONITORS:
                    self.monitor_rects = P.unpack_monitors(p)
                    self.monitors = max(1, len(self.monitor_rects))
                    self.on_monitors(self.monitor_rects)
                elif mtype == P.T.AUDIO:
                    self.on_audio(p)
                elif mtype == P.T.AUDIO_STATUS:
                    self.on_audio_status(p.decode("utf-8", "replace"))
                elif mtype == P.T.CLIPBOARD:
                    self.on_clipboard(p.decode("utf-8", "replace"))
                elif mtype == P.T.BYE:
                    reason = "O computador remoto encerrou a sessão."
                    break
        except Exception as e:  # noqa: BLE001
            if not self._closed_by_user:
                reason = f"Conexão perdida ({e})."
        finally:
            self._running = False
            self._frames.put(None)
            if self.conn:
                self.conn.close()
            self.on_closed(None if self._closed_by_user else reason)

    # ----- envio -----
    def _send(self, mtype: int, payload: bytes = b"") -> None:
        if self.conn and self._running:
            try:
                self.conn.send(mtype, payload)
            except OSError:
                pass

    def mouse_move(self, nx: int, ny: int) -> None:
        self._send(P.T.MOUSE_MOVE, P.pack_mouse_move(nx, ny))

    def mouse_button(self, button: int, down: bool, nx: int, ny: int) -> None:
        self._send(P.T.MOUSE_BTN, P.pack_mouse_btn(button, down, nx, ny))

    def wheel(self, delta: int, horizontal: bool = False) -> None:
        self._send(P.T.MOUSE_WHEEL, P.pack_wheel(delta, horizontal))

    def key(self, vk: int, down: bool) -> None:
        self._send(P.T.KEY, P.pack_key(vk, down))

    def release_all(self) -> None:
        self._send(P.T.RELEASE_ALL)

    def clipboard(self, text: str) -> None:
        self._send(P.T.CLIPBOARD, text.encode("utf-8")[:P.MAX_CLIPBOARD])

    def mic(self, on: bool) -> None:
        self._send(P.T.MIC_CTRL, b"\x01" if on else b"\x00")

    def mic_data(self, data: bytes) -> None:
        self._send(P.T.MIC, data)

    def audio(self, on: bool) -> None:
        self._send(P.T.AUDIO_CTRL, b"\x01" if on else b"\x00")

    def settings(self, quality: int, fps: int, monitor: int, sharp_text: bool) -> None:
        self._send(P.T.SETTINGS, P.pack_settings(quality, fps, monitor, sharp_text))

    def close(self) -> None:
        self._closed_by_user = True
        self._send(P.T.BYE)
        self._running = False
        if self.conn:
            self.conn.close()
