"""Configuração persistente (%APPDATA%\\Ponte\\config.json no Windows)."""
from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import string
import sys
import time
import uuid
from pathlib import Path

from .protocol import DEFAULT_PORT


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "Ponte"
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_password(length: int = 10) -> str:
    # Sem caracteres ambíguos (0/O, 1/l/I) para facilitar ditar por telefone.
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    return "".join(secrets.choice(alphabet) for _ in range(length))


DEFAULTS = {
    "password": "",
    "port": DEFAULT_PORT,
    "allow_on_start": False,
    "view_only": False,
    "recents": [],
    "quality": "Média",
    "show_banner": True,
    "audio": True,
    "volume": 100,
    "mic_device": "",        # microfone enviado ao PC remoto ("" = automático)
    "banner_compact": False,
    "banner_pos": None,
    "device_id": "",
    "saved_passwords": {},   # endereço -> senha criptografada (DPAPI)
    "remember_password": True,
    "trusted": {},
    "silent_host": False,
    "run_as_admin": False,   # abrir a Ponte como administrador (controlar janelas de admin)    # silenciar este PC durante o acesso
    "restore_output": "",    # saída de som a devolver (se a Ponte fechou com o som desviado)           # id do PC -> {"name": ..., "since": ...}
}


# ---------- senhas salvas: criptografia do próprio Windows (DPAPI) ----------
# Só o mesmo usuário do Windows, neste mesmo PC, consegue descriptografar.

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ENTROPY = b"Ponte-acesso-remoto"

    def _blob(data: bytes) -> _BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        b = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        b._buf = buf  # mantém o buffer vivo
        return b

    def _dpapi(data: bytes, protect: bool) -> bytes:
        inp, ent, out = _blob(data), _blob(_ENTROPY), _BLOB()
        fn = _crypt32.CryptProtectData if protect else _crypt32.CryptUnprotectData
        args = (ctypes.byref(inp), "Ponte" if protect else None, ctypes.byref(ent),
                None, None, 0x1, ctypes.byref(out))  # 0x1 = CRYPTPROTECT_UI_FORBIDDEN
        if not fn(*args):
            raise OSError(ctypes.get_last_error(), "DPAPI falhou")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def protect(text: str) -> str:
        return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")

    def unprotect(token: str) -> str | None:
        try:
            if token.startswith("dpapi:"):
                return _dpapi(base64.b64decode(token[6:]), False).decode("utf-8")
        except Exception:
            return None
        return None
else:  # fora do Windows (testes): só ofusca
    def protect(text: str) -> str:
        return "b64:" + base64.b64encode(text.encode("utf-8")).decode("ascii")

    def unprotect(token: str) -> str | None:
        try:
            if token.startswith("b64:"):
                return base64.b64decode(token[4:]).decode("utf-8")
        except Exception:
            return None
        return None


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "config.json")
        self.data = json.loads(json.dumps(DEFAULTS))  # cópia profunda
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            pass
        changed = False
        if not self.data.get("password"):
            self.data["password"] = generate_password()
            changed = True
        if not self.data.get("device_id"):
            self.data["device_id"] = uuid.uuid4().hex
            changed = True
        if changed:
            self.save()

    def __getitem__(self, key):
        if key not in self.data:
            self.data[key] = json.loads(json.dumps(DEFAULTS.get(key)))
        return self.data[key]

    @property
    def device_name(self) -> str:
        return socket.gethostname()

    # ----- senhas salvas (PC que conecta) -----
    def saved_password(self, address: str) -> str | None:
        token = self["saved_passwords"].get(address.strip())
        return unprotect(token) if token else None

    def save_password(self, address: str, password: str) -> None:
        self["saved_passwords"][address.strip()] = protect(password)
        self.save()

    def forget_password(self, address: str) -> None:
        if self["saved_passwords"].pop(address.strip(), None) is not None:
            self.save()

    # ----- computadores confiáveis (PC acessado) -----
    def is_trusted(self, device_id: str) -> bool:
        return bool(device_id) and device_id in self["trusted"]

    def trust(self, device_id: str, name: str) -> None:
        if device_id:
            self["trusted"][device_id] = {"name": name or "?", "since": time.strftime("%d/%m/%Y %H:%M")}
            self.save()

    def untrust_all(self) -> None:
        self.data["trusted"] = {}
        self.save()

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    def add_recent(self, address: str) -> None:
        rec = [a for a in self.data.get("recents", []) if a != address]
        self.data["recents"] = [address] + rec[:9]
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
