"""Diagnóstico de áudio da Ponte.

Testa, neste PC, exatamente o que a Ponte usa:
  1. captura do som do sistema (o que o PC remoto envia);
  2. reprodução (o que o seu PC toca);
  3. microfone.
Grava tudo em diagnostico_audio.txt, ao lado deste arquivo.
"""
import os
import platform
import sys
import threading
import time
import traceback

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "diagnostico_audio.txt")
lines = []


def log(msg=""):
    print(msg)
    lines.append(msg)


def section(title, fn):
    log(f"\n== {title} ==")
    try:
        fn()
    except Exception:
        log("ERRO:\n" + traceback.format_exc())


def info():
    log(f"Windows: {platform.platform()} | Python {sys.version.split()[0]}")
    import soundcard
    from ponte import __version__
    log(f"Ponte {__version__} | numpy {np.__version__} | soundcard {getattr(soundcard, '__version__', '?')}")
    from ponte import audio as A
    sc = A._soundcard()
    log(f"Saída padrão: {sc.default_speaker().name}")
    for s in sc.all_speakers():
        log(f"  saída: {s.name}  ({s.channels} canais)")
    try:
        log(f"Microfone padrão: {sc.default_microphone().name}")
    except Exception as e:  # noqa: BLE001
        log(f"Microfone padrão: nenhum ({e})")
    cable = A.find_virtual_cable()
    log(f"Cabo virtual escolhido para o microfone remoto: "
        f"{next((s.name for s in sc.all_speakers() if s.id == cable), 'nenhum')}")
    if A.default_output_is_cable():
        log("!! A SAÍDA PADRÃO É O CABO VIRTUAL — nada sai no fone/caixa deste PC. "
            "Troque no ícone de som da barra de tarefas.")


def level(chunks):
    if not chunks:
        return 0.0, 0.0
    from ponte import audio as A
    pcm = np.concatenate([A.from_pcm16(c) for c in chunks])
    return float(np.abs(pcm).max()), float(np.sqrt((pcm ** 2).mean()))


def test_capture():
    """Igual ao PC remoto: thread separada lendo o som do sistema."""
    from ponte import audio as A
    log("Gravando 5 s do som do sistema. DEIXE UMA MÚSICA/VÍDEO TOCANDO NESTE PC AGORA.")
    chunks, err = [], []

    def run():
        try:
            src = A.LoopbackSource()
            log(f"Capturando de: {src.name}")
            t0 = time.time()
            while time.time() - t0 < 5:
                chunks.append(src.read())
            src.close()
        except Exception:
            err.append(traceback.format_exc())

    t = threading.Thread(target=run)
    t.start()
    t.join(15)
    if err:
        log("ERRO na captura:\n" + err[0])
        return
    peak, rms = level(chunks)
    log(f"Blocos: {len(chunks)} (esperado ~250) | pico {peak:.3f} | média {rms:.4f}")
    if peak < 0.001:
        log(">> SILÊNCIO: nada estava tocando na saída padrão, ou o som sai por outro dispositivo.")
    else:
        log(">> OK: a captura do som do sistema funciona.")


def test_playback():
    """Igual ao PC que conecta: AudioPlayer tocando blocos de 20 ms."""
    from ponte import audio as A
    errors = []
    p = A.AudioPlayer(on_error=errors.append)
    log("Tocando um BIP de 2 s na saída padrão — você deve ouvir agora.")
    n = 0
    for i in range(100):  # 2 s
        t = (np.arange(A.BLOCK) + n) / A.RATE
        n += A.BLOCK
        tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        p.feed(A.to_pcm16(np.stack([tone, tone], axis=1)))
        time.sleep(0.02)
    time.sleep(0.5)
    p.close()
    log(f"Tocou em: {getattr(p, 'device_name', '?')}")
    log("ERRO: " + errors[0] if errors else ">> Se você ouviu o bip, a reprodução funciona.")


def test_mic():
    from ponte import audio as A
    log("Gravando 3 s do microfone padrão. FALE ALGUMA COISA AGORA.")
    chunks, errors = [], []
    m = A.MicStreamer(chunks.append, on_error=errors.append)
    time.sleep(3)
    m.close()
    time.sleep(0.2)
    if errors:
        log("ERRO: " + errors[0])
        return
    peak, rms = level(chunks)
    log(f"Blocos: {len(chunks)} (esperado ~150) | pico {peak:.3f} | média {rms:.4f}")
    log(">> OK: o microfone funciona." if peak > 0.01 else ">> Microfone em silêncio ou mudo.")


if __name__ == "__main__":
    section("Dispositivos", info)
    section("1. Captura do som do sistema", test_capture)
    section("2. Reprodução", test_playback)
    section("3. Microfone", test_mic)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nResultado salvo em {OUT}")
