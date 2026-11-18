"""Descobre como este computador pode ser encontrado (Tailscale, nome, IP local)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys


def _tailscale_exe() -> str | None:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cand = os.path.join(base, "Tailscale", "tailscale.exe")
            if os.path.exists(cand):
                return cand
    return None


def tailscale_ip() -> str | None:
    exe = _tailscale_exe()
    if not exe:
        return None
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                             timeout=4, **kwargs).stdout.strip().splitlines()
        return out[0].strip() if out else None
    except Exception:
        return None


def lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # não envia nada; só escolhe a interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def hostname() -> str:
    return socket.gethostname()


def parse_address(text: str, default_port: int) -> tuple[str, int]:
    """'100.64.0.2', 'meu-pc', 'meu-pc:7070' -> (host, porta)."""
    text = text.strip()
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            return host.strip(), int(port)
    return text, default_port
