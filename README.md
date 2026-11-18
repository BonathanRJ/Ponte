# Ponte — acesso remoto estilo AnyDesk

Veja e controle outro computador (tela, mouse, teclado e área de transferência)
pela rede Tailscale ou pela rede local. Python + PySide6.

## Instalação (nos dois computadores)

1. Instale o **Python 3.11 ou mais novo** (python.org, marque "Add python.exe to PATH").
2. Copie a pasta `Ponte` para o PC (ex.: `C:\Ponte`).
3. Dê dois cliques em **`instalar.bat`**.
4. Abra com **`Ponte.bat`**.

> Para não precisar instalar Python no outro PC: rode `gerar_exe.bat` no seu PC
> e leve só o `dist\Ponte.exe`.

Os dois PCs precisam estar **logados na mesma conta do Tailscale** (ou na mesma rede local).

## Uso

**No PC que vai ser acessado**
1. Marque **"Permitir que outros computadores acessem este"**.
   Na primeira vez, o Windows pergunta sobre o firewall: clique em **Permitir**.
2. Anote o **endereço** (IP do Tailscale, `100.x.y.z`, ou o nome do PC) e a **senha**.
3. Para deixar sempre disponível, marque **"Permitir acesso automaticamente ao abrir"**.
   Ao fechar a janela, a Ponte continua na bandeja (perto do relógio) aceitando conexões.
   Para iniciar com o Windows, coloque um atalho do `Ponte.bat` em `shell:startup`.

**No seu PC**
1. Em **"Acessar outro computador"**, digite o endereço e a senha e clique em **Conectar**.
2. Na barra da janela remota: qualidade, monitor, ajustar/100%, tela cheia,
   tecla Windows, Ctrl+Shift+Esc, Alt+Tab e Desconectar.

### Primeira conexão e as seguintes

- **Primeira vez:** digite a senha. No PC acessado aparece **"Fulano quer acessar"**,
  com **Aceitar / Recusar** (recusa sozinho em 60 s).
- Ao aceitar, marque **"Permitir este computador sem pedir confirmação nas próximas
  vezes"** para não precisar de ninguém do outro lado da próxima vez (é opcional).
- **"Lembrar a senha neste computador"** (ligado por padrão) guarda a senha
  criptografada pelo Windows. Nas próximas vezes, escolha o endereço e clique em
  **Conectar**, sem digitar nada.
- Se a senha for trocada no PC acessado, a salva para de valer e a Ponte pede a nova.
- **"Pedir de novo"** (PC acessado) volta a exigir confirmação de todos;
  **"Esquecer"** (PC que conecta) apaga a senha salva daquele endereço.

### Durante a conexão

- A janela principal some e só a janela do PC remoto fica na barra de tarefas.
  O botão **Painel** (primeiro da barra) mostra/oculta a janela principal.
- **Vários monitores no PC remoto:** escolha em **Tela** (Monitor 1, 2… ou
  **Todos os monitores**) ou use o botão ao lado para alternar.
- **Tela cheia com vários monitores aqui:** se os dois PCs tiverem mais de um
  monitor, cada monitor remoto ocupa um monitor seu. A setinha do botão de tela
  cheia tem: **Tela cheia no meu monitor 1/2…** e **Usar todos os meus monitores**
  (uma janela só ocupando todos, como na Área de Trabalho Remota). Para o PC
  remoto ter um monitor para cada monitor seu, ele precisa de um segundo monitor
  (real, virtual ou um adaptador HDMI "fantasma").
- **Atalhos do Windows** (Alt+Tab, tecla Windows, Ctrl+Shift+Esc…): com a janela
  da conexão em primeiro plano, vão para o PC remoto. Para usá-los no seu PC,
  minimize a Ponte ou clique em outra janela.
- **Gerenciador de Tarefas e programas de administrador:** o Windows não deixa um
  programa comum controlá-los. No PC acessado, marque **"Abrir a Ponte como
  administrador"** (o Windows pede permissão ao abrir a Ponte).
- Ao receber a primeira conexão, o PC acessado marca sozinho "Permitir acesso
  automaticamente ao abrir a Ponte".

Enquanto alguém está conectado, o PC acessado mostra uma **faixa vermelha no topo**
com o botão **Desconectar**.

## Segurança

- A senha nunca trafega: o login é um desafio HMAC-SHA256.
- Depois de 5 senhas erradas, o IP fica bloqueado por 5 minutos.
- A imagem **não é criptografada pela Ponte**. Pelo Tailscale, o tráfego já vai
  criptografado (WireGuard). **Não abra a porta 7070 no roteador para a internet.**
  Use o Tailscale.
- A senha fica em `%APPDATA%\Ponte\config.json`. Use o botão **Nova** para trocá-la.

## Limitações conhecidas

- Tela de bloqueio, Ctrl+Alt+Del e janelas do UAC ("Deseja permitir…") não aparecem
  nem recebem cliques. Isso exigiria rodar a Ponte como serviço do Windows.
- Para controlar programas abertos como administrador, abra a Ponte do PC
  remoto como administrador.
- Sem transferência de arquivos (o clipboard sincroniza só texto).
- Áudio: usa ~1,5 Mbit/s em cada sentido; em internet lenta, desligue o botão
  **Áudio** na barra da janela remota.

## Ouvir o PC remoto sem o som sair nele

No PC acessado, marque **"Silenciar este PC durante o acesso"** (precisa do
VB-CABLE instalado nele). Durante a conexão, o som do Windows vai para o cabo
virtual: nada sai nas caixas, e quem acessa ouve normalmente. Ao desconectar (ou
ao fechar a Ponte), a saída de som volta a ser a de antes.

Com o **microfone** ligado, o PC acessado volta a tocar pela saída normal enquanto
o microfone estiver ligado: com um único cabo virtual, o som da call e a sua voz
se misturariam e as pessoas da call ouviriam a si mesmas.

A Ponte acompanha trocas de saída de som feitas durante a conexão.

## Microfone (para entrar em calls no PC remoto)

1. No **PC remoto**, instale o **VB-CABLE** (grátis): vb-audio.com/Cable →
   extraia → `VBCABLE_Setup_x64.exe` como administrador → reinicie o PC.
   **Não** defina o CABLE como saída de som padrão.
2. No app de call do PC remoto (Teams, Discord, Meet…), escolha
   **"CABLE Output (VB-Audio Virtual Cable)"** como **microfone**.
3. Na janela remota da Ponte, clique em **Microfone**. Ele começa sempre desligado.
4. Use **fone de ouvido**: com caixa de som, o seu microfone capta o áudio da
  call e as outras pessoas ouvem eco.
- Uma sessão por vez.

## Como funciona

| Arquivo | Função |
|---|---|
| `ponte/capture.py` | captura de tela com `mss` |
| `ponte/encoder.py` | divide a tela em blocos de 64 px e reenvia em JPEG só o que mudou |
| `ponte/protocol.py` | mensagens binárias, autenticação HMAC |
| `ponte/host.py` | servidor: sessão, controle de fluxo (máx. 2 quadros sem confirmação), bloqueio por senha errada |
| `ponte/winput.py` | injeção de mouse e teclado via `SendInput` do Windows |
| `ponte/client.py` / `viewer.py` | conexão e janela de controle |
| `ponte/app.py` | janela principal, bandeja e faixa de aviso |

Teste sem interface: `python -m tests.test_loopback`.
