"""Teste de ponta a ponta sem interface: servidor + cliente em 127.0.0.1,
com uma "tela" falsa que muda a cada captura.

Rodar:  python -m tests.test_loopback   (na pasta Ponte)
"""
import io
import threading
import time

import numpy as np
from PIL import Image

from ponte import protocol as P
from ponte.capture import Rect
from ponte.client import RemoteClient
from ponte.host import HostServer
from ponte.winput import RecordingInjector

W, H = 800, 600


class FakeCapture:
    """Tela sintética: gradiente + um quadrado que anda (simula mudança parcial)."""
    tick = 0

    def __init__(self):
        yy, xx = np.mgrid[0:H, 0:W]
        base = np.zeros((H, W, 4), np.uint8)
        base[..., 0] = (xx * 255 // W)
        base[..., 1] = (yy * 255 // H)
        base[..., 2] = 128
        base[..., 3] = 255
        self.base = base

    def monitors(self):
        return [Rect(0, 0, W, H), Rect(W, 0, W, H)]

    def current(self):
        f = self.base.copy()
        x = (FakeCapture.tick * 37) % (W - 100)
        f[200:300, x:x + 100, :3] = (255, 255, 0)
        return f

    def grab(self, monitor=0):
        FakeCapture.tick += 1
        if monitor == P.ALL_MONITORS:  # dois monitores lado a lado
            f = np.concatenate([self.current(), self.current()[:, ::-1]], axis=1)
            return np.ascontiguousarray(f), "RGBX", Rect(0, 0, 2 * W, H)
        return self.current(), "RGBX", self.monitors()[monitor]

    def close(self):
        pass


class FakeAudio:
    """Gera um tom de 440 Hz em blocos de 20 ms, no ritmo real."""
    def __init__(self):
        from ponte import audio as A
        self.A = A
        self.n = 0

    def read(self):
        A = self.A
        time.sleep(0.02)
        t = (np.arange(A.BLOCK) + self.n) / A.RATE
        self.n += A.BLOCK
        tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        return A.to_pcm16(np.stack([tone, tone], axis=1))

    def close(self):
        pass


def check(cond, msg):
    print(("  OK   " if cond else "  FALHOU ") + msg)
    if not cond:
        raise SystemExit(1)


def main():
    inj = RecordingInjector()
    events = []
    mic_received = []

    class FakeMicSink:
        def __init__(self, on_error):
            self.closed = False
        def feed(self, data):
            mic_received.append(data)
        def close(self):
            self.closed = True

    server = HostServer(lambda: "segredo123", inj, port=0, bind="127.0.0.1",
                        capture_factory=FakeCapture, audio_factory=FakeAudio,
                        mic_sink_factory=FakeMicSink,
                        on_event=lambda k, d: events.append((k, d)))
    server.start()
    port = server.port
    print(f"servidor em 127.0.0.1:{port}")

    # 1) senha errada
    bad = RemoteClient("127.0.0.1", port, "errada", lambda *a: None)
    try:
        bad.connect()
        check(False, "senha errada deveria falhar")
    except P.AuthError:
        check(True, "senha errada é recusada")

    # 2) conexão válida e montagem da imagem
    canvas = {"img": None, "frames": 0, "bytes": 0}
    closed = threading.Event()

    def on_frame(w, h, tiles):
        if canvas["img"] is None or canvas["img"].size != (w, h):
            canvas["img"] = Image.new("RGB", (w, h))
        for x, y, tw, th, data in tiles:
            canvas["img"].paste(Image.open(io.BytesIO(data)), (x, y))
            canvas["bytes"] += len(data)
        canvas["frames"] += 1

    clip = []
    cli = RemoteClient("127.0.0.1", port, "segredo123", on_frame,
                       on_clipboard=clip.append, on_closed=lambda r: closed.set())
    cli.connect()
    check((cli.width, cli.height, cli.monitors) == (W, H, 2), "AUTH_OK traz tamanho e nº de monitores")
    cli.start()
    cli.settings(90, 30, 0, True)
    time.sleep(1.5)
    check(canvas["frames"] > 5, f"recebeu quadros ({canvas['frames']} em 1,5 s)")
    avg = canvas["bytes"] / max(1, canvas["frames"])
    print(f"       média {avg/1024:.1f} KiB/quadro (o 1º é a tela inteira; os demais, só o que mudou)")

    # compara a imagem montada com a "tela" do servidor (tolerância de JPEG)
    time.sleep(0.2)
    ref = Image.frombuffer("RGB", (W, H), np.ascontiguousarray(FakeCapture().current()), "raw", "RGBX", 0, 1).convert("RGB")
    got = np.asarray(canvas["img"], np.int16)
    err_static = np.abs(got[400:, :] - np.asarray(ref, np.int16)[400:, :]).mean()
    check(err_static < 4, f"imagem reconstruída confere (erro médio {err_static:.2f})")

    # 3) entrada
    cli.mouse_move(32767, 32767)
    cli.mouse_button(P.BTN_LEFT, True, 100, 200)
    cli.mouse_button(P.BTN_LEFT, False, 100, 200)
    cli.wheel(-120)
    cli.key(0x41, True)
    cli.key(0x41, False)
    cli.release_all()
    time.sleep(0.3)
    kinds = [e[0] for e in inj.events]
    check(("move", 32767, 32767) in inj.events, "movimento do mouse chegou")
    check(("btn", P.BTN_LEFT, True, 100, 200) in inj.events, "clique chegou")
    check(("wheel", -120, False) in inj.events, "rolagem chegou")
    check(("key", 0x41, True) in inj.events and ("key", 0x41, False) in inj.events, "tecla A chegou")
    check("release_all" in kinds, "soltar teclas chegou")

    # 4) clipboard nos dois sentidos
    cli.clipboard("olá do cliente")
    server.send_clipboard("olá do servidor")
    time.sleep(0.3)
    check(("clipboard", "olá do cliente") in events, "clipboard cliente -> servidor")
    check(clip == ["olá do servidor"], "clipboard servidor -> cliente")

    # 4b) áudio: só chega depois de pedir, e para quando desligado
    from ponte import audio as A
    chunks = []
    cli.on_audio = chunks.append
    time.sleep(0.3)
    check(not chunks, "sem áudio antes de ligar")
    cli.audio(True)
    time.sleep(1.0)
    n = len(chunks)
    check(35 <= n <= 60, f"áudio chegando em tempo real ({n} blocos de 20 ms em 1 s)")
    pcm = np.concatenate([A.from_pcm16(c) for c in chunks])
    check(pcm.shape[1] == 2 and abs(np.abs(pcm).max() - 0.5) < 0.01, "áudio estéreo e amplitude correta")
    spec = np.abs(np.fft.rfft(pcm[:, 0]))
    peak = np.argmax(spec) * A.RATE / len(pcm)
    check(abs(peak - 440) < 5, f"tom de 440 Hz reconhecido ({peak:.0f} Hz)")
    cli.audio(False)
    time.sleep(0.2)
    n = len(chunks)
    time.sleep(0.4)
    check(len(chunks) == n, "áudio para ao desligar")
    cli.audio(True)
    time.sleep(0.3)
    check(len(chunks) > n, "áudio volta ao religar")
    cli.audio(False)

    # 4c) microfone: cliente -> servidor, usando MicStreamer com fonte falsa
    status = []
    cli.on_audio_status = status.append
    cli.mic_data(b"\x01" * 3840)
    time.sleep(0.2)
    check(not mic_received, "microfone ignorado antes de ligar")
    cli.mic(True)
    streamer = A.MicStreamer(cli.mic_data, source_factory=FakeAudio)
    time.sleep(1.0)
    streamer.muted = True
    time.sleep(0.1)
    n = len(mic_received)
    check(35 <= n <= 60, f"microfone chegando no servidor ({n} blocos em 1 s)")
    check(any("CABLE Output" in s for s in status), "servidor confirma e explica o 'CABLE Output'")
    time.sleep(0.3)
    check(len(mic_received) == n, "mudo para de enviar")
    streamer.close()
    cli.mic(False)
    time.sleep(0.2)
    cli.mic_data(b"\x01" * 3840)
    time.sleep(0.2)
    check(len(mic_received) == n, "microfone ignorado depois de desligar")

    # sem VB-CABLE: erro claro em vez de tocar nas caixas
    from ponte.host import _default_mic_sink
    try:
        _default_mic_sink(lambda m: None)
        check(False, "deveria exigir VB-CABLE/soundcard")
    except RuntimeError as e:
        check(True, f"sem cabo virtual: '{e}'")

    # 4d) geometria dos monitores e modo "todos os monitores"
    check(cli.monitor_rects == [(0, 0, W, H), (W, 0, W, H)],
          f"cliente recebe onde fica cada monitor ({cli.monitor_rects})")
    sizes = []
    cli.on_frame = lambda w, h, tiles: (sizes.append((w, h)), on_frame(w, h, tiles))
    cli.settings(60, 30, P.ALL_MONITORS, False)
    time.sleep(0.5)
    check(sizes and sizes[-1] == (2 * W, H), f"modo 'todos': imagem com os dois monitores {sizes[-1:]}")
    cli.mouse_move(65535, 0)
    time.sleep(0.2)
    check(("move", 65535, 0) in inj.events, "mouse no modo 'todos' chega normalizado na área inteira")
    cli.settings(60, 30, 0, False)
    time.sleep(0.4)
    check(sizes[-1] == (W, H), "voltar para um monitor só")
    cli.on_frame = on_frame

    # 5) troca de monitor gera quadro inteiro
    before = canvas["frames"]
    cli.settings(60, 30, 1, False)
    time.sleep(0.4)
    check(canvas["frames"] > before, "troca de monitor continua enviando")

    # 6) segunda conexão enquanto ocupado
    other = RemoteClient("127.0.0.1", port, "segredo123", lambda *a: None)
    try:
        other.connect()
        check(False, "deveria estar ocupado")
    except P.BusyError:
        check(True, "segunda conexão recebe 'ocupado'")

    # 7) servidor derruba a sessão (botão Desconectar)
    check(server.connected_peer == "127.0.0.1", "servidor sabe quem está conectado")
    server.disconnect()
    check(closed.wait(3), "cliente percebe a desconexão")
    time.sleep(0.3)
    check(any(k == "disconnected" for k, _ in events), "evento 'disconnected' disparado")
    check(server.connected_peer is None, "servidor livre para nova sessão")

    # 8) reconecta
    cli2 = RemoteClient("127.0.0.1", port, "segredo123", on_frame)
    cli2.connect()
    cli2.start()
    time.sleep(0.3)
    cli2.close()
    check(True, "reconexão funciona")

    # 9) bloqueio após várias senhas erradas
    for _ in range(5):
        try:
            RemoteClient("127.0.0.1", port, "x", lambda *a: None).connect()
        except P.AuthError:
            pass
    try:
        RemoteClient("127.0.0.1", port, "segredo123", lambda *a: None).connect()
        check(False, "IP deveria estar bloqueado")
    except P.AuthError:
        check(True, "IP bloqueado após 5 senhas erradas (mesmo com a senha certa)")

    server.stop()

    # 10) pedido de permissão e computadores confiáveis
    asked = []
    decision = {"accept": True}
    trusted = {"pc-confiavel"}

    def ask(peer, dev_id, name):
        asked.append((peer, dev_id, name))
        time.sleep(0.3)  # "pessoa pensando"
        return decision["accept"]

    srv2 = HostServer(lambda: "segredo123", RecordingInjector(), port=0, bind="127.0.0.1",
                      capture_factory=FakeCapture, audio_factory=FakeAudio,
                      mic_sink_factory=FakeMicSink,
                      is_trusted=lambda d: d in trusted, ask_permission=ask)
    srv2.start()

    def try_connect(dev_id, name="PC-CASA", pw="segredo123"):
        waited = []
        c = RemoteClient("127.0.0.1", srv2.port, pw, lambda *a: None,
                         device_id=dev_id, device_name=name, on_waiting=lambda: waited.append(1))
        c.connect()
        c.start()
        time.sleep(0.2)
        label = srv2.connected_label
        c.close()
        time.sleep(0.3)
        return waited, label

    waited, label = try_connect("pc-novo")
    check(waited and asked[-1] == ("127.0.0.1", "pc-novo", "PC-CASA"),
          "PC novo: cliente avisado e servidor pergunta com nome e id")
    check(label == "PC-CASA (127.0.0.1)", f"sessão identificada como '{label}'")

    decision["accept"] = False
    try:
        try_connect("pc-novo")
        check(False, "recusa deveria falhar")
    except P.DeniedError:
        check(True, "recusar a permissão bloqueia a conexão")

    n = len(asked)
    waited, _ = try_connect("pc-confiavel")
    check(not waited and len(asked) == n, "PC confiável entra sem perguntar")

    try:
        try_connect("pc-confiavel", pw="errada")
        check(False, "senha errada deveria falhar mesmo confiável")
    except P.AuthError:
        check(True, "PC confiável ainda precisa da senha certa")
    check(len(asked) == n, "senha errada nem chega a pedir permissão")
    srv2.stop()

    # 12) silenciar o PC acessado + seguir a saída padrão
    import tempfile
    from pathlib import Path
    from ponte.config import Config
    from ponte.winsession import OutputRouter
    state = {"default": "id-realtek"}
    opened = []

    class DevAudio(FakeAudio):
        def __init__(self):
            super().__init__()
            self.device_id = state["default"]
            self.name = self.device_id
            opened.append(self.device_id)

    with tempfile.TemporaryDirectory() as d:
        rcfg = Config(Path(d) / "r.json")
        switches = []

        def set_default(dev):
            switches.append(dev)
            state["default"] = dev

        router = OutputRouter(rcfg, set_default=set_default,
                              current_default=lambda: state["default"],
                              find_cable=lambda: "id-cable")
        silent = {"on": True}
        srv3 = HostServer(lambda: "segredo123", RecordingInjector(), port=0, bind="127.0.0.1",
                          capture_factory=FakeCapture, audio_factory=DevAudio,
                          mic_sink_factory=FakeMicSink,
                          silent_mode=lambda: silent["on"], output_router=router,
                          default_output_id=lambda: state["default"])
        srv3.start()
        msgs = []
        c3 = RemoteClient("127.0.0.1", srv3.port, "segredo123", lambda *a: None,
                          on_audio_status=msgs.append)
        c3.connect()
        c3.start()
        c3.audio(True)
        time.sleep(0.4)
        check(switches == ["id-cable"] and opened == ["id-cable"],
              "silenciar: saída vai para o cabo ANTES de abrir a captura")
        check(rcfg["restore_output"] == "id-realtek", "saída anterior guardada para devolver")

        c3.mic(True)
        time.sleep(1.5)
        check(switches[-1] == "id-realtek", "microfone ligado: som volta para a saída normal")
        check(any("Microfone ligado" in m for m in msgs), "quem acessa é avisado do motivo")
        c3.mic(False)
        time.sleep(1.5)
        check(switches[-1] == "id-cable", "microfone desligado: volta a silenciar")

        # alguém troca a saída no meio da sessão -> captura acompanha
        silent["on"] = False
        time.sleep(1.5)
        n_open = len(opened)
        state["default"] = "id-fone"
        time.sleep(1.5)
        check(len(opened) > n_open and opened[-1] == "id-fone",
              "trocar a saída no meio da sessão: captura acompanha")
        silent["on"] = True
        time.sleep(1.5)
        c3.close()
        time.sleep(1.5)
        check(state["default"] == "id-fone" and not rcfg["restore_output"],
              "fim da sessão: saída devolvida para a anterior")

        # Ponte fechou no meio com o som desviado -> recover() devolve
        rcfg["restore_output"] = "id-realtek"
        state["default"] = "id-cable"
        OutputRouter(rcfg, set_default=set_default, current_default=lambda: state["default"],
                     find_cable=lambda: "id-cable").recover()
        check(state["default"] == "id-realtek", "ao abrir de novo, devolve o som se ficou desviado")
        srv3.stop()

    # 13) janela de administrador em foco no PC acessado -> aviso para quem acessa
    elev = {"v": (True, 42)}
    srv4 = HostServer(lambda: "segredo123", RecordingInjector(), port=0, bind="127.0.0.1",
                      capture_factory=FakeCapture, audio_factory=FakeAudio,
                      elevated_check=lambda: elev["v"])
    srv4.start()
    notices = []
    c4 = RemoteClient("127.0.0.1", srv4.port, "segredo123", lambda *a: None)
    c4.on_notice = notices.append
    c4.connect()
    c4.start()
    c4.mouse_button(P.BTN_LEFT, True, 10, 10)
    c4.mouse_button(P.BTN_LEFT, False, 10, 10)
    time.sleep(0.3)
    check(len(notices) == 1 and "administrador" in notices[0], "clique em janela de admin: quem acessa é avisado")
    c4.key(0x41, True)
    c4.key(0x41, False)
    time.sleep(1.7)
    c4.key(0x41, False)
    time.sleep(0.3)
    check(len(notices) == 1, "mesmo aviso não se repete para a mesma janela")
    elev["v"] = (False, 7)
    time.sleep(1.6)
    c4.key(0x41, False)
    time.sleep(0.2)
    elev["v"] = (True, 43)
    time.sleep(1.6)
    c4.key(0x41, False)
    time.sleep(0.3)
    check(len(notices) == 2, "outra janela de admin depois: avisa de novo")
    c4.close()
    srv4.stop()

    # 11) senha salva no PC que conecta
    import tempfile
    from pathlib import Path
    from ponte.config import Config
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(Path(d) / "c.json")
        cfg.save_password("100.96.31.27:17070", "segredo123")
        cfg.trust("abc", "PC-TRABALHO")
        raw = (Path(d) / "c.json").read_text()
        check("segredo123" not in raw, "senha salva não fica em texto puro no arquivo")
        cfg2 = Config(Path(d) / "c.json")
        check(cfg2.saved_password("100.96.31.27:17070") == "segredo123", "senha salva é recuperada")
        check(cfg2.is_trusted("abc") and not cfg2.is_trusted(""), "lista de confiáveis persiste")
        check(cfg2["device_id"] == cfg["device_id"] and len(cfg2["device_id"]) == 32,
              "id do computador é fixo")
    print("tudo certo")


if __name__ == "__main__":
    main()
