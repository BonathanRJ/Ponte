"""Protocolo binário da Ponte.

Cada mensagem: [4 bytes tamanho (big-endian)] [1 byte tipo] [payload]
O tamanho conta o byte de tipo + payload.

Fluxo:
  servidor -> HELLO(versão, nonce de 32 bytes)
  cliente  -> AUTH(HMAC-SHA256(senha, nonce))
  servidor -> AUTH_OK(largura, altura, nº monitores) | AUTH_FAIL | BUSY | DENIED
  servidor -> FRAME ...   cliente -> FRAME_ACK (controle de fluxo)
  cliente  -> eventos de mouse/teclado/clipboard/configurações
"""
from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import threading

PROTOCOL_VERSION = 2  # 2: AUTH leva id/nome do PC; servidor pode pedir permissão (WAITING)
DEFAULT_PORT = 7070
MAX_MESSAGE = 64 * 1024 * 1024
MAX_CLIPBOARD = 1024 * 1024


class T:
    """Tipos de mensagem."""
    HELLO = 1
    AUTH = 2
    AUTH_OK = 3
    AUTH_FAIL = 4
    BUSY = 5
    DENIED = 6
    WAITING = 7         # servidor -> cliente: senha certa, aguardando a pessoa aceitar
    FRAME = 10
    FRAME_ACK = 11
    MONITORS = 12       # servidor -> cliente: retângulos dos monitores (modo "todos")
    NOTICE = 13         # servidor -> cliente: aviso em texto (ex.: janela de administrador)
    MOUSE_MOVE = 20
    MOUSE_BTN = 21
    MOUSE_WHEEL = 22
    KEY = 30
    RELEASE_ALL = 31
    CLIPBOARD = 40
    SETTINGS = 50
    AUDIO = 60          # servidor -> cliente: PCM 16 bits 48 kHz estéreo
    AUDIO_CTRL = 61     # cliente -> servidor: 1 byte (1 = ligar, 0 = desligar)
    AUDIO_STATUS = 62   # servidor -> cliente: texto (erro ao capturar)
    MIC = 63            # cliente -> servidor: PCM do microfone (mesmo formato)
    MIC_CTRL = 64       # cliente -> servidor: 1 byte (1 = ligar, 0 = desligar)
    BYE = 99


# Botões do mouse
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_X1, BTN_X2 = range(5)


class ProtocolError(Exception):
    pass


class AuthError(Exception):
    pass


class BusyError(Exception):
    pass


class DeniedError(Exception):
    pass


class Connection:
    """Socket com mensagens enquadradas. send() é thread-safe."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._send_lock = threading.Lock()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        _enable_keepalive(sock)

    def send(self, msg_type: int, payload: bytes = b"") -> None:
        header = struct.pack(">IB", len(payload) + 1, msg_type)
        with self._send_lock:
            if len(payload) < 65536:
                self.sock.sendall(header + payload)
            else:  # evita copiar payloads grandes (quadros)
                self.sock.sendall(header)
                self.sock.sendall(payload)

    def recv(self) -> tuple[int, bytes]:
        (size,) = struct.unpack(">I", self._recv_exact(4))
        if size < 1 or size > MAX_MESSAGE:
            raise ProtocolError(f"tamanho de mensagem inválido: {size}")
        body = self._recv_exact(size)
        return body[0], bytes(body[1:])

    def _recv_exact(self, n: int) -> bytearray:
        # Lê direto num buffer do tamanho certo (sem ir concatenando pedaços).
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = self.sock.recv_into(view[got:], min(n - got, 1 << 20))
            if not k:
                raise ConnectionError("conexão encerrada")
            got += k
        return buf

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _enable_keepalive(sock: socket.socket) -> None:
    """Percebe em ~20 s uma conexão que morreu sem aviso (Wi-Fi caiu, PC dormiu).

    Sem isso, a sessão podia ficar "congelada" para sempre esperando dados.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows: (ligar, ociosidade ms, intervalo ms)
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10_000, 2_000))
        elif hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
    except (OSError, AttributeError, ValueError):
        pass


# ---------- autenticação ----------

def new_nonce() -> bytes:
    return os.urandom(32)


def auth_digest(password: str, nonce: bytes) -> bytes:
    return hmac.new(password.encode("utf-8"), nonce, hashlib.sha256).digest()


def check_auth(password: str, nonce: bytes, digest: bytes) -> bool:
    return hmac.compare_digest(auth_digest(password, nonce), digest)


# ---------- payloads ----------

def pack_hello(nonce: bytes) -> bytes:
    return struct.pack(">H", PROTOCOL_VERSION) + nonce


def unpack_hello(p: bytes) -> tuple[int, bytes]:
    (ver,) = struct.unpack_from(">H", p)
    return ver, p[2:]


def pack_auth(digest: bytes, device_id: str, device_name: str) -> bytes:
    import json
    info = json.dumps({"id": device_id[:64], "name": device_name[:64]}).encode("utf-8")
    return digest + info


def unpack_auth(p: bytes) -> tuple[bytes, str, str]:
    import json
    digest, rest = p[:32], p[32:]
    try:
        info = json.loads(rest.decode("utf-8")) if rest else {}
    except Exception:
        info = {}
    return digest, str(info.get("id", ""))[:64], str(info.get("name", ""))[:64]


def pack_auth_ok(width: int, height: int, monitors: int) -> bytes:
    return struct.pack(">HHB", width, height, monitors)


def unpack_auth_ok(p: bytes) -> tuple[int, int, int]:
    return struct.unpack(">HHB", p)


_FRAME_HDR = struct.Struct(">HHH")
_TILE_HDR = struct.Struct(">HHHHI")


def pack_frame(width: int, height: int, tiles: list[tuple[int, int, int, int, bytes]]) -> bytes:
    parts = [_FRAME_HDR.pack(width, height, len(tiles))]
    for x, y, w, h, data in tiles:
        parts.append(_TILE_HDR.pack(x, y, w, h, len(data)))
        parts.append(data)
    return b"".join(parts)


def unpack_frame(p: bytes):
    width, height, n = _FRAME_HDR.unpack_from(p)
    off = _FRAME_HDR.size
    tiles = []
    for _ in range(n):
        x, y, w, h, size = _TILE_HDR.unpack_from(p, off)
        off += _TILE_HDR.size
        tiles.append((x, y, w, h, p[off:off + size]))
        off += size
    return width, height, tiles


def pack_mouse_move(nx: int, ny: int) -> bytes:
    return struct.pack(">HH", nx, ny)


def unpack_mouse_move(p: bytes) -> tuple[int, int]:
    return struct.unpack(">HH", p)


def pack_mouse_btn(button: int, down: bool, nx: int, ny: int) -> bytes:
    return struct.pack(">BBHH", button, 1 if down else 0, nx, ny)


def unpack_mouse_btn(p: bytes) -> tuple[int, bool, int, int]:
    b, d, x, y = struct.unpack(">BBHH", p)
    return b, bool(d), x, y


def pack_wheel(delta: int, horizontal: bool) -> bytes:
    return struct.pack(">hB", max(-32768, min(32767, delta)), 1 if horizontal else 0)


def unpack_wheel(p: bytes) -> tuple[int, bool]:
    d, h = struct.unpack(">hB", p)
    return d, bool(h)


def pack_key(vk: int, down: bool) -> bytes:
    return struct.pack(">HB", vk, 1 if down else 0)


def unpack_key(p: bytes) -> tuple[int, bool]:
    vk, d = struct.unpack(">HB", p)
    return vk, bool(d)


def pack_settings(quality: int, fps: int, monitor: int, sharp_text: bool) -> bytes:
    return struct.pack(">BBBB", quality, fps, monitor, 1 if sharp_text else 0)


def unpack_settings(p: bytes) -> tuple[int, int, int, bool]:
    q, f, m, s = struct.unpack(">BBBB", p)
    return q, f, m, bool(s)


ALL_MONITORS = 255
_MON = struct.Struct(">iiHH")


def pack_monitors(rects) -> bytes:
    """rects: [(x, y, w, h)] relativos ao canto da área "todos os monitores"."""
    return struct.pack(">B", len(rects)) + b"".join(_MON.pack(*r) for r in rects)


def unpack_monitors(p: bytes) -> list[tuple[int, int, int, int]]:
    (n,) = struct.unpack_from(">B", p)
    return [_MON.unpack_from(p, 1 + i * _MON.size) for i in range(n)]
