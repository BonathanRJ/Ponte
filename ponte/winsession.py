"""Nome e ícone da Ponte no Mixer de volume do Windows.

O Windows agrupa o som de cada programa numa "sessão de áudio". Sem isto a
sessão apareceria como "Python" (o executável que roda a Ponte). Aqui usamos a
API de sessões (IAudioSessionManager / IAudioSessionControl) via ctypes, sem
dependências extras, para chamar SetDisplayName("Ponte") e SetIconPath(...).
"""
from __future__ import annotations

import os
import sys

import threading  # noqa: F401  (usado por start_presence)

from .log import log, log_exc

APP_NAME = "Ponte"
ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ponte.ico")


def ensure_icon() -> str | None:
    """Cria ponte.ico (quadrado vermelho com 'P', igual ao ícone da janela)."""
    if os.path.exists(ICON_PATH):
        return ICON_PATH
    try:
        from PIL import Image, ImageDraw, ImageFont
        size = 256
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((16, 16, size - 16, size - 16), radius=56, fill=(229, 72, 77, 255))
        font = None
        for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(name, 150)
                break
            except Exception:
                continue
        font = font or ImageFont.load_default()
        box = d.textbbox((0, 0), "P", font=font)
        w, h = box[2] - box[0], box[3] - box[1]
        d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), "P", font=font, fill="white")
        img.save(ICON_PATH, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
        return ICON_PATH
    except Exception as e:  # noqa: BLE001
        log_exc("não consegui criar ponte.ico", e)
        return None


if sys.platform == "win32":
    import ctypes
    from ctypes import HRESULT, POINTER, WINFUNCTYPE, byref, c_void_p, c_wchar_p
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    _ole32 = ctypes.OleDLL("ole32")

    def _guid(text: str) -> GUID:
        g = GUID()
        _ole32.IIDFromString(c_wchar_p(text), byref(g))
        return g

    CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    IID_IAudioSessionManager = "{BFA971F1-4D5E-40BB-935E-967039BFBEE4}"
    CLSCTX_ALL = 0x17

    def _method(obj: c_void_p, index: int, *argtypes):
        """Função da vtable de um objeto COM (índice conforme o .h do Windows SDK)."""
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        return WINFUNCTYPE(HRESULT, c_void_p, *argtypes)(vtbl[index])

    def _release(obj: c_void_p) -> None:
        if obj and obj.value:
            vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
            WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtbl[2])(obj)

    def name_session(device_id: str | None = None) -> bool:
        """Dá nome/ícone à sessão de áudio padrão DESTE processo no dispositivo.

        device_id: ID do WASAPI (soundcard Speaker.id); None = saída padrão.
        Precisa ser chamado numa thread com COM inicializado.
        """
        enum = c_void_p()
        dev = c_void_p()
        mgr = c_void_p()
        ctl = c_void_p()
        try:
            _ole32.CoCreateInstance(byref(_guid(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                                    byref(_guid(IID_IMMDeviceEnumerator)), byref(enum))
            if device_id:
                # IMMDeviceEnumerator::GetDevice (5)
                _method(enum, 5, c_wchar_p, POINTER(c_void_p))(enum, device_id, byref(dev))
            else:
                # IMMDeviceEnumerator::GetDefaultAudioEndpoint (4): eRender=0, eConsole=0
                _method(enum, 4, wintypes.DWORD, wintypes.DWORD, POINTER(c_void_p))(enum, 0, 0, byref(dev))
            # IMMDevice::Activate (3)
            _method(dev, 3, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))(
                dev, byref(_guid(IID_IAudioSessionManager)), CLSCTX_ALL, None, byref(mgr))
            # IAudioSessionManager::GetAudioSessionControl (3): sessão padrão (GUID nulo)
            _method(mgr, 3, c_void_p, wintypes.DWORD, POINTER(c_void_p))(mgr, None, 0, byref(ctl))
            # IAudioSessionControl::SetDisplayName (5) e SetIconPath (7)
            _method(ctl, 5, c_wchar_p, c_void_p)(ctl, APP_NAME, None)
            icon = ensure_icon()
            if icon:
                _method(ctl, 7, c_wchar_p, c_void_p)(ctl, icon, None)
            return True
        except Exception as e:  # noqa: BLE001
            log_exc("não consegui dar nome à sessão de áudio", e)
            return False
        finally:
            for obj in (ctl, mgr, dev, enum):
                _release(obj)
else:
    def name_session(device_id: str | None = None) -> bool:  # noqa: ARG001
        return False


_presence_started = False


def start_presence() -> None:
    """Deixa a Ponte listada no Mixer de volume desde que abre.

    Cria um fluxo de saída INICIALIZADO MAS PARADO na saída padrão: isso cria a
    sessão de áudio (que o Mixer lista) sem tocar nada, sem gastar CPU e sem
    impedir o PC de suspender. A thread fica viva para manter o fluxo.
    """
    global _presence_started
    if _presence_started or sys.platform != "win32":
        return
    _presence_started = True
    import threading

    def run():
        try:
            from . import audio as A
            ok, why = A.available()
            if not ok:
                log(f"sessão no mixer não criada: {why}")
                return
            sc = A._soundcard()  # inicializa COM nesta thread
            speaker = sc.default_speaker()
            idle_player = speaker.player(samplerate=A.RATE, channels=A.CHANNELS,
                                         blocksize=A.WASAPI_BUFFER)  # sem __enter__ = não toca
            name_session(speaker.id)
            log(f"Ponte registrada no Mixer de volume ({speaker.name})")
            threading.Event().wait()  # mantém idle_player vivo enquanto o app roda
            del idle_player
        except Exception as e:  # noqa: BLE001
            log_exc("não consegui registrar a Ponte no Mixer de volume", e)

    threading.Thread(target=run, name="ponte-mixer", daemon=True).start()


# ---------- trocar a saída padrão do Windows ----------
# Não existe API pública para isso; todos os programas que fazem (SoundSwitch,
# AudioSwitcher, nircmd) usam a interface IPolicyConfig, estável desde o Windows 7.

if sys.platform == "win32":
    CLSID_PolicyConfigClient = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
    IID_IPolicyConfig = "{F8679F50-850A-41CF-9C72-430F290290C8}"

    def set_default_output(device_id: str) -> None:
        """Define a saída padrão (Console, Multimídia e Comunicações). Levanta OSError se falhar."""
        cfg = c_void_p()
        _ole32.CoCreateInstance(byref(_guid(CLSID_PolicyConfigClient)), None, CLSCTX_ALL,
                                byref(_guid(IID_IPolicyConfig)), byref(cfg))
        try:
            set_default = _method(cfg, 13, c_wchar_p, wintypes.DWORD)  # IPolicyConfig::SetDefaultEndpoint
            for role in (0, 1, 2):  # eConsole, eMultimedia, eCommunications
                set_default(cfg, device_id, role)
        finally:
            _release(cfg)
else:
    def set_default_output(device_id: str) -> None:  # noqa: ARG001
        raise OSError("só funciona no Windows")


class OutputRouter:
    """Manda o som do Windows para o cabo virtual durante o acesso e depois devolve.

    A saída anterior fica gravada na configuração ("restore_output") ANTES da
    troca: se a Ponte fechar no meio da sessão, recover() devolve o som na
    próxima vez que ela abrir.
    """

    def __init__(self, cfg, set_default=None, current_default=None, find_cable=None):
        from . import audio as A
        self.cfg = cfg
        self._set_default = set_default or set_default_output
        self._current = current_default or A.default_speaker_id
        self._find_cable = find_cable or A.find_virtual_cable

    def silence(self) -> str | None:
        """Troca para o cabo. Retorna uma mensagem de erro, ou None se deu certo."""
        cable = self._find_cable()
        if not cable:
            return ("para silenciar o PC acessado, instale o VB-CABLE nele "
                    "(vb-audio.com/Cable)")
        current = self._current()
        if current == cable:
            return None  # já está no cabo (ex.: o próprio usuário escolheu)
        if not self.cfg["restore_output"]:
            self.cfg["restore_output"] = current or ""
        try:
            self._set_default(cable)
            log(f"saída padrão trocada para o cabo virtual (antes: {current})")
            return None
        except Exception as e:  # noqa: BLE001
            self.cfg["restore_output"] = ""
            return log_exc("não consegui trocar a saída de som", e)

    def restore(self) -> None:
        prev = self.cfg["restore_output"]
        if not prev:
            return
        try:
            self._set_default(prev)
            log(f"saída padrão devolvida para {prev}")
        except Exception as e:  # noqa: BLE001
            log_exc("não consegui devolver a saída de som", e)
        finally:
            self.cfg["restore_output"] = ""

    def recover(self) -> None:
        """Ao abrir a Ponte: se ela fechou com o som desviado, devolve."""
        if self.cfg["restore_output"]:
            log("a Ponte fechou com o som desviado para o cabo; devolvendo")
            self.restore()
