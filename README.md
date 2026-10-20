# Avantra — ponte de voz para o Claude Code

Conversa por voz com o Claude Code, em português, no Windows. Você chama
**"Avantra"** (ou aperta `Pause`), fala, e ouve a resposta. O contexto se
mantém entre as falas e atravessa reinícios.

A API do Claude não aceita áudio, então isto é um laço local em volta do
`claude` de linha de comando:

```
microfone → VAD (Silero) → transcrição (faster-whisper, GPU) → claude → voz (Edge / Piper)
```

E como a assistente tem permissão total na máquina, existe um **guarda** no
meio: cada ferramenta passa por ele antes de rodar, e o que for destrutivo é
lido em voz alta para você autorizar.

Não é o modo de voz do ChatGPT. Os números reais estão abaixo, medidos numa
RTX 3060 Ti com Ryzen 7 5700G, e a diferença é grande: **3,6 a 4,9 s por
turno**. Leia a seção de latência antes de decidir se serve pra você.

---

## Como usar

```bash
start.cmd
```

Deixe a janela aberta. Ela mostra o que você falou, o que ele respondeu e
quanto tempo cada etapa levou.

A ponte sobe **calada**. Aperte `Pause` e a linha de estado muda pra
`· escutando` — aí pode falar. (Se preferir que ela já nasça ouvindo, mude
`hotkeys.armed_on_start` pra `true`; só lembre que aí o seu primeiro `Pause`
vai *desligar*.)

| atalho | o que faz |
|---|---|
| `Pause` | liga/desliga a escuta — e, **enquanto ele fala ou pensa, para tudo na hora** |
| `Ctrl+Alt+V` | escuta uma fala só, e desarma sozinho |
| `Ctrl+Alt+X` | cala a boca dele agora e descarta o turno |

Falando o nome **Avantra** ele também acorda — e o resto da frase já vale como
pedido: *"Avantra, me explica como está a cena da taverna"*. Antes de confiar
nisso, rode a calibração (veja abaixo): nome próprio inventado é o pior caso
para transcrição automática.

Comandos que a ponte resolve sozinha, sem gastar um turno:

| você fala | acontece |
|---|---|
| `dormir`, `para de escutar`, `tchau` | desarma o microfone |
| `cala a boca`, `chega`, `cancela` | corta a fala no meio |
| `liga as ferramentas` | ele passa a ler, escrever e rodar comandos |
| `modo conversa` | volta a só conversar |
| `modo rápido` / `modo esperto` | troca entre haiku e sonnet |
| `trabalhar no tavern` | troca a pasta de trabalho |
| `abre o world of warcraft` | abre um programa da lista `[apps]` |
| `retoma a conversa do tavern tale` | entra numa conversa antiga e continua dela |
| `status` | diz onde está e em que modo |

### Abrir programas

Os nomes vêm da seção `[apps]` do `config.toml`, e **a ponte resolve o nome
sozinha, sem passar pelo modelo**. Isso é de propósito: a transcrição erra
(vimos "Avantra" virar "Azeite"), e com o microfone aberto uma frase mal
entendida não pode virar um comando qualquer rodando na máquina. Só abre o que
estiver escrito na lista. "Abre o photoshop", se não estiver lá, vira uma
pergunta normal para o Claude em vez de executar algo.

### Conversas anteriores

Toda conversa do Claude Code, do app ou do terminal, fica em
`~/.claude/projects/<pasta>/<id>.jsonl`. A ponte libera essa pasta para o
Avantra (`[brain].extra_dirs`), então dá para pedir "me lembra o que a gente
decidiu sobre a hunt" e ele vai ler de verdade. A ferramenta:

```bash
python tools\conversas.py listar
python tools\conversas.py buscar tavern tale
python tools\conversas.py ler 0169c067 --ultimas 40
```

São 44 conversas e 158 MB nesta máquina; o índice fica em cache e leva ~1 s.

**Ler é barato; retomar é caro.** `retoma a conversa X` reinicia o processo
dentro daquela sessão, e o histórico inteiro é reenviado uma vez. Medido numa
conversa de 297 mensagens: **29,5 s e 607 mil tokens de criação de cache, mais
de 10 dólares nessa única pergunta.** Os turnos seguintes ficam baratos, porque
leem do cache. Então retomar vale quando você vai *ficar* naquela conversa, e
não para pular entre várias. Para só relembrar o que foi dito, peça para ele
ler — custa alguns milhares de tokens.

---

## Latência: os números honestos

Tudo medido nesta máquina (Ryzen 7 5700G, RTX 3060 Ti 8 GB, Windows 11).
Cada turno vai pro `metrics.csv`, então você pode conferir os seus próprios.

### O turno completo

O número que importa é **quanto tempo você fica no silêncio depois de calar a
boca**. Com a configuração padrão (haiku, whisper medium na GPU, Piper):

| etapa | tempo | observação |
|---|---|---|
| VAD decidir que você parou | **750 ms** | é o `silence_ms`; é uma espera deliberada |
| transcrever (whisper medium, GPU) | **310–790 ms** | varia com o tamanho da fala |
| Claude até a primeira frase | **~1.890 ms** | domina tudo; é o chão |
| TTS até sair som | **60–300 ms** | |
| **total até você ouvir algo** | **3,6–4,4 s** | medido, não estimado |

Medições reais **falando no microfone**, haiku, whisper medium na GPU:

```
[voce] Estou falando.
  stt 309 ms | claude 1a frase 1882 ms | tts 297 ms | boca fechada -> som 3557 ms

[voce] Então me responde aí, quanto é 2 mais 2?
  stt 751 ms | claude 1a frase 1907 ms | tts  61 ms | boca fechada -> som 4389 ms

[voce] Ah, legal! Show de bola!
  stt 786 ms | claude 1a frase 1889 ms | tts  68 ms | boca fechada -> som 4445 ms
```

O turno real fica em **3,6 a 4,4 segundos**. O tempo do Claude é notavelmente
constante (1882, 1907, 1889 ms) — é o chão, e é 45% do turno. Os outros 55%
são o VAD esperando os 750 ms de silêncio, mais a transcrição.

O modo de voz do ChatGPT responde em ~300 ms. Isso não é alcançável aqui, e a
razão é estrutural: ele usa um modelo que recebe áudio e devolve áudio na mesma
passada. A API do Claude não aceita áudio. Aqui são quatro processos em
sequência, e o Claude escreve a resposta inteira em texto antes de a primeira
palavra virar som.

### O que foi otimizado, e quanto rendeu

- **Processo `claude` persistente** em vez de um por frase. Um `claude -p`
  disparado do zero levou **10,2 s** pra responder "OK". Mantendo o processo
  vivo, o boot (≈11 s) e a criação do cache do prompt são pagos uma vez só.
- **Falar por frases, não pela resposta inteira.** Assim que a primeira frase
  fica pronta ela já vira som, enquanto o resto ainda está sendo escrito.
- **Síntese e reprodução em threads separadas.** Sem isso, cada frase esperava
  a anterior terminar de ser sintetizada: 1,2–2,0 s por frase com o Edge TTS
  virou 249 ms só na primeira.
- **Aquecimento no start.** A primeira fala sua não paga a criação do cache.

### Escolha de modelo (o maior fator)

Tempo até a primeira frase sair falada, mediana de 3 perguntas:

| modelo | ferramentas | pasta | boot | 1ª frase | total |
|---|---|---|---|---|---|
| haiku | chat | vazia | 10,0 s | **2045 ms** | 5640 ms |
| haiku | chat | projeto | 12,0 s | **2373 ms** | 5963 ms |
| haiku | full | projeto | 9,7 s | 2455 ms | 5807 ms |
| sonnet | chat | vazia | 11,5 s | 2540 ms | 7432 ms |
| sonnet | full | projeto | 13,5 s | 3354 ms | 7882 ms |
| sonnet | chat | projeto | 12,1 s | 4987 ms | 8027 ms |

O haiku é quase o dobro mais rápido e dá conta de conversa. Padrão: haiku.
Pra uma pergunta que exige raciocínio, fale *"modo esperto"*.

### Transcrição

4 falas sintéticas, mediana por fala. WER é taxa de erro de palavra.

| modelo | device | por fala | WER |
|---|---|---|---|
| tiny | cpu | 259 ms | 19% |
| small | cpu | 1620 ms | 9% |
| medium | cpu | 4840 ms | 12% |
| small | cuda | 194 ms | 12% |
| **medium** | **cuda** | **416 ms** | **9%** |
| large-v3 | cuda | 528 ms | 10% |

Duas conclusões que contrariam o senso comum: **`medium` na CPU é pior *e* 3x
mais lento que `small`**, e **`large-v3` não é melhor que `medium`** — só leva
86 s pra carregar e come mais VRAM. Padrão: `medium` na GPU.

O WER acima é contra voz sintética, que é mais fácil que voz humana. Trate como
medida de *tempo*, não de qualidade. A qualidade real só aparece com sua voz.

Se precisar da VRAM pra outra coisa, troque pra `small`/`cuda` (194 ms) ou
`small`/`cpu` (1620 ms) em `config.toml`.

### Síntese de voz

| backend | 1º som | offline | observação |
|---|---|---|---|
| **Edge Francisca** (atual) | **~1850 ms** | **não** | neural, feminina; o texto vai pra Microsoft |
| Piper | 163–249 ms | sim | 4 vozes pt-BR, todas masculinas |
| SAPI Maria | imediato | sim | feminina, mas robótica |

A escolha atual custa cerca de **1,3 s a mais por turno** (≈4,9 s em vez de
≈3,6 s). Só a primeira frase paga: a fila sintetiza a seguinte enquanto a
atual toca. `edge_reserva = "piper"` cobre queda de internet — o Edge falha na
hora de sintetizar, não ao subir, então sem reserva a ponte emudeceria no meio
da conversa sem dizer por quê.

**Não existe voz feminina pt-BR no Piper.** Os quatro datasets do repositório
oficial (cadu, edresson, faber, jeff) são todos masculinos, e subir o tom de
uma voz masculina não produz voz feminina — produz voz masculina acelerada.
Ouça você mesmo com `tools/comparar_vozes_femininas.py`. Feminina **e**
offline **e** boa só com clonagem.

Nesta máquina, das vozes do Windows **só a "Microsoft Maria Desktop"
sintetiza** — as vozes "Online" (Thalita, Francisca, Antonio) retornam sem
tocar áudio pelo System.Speech.

Entre as vozes pt-BR do Piper, **`pt_BR-edresson-low` não serve**: o modelo não
tem o fonema do til no mapa e engole as vogais nasais ("não", "informação").

### O caso ruim: quando ele usa ferramentas

Com `tools = "full"`, se o Claude decidir investigar os arquivos, o turno
explode. Medido: a pergunta *"o que é o projeto Tavern Tale?"* levou
**49,5 s até a primeira palavra**, usando `Glob`, `Glob`, `Bash` e `Read`.
A resposta foi boa, mas 50 s de silêncio não é conversa.

Por isso o padrão é `tools = "chat"`, e quando ele pega uma ferramenta a ponte
fala *"só um segundo, deixa eu olhar"* pra você não achar que travou.

---

## Permissão: duas listas diferentes

Permissão total **não** é o mesmo que acesso a toda pasta, e confundir as duas
custou um teste frustrado aqui: com `bypassPermissions` ligado, o Avantra ainda
respondeu *"só posso criar pastas dentro do diretório desta sessão"*. O modo de
permissão tira o **pedido de confirmação**; as ferramentas de arquivo continuam
confinadas às pastas declaradas.

Por isso existem duas listas, e elas são de propósito diferentes:

| lista | o que define | hoje |
|---|---|---|
| `[brain].extra_dirs` | onde ele **pode chegar** | `C:\Users\cadub`, `X:\`, histórico de conversas |
| `[guarda].pastas_livres` | onde ele **age sem perguntar** | Tavern Tale, voice-bridge |

Se as duas fossem iguais, liberar o computador inteiro desligaria o guarda
junto. Do jeito que está: criar uma pasta no Desktop passa direto (é rotina),
escrever um arquivo lá é anunciado antes, e apagar qualquer coisa em qualquer
lugar sempre pede confirmação falada.

## Interromper ele falando

A forma confiável de interromper é a tecla **`Pause`**: ela corta a fala *e* o
turno do Claude imediatamente, sem depender de transcrição nenhuma — funciona
com caixa de som, com barulho de fundo, no meio de uma palavra. No meio de uma
confirmação ela só cala a boca, e a pergunta continua esperando sua resposta.

A alternativa por voz (`interromper_pelo_nome`, **desligada por padrão**):
dizer **"Avantra"** enquanto ele fala corta na hora. Se vier com pedido junto —
*"Avantra, para com isso"* — a frase inteira é aproveitada. Para isso o
microfone fica **aberto durante a fala dele**.

Com **caixa de som** (o caso aqui) isso significa que o microfone capta a voz
dele, e a ponte precisa separar "o Cadu falou" de "isso é o alto-falante".
Três defesas, em `[tts]`:

1. **`interrupcao_max_s`** (4 s) — chamar alguém é curto; o eco de uma
   resposta inteira é longo. Fala mais longa que isso durante a reprodução é
   descartada sem nem transcrever, o que também economiza GPU.
2. **`eco_janela_s`** (12 s) — compara a transcrição com tudo que ele falou
   nos últimos N segundos. A janela precisa cobrir as pausas: o VAD só fecha
   uma fala **depois** do silêncio, então o eco chega transcrito quando
   aquela frase já parou de tocar. Comparar só com o que toca no instante
   exato deixaria passar quase todo eco.
3. **`eco_limiar`** (0.6) — fração das palavras que precisa coincidir. Se ele
   começar a se interromper sozinho, baixe para 0.5; se eco virar pergunta,
   suba para 0.7.

Mais uma: se ele acabou de dizer o próprio nome ("eu sou o Avantra"), a
ativação é ignorada — ele não se acorda sozinho.

A comparação é por **palavra inteira**, não por trecho. Com substring, "sim"
casa dentro de "assim" — e como a pergunta de confirmação dizia *"eu ouvi
assim"*, toda autorização era descartada como eco. Pelo mesmo motivo a
pergunta virou **"Posso?"**: ela não pode conter as palavras da resposta.

Como o `Pause` resolve o mesmo problema sem nenhum desses riscos, o padrão
aqui é **microfone mudo enquanto ele fala**: o eco deixa de existir em vez de
ser filtrado. As defesas acima continuam valendo na confirmação, onde o
microfone precisa ficar aberto para você poder responder.

Com fone de ouvido não há eco nenhum: aí vale ligar `interromper_pelo_nome`.

## O guarda

`permission_mode = "bypassPermissions"` com uma rede embaixo. A ponte registra
um hook `PreToolUse` do tipo `http` apontando para um servidor local; cada
chamada de ferramenta passa por ele antes de executar. Verificado: **o hook
dispara mesmo em `bypassPermissions`**, recebe o comando exato, e a decisão
devolvida é respeitada.

Testado de ponta a ponta com um `rm` real:

| resposta falada | resultado |
|---|---|
| "não" | arquivo intacto; o Claude relata que foi bloqueado e não tenta outro caminho |
| "sim" | arquivo apagado |

O que pede confirmação está em `SHELL_PERIGOSO` e `PASTAS_SENSIVEIS`, em
[`bridge/guarda.py`](bridge/guarda.py): apagar, formatar, matar processo,
mexer no registro, descartar trabalho no git, mover ou renomear, desinstalar,
baixar e executar script da internet, mexer no firewall, e escrever fora das
pastas livres ou em pasta de sistema.

Uma coisa que o guarda **não** pede mais: confirmação para alterar os
arquivos da própria ponte (`config.toml`, os `.py`, os `.cmd`). A regra existe
e continua escrita, mas desligada desde 19/10/2026 — pedir a cada ajuste de
rotina atrapalhava mais do que protegia. Para religar, `proteger_a_ponte =
true` na seção `[guarda]`. Vale lembrar o que ela cobria: o `config.toml`
guarda a senha falada, e o `guarda.py` é a própria trava.

A pergunta **repete a ação como foi entendida**, não como você acha que pediu —
é esse o ponto, já que o que chega até ele é uma transcrição. Resposta não
reconhecida pergunta de novo; silêncio ou dúvida recusa. Tudo vai para
`logs/acoes-AAAA-MM-DD.log`.

## Ver e usar a tela

A Avantra enxerga o monitor e mexe no mouse e no teclado. Quem faz isso e o
`mcp_tela.py`, um servidor MCP que mora aqui na pasta e sobe junto com o
`claude`, declarado no `mcp.json`.

Por que um servidor nosso, e nao o "computer use" do aplicativo Claude:
aquele e interno do app — a propria linha de comando dele lista
`computer-use` entre os nomes de servidor reservados, e nao ha executavel
publicado para ser chamado de fora. A ponte roda o `claude` pela linha de
comando, em outro processo, entao nao herda aquelas ferramentas.

As travas, que sao o ponto:

| trava | o que faz |
|---|---|
| lista de liberados | ela so clica e digita em programa que voce liberou falando. Comeca vazia toda vez que a ponte sobe. |
| mascara na captura | janela de programa nao liberado sai coberta de cinza na foto. Respeita o empilhamento: o que esta por cima continua por cima. |
| janela sob o ponto | clique e rolagem olham a janela embaixo do ponto; digitacao e tecla olham a que esta na frente. Nao da para digitar no programa errado. |
| guarda | `liberar_aplicativo` e lida em voz alta antes de valer, e atalhos de sistema (`win+r`, `alt+f4`, `ctrl+shift+esc`) tambem. |

A area de trabalho e a barra de tarefas aparecem sempre — cobri-las deixaria
a captura inutil —, mas aparecer nao e poder clicar: para mexer nelas, o
pedido e "explorador", como qualquer outro programa.

Na pratica, por voz:

> — Avantra, abre o bloco de notas e anota a ideia do chefe da taverna.
> — Posso mexer no Bloco de notas, para anotar a ideia? (voce diz que sim)

As ferramentas: `ver_tela`, `ver_perto`, `liberar_aplicativo`,
`aplicativos_liberados`, `janela_ativa`, `mover_mouse`, `clicar`, `arrastar`,
`rolar`, `digitar`, `teclar`, `abrir_aplicativo`, `esperar`.

Para testar sem falar nada:

```
.venv/Scripts/python.exe tools/testar_tela.py
```

Ele sobe o servidor como o `claude` sobe, tira duas capturas (uma com nada
liberado, outra depois de liberar) e grava em `tmp/`, para voce conferir com
os proprios olhos que o que nao foi liberado saiu coberto.

O que ele nao resolve, e voce deve saber: a captura e da tela inteira do
monitor ativo, entao o tamanho e a posicao de uma janela coberta continuam
visiveis, mesmo com o conteudo escondido. E modelo pequeno erra coordenada:
em tarefa que importa, peca para ela conferir o resultado na tela.

## Entrar nas sessoes do aplicativo

A barra lateral do aplicativo Claude so lista o que nasceu dentro dele:
conversa criada pela linha de comando nao aparece la. Mas o contrario
funciona — a ponte entra numa sessao que nasceu no aplicativo, e e isso que
faz a conversa de voz aparecer no chat de texto dela.

Por que funciona: o aplicativo guarda um arquivo por sessao em
`%APPDATA%\Claude\claude-code-sessions\<conta>\<org>\local_*.json`, e esse
arquivo tem so os metadados — titulo, pasta, modelo, datas — mais o
`cliSessionId`. O texto da conversa nao esta ali: fica em
`~/.claude/projects/<pasta>/<cliSessionId>.jsonl`, o mesmo arquivo que o
`claude --resume` abre e continua escrevendo.

Por voz:

| o que voce fala | o que acontece |
|---|---|
| "quais sessoes eu tenho" | le os titulos das cinco mais recentes |
| "muda para a sessao do Botivia" | entra nela e confirma falando |
| "em que sessao estamos" | diz onde esta |

Entrar e **sem fork**, de proposito: a ponte continua no mesmo transcrito,
entao o que voce falar e o que a Avantra fizer — ferramentas inclusive —
ficam gravados ali.

Tres coisas antes de usar:

- **Nao deixe a mesma sessao aberta no aplicativo** enquanto a ponte esta
  nela: sao dois processos escrevendo no mesmo arquivo. A ponte avisa quando
  a sessao foi usada hoje, que e o caso provavel de estar aberta.
- **A primeira resposta demora e custa.** Entrar reenvia o historico inteiro
  uma vez: numa conversa de 18 MB isso e bem sentido. A ponte avisa acima de
  8 MB.
- **O aplicativo le o arquivo quando voce abre a sessao.** Se ela ja estiver
  aberta na tela, feche e abra de novo para ver o que foi falado.

Para conferir sem falar nada:

```
.venv/Scripts/python.exe tools/testar_sessoes.py
```

Ele lista as sessoes e mostra, para cada frase que voce diria, em qual delas
a ponte entenderia que voce quer entrar. Com `--entrar <nome>` ele sobe o
claude naquela conversa, mas sempre numa copia, para provar o mecanismo sem
escrever no seu historico.

## Armadilhas que custaram caro aqui

Coisas que falham **sem dar erro**, encontradas montando isto nesta máquina.
Se algum dia o microfone "parar de funcionar", comece por aqui.

**1. O Silero v5 precisa de 576 amostras, não 512.** O modelo espera 64
amostras de contexto da janela anterior grudadas na frente das 512 novas. Como
a entrada do ONNX tem dimensão livre, mandar só 512 não dá erro: ele devolve
`p(fala) ≈ 0.001` para qualquer coisa, inclusive fala limpa com pico 0.96. O
sintoma é a ponte parecer surda. Veja `CONTEXTO` em `bridge/audio.py`.

**2. O endpoint MME entrega silêncio numa taxa que não é a nativa.** O Windows
lista o mesmo microfone uma vez por API de áudio. Medido no QuadCast:

| API | 16 kHz | 48 kHz | 44,1 kHz (nativa) |
|---|---|---|---|
| MME | **silêncio** | **silêncio** | ok |
| DirectSound | ok | ok | ok |
| WASAPI | erro honesto | ok | erro honesto |
| WDM-KS | não abre | não abre | não abre |

O MME aceita a taxa, não reclama, e devolve zeros. Por isso `escolher_entrada()`
nunca prefere MME, e quando ele é a única opção força a taxa nativa.

**3. Dois modelos Whisper não cabem na GPU junto com o resto.** Com `medium` e
`tiny` carregados, a transcrição estourou com `MemoryError: bad allocation`.
Por isso `wake.model = ""` reaproveita o modelo principal. Confira com
`nvidia-smi --query-gpu=memory.used,memory.free --format=csv`.

**4. Duas cópias da ponte abertas brigam pelos atalhos.** Atalho global no
Windows é exclusivo: quem registra primeiro fica com ele, e a segunda janela
simplesmente não recebe as teclas. Se vários atalhos falharem de uma vez, é
quase sempre isso.

**5. O `claude -p --input-format stream-json` só emite o evento de init depois
de receber a primeira mensagem.** Esperar o init antes de escrever trava para
sempre. Veja o `warmup` em `bridge/brain.py`.

---

## Instalação

```bash
git clone https://github.com/Cadubt/avantra-voice-bridge.git
cd avantra-voice-bridge
setup.cmd
```

O `setup.cmd` cria o ambiente, instala as dependências (incluindo as de CUDA)
e gera o seu `config.toml` a partir do `config.example.toml`. Depois:

1. **Ajuste o `config.toml`**: as pastas de trabalho, os programas que ela
   pode abrir e, se quiser, a senha falada do guarda.
2. **Rode `start.cmd`**.

Precisa de:

- **Python 3.12 de verdade** — o `python` do Windows pode ser só o atalho da
  Microsoft Store, que não funciona. `winget install Python.Python.3.12`.
- **Claude Code** no PATH (o `claude`), já logado.
- **GPU NVIDIA** para a transcrição rápida. Sem ela, troque `stt.device` para
  `cpu` e `stt.model` para `small` — fica bem mais lento (veja a tabela).

Os modelos de voz e de detecção de fala (~250 MB) são baixados sozinhos na
primeira vez, para a pasta `models/`.

### O que fica fora do git

O repositório é público, então três tipos de coisa nunca sobem (veja o
`.gitignore`):

- **`config.toml`** — guarda a senha falada do guarda, os caminhos da sua
  máquina e os nomes dos seus projetos. Só o `config.example.toml` vai pro
  git.
- **`logs/`, `Conversas de Voz/`, `transcript.log`** — o que você falou e o
  que ela fez.
- **`.session_*`, `.cache_conversas.json`** — estado das suas sessões.

---

## Ajustes

Tudo em [`config.toml`](config.toml), comentado linha por linha. Os que mais
mudam a sensação de uso:

- **`vad.silence_ms`** (750). Quanto de silêncio conta como "terminei de
  falar". Abaixar deixa mais ágil mas corta você no meio da frase; subir dá
  tempo de pensar mas atrasa toda resposta.
- **`vad.threshold`** (0.6). Se ele disparar sozinho com barulho de fundo,
  suba. Se não te ouvir, baixe.
- **`brain.model`** (haiku). Veja a tabela acima.
- **`brain.tools`** (chat). Veja o caso ruim acima.
- **`stt.model`** / **`stt.device`** (medium / cuda).
- **`tts.backend`** (piper).

### Calibrar a palavra de ativação

```bash
.venv\Scripts\python.exe tools\calibrar_wake.py
```

Você diz "Avantra" algumas vezes, ele mostra o que o Whisper entendeu e grava as
grafias reais no `config.toml`. Isso é necessário: nos testes com voz sintética
o nome saiu como *"Aze"*, *"A Izir"*, *"A Izzy"*, *"Aizuki"* e *"A Isaac"*.
A ponte tem uma comparação por semelhança como rede de segurança, mas ela é
deliberadamente conservadora — com a tolerância alta o suficiente pra pegar
*"a izir"*, ela também acordaria em *"quero sair daqui"*.

O atalho `Pause` é o caminho confiável. A palavra de ativação é conveniência.

---

## Pastas de trabalho

A pasta onde o `claude` roda define quais arquivos ele vê, qual `CLAUDE.md`
carrega e quais servidores MCP sobem — incluindo o do Blender. Troque por voz:

```
"trabalhar no tavern"   ->  X:\...\Tavern Tale
"muda para cenas"       ->  X:\...\Tavern Tale\Blender Models\Scenes
"vai pra ponte"         ->  X:\Dev\avantra-voice-bridge
```

Adicione as suas na seção `[workspaces]`. Cada pasta guarda a própria conversa
num arquivo `.session_<nome>`, então você volta pra uma e continua de onde
parou.

Obs.: não existe `CLAUDE.md` em nenhum lugar da pasta do Tavern Tale. Se você
quer que ele saiba das convenções do projeto quando conversarem por voz, vale
criar um.

## Continuar uma conversa que começou no app

Toda conversa do Claude Code fica em
`~/.claude/projects/<pasta-codificada>/<id>.jsonl`. Pegue o id e ponha em
`brain.resume_session_id`, com `fork_session = true` (trabalha numa cópia, pra
não escrever na conversa que você ainda usa no app).

Com `resume_session_id = ""` ele guarda a sessão em `.session_id` e retoma
sozinho na próxima vez que você ligar a ponte.

Por voz existe caminho melhor, e sem cópia: "muda para a sessão do Botívia"
entra pelo título, na sessão como ela aparece na barra lateral do aplicativo.
Veja *Entrar nas sessoes do aplicativo*, acima.

---

## Limitações

- **Não dá pra interromper no meio.** É turno a turno: você fala, ele responde.
  `Ctrl+Alt+X` corta a fala dele, mas não existe barge-in de verdade.
- **O microfone fica fechado enquanto ele fala**, senão ele se ouve pelas
  caixas e transcreve a própria voz.
- **A conversa de voz nasce fora do app.** A ponte roda a própria sessão, que
  o app não lista — ele só mostra o que nasceu dentro dele. Para o que você
  fala cair no chat de uma sessão do aplicativo, entre nela primeiro: "muda
  para a sessão tal". Aí sim o que for falado e feito fica gravado ali.
- **Resposta longa demora pra acabar.** Ele fala frase por frase, então começa
  rápido, mas uma resposta de dez frases leva o tempo de dez frases.

## Arquivos

```
voice_bridge.py          laço principal e máquina de estados
config.example.toml      tudo que se ajusta (copie para config.toml)
mcp.json                 os servidores MCP que a ponte sobe (Blender, tela)
mcp_tela.py              ver a tela, mouse e teclado (servidor MCP)
setup.cmd / start.cmd    instalar uma vez / ligar a ponte
bridge/audio.py          microfone + VAD (Silero em onnxruntime, sem torch)
bridge/stt.py            faster-whisper + palavra de ativação
bridge/brain.py          o processo claude persistente
bridge/tts.py            Edge / Piper / SAPI, com fila em duas threads
bridge/guarda.py         o guarda: cada ferramenta passa por ele
bridge/comandos.py       comandos locais ("encerrar conversa", "usa o opus")
bridge/sessoes.py        lê as sessões do aplicativo Claude
bridge/hotkeys.py        atalhos globais via RegisterHotKey
bridge/metrics.py        cronometragem por turno
bridge/config.py         lê o config.toml (ou o exemplo, num clone novo)
tools/                   medição, calibração e testes (veja abaixo)
```

Gerados durante o uso, fora do git: `config.toml`, `logs/` (conversa,
latência e ações do guarda, um arquivo por dia), `models/`, `.session_*`.

| ferramenta | pra que |
|---|---|
| `tools/dizer.py` | fala um texto, pra testar a voz |
| `tools/comparar_vozes.py` | toca as 4 vozes pt-BR do Piper em vários tons |
| `tools/comparar_sotaque.py` | compara Piper local com Edge online |
| `tools/comparar_modelos.py` | mede haiku vs sonnet, chat vs full |
| `tools/testar_stt.py` | mede transcrição por modelo e device |
| `tools/testar_brain.py` | mede só a ponte com o Claude |
| `tools/calibrar_wake.py` | ensina as grafias do nome com a sua voz |
| `tools/testar_tela.py` | prova que a captura mascara o que nao foi liberado |
| `tools/testar_sessoes.py` | lista as sessoes do app e prova a busca por voz |
| `tools/testar_guarda.py` | 50 casos: nada de rotina interrompe, nada perigoso escapa |
| `tools/testar_gatilho.py` | mede o gatilho com e sem o viés do initial_prompt |
| `tools/testar_vocabulario.py` | mede o acerto em nomes próprios do projeto |
| `tools/comparar_vozes_femininas.py` | toca as opções femininas (não há no Piper) |
| `tools/conversas.py` | lista, busca e lê conversas antigas do Claude Code |
| `tools/diagnostico_mic.py` | descobre qual entrada de áudio entrega a sua voz |
| `tools/testar_mic_ffmpeg.ps1` | mede o nível do microfone por fora do Python |
| `tools/listar_vozes_windows.ps1` | lista as vozes do Windows nas duas APIs |
| `python -m bridge.audio --list` | lista os microfones |
| `python -m bridge.audio` | monitor ao vivo do VAD reagindo à sua voz |
