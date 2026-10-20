"""As sessoes do aplicativo Claude, vistas de fora.

O aplicativo guarda uma pasta de sessoes por conta e organizacao, com um
arquivo por sessao. Esse arquivo tem SO os metadados — titulo, pasta, modelo,
datas — e um campo que vale ouro: `cliSessionId`, o identificador da conversa
do Claude Code. O texto da conversa nao esta ali: fica em
~/.claude/projects/<pasta>/<cliSessionId>.jsonl, que e o mesmo arquivo que o
`claude --resume` abre e continua escrevendo.

E por isso que a ponte consegue entrar numa sessao que nasceu no aplicativo:
sem fork, o que voce falar e o que a Avantra fizer entram no transcrito
daquela sessao, e aparecem no chat de texto dela quando voce a abrir.

Nada aqui escreve nos arquivos do aplicativo. So leitura.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

PROJETOS = Path.home() / ".claude" / "projects"


def _raiz() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code-sessions"


def _normal(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower().strip()


def _pasta_de_projeto(cwd: str) -> Path:
    """O nome que o Claude Code da a pasta do projeto: tudo que nao e letra
    ou numero vira traco: a pasta "X:/Cadu .../Scenes" vira "X--Cadu----Scenes"."""
    return PROJETOS / re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def transcricao(item: dict) -> Path | None:
    alvo = _pasta_de_projeto(item.get("cwd", "")) / (item.get("cli", "") + ".jsonl")
    return alvo if alvo.is_file() else None


def listar(incluir_arquivadas: bool = False) -> list[dict]:
    """Todas as sessoes do aplicativo, da mais recente para a mais antiga."""
    raiz = _raiz()
    itens = []
    if not raiz.is_dir():
        return itens
    for arquivo in raiz.glob("*/*/local_*.json"):
        try:
            d = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("isArchived") and not incluir_arquivadas:
            continue
        if not d.get("cliSessionId"):
            continue
        cwd = d.get("cwd", "")
        itens.append({
            "titulo": d.get("title") or "(sem titulo)",
            "cli": d["cliSessionId"],
            "app": d.get("sessionId", ""),
            "cwd": cwd,
            "pasta": Path(cwd).name if cwd else "",
            "modelo": d.get("model", ""),
            "quando": d.get("lastActivityAt", 0),
            "arquivada": bool(d.get("isArchived")),
        })
    itens.sort(key=lambda i: -i["quando"])
    return itens


def quando(item: dict) -> str:
    """Data falavel: 'hoje', 'ontem', ou '16 de outubro'."""
    ms = item.get("quando", 0)
    if not ms:
        return "sem data"
    d = datetime.fromtimestamp(ms / 1000)
    hoje = datetime.now().date()
    dias = (hoje - d.date()).days
    if dias == 0:
        return "hoje"
    if dias == 1:
        return "ontem"
    meses = ("janeiro", "fevereiro", "março", "abril", "maio", "junho",
             "julho", "agosto", "setembro", "outubro", "novembro", "dezembro")
    return "%d de %s" % (d.day, meses[d.month - 1])


def rotulo(item: dict) -> str:
    pasta = item.get("pasta", "")
    return item["titulo"] + ((", na pasta " + pasta) if pasta else "")


def tamanho(item: dict) -> int:
    """Bytes do transcrito. Serve para avisar que retomar vai custar caro:
    o historico inteiro volta para o modelo uma vez."""
    alvo = transcricao(item)
    try:
        return alvo.stat().st_size if alvo else 0
    except OSError:
        return 0


def pontuar(item: dict, termos: list[str]) -> int:
    """Quanto o titulo (e a pasta) casam com o que foi falado."""
    alvo = _normal(item["titulo"] + " " + item.get("pasta", ""))
    palavras = set(alvo.split())
    nota = 0
    for t in termos:
        if not t:
            continue
        if t in palavras:
            nota += 4
        elif t in alvo:
            nota += 2
    return nota


def procurar(busca: str, itens: list[dict] | None = None) -> dict | None:
    """A sessao que melhor casa com o nome falado, ou None."""
    itens = listar() if itens is None else itens
    termos = [t for t in _normal(busca).split() if len(t) > 2]
    if not termos or not itens:
        return None
    marcados = [(pontuar(i, termos), i["quando"], i) for i in itens]
    marcados = [m for m in marcados if m[0] > 0]
    if not marcados:
        return None
    marcados.sort(key=lambda m: (-m[0], -m[1]))
    return marcados[0][2]


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for i in listar():
        print("%-34s | %-12s | %-9s | %5.1f MB | %s"
              % (i["titulo"][:34], i["pasta"][:12], quando(i),
                 tamanho(i) / 1e6, i["cli"][:8]))
