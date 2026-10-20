"""O guarda: intercepta cada ferramenta antes de ela rodar.

Com permissao total (bypassPermissions) o Claude nao pergunta nada antes de
executar. Como o que chega ate ele e uma TRANSCRICAO da sua voz, e transcricao
erra, isso seria perigoso demais sozinho.

Entao a ponte registra um hook PreToolUse do tipo http apontando para um
servidor local aqui dentro. Cada chamada de ferramenta passa por aqui ANTES de
acontecer:

  - rotina (ler arquivo, listar pasta, git status...)  -> libera na hora
  - perigosa (apagar, formatar, matar processo...)     -> fala o que vai fazer
                                                          e espera voce dizer
                                                          sim ou nao

Medido: o hook dispara mesmo em bypassPermissions, e a decisao devolvida aqui
e respeitada — o Claude ve a ferramenta como bloqueada e explica isso.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# --------------------------------------------------------------------------
# Classificacao de risco
# --------------------------------------------------------------------------

# Ferramentas que so LEEM. Nunca interrompem.
SO_LEITURA = {
    "Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite", "Task",
    "WebFetch", "WebSearch", "BashOutput",
}

# Comandos de shell que merecem confirmacao falada. Casados contra o comando
# inteiro, sem distinguir maiusculas. Conservador de proposito: e melhor
# perguntar a mais do que apagar a mais.

# Um comando so e perigoso quando o verbo esta na POSICAO DE COMANDO: no
# inicio da linha, ou depois de ; | & ( { ou de um pipe. Procurar a palavra
# solta em qualquer lugar faz o guarda disparar por causa de um caminho
# (C:\Users\del\...) ou de um nome de variavel, e falso alarme treina voce a
# ignorar o aviso de verdade.
_CMD = r"(?:^|[;&|(){}]\s*|\|\s*)"

SHELL_PERIGOSO = [
    # Senha e credencial: trocar senha de conta ou guardar credencial nova
    # sempre pede confirmacao com a senha de voz.
    (r"\bnet\s+user\b|\bset-localuser\b|\bset-aduser\b|\bset-adaccountpassword\b|"
     r"\bchpasswd\b|\bpasswd\b|\bcmdkey\b|\bconvertto-securestring\b",
     "alterar senha ou credencial"),
    # Escrever no config.toml da ponte por shell (Set-Content, >, sed -i...).
    (r"(?:set-content|add-content|out-file|\btee\b|\bsed\s+-i|>>?\s*[\"']?[^\s]*)"
     r"[^\n]*config\.toml",
     "alterar a configuracao da ponte de voz"),
    (_CMD + r"(?:rm|del|erase|rmdir|rd)\b|\bremove-item\b",
     "apagar arquivo ou pasta"),
    # "format" sozinho casava com Format-Table e Format-List, que so imprimem.
    # Anunciar "vou FORMATAR UM DISCO" para um Get-ChildItem e o pior tipo de
    # erro: ensina voce a ignorar o guarda.
    (r"\bformat\s+[a-z]:|\bformat\.com\b|\bformat-volume\b|\bmkfs|"
     r"\bdiskpart\b|\bclear-disk\b|\binitialize-disk\b", "formatar disco"),
    (r"\bshutdown\b|\brestart-computer\b|\breboot\b", "desligar ou reiniciar"),
    (r"\btaskkill\b|" + _CMD + r"(?:kill|pkill)\b|\bstop-process\b",
     "matar processo"),
    (r"\breg\s+(delete|add)\b|\bset-itemproperty\b.*hk(lm|cu)",
     "mexer no registro do Windows"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+.*--force|push\s+.*-f\b)",
     "descartar trabalho no git"),
    (r"\bgit\s+checkout\s+--\s|\bgit\s+restore\b", "descartar alteracoes locais"),
    (_CMD + r"(?:move|mv|rename|ren)\b|\bmove-item\b|\brename-item\b",
     "mover ou renomear"),
    (r"\bnpm\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b", "publicar pacote"),
    (r"\bcurl\b.*\|\s*(sh|bash|powershell)|\biwr\b.*\|\s*iex",
     "baixar e executar script da internet"),
    (r"\bschtasks\b|\bnew-scheduledtask\b|\bsc\s+(create|delete|config)\b",
     "criar ou mexer em servico ou tarefa agendada"),
    (r"\bnetsh\b|\b(?:set|new|remove|disable)-netfirewall", 
     "mexer no firewall ou na rede"),
    (r"\bpip\s+uninstall\b|\bnpm\s+uninstall\b|\bwinget\s+uninstall\b",
     "desinstalar programa"),
    # Exige um ALVO que pareca arquivo. Antes casava em "2>&1" e em qualquer
    # ">" dentro de um script, o que disparava em comando de leitura.
    (r"(?<![0-9&])>(?!>)\s*[\"']?[\w./\:-]+\.[A-Za-z0-9]{1,6}",
     "sobrescrever arquivo com redirecionamento"),
]

# Pastas em que escrever sempre pede confirmacao, por mais inofensivo que
# pareca o arquivo.
# --------------------------------------------------------------------------
# Blender
#
# O guarda intercepta ferramenta, nao so shell. As do Blender que apenas LEEM
# a cena (resumos, telas, documentacao, navegacao) passam direto — perguntar a
# cada inspecao tornaria o uso insuportavel. O que executa codigo bpy passa
# por uma leitura do codigo: geometria apagada nao volta, e o arquivo salvo
# por cima tambem nao.
# --------------------------------------------------------------------------

BLENDER_PERIGOSO = [
    (r"\bbpy\.ops\.object\.delete\b|\bbpy\.ops\.mesh\.delete\b|"
     r"\bbpy\.ops\.object\.(?:vertex_group_remove|shape_key_remove)\b",
     "apagar geometria ou objeto na cena"),
    (r"\bbpy\.data\.\w+\.remove\b|\.objects\.unlink\b|\bbmesh\.ops\.delete\b",
     "remover dados da cena"),
    (r"\.clear\(\)|\bbpy\.ops\.object\.select_all\b.*\bdelete\b",
     "limpar uma colecao inteira"),
    (r"\bmodifier_apply\b|\bbpy\.ops\.object\.convert\b",
     "aplicar um modificador de forma irreversivel"),
    (r"\bbpy\.ops\.object\.(?:join|separate)\b",
     "juntar ou separar malhas"),
    (r"\bbpy\.ops\.wm\.save_mainfile\b|\bbpy\.ops\.wm\.save_as_mainfile\b",
     "salvar por cima do arquivo blend"),
    (r"\bbpy\.ops\.wm\.(?:read_homefile|open_mainfile|quit_blender)\b",
     "fechar ou trocar o arquivo aberto, perdendo o que nao foi salvo"),
    (r"\bbpy\.ops\.object\.booleanop\b|\boperation\s*=\s*['\"]DIFFERENCE['\"]",
     "recortar geometria com um booleano"),
]

# Prefixos de ferramenta que so leem. Nunca interrompem.
BLENDER_SO_LEITURA = ("get_", "search_", "jump_", "render_thumbnail",
                      "render_viewport")


def _blender(tool_name: str, entrada: dict) -> Veredito | None:
    """-> Veredito se for ferramenta do Blender, senao None."""
    curto = tool_name.rsplit("__", 1)[-1]
    if curto.startswith(BLENDER_SO_LEITURA):
        return Veredito(True, "leitura da cena")
    codigo = str(entrada.get("code") or entrada.get("script") or "")
    if not codigo:
        return Veredito(True, "ferramenta do Blender sem codigo")
    baixo = codigo.lower()
    for padrao, oque in BLENDER_PERIGOSO:
        if re.search(padrao, baixo):
            return Veredito(False, oque, "no Blender, " + oque, perigosa=True)
    return Veredito(True, "codigo bpy sem operacao destrutiva")


# --------------------------------------------------------------------------
# Tela
#
# O servidor mcp_tela deixa a Avantra ver o monitor e usar mouse e teclado.
# Olhar e mover o cursor nao mudam nada, entao passam direto. O que muda de
# verdade e QUEM ela pode controlar: por isso a ferramenta que pergunta e a
# que libera um aplicativo. Depois de liberado, clicar e digitar ali seguem
# sem interrupcao — confirmar cada clique tornaria o uso impossivel, e o
# servidor ja recusa sozinho qualquer acao em programa nao liberado.
# --------------------------------------------------------------------------

TELA_SO_LEITURA = {"ver_tela", "ver_perto", "janela_ativa",
                   "aplicativos_liberados", "mover_mouse", "esperar"}

# Atalhos que saem do aplicativo e mexem no Windows inteiro. A lista de
# liberados nao protege contra estes, porque eles nao pertencem a janela.
TECLAS_DE_SISTEMA = {
    "win+r": "abrir a caixa Executar do Windows",
    "win+l": "bloquear o computador",
    "win+d": "minimizar tudo o que esta aberto",
    "win+e": "abrir o Explorador de Arquivos",
    "alt+f4": "fechar a janela que esta na frente",
    "ctrl+alt+del": "abrir a tela de seguranca do Windows",
    "ctrl+alt+delete": "abrir a tela de seguranca do Windows",
    "ctrl+shift+esc": "abrir o gerenciador de tarefas",
}


def _tela(tool_name: str, entrada: dict, confirmar: bool = True):
    """-> Veredito se for ferramenta do mcp_tela, senao None.

    confirmar=False (o padrao desde 19/10/2026, a pedido do Cadu) deixa tudo
    do mcp_tela passar direto: liberar aplicativo e atalhos de sistema
    incluidos. Ele quis o mesmo comportamento que tem no aplicativo Claude,
    onde pede e acontece.
    """
    curto = tool_name.rsplit("__", 1)[-1]

    if curto in TELA_SO_LEITURA:
        return Veredito(True, "so olha a tela")

    if not confirmar:
        return Veredito(True, "computer use liberado sem confirmacao")

    if curto == "liberar_aplicativo":
        apps = entrada.get("apps") or []
        if isinstance(apps, str):
            apps = [apps]
        nomes = ", ".join(str(a) for a in apps) or "um programa"
        motivo = str(entrada.get("motivo") or "").strip()
        descricao = "poder mexer no %s com o mouse e o teclado" % nomes
        if motivo:
            descricao += ", para " + motivo
        return Veredito(False, "controlar um aplicativo", descricao,
                        perigosa=True)

    if curto == "teclar":
        combo = str(entrada.get("combo", "")).lower().replace(" ", "")
        oque = TECLAS_DE_SISTEMA.get(combo)
        if oque:
            return Veredito(False, oque, oque, perigosa=True)
        return Veredito(True, "tecla comum")

    if curto in ("clicar", "digitar", "arrastar", "rolar", "abrir_aplicativo"):
        return Veredito(True, "aplicativo ja liberado por voz")

    return None


PASTAS_SENSIVEIS = [
    "c:\\windows", "c:\\program files", "c:\\programdata",
    "\\appdata\\roaming\\microsoft", "\\system32",
    "\\.ssh", "\\.aws", "\\.config\\gh", "\\.gnupg",
]


class Veredito:
    __slots__ = ("liberar", "motivo", "descricao", "perigosa")

    def __init__(self, liberar, motivo="", descricao="", perigosa=False):
        self.liberar = liberar
        self.motivo = motivo
        self.descricao = descricao        # o que sera FALADO para voce
        self.perigosa = perigosa


# --------------------------------------------------------------------------
# Traduzir a chamada de ferramenta para portugues falavel
#
# Ler um comando de shell em voz alta ("rodar rm traco rf barra c barra
# users...") nao serve para voce conferir nada. O que voce precisa ouvir e o
# que vai ACONTECER, em portugues, com o nome do arquivo e onde ele fica.
# --------------------------------------------------------------------------

PASTAS_CONHECIDAS = [
    ("\\desktop", "no seu Desktop"),
    ("\\downloads", "na sua pasta de Downloads"),
    ("\\documents", "nos seus Documentos"),
    ("\\meus documentos", "nos seus Documentos"),
    ("tavern tale\\blender models\\scenes", "na pasta de cenas do Blender"),
    ("tavern tale\\taverntale", "no projeto Unity do Tavern Tale"),
    ("tavern tale", "na pasta do Tavern Tale"),
    ("voice-bridge", "na pasta da ponte de voz"),
    ("c:\\windows", "dentro do Windows"),
    ("program files", "em Arquivos de Programas"),
]

# Caminhos dentro de um comando de shell, com ou sem aspas.
_CAMINHO = re.compile(r'"([^"]{3,})"|\'([^\']{3,})\'|([A-Za-z]:[\\/][^\s"\']+|'
                      r'/[a-zA-Z]/[^\s"\']+|\./[^\s"\']+|'
                      # nome solto com extensao, de "echo x > relatorio.md"
                      r'[\w.-]+\.[A-Za-z0-9]{1,6})')


def _onde_fica(caminho: str) -> str:
    c = (caminho or "").lower().replace("/", "\\")
    # "/c/users/cadub/desktop" (estilo git bash) -> "c:\users\cadub\desktop"
    c = re.sub(r"^\\([a-z])\\", r"\1:\\", c)
    for trecho, falado in PASTAS_CONHECIDAS:
        if trecho in c:
            return falado
    pai = Path(caminho).parent.name
    return ("na pasta " + pai) if pai else ""


def _nome_e_tipo(caminho: str) -> tuple[str, str]:
    p = Path(caminho.replace("/", "\\"))
    nome = p.name or caminho
    try:
        if p.is_dir():
            return nome, "a pasta"
        if p.is_file():
            return nome, "o arquivo"
    except OSError:
        pass
    return nome, "a pasta" if not p.suffix else "o arquivo"


def _caminhos_em(cmd: str) -> list[str]:
    achados = []
    for m in _CAMINHO.finditer(cmd):
        valor = next((g for g in m.groups() if g), "")
        if valor and not valor.startswith("-"):
            achados.append(valor)
    return achados


def descrever(tool_name: str, entrada: dict, motivo: str) -> str:
    """Frase em portugues do que vai acontecer, para ser falada."""
    cmd = str(entrada.get("command", ""))
    alvo_arquivo = str(entrada.get("file_path") or entrada.get("path") or "")

    if tool_name in ("Write", "Edit", "NotebookEdit", "MultiEdit") and alvo_arquivo:
        nome, _ = _nome_e_tipo(alvo_arquivo)
        onde = _onde_fica(alvo_arquivo)
        verbo = "criar" if tool_name == "Write" else "alterar"
        return "%s o arquivo %s%s" % (verbo, nome, (" " + onde) if onde else "")

    caminhos = _caminhos_em(cmd)
    alvo = caminhos[0] if caminhos else ""

    if "apagar" in motivo and alvo:
        nome, tipo = _nome_e_tipo(alvo)
        onde = _onde_fica(alvo)
        recursivo = bool(re.search(r"-[a-z]*r|/s\b", cmd.lower()))
        extra = ", com tudo que estiver dentro" if recursivo and tipo == "a pasta" else ""
        return "apagar %s %s%s%s" % (tipo, nome, (" " + onde) if onde else "", extra)

    if "mover" in motivo and len(caminhos) >= 2:
        n1, t1 = _nome_e_tipo(caminhos[0])
        destino = _onde_fica(caminhos[1]) or Path(caminhos[1]).name
        # "para no seu Desktop" soa errado: tira a preposicao duplicada.
        destino = re.sub(r"^(no|na|em|nos|nas|dentro do)\s+", "", destino)
        return "mover %s %s para %s" % (t1, n1, destino)
    if "mover" in motivo and alvo:
        n1, t1 = _nome_e_tipo(alvo)
        return "mover ou renomear %s %s" % (t1, n1)

    if "matar processo" in motivo:
        m = re.search(r"(?:/im|/f\s+/im|-n|--name)?\s*([\w .-]+\.exe)", cmd, re.I)
        if m:
            return "fechar à força o programa %s" % m.group(1)
        return "fechar à força um programa que está rodando"

    if "git" in motivo:
        if "reset" in cmd:
            return "descartar alterações do git que ainda não foram salvas"
        if "clean" in cmd:
            return "apagar os arquivos não rastreados do projeto"
        if "push" in cmd:
            return "sobrescrever o repositório remoto com a versão local"
        return motivo

    if "sobrescrever arquivo" in motivo and alvo:
        nome, _ = _nome_e_tipo(alvo)
        return "sobrescrever o arquivo %s%s" % (
            nome, (" " + _onde_fica(alvo)) if _onde_fica(alvo) else "")

    if "formatar" in motivo:
        return "FORMATAR UM DISCO"
    if "desligar" in motivo:
        return "desligar ou reiniciar o computador"
    if "registro" in motivo:
        return "alterar o registro do Windows"
    if "firewall" in motivo:
        return "mexer na configuração de rede ou do firewall"
    if "desinstalar" in motivo:
        return "desinstalar um programa"
    if "baixar e executar" in motivo:
        return "baixar um script da internet e executá-lo"
    if "publicar" in motivo:
        return "publicar um pacote na internet"
    if "pasta do sistema" in motivo and alvo_arquivo:
        return "escrever em %s, que fica numa pasta do sistema" % Path(
            alvo_arquivo).name
    if "fora das pastas" in motivo and alvo_arquivo:
        nome, _ = _nome_e_tipo(alvo_arquivo)
        return "criar o arquivo %s %s" % (nome, _onde_fica(alvo_arquivo))

    # Nao soube traduzir: melhor dizer o comando do que inventar.
    return "%s (comando: %s)" % (motivo, cmd.strip()[:120] or alvo_arquivo)


def _texto_do_comando(entrada: dict) -> str:
    for chave in ("command", "file_path", "path", "pattern", "url"):
        v = entrada.get(chave)
        if isinstance(v, str) and v:
            return v
    return json.dumps(entrada, ensure_ascii=False)[:200]


PASTA_DA_PONTE = "x:\\dev\\voice-bridge"


def _e_arquivo_da_ponte(caminho: str) -> bool:
    c = (caminho or "").lower().replace("/", "\\")
    return c.startswith(PASTA_DA_PONTE) and (
        c.endswith("config.toml") or c.endswith(".py") or c.endswith(".cmd"))


def _em_pasta_sensivel(caminho: str) -> bool:
    c = (caminho or "").lower().replace("/", "\\")
    return any(p in c for p in PASTAS_SENSIVEIS)


def avaliar(tool_name: str, tool_input: dict, pastas_livres=(),
            proteger_ponte: bool = False,
            confirmar_tela: bool = False) -> Veredito:
    """Decide se a chamada passa direto ou precisa da sua confirmacao falada."""
    entrada = tool_input or {}

    if tool_name in SO_LEITURA:
        return Veredito(True, "so leitura")

    if tool_name in ("Bash", "PowerShell"):
        cmd = str(entrada.get("command", ""))
        baixo = cmd.lower()
        for padrao, oque in SHELL_PERIGOSO:
            if re.search(padrao, baixo):
                return Veredito(False, oque,
                                descrever(tool_name, entrada, oque),
                                perigosa=True)
        return Veredito(True, "comando de rotina")

    if tool_name in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        alvo = str(entrada.get("file_path") or entrada.get("path") or "")
        if _em_pasta_sensivel(alvo):
            return Veredito(False, "pasta do sistema",
                            descrever(tool_name, entrada, "pasta do sistema"),
                            perigosa=True)
        # A propria ponte. Mexer aqui e mexer na senha falada e no proprio
        # guarda, entao ja houve confirmacao obrigatoria. O Cadu desligou em
        # 19/10/2026: pedir ele tambem no ajuste de rotina atrapalhava mais do
        # que protegia. Religue com proteger_a_ponte = true no config.toml.
        if proteger_ponte and _e_arquivo_da_ponte(alvo):
            return Veredito(False, "alterar a ponte de voz",
                            descrever(tool_name, entrada, "alterar a ponte de voz"),
                            perigosa=True)
        if pastas_livres:
            baixo = alvo.lower().replace("/", "\\")
            dentro = any(baixo.startswith(str(p).lower().replace("/", "\\"))
                         for p in pastas_livres)
            if not dentro:
                return Veredito(False, "fora das pastas do projeto",
                                descrever(tool_name, entrada,
                                          "fora das pastas do projeto"),
                                perigosa=True)
        return Veredito(True, "escrita dentro do projeto")

    if "__tela__" in tool_name:
        v = _tela(tool_name, entrada, confirmar_tela)
        if v is not None:
            return v

    if "blender" in tool_name.lower():
        v = _blender(tool_name, entrada)
        if v is not None:
            return v

    # Outras ferramentas MCP: libera. Se voce instalar uma que apague coisas,
    # adicione o tratamento dela aqui, como foi feito com o Blender.
    return Veredito(True, "ferramenta sem risco conhecido")


# --------------------------------------------------------------------------
# Servidor que o hook chama
# --------------------------------------------------------------------------

class Guarda:
    """Servidor HTTP local que responde aos hooks PreToolUse.

    confirmar(descricao) -> bool e fornecido pela ponte: e ele que fala a acao
    em voz alta e espera o seu sim ou nao.
    """

    def __init__(self, confirmar, pastas_livres=(), porta: int = 0,
                 log=print, ativo: bool = True,
                 proteger_ponte: bool = False,
                 confirmar_tela: bool = False) -> None:
        self.confirmar = confirmar
        self.pastas_livres = [str(p) for p in pastas_livres]
        self.log = log
        self.ativo = ativo
        self.proteger_ponte = proteger_ponte
        self.confirmar_tela = confirmar_tela
        self._srv = HTTPServer(("127.0.0.1", porta), self._handler())
        self.porta = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True)
        self.bloqueadas = 0
        self.liberadas = 0

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/permissao" % self.porta

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        try:
            self._srv.shutdown()
            self._srv.server_close()
        except Exception:
            pass

    def settings_json(self, timeout: int = 180, desligar_plugins=()) -> str:
        """O --settings que registra o hook apontando para este servidor.

        desligar_plugins vale so para a ponte: plugins com hook proprio
        rodam a cada turno e, numa maquina apertada de memoria, falham e
        poluem a resposta falada ("um hook do GitKraken travou com falta de
        memoria"). Desligar aqui nao mexe no Claude Code que voce usa no app.
        """
        cfg = {"hooks": {"PreToolUse": [{
            "matcher": ".*",
            "hooks": [{"type": "http", "url": self.url, "timeout": timeout,
                       "onFailure": "block"}]}]}}
        if desligar_plugins:
            cfg["enabledPlugins"] = {p: False for p in desligar_plugins}
        return json.dumps(cfg)

    # ---------------- interno ----------------

    def _decidir(self, dados: dict) -> dict:
        nome = dados.get("tool_name", "?")
        entrada = dados.get("tool_input") or {}
        v = avaliar(nome, entrada, self.pastas_livres, self.proteger_ponte,
                    self.confirmar_tela)

        if v.liberar or not self.ativo:
            self.liberadas += 1
            return self._resposta("allow", v.motivo or "liberado")

        self.log("  [guarda] %s parece perigoso (%s): %s"
                 % (nome, v.motivo, _texto_do_comando(entrada)[:120]))
        try:
            ok = self.confirmar(v.descricao, v.motivo)
        except Exception as exc:
            self.log("  [guarda] nao consegui perguntar (%s); bloqueando" % exc)
            ok = False

        if ok:
            self.liberadas += 1
            self.log("  [guarda] voce autorizou")
            return self._resposta("allow", "autorizado por voz pelo Cadu")
        self.bloqueadas += 1
        self.log("  [guarda] voce NAO autorizou")
        return self._resposta(
            "deny", "O Cadu nao autorizou esta acao quando a ponte de voz "
                    "perguntou. Nao tente de novo por outro caminho; explique "
                    "o que voce ia fazer e espere ele decidir.")

    @staticmethod
    def _resposta(decisao: str, motivo: str) -> dict:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decisao,
            "permissionDecisionReason": motivo}}

    def _handler(guarda_self):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                bruto = self.rfile.read(n).decode("utf-8", "replace")
                try:
                    dados = json.loads(bruto)
                except json.JSONDecodeError:
                    dados = {}
                try:
                    resposta = guarda_self._decidir(dados)
                except Exception as exc:
                    guarda_self.log("  [guarda] erro: %s" % exc)
                    resposta = Guarda._resposta("deny", "erro no guarda da ponte")
                corpo = json.dumps(resposta, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def log_message(self, *a):
                pass

        return H
