"""A ponte com o Claude Code.

Dois modos:

  persistent - um unico processo `claude` vivo, recebendo cada fala por stdin
               em stream-json. Paga o boot e o prompt de sistema UMA vez;
               as falas seguintes sao so a ida e volta da API. E o modo bom.

  oneshot    - um `claude -p --resume <id>` por fala. Mais lento (boot do
               processo a cada frase), mas nao depende do processo sobreviver.

Os dois guardam o session-id em disco, entao o contexto atravessa reinicios.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT / ".session_id"

# So fim de frase de verdade. Nao corta em ":" nem ";": isso partia
# 'Voce pediu: "responda pronto"' em tres pedacos falados separados.
#
# E exige ESPACO depois do ponto, nunca fim de texto: durante o streaming o
# "fim do texto" e so o fim do pedaco que chegou ate agora, e "Battle.net"
# chegava como "Battle." e virava duas frases faladas — "Abri o Battle" /
# "net para voce". O resto sai no esvaziar(), quando a resposta termina.
_SENT_FIM = re.compile(r"[.!?…]+[\"')\]]?(?=\s)|\n\n")

# Mensagem de aquecimento. Precisa deixar claro que nao e o usuario falando,
# senao o Claude passa a atribuir isso ao Cadu quando ele pergunta o que
# disse no inicio da conversa.
WARMUP = ("[handshake automatico da ponte de voz, o usuario nao falou nada "
          "ainda — ignore esta mensagem no historico] Responda apenas: pronto.")


@dataclass
class Resposta:
    texto: str = ""
    erro: str = ""
    session_id: str = ""
    ms_primeiro_texto: float = 0.0
    ms_total: float = 0.0
    ferramentas: list = field(default_factory=list)
    cancelada: bool = False

    @property
    def ok(self) -> bool:
        return not self.erro and bool(self.texto.strip())


class _Frases:
    """Acumula pedacos de texto e solta frases inteiras conforme ficam prontas.

    E o que deixa o TTS comecar a falar antes do Claude terminar de escrever.
    """

    def __init__(self, minimo: int = 12) -> None:
        self.buf = ""
        self.minimo = minimo

    def alimentar(self, pedaco: str) -> list[str]:
        self.buf += pedaco
        saida = []
        while True:
            m = None
            for m in _SENT_FIM.finditer(self.buf):
                break
            if not m:
                break
            corte = m.end()
            frase = self.buf[:corte].strip()
            # Nao solta "Ok." sozinho: junta com o que vier.
            if len(frase) < self.minimo and len(self.buf) < 200:
                break
            self.buf = self.buf[corte:]
            if frase:
                saida.append(frase)
        return saida

    def esvaziar(self) -> str:
        resto, self.buf = self.buf.strip(), ""
        return resto


def _erro_legivel(bruto, texto_final: str = "") -> str:
    """Transforma o erro do CLI em algo que faz sentido ouvir em voz alta."""
    codigo = str(bruto).strip()
    detalhe = (texto_final or "").strip()
    if codigo == "429" or "limit" in detalhe.lower():
        # O CLI poe o horario de reset no proprio texto da resposta.
        quando = ""
        m = re.search(r"resets?\s+([^\n·]+)", detalhe, re.I)
        if m:
            quando = " Volta às " + m.group(1).strip().split("(")[0].strip() + "."
        return "você bateu o limite de uso da sua conta Claude." + quando
    if codigo in ("500", "502", "503", "529"):
        return "o serviço da Anthropic está instável agora. Tente de novo."
    if codigo == "401" or codigo == "403":
        return "a sua sessão do Claude expirou. Rode claude login no terminal."
    if detalhe:
        return detalhe[:200]
    return codigo if not codigo.isdigit() else "erro " + codigo


def _achar_claude() -> str:
    for nome in ("claude.exe", "claude.cmd", "claude"):
        caminho = shutil.which(nome)
        if caminho:
            return caminho
    raise FileNotFoundError(
        "Nao achei o executavel 'claude' no PATH. Abra um terminal novo ou "
        "instale o Claude Code.")


def _ler_session_salvada() -> str:
    try:
        return SESSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _salvar_session(sid: str) -> None:
    try:
        SESSION_FILE.write_text(sid, encoding="utf-8")
    except OSError:
        pass


def _expandir_dirs(cfg) -> list[str]:
    """Pastas extras que o Claude pode ler alem da pasta de trabalho.

    Sem isto ele nao alcanca ~/.claude/projects, onde ficam as conversas
    gravadas, e nao consegue retomar o que voces conversaram antes.
    """
    args = []
    for bruto in cfg.get("extra_dirs", []) or []:
        caminho = Path(os.path.expandvars(str(bruto))).expanduser()
        if caminho.is_dir():
            args += ["--add-dir", str(caminho)]
        else:
            print("[brain] extra_dirs: '%s' nao existe, ignorando" % bruto,
                  flush=True)
    return args


# Onde o aplicativo Claude ja guardou as extensoes. Ele mudou de lugar em
# outubro de 2026 (de %APPDATA%\Claude para %LOCALAPPDATA%\Claude-Data\...),
# e o MCP do Blender quebrou em silencio por apontar para o antigo.
_RAIZES_EXTENSOES = [
    r"%LOCALAPPDATA%\Claude-Data\claude_pzs8sxrjxfjjc\Claude Extensions",
    r"%APPDATA%\Claude\Claude Extensions",
    r"%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\Claude Extensions",
]


def _achar_extensao(cmd: str) -> str | None:
    """Procura o executavel de uma extensao do Claude nos lugares conhecidos."""
    marca = "claude extensions"
    baixo = cmd.lower().replace("/", "\\")
    i = baixo.find(marca)
    if i < 0:
        return None
    resto = cmd.replace("/", "\\")[i + len(marca):].lstrip("\\")
    for raiz in _RAIZES_EXTENSOES:
        candidato = Path(os.path.expandvars(raiz)) / resto
        if candidato.exists():
            return str(candidato)
    return None


def _resolver_mcp(caminho: Path) -> Path | None:
    """Expande %APPDATA%, %PONTE_DIR% etc. dentro do mcp.json.

    O mcp.json do repositorio usa variaveis em vez de caminhos desta maquina,
    para funcionar em qualquer clone sem expor o nome de usuario. O claude nao
    expande variaveis sozinho, entao a ponte grava uma copia resolvida (fora
    do git) e passa essa. A expansao e feita valor a valor, depois de ler o
    JSON: expandir o texto cru quebraria as barras invertidas dos caminhos.
    """
    os.environ.setdefault("PONTE_DIR", str(ROOT))
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("[brain] mcp.json invalido (%s); usando como esta" % exc, flush=True)
        return None

    def expandir(v):
        if isinstance(v, str):
            return os.path.expandvars(v)
        if isinstance(v, list):
            return [expandir(x) for x in v]
        if isinstance(v, dict):
            return {k: expandir(x) for k, x in v.items()}
        return v

    resolvido = expandir(dados)
    for nome, srv in (resolvido.get("mcpServers") or {}).items():
        cmd = srv.get("command", "")
        if cmd and not Path(cmd).exists():
            achado = _achar_extensao(cmd)
            if achado:
                srv["command"] = achado
            else:
                print("[brain] MCP '%s': executavel nao encontrado (%s)"
                      % (nome, cmd), flush=True)

    destino = ROOT / ".mcp.resolvido.json"
    try:
        destino.write_text(json.dumps(resolvido, ensure_ascii=False,
                                      indent=2), encoding="utf-8")
    except OSError:
        return None
    return destino


def _args_comuns(cfg) -> list[str]:
    args = ["--model", str(cfg.get("model", "sonnet"))]
    # Servidores MCP (Blender e o que mais voce declarar em mcp.json). O CLI
    # nao herda as extensoes do aplicativo Claude Desktop, entao sem isto a
    # Avantra nao tem nenhuma ferramenta do Blender.
    mcp = (cfg.get("mcp_config") or "").strip()
    if mcp:
        caminho = Path(os.path.expandvars(mcp)).expanduser()
        if not caminho.is_absolute():
            caminho = ROOT / caminho
        if caminho.exists():
            resolvido = _resolver_mcp(caminho)
            args += ["--mcp-config", str(resolvido or caminho)]
        else:
            print("[brain] mcp_config: '%s' nao existe, ignorando" % mcp,
                  flush=True)

    # Sem isto o prompt de sistema e GRAVADO na primeira mensagem da conversa
    # e reenviado igual em toda retomada — entao ela continuava se
    # apresentando pelo nome antigo mesmo depois de voce renomear.
    args += ["--system-prompt-snapshot", "off"]

    esforco = (cfg.get("effort") or "").strip()
    if esforco:
        args += ["--effort", esforco]
    nome = (cfg.get("session_name") or "").strip()
    if nome:
        args += ["--name", nome]
    args += _expandir_dirs(cfg)
    # Registra o hook PreToolUse que leva cada ferramenta ao guarda da ponte.
    extra = (cfg.get("settings_json") or "").strip()
    if extra:
        args += ["--settings", extra]
    prompt = (cfg.get("system_prompt") or "").strip()
    if prompt:
        args += ["--append-system-prompt", prompt]
    if str(cfg.get("tools", "full")).lower() == "chat":
        # Sem ferramenta nenhuma: so conversa. Tambem deixa o prompt menor.
        args += ["--tools", ""]
    else:
        args += ["--permission-mode", str(cfg.get("permission_mode", "acceptEdits"))]
    return args


def _resolver_cwd(cfg) -> str:
    alvo = (cfg.get("cwd") or "").strip()
    if alvo and Path(alvo).is_dir():
        return alvo
    if alvo:
        print("[brain] cwd '" + alvo + "' nao existe; usando " + str(ROOT), flush=True)
    return str(ROOT)


def _args_sessao(cfg) -> tuple[list[str], str]:
    """-> (args de sessao, session_id esperado se ja souber)."""
    pedido = (cfg.get("resume_session_id") or "").strip()
    if pedido.lower() == "none":
        return [], ""
    if not pedido:
        pedido = _ler_session_salvada()
    if pedido:
        args = ["--resume", pedido]
        if cfg.get("fork_session", True):
            args.append("--fork-session")   # nao escreve na conversa original
            return args, ""                 # o id novo vem no init
        return args, pedido
    novo = str(uuid.uuid4())
    return ["--session-id", novo], novo


class PersistentBrain:
    """Um processo `claude` vivo, alimentado por stdin em stream-json."""

    modo = "persistent"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.session_id = ""
        self.cwd = _resolver_cwd(cfg)
        self._proc: subprocess.Popen | None = None
        self._eventos: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._pronto = threading.Event()
        self._morto = threading.Event()
        self._lock = threading.Lock()
        self.stderr_tail: list[str] = []

    # ---------------- ciclo de vida ----------------

    def start(self, timeout: float = 120.0, warmup: bool = True) -> None:
        """Sobe o processo.

        Atencao: o `claude` so emite o evento de init DEPOIS de receber a
        primeira mensagem pelo stdin. Esperar o init antes de mandar algo
        trava para sempre. Por isso quem espera e o warmup, nao o start.
        """
        exe = _achar_claude()
        args_sessao, sid_previsto = _args_sessao(self.cfg)
        argv = [exe, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--include-partial-messages",
                *_args_comuns(self.cfg), *args_sessao]

        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self._proc = subprocess.Popen(
            argv, cwd=self.cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.session_id = sid_previsto

        self._reader = threading.Thread(target=self._ler_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._ler_stderr, daemon=True).start()

        t0 = time.perf_counter()
        if warmup:
            # Alem de destravar o init, isso ja cria o cache do prompt de
            # sistema, entao a sua primeira fala de verdade nao paga por ele.
            r = self.ask(WARMUP)
            if r.erro:
                self.close()
                raise RuntimeError(
                    "O claude nao subiu: %s. stderr: %s"
                    % (r.erro, " | ".join(self.stderr_tail[-3:]) or "(vazio)"))
        if not self.vivo:
            raise RuntimeError("O processo claude morreu ao subir. stderr: %s"
                               % (" | ".join(self.stderr_tail[-3:]) or "(vazio)"))
        print("[brain] sessao %s pronta em %.1fs (cwd=%s)"
              % (self.session_id[:8] or "?", time.perf_counter() - t0, self.cwd),
              flush=True)

    def close(self) -> None:
        self._morto.set()
        p = self._proc
        if p is None:
            return
        try:
            if p.stdin and not p.stdin.closed:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        self._proc = None

    @property
    def vivo(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---------------- leitura ----------------

    def _ler_stdout(self) -> None:
        p = self._proc
        if p is None or p.stdout is None:
            return
        for linha in p.stdout:
            linha = linha.strip()
            if not linha:
                continue
            try:
                ev = json.loads(linha)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id", "") or self.session_id
                if self.session_id:
                    _salvar_session(self.session_id)
                self._pronto.set()
            self._eventos.put(ev)
        self._morto.set()
        self._eventos.put({"type": "__fim__"})

    def _ler_stderr(self) -> None:
        p = self._proc
        if p is None or p.stderr is None:
            return
        for linha in p.stderr:
            linha = linha.rstrip()
            if linha:
                self.stderr_tail.append(linha)
                del self.stderr_tail[:-20]

    # ---------------- pergunta ----------------

    def ask(self, texto: str, on_sentence=None, cancel=None,
            on_tool=None) -> Resposta:
        """Manda uma fala e espera a resposta.

        on_sentence(frase) e chamado assim que cada frase fica pronta, antes
        de a resposta terminar. cancel e um threading.Event.
        """
        with self._lock:
            return self._ask(texto, on_sentence, cancel, on_tool)

    def _ask(self, texto, on_sentence, cancel, on_tool) -> Resposta:
        if not self.vivo:
            return Resposta(erro="o processo claude morreu")

        # Descarta eventos velhos que sobraram do turno anterior.
        try:
            while True:
                self._eventos.get_nowait()
        except queue.Empty:
            pass

        msg = {"type": "user",
               "message": {"role": "user",
                           "content": [{"type": "text", "text": texto}]}}
        t0 = time.perf_counter()
        try:
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            return Resposta(erro="nao consegui escrever no claude: " + str(exc))

        frases = _Frases()
        r = Resposta(session_id=self.session_id)
        acumulado = ""
        limite = float(self.cfg.get("timeout_s", 300))

        while True:
            if cancel is not None and cancel.is_set():
                r.cancelada = True
                r.texto = acumulado
                break
            if time.perf_counter() - t0 > limite:
                r.erro = "tempo esgotado (%.0fs)" % limite
                break
            try:
                ev = self._eventos.get(timeout=0.1)
            except queue.Empty:
                continue

            tipo = ev.get("type")
            if tipo == "__fim__":
                r.erro = "o processo claude encerrou"
                break

            if tipo == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta":
                    d = e.get("delta", {})
                    if d.get("type") == "text_delta":
                        pedaco = d.get("text", "")
                        if pedaco:
                            if not r.ms_primeiro_texto:
                                r.ms_primeiro_texto = (time.perf_counter() - t0) * 1000
                            acumulado += pedaco
                            if on_sentence:
                                for f in frases.alimentar(pedaco):
                                    on_sentence(f)

            elif tipo == "assistant":
                for bloco in ev.get("message", {}).get("content", []):
                    if bloco.get("type") == "tool_use":
                        nome = bloco.get("name", "?")
                        r.ferramentas.append(nome)
                        if on_tool:
                            on_tool(nome)

            elif tipo == "result":
                final = (ev.get("result") or "").strip()
                if ev.get("is_error"):
                    # api_error_status vem como NUMERO (429, 500...), nao texto.
                    bruto = (ev.get("api_error_status")
                             or ev.get("subtype") or "erro")
                    r.erro = _erro_legivel(bruto, final)
                self.session_id = ev.get("session_id", self.session_id)
                r.session_id = self.session_id
                if r.erro:
                    # Em erro, o "result" traz a mensagem crua do servico, em
                    # ingles ("You've hit your session limit · resets 7:50pm").
                    # Nao vale falar isso em voz alta: quem fala e o texto
                    # amigavel que _erro_legivel montou.
                    r.texto = ""
                    break
                # Se os deltas nao vieram (ou vieram pela metade), usa o texto final.
                if final and len(final) > len(acumulado.strip()):
                    extra = final[len(acumulado.strip()):] if \
                        final.startswith(acumulado.strip()) else final
                    if on_sentence and extra.strip():
                        for f in frases.alimentar(extra):
                            on_sentence(f)
                    acumulado = final
                r.texto = acumulado.strip()
                break

        if on_sentence and not r.cancelada:
            resto = frases.esvaziar()
            if resto:
                on_sentence(resto)
        if not r.texto:
            r.texto = acumulado.strip()
        r.ms_total = (time.perf_counter() - t0) * 1000
        return r


class OneshotBrain:
    """Um `claude -p --resume <id>` por fala. Sem streaming: so fala no fim."""

    modo = "oneshot"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.cwd = _resolver_cwd(cfg)
        self.session_id = ""

    def start(self, timeout: float = 0) -> None:
        pedido = (self.cfg.get("resume_session_id") or "").strip()
        if pedido.lower() == "none":
            self.session_id = ""
        else:
            self.session_id = pedido or _ler_session_salvada()
        print("[brain] modo oneshot, cwd=%s, sessao=%s"
              % (self.cwd, self.session_id[:8] or "(nova)"), flush=True)

    def close(self) -> None:
        pass

    @property
    def vivo(self) -> bool:
        return True

    def ask(self, texto: str, on_sentence=None, cancel=None, on_tool=None) -> Resposta:
        exe = _achar_claude()
        argv = [exe, "-p", texto, "--output-format", "json", *_args_comuns(self.cfg)]
        if self.session_id:
            argv += ["--resume", self.session_id]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                argv, cwd=self.cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=float(self.cfg.get("timeout_s", 300)))
        except subprocess.TimeoutExpired:
            return Resposta(erro="tempo esgotado")
        if proc.returncode != 0:
            return Resposta(erro=(proc.stderr or "").strip()[:300] or "erro no claude")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return Resposta(erro="resposta nao era JSON: " + proc.stdout[:200])

        self.session_id = data.get("session_id", self.session_id)
        _salvar_session(self.session_id)
        r = Resposta(texto=(data.get("result") or "").strip(),
                     session_id=self.session_id,
                     ms_total=(time.perf_counter() - t0) * 1000)
        r.ms_primeiro_texto = r.ms_total     # nao ha streaming aqui
        if data.get("is_error"):
            r.erro = data.get("subtype") or "erro"
        if on_sentence and r.texto:
            on_sentence(r.texto)
        return r


def build_brain(cfg):
    if str(cfg.get("mode", "persistent")).lower() == "oneshot":
        return OneshotBrain(cfg)
    return PersistentBrain(cfg)
