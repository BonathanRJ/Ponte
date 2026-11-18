"""Áudio do computador remoto.

Captura: grava o que está tocando nas caixas/fones (WASAPI "loopback") com a
biblioteca `soundcard`. Reprodução: toca no dispositivo de som padrão.

Formato na rede: PCM 16 bits, 48 kHz, estéreo, blocos de 20 ms
(3840 bytes; ~1,5 Mbit/s). Sem compressão, para não exigir codecs extras.
"""
from __future__ import annotations

import queue
import sys
import threading
import warnings

import numpy as np

from .log import log, log_exc

RATE = 48000
CHANNELS = 2
BLOCK = 960  # 20 ms
WASAPI_BUFFER = BLOCK * 5  # 100 ms de folga no buffer do Windows (evita engasgos/silêncio)


def _com_init():
    """soundcard usa COM no Windows; cada thread precisa inicializar."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        except Exception:
            pass


_import_lock = threading.Lock()


def _import_soundcard():
    """Importa o soundcard numa thread nova e "limpa".

    Ao ser importado, o soundcard chama CoInitializeEx e trata como ERRO o
    retorno S_FALSE ("COM já iniciado nesta thread") — era o
    "RuntimeError: Error 0x100000001". Uma thread recém-criada nunca tem COM
    iniciado, então a importação sempre funciona.
    """
    mod = sys.modules.get("soundcard")
    if mod is not None and hasattr(mod, "default_speaker"):
        return mod
    with _import_lock:
        mod = sys.modules.get("soundcard")
        if mod is not None and hasattr(mod, "default_speaker"):
            return mod
        result = {}

        def run():
            try:
                warnings.filterwarnings("ignore", module="soundcard")
                warnings.filterwarnings("ignore", message=".*discontinuity.*")
                import soundcard  # noqa: WPS433
                result["mod"] = soundcard
            except BaseException as e:  # noqa: BLE001
                result["err"] = e

        t = threading.Thread(target=run, name="ponte-import-soundcard", daemon=True)
        t.start()
        t.join()
        if "err" in result:
            raise result["err"]
        return result["mod"]


def _soundcard():
    """soundcard pronto para uso na thread atual (que também precisa de COM)."""
    mod = _import_soundcard()
    _com_init()
    return mod


def available() -> tuple[bool, str]:
    try:
        import importlib.util
        if importlib.util.find_spec("soundcard") is None:
            return False, "biblioteca 'soundcard' não instalada (rode o instalar.bat de novo)"
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def to_pcm16(block: np.ndarray) -> bytes:
    if block.ndim == 1:
        block = np.repeat(block[:, None], CHANNELS, axis=1)
    elif block.shape[1] == 1:
        block = np.repeat(block, CHANNELS, axis=1)
    elif block.shape[1] > CHANNELS:
        block = block[:, :CHANNELS]
    return (np.clip(block, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def from_pcm16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").reshape(-1, CHANNELS).astype(np.float32) / 32768.0


class LoopbackSource:
    """Lê o som do sistema em blocos de 20 ms. Usar de dentro de uma única thread."""

    def __init__(self):
        sc = _soundcard()
        speaker = sc.default_speaker()
        self.name = speaker.name
        self.device_id = speaker.id
        # Pelo ID exato: por nome, "CABLE Input" e "CABLE In 16ch" podem se confundir.
        mic = sc.get_microphone(id=speaker.id, include_loopback=True)
        self._rec = mic.recorder(samplerate=RATE, channels=CHANNELS, blocksize=WASAPI_BUFFER)
        self._rec.__enter__()
        log(f"captura do som do sistema aberta em: {self.name}")

    def read(self) -> bytes:
        return to_pcm16(self._rec.record(numframes=BLOCK))

    def close(self):
        try:
            self._rec.__exit__(None, None, None)
        except Exception:
            pass


VIRTUAL_CABLE_HINTS = ("cable input", "vb-audio", "voicemeeter input", "virtual audio")


def find_virtual_cable() -> str | None:
    """ID da 'saída' do cabo virtual (onde a Ponte toca a voz recebida).

    Prefere o "CABLE Input" comum ao "CABLE In 16ch": é o comum que aparece
    como "CABLE Output" nos apps de call.
    """
    try:
        sc = _soundcard()
        cands = [s for s in sc.all_speakers()
                 if any(h in s.name.lower() for h in VIRTUAL_CABLE_HINTS)]
        cands.sort(key=lambda s: ("16ch" in s.name.lower(), s.name.lower()))
        if cands:
            return cands[0].id
    except Exception:
        pass
    return None


def default_speaker_id() -> str | None:
    """ID da saída padrão atual do Windows (para seguir trocas durante a sessão)."""
    try:
        return _soundcard().default_speaker().id
    except Exception:
        return None


def default_output_is_cable() -> str | None:
    """Nome da saída padrão do Windows se ela for o cabo virtual (configuração errada)."""
    try:
        name = _soundcard().default_speaker().name
        if any(h in name.lower() for h in VIRTUAL_CABLE_HINTS):
            return name
    except Exception:
        pass
    return None


# Microfones "falsos" que o Windows às vezes deixa como padrão (não captam a sua voz).
VIRTUAL_MIC_HINTS = ("steam streaming", "cable output", "cable in", "vb-audio", "voicemeeter",
                     "virtual audio", "stereo mix", "mixagem estéreo", "what u hear")


def _is_virtual_mic(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in VIRTUAL_MIC_HINTS)


def _in_fresh_thread(fn):
    """Roda `fn` numa thread nova (COM limpo) e devolve o resultado."""
    result = {}

    def run():
        try:
            result["ok"] = fn(_soundcard())
        except BaseException as e:  # noqa: BLE001
            result["err"] = e

    t = threading.Thread(target=run, name="ponte-audio-query", daemon=True)
    t.start()
    t.join(5)
    if "err" in result:
        raise result["err"]
    return result.get("ok")


def list_microphones() -> tuple[list[tuple[str, str]], str | None]:
    """([(id, nome), ...], id do microfone padrão do Windows). Pode ser chamada da interface."""
    def query(sc):
        mics = [(m.id, m.name) for m in sc.all_microphones()]
        try:
            default = sc.default_microphone().id
        except Exception:
            default = None
        return mics, default
    try:
        return _in_fresh_thread(query) or ([], None)
    except Exception:
        return [], None


def _mic_candidates(sc, preferred_id: str | None) -> list:
    """Ordem de tentativa: o escolhido, o padrão (se for real), os outros reais, o resto."""
    mics = list(sc.all_microphones())
    try:
        default = sc.default_microphone()
    except Exception:
        default = None
    order = []
    if preferred_id:
        order += [m for m in mics if m.id == preferred_id]
    if default is not None and not _is_virtual_mic(default.name):
        order.append(default)
    order += [m for m in mics if not _is_virtual_mic(m.name)]
    if default is not None:
        order.append(default)
    order += mics
    seen, out = set(), []
    for m in order:
        if m.id not in seen:
            seen.add(m.id)
            out.append(m)
    return out


class MicSource:
    """Lê um microfone deste PC em blocos de 20 ms.

    `mic_id` vazio = automático: o padrão do Windows, a não ser que seja um
    microfone virtual (ex.: "Steam Streaming Microphone", que não capta nada).
    Se um aparelho não abrir (o soundcard recusa alguns formatos com
    AssertionError), tenta o próximo.
    """

    def __init__(self, mic_id: str | None = None):
        sc = _soundcard()
        errors = []
        for mic in _mic_candidates(sc, mic_id):
            try:
                rec = mic.recorder(samplerate=RATE, channels=CHANNELS, blocksize=WASAPI_BUFFER)
                rec.__enter__()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{mic.name}: {type(e).__name__}")
                log(f"microfone não abriu: {mic.name} ({type(e).__name__}: {e})")
                continue
            self._rec = rec
            self.name = mic.name
            self.id = mic.id
            if mic_id and mic.id != mic_id:
                log(f"o microfone escolhido não abriu; usando {mic.name}")
            log(f"microfone aberto: {self.name}")
            return
        raise RuntimeError("nenhum microfone abriu" + (f" ({'; '.join(errors)})" if errors else ""))

    def read(self) -> bytes:
        return to_pcm16(self._rec.record(numframes=BLOCK))

    def close(self):
        try:
            self._rec.__exit__(None, None, None)
        except Exception:
            pass


class MicStreamer:
    """Thread que lê o microfone e entrega cada bloco para `send`."""

    def __init__(self, send, on_error=lambda msg: None, source_factory=MicSource,
                 mic_id: str | None = None, on_open=lambda name: None):
        self.send = send
        self.on_error = on_error
        self.on_open = on_open
        self.source_factory = source_factory
        self.mic_id = mic_id
        self.muted = False
        self.name = ""
        self.peak = 0  # maior volume desde a última leitura (para o indicador da janela)
        self._running = True
        threading.Thread(target=self._run, name="ponte-mic", daemon=True).start()

    def take_peak(self) -> int:
        p, self.peak = self.peak, 0
        return p

    def _run(self):
        try:
            src = (self.source_factory(self.mic_id) if self.source_factory is MicSource
                   else self.source_factory())
        except Exception as e:  # noqa: BLE001
            self.on_error(log_exc("Não consegui abrir o microfone", e))
            return
        self.name = getattr(src, "name", "")
        self.on_open(self.name)
        n = 0
        try:
            while self._running:
                data = src.read()
                n += 1
                if n % 5 == 0:  # amostra o volume sem pesar
                    a = np.frombuffer(data, dtype="<i2")
                    if a.size:
                        self.peak = max(self.peak, int(np.abs(a.astype(np.int32)).max()))
                if self._running and not self.muted:
                    self.send(data)
        except Exception as e:  # noqa: BLE001
            if self._running:
                self.on_error(log_exc("O microfone parou", e))
        finally:
            src.close()

    def close(self):
        self._running = False


class AudioPlayer:
    """Toca os blocos recebidos com um pequeno buffer anti-engasgo.

    Se a rede atrasar e acumular mais de MAX_QUEUE blocos, descarta os mais
    antigos para o som não ficar atrasado em relação à imagem.
    """
    PREBUFFER = 3    # 60 ms antes de começar
    MAX_QUEUE = 12   # 240 ms
    KEEP = 4

    def __init__(self, on_error=lambda msg: None, speaker_name: str | None = None):
        self.speaker_name = speaker_name
        self.q: queue.Queue[bytes] = queue.Queue()
        self.volume = 1.0
        self.on_error = on_error
        self.device_name = "?"
        self.played = 0      # blocos tocados (para o contador da janela)
        self.error = ""
        self._running = True
        self._thread = threading.Thread(target=self._run, name="ponte-audio-play", daemon=True)
        self._thread.start()

    def feed(self, data: bytes) -> None:
        self.q.put(data)
        if self.q.qsize() > self.MAX_QUEUE:
            while self.q.qsize() > self.KEEP:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break

    def flush(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def _run(self):
        # Se o fone for desconectado ou a saída padrão mudar, o Windows invalida o
        # dispositivo (erro 0x88890004) — antes o som morria até reconectar.
        # Agora reabre sozinho, na saída padrão atual.
        fails = 0
        while self._running:
            played_before = self.played
            try:
                self._play_until_stopped()
                return
            except Exception as e:  # noqa: BLE001
                if self.played - played_before > 50:
                    fails = 0  # estava tocando bem: é uma falha nova
                fails += 1
                if fails >= 5:
                    self.error = log_exc("Não consegui tocar o áudio", e)
                    self.on_error(self.error)
                    return
                log(f"reprodução de áudio caiu ({type(e).__name__}: {e}); reabrindo")
                self.flush()
                threading.Event().wait(0.5 * fails)

    def _play_until_stopped(self):
        sc = _soundcard()
        speaker = (sc.get_speaker(self.speaker_name) if self.speaker_name
                   else sc.default_speaker())
        self.device_name = speaker.name
        log(f"reprodução aberta em: {speaker.name}")
        with speaker.player(samplerate=RATE, channels=CHANNELS, blocksize=WASAPI_BUFFER) as player:
            from .winsession import name_session
            name_session(speaker.id)  # aparece como "Ponte" no Mixer de volume
            while self._running:
                # espera juntar um pouco antes de começar (ou recomeçar)
                while self._running and self.q.qsize() < self.PREBUFFER:
                    threading.Event().wait(0.01)
                while self._running:
                    try:
                        data = self.q.get(timeout=0.2)
                    except queue.Empty:
                        break  # ficou sem som: volta a pré-carregar
                    samples = from_pcm16(data)
                    if self.volume != 1.0:
                        samples = samples * self.volume
                    player.play(samples)
                    self.played += 1

    def close(self):
        self._running = False
