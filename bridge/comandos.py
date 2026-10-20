"""Comandos locais: coisas que a ponte resolve sozinha, sem gastar um turno.

Tudo que nao casar aqui vai para o Claude.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .stt import normalizar


@dataclass
class Comando:
    acao: str
    argumento: str = ""


# (acao, lista de padroes). Os padroes sao casados contra o texto normalizado
# (minusculo, sem acento), ancorados no comeco para nao disparar no meio de
# uma frase comum.
REGRAS = [
    # Desarmar a escuta falando. So frases EXPLICITAS sobre encerrar, e ainda
    # assim com confirmacao: "dormir", "tchau" e "ate logo" ja estiveram aqui
    # e eram ambiguos demais — sao coisas que voce diz para outra pessoa na
    # sala, e a escuta desligava sozinha.
    # O "(?!.*\bnao\b)" barra "nao quero encerrar a conversa". O "(.*\s)?"
    # aceita um comeco antes do comando: na pratica voce diz "Entendi.
    # Encerrar a conversa", e com a ancora presa no inicio isso nao pegava.
    # E seguro afrouxar aqui porque encerrar SEMPRE pede confirmacao.
    ("encerrar_conversa", [
        r"^(?!.*\bnao\b)(.*\s)?encerra(r)?( a)? convers(a|e|ar)$",
        r"^(?!.*\bnao\b)(.*\s)?termina(r)?( a)? convers(a|e|ar)$",
        r"^(?!.*\bnao\b)(.*\s)?fim (da|de) convers(a|e|ar)$",
        r"^(?!.*\bnao\b)(.*\s)?encerra(r)? (o )?(papo|bate.?papo)$",
        r"^(?!.*\bnao\b)(.*\s)?(pode )?encerrar$",
        r"^(?!.*\bnao\b)(.*\s)?acabou( a conversa)?$",
        r"^(?!.*\bnao\b)(.*\s)?para de (me )?(escutar|ouvir)$",
        r"^(?!.*\bnao\b)(.*\s)?desliga (o microfone|a escuta)$",
        r"^(modo )?sil[e]ncio$",
    ]),
    ("cancelar", [
        r"^(cala a boca|calado|cala)$", r"^para$", r"^parar$", r"^chega$",
        r"^cancela(r)?$", r"^esquece$",
    ]),
    ("ferramentas_on", [
        r"^liga(r)? as ferramentas$", r"^(modo )?completo$",
        r"^ativa(r)? as ferramentas$", r"^voce pode mexer nos arquivos$",
        r"^modo trabalho$",
    ]),
    ("ferramentas_off", [
        r"^desliga(r)? as ferramentas$", r"^(modo )?convers(a|e|ar)$",
        r"^so conversa(r)?$",
    ]),
    # Pergunta de consulta: nao muda nada, entao pode ser generosa. Basta
    # perguntar algo que mencione modelo ou esforco.
    ("qual_modelo", [
        r"^(?!.*\b(muda|mude|troca|troque|usa|use|coloca|coloque)\b)"
        r".*\b(qual|que|quais)\b.*\b(modelo|esforco|esforcos)\b.*$",
        r"^(me diz o |diz o )?(modelo|esforco)( atual)?$",
        r"^(em )?que modelo (voce )?(esta|ta)( usando)?$",
    ]),
    ("status", [
        r"^status$", r"^como voce esta$", r"^voce esta ai$", r"^onde voce esta$",
        r"^em que pasta voce esta$",
    ]),
    ("sair", [
        r"^encerra(r)?( a ponte)?$", r"^fecha(r)? (o )?programa$", r"^sair$",
    ]),
]

# "trabalhar no tavern", "muda para cenas", "vai para a ponte"
_TROCA = re.compile(
    r"^(?:trabalhar?|trabalha|muda(?:r)?|vai|va|abre|abrir|troca(?:r)?)"
    r"\s+(?:para|pro|pra|no|na|em|o|a)?\s*(?:pasta\s+)?(?:do |da )?(.+?)$")

# "retoma a conversa do tavern tale", "continua a conversa sobre o blender",
# "volta pra conversa da hunt". Entrar de verdade na conversa antiga, em vez
# de so ler o historico dela.
_RETOMAR = re.compile(
    r"^(?:retoma(?:r)?|continua(?:r)?|volta(?:r)?|entra(?:r)?)"
    r"\s+(?:n[ao]\s+|para\s+a?\s*|pra\s+a?\s*|pro\s+)?"
    r"(?:nossa\s+|minha\s+|aquela\s+|a\s+|o\s+)?"
    r"(?:conversa|sessao|sessão|papo|chat)"
    r"(?:\s+(?:sobre|do|da|de|dos|das|no|na|que))?\s*(.*)$")


# As sessoes do APLICATIVO Claude, as que aparecem na barra lateral dele.
# Diferente do _RETOMAR acima, que procura no conteudo de qualquer conversa:
# aqui o alvo e o TITULO da sessao, e entrar nela significa escrever no mesmo
# transcrito que o aplicativo mostra.
_SESSOES = re.compile(
    r"^(?:quais|que|quantas|lista(?:r)?|liste|mostra(?:r)?|mostre|me diga|"
    r"diz|fala)(?:\s+(?:sao|as|os|minhas|nossas|todas|tem))*"
    r"\s+(?:sessoes|secoes|abas)\b.*$")

_QUAL_SESSAO = re.compile(
    r"^(?:em\s+)?(?:qual|que)\s+(?:e\s+a\s+)?(?:sessao|secao|aba)\b.*$")

_SESSAO = re.compile(
    r"^(?:muda(?:r)?|mude|troca(?:r)?|troque|vai|va|volta(?:r)?|volte|"
    r"entra(?:r)?|entre|abre|abrir|passa(?:r)?|passe|continua(?:r)?|"
    r"retoma(?:r)?|me leva|leva)\s+"
    r"(?:de volta\s+)?(?:para|pra|pro|na|no|a|o|em)?\s*"
    r"(?:a\s+|o\s+|minha\s+|nossa\s+|aquela\s+)?"
    r"(?:sessao|secao|aba)\s+"
    r"(?:do |da |de |dos |das |sobre |chamada |que fala de )?(.+?)$")


# Trocar de modelo e de esforco por voz. Os nomes falados estao em [modelos]
# e [esforcos] do config.toml, para voce poder dizer "opus" e nao
# "claude-opus-5-5".
# As formas em -e ("mude", "use", "troque") sao tao naturais quanto as em -a,
# e faltavam: "Mude para o Opus" nao casava com "muda(r)?" e ia parar no
# Claude, que respondia que nao conseguia trocar de modelo.
_VERBOS_TROCA = (r"usa|use|usar|muda|mude|mudar|troca|troque|trocar|"
                 r"vai|va|volta|volte|voltar|passa|passe|passar|"
                 r"quero|queria|gostaria de usar|coloca|coloque|colocar|"
                 r"poe|poem|ponha|por|liga|ligue|ligar|bota|bote|botar|"
                 r"seta|sete|setar|roda|rode|rodar|ativa|ative|ativar|"
                 r"comuta|alterna|chaveia|me da|me de")

_MODELO = re.compile(
    r"^(?:" + _VERBOS_TROCA + r")\s+"
    r"(?:para |pra |pro |o |a |no |na |de volta (?:para |pro |pra )?)*(.+?)$")

# "esforco maximo", "modo max", "pensa no maximo", "esforco alto"
_ESFORCO = re.compile(
    r"^(?:(?:" + _VERBOS_TROCA + r")\s+)?"
    r"(?:o |no |para o |pro )?"
    r"(?:esforco|modo|nivel|pensa(?:r)?|pensamento)\s+"
    r"(?:no |em |de |para |pro |maximo e )?(maximo|max|alto|altissimo|medio|"
    r"baixo|normal|padrao|high|xhigh|low|medium)$")


def _interpretar_modelo(n: str, modelos: dict, esforcos: dict):
    m = _ESFORCO.match(n)
    if m:
        pedido = m.group(1)
        for nome in sorted(esforcos or {}, key=lambda s: -len(s)):
            if normalizar(nome) == pedido:
                return Comando("trocar_esforco", nome)
    m = _MODELO.match(n)
    if m:
        alvo = m.group(1).strip(" .,!?")
        for nome in sorted(modelos or {}, key=lambda s: -len(s)):
            if _casa_nome(alvo, nome):
                return Comando("trocar_modelo", nome)
        for nome in sorted(esforcos or {}, key=lambda s: -len(s)):
            if _casa_nome(alvo, "esforco " + nome) or _casa_nome(alvo, nome):
                return Comando("trocar_esforco", nome)
    return None


# "abre o world of warcraft", "liga o blender", "inicia o steam".
# Abrir programa NAO passa pelo modelo: a ponte resolve o nome contra a lista
# de [apps] do config e executa so o que estiver la. Assim uma transcricao
# errada nao vira um comando qualquer rodando na sua maquina.
_ABRIR = re.compile(
    r"^(?:abre|abrir|inicia(?:r)?|liga(?:r)?|executa(?:r)?|roda(?:r)?|"
    r"starta|chama)\s+(?:o |a |os |as )?(.+?)$")


def _casa_nome(alvo: str, nome: str) -> bool:
    """'world of warcraft' casa dentro de 'o world of warcraft, por favor'."""
    nn = normalizar(nome)
    if not nn:
        return False
    return (alvo == nn or alvo.startswith(nn + " ") or alvo.endswith(" " + nn)
            or (" " + nn + " ") in (" " + alvo + " "))


def interpretar(texto: str, workspaces: dict, apps: dict | None = None,
                modelos: dict | None = None, esforcos: dict | None = None):
    n = normalizar(texto).strip(" .!?")
    if not n:
        return None

    for acao, padroes in REGRAS:
        for p in padroes:
            if re.match(p, n):
                return Comando(acao)

    # Antes de "abrir" e de trocar de pasta: "usa o opus" nao e abrir programa.
    cmd = _interpretar_modelo(n, modelos or {}, esforcos or {})
    if cmd:
        return cmd

    # Antes do _RETOMAR: as duas casam a palavra "sessao", e quem fala em
    # sessao esta falando da barra lateral do aplicativo, nao de uma busca
    # no conteudo de conversas antigas.
    if _SESSOES.match(n):
        return Comando("listar_sessoes")
    if _QUAL_SESSAO.match(n):
        return Comando("qual_sessao")
    m = _SESSAO.match(n)
    if m:
        return Comando("trocar_sessao", m.group(1).strip(" .,!?"))

    # Antes da troca de pasta: "volta pra conversa do tavern" casaria nas duas.
    m = _RETOMAR.match(n)
    if m:
        return Comando("retomar_conversa", m.group(1).strip())

    # Antes da troca de pasta tambem: "abre o blender" nao e trocar de pasta.
    m = _ABRIR.match(n)
    if m:
        alvo = m.group(1).strip(" .,!?")
        # Do nome mais longo para o mais curto: "world of warcraft" ganha de
        # "warcraft" quando os dois estiverem na lista.
        for nome in sorted(apps or {}, key=lambda s: -len(s)):
            if _casa_nome(alvo, nome):
                return Comando("abrir_app", nome)

    m = _TROCA.match(n)
    if m:
        alvo = m.group(1).strip()
        for nome in workspaces:
            nn = normalizar(nome)
            # "tavern", "tavern tale", "pasta do tavern" -> tavern
            if nn and (alvo == nn or alvo.startswith(nn + " ") or nn in alvo.split()):
                return Comando("trocar_pasta", nome)
    return None
