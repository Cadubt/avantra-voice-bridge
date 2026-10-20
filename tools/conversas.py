"""Indexa, busca e le as conversas do Claude Code gravadas em disco.

Toda conversa do Claude Code — do app ou do terminal — fica em
~/.claude/projects/<pasta-codificada>/<id>.jsonl. Sao 44 arquivos e 158 MB
nesta maquina, com imagens em base64 no meio, entao ler tudo a cada consulta
e lento: o indice fica em cache e so reprocessa o que mudou.

    python tools\\conversas.py listar
    python tools\\conversas.py listar --pasta tavern
    python tools\\conversas.py buscar "mapa de normais"
    python tools\\conversas.py ler <id> --ultimas 20
    python tools\\conversas.py ler <id> --completa > conversa.txt

Serve tanto para voce quanto para o proprio Avantra: com a pasta liberada em
[brain].extra_dirs, ele roda isto sozinho quando voce pede para retomar algo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

PROJETOS = Path.home() / ".claude" / "projects"
CACHE = Path(__file__).resolve().parent.parent / ".cache_conversas.json"

# Linhas com imagem em base64 passam de 1 MB. Nao ha texto util depois disso.
LIMITE_LINHA = 400_000


def normalizar(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def texto_de(msg) -> str:
    """Extrai so o texto de uma mensagem, ignorando imagem e tool_use."""
    if isinstance(msg, str):
        return msg
    if not isinstance(msg, dict):
        return ""
    conteudo = msg.get("content")
    if isinstance(conteudo, str):
        return conteudo
    partes = []
    if isinstance(conteudo, list):
        for b in conteudo:
            if isinstance(b, dict) and b.get("type") == "text":
                partes.append(b.get("text", ""))
            elif isinstance(b, str):
                partes.append(b)
    return " ".join(p for p in partes if p).strip()


def ler_conversa(caminho: Path, completa: bool = False) -> dict:
    """Le um .jsonl e devolve metadados, e as falas se completa=True."""
    info = {
        "id": caminho.stem,
        "arquivo": str(caminho),
        "pasta": caminho.parent.name,
        "titulo": "",
        "cwd": "",
        "inicio": "",
        "fim": "",
        "n_usuario": 0,
        "n_assistente": 0,
        "amostra": [],
        "falas": [],
    }
    try:
        fh = open(caminho, encoding="utf-8", errors="replace")
    except OSError:
        return info
    with fh:
        for linha in fh:
            if len(linha) > LIMITE_LINHA:
                # Linha gigante: so vale a pena se tiver titulo (nao tem).
                continue
            try:
                o = json.loads(linha)
            except json.JSONDecodeError:
                continue
            tipo = o.get("type")
            if tipo == "custom-title":
                info["titulo"] = o.get("customTitle", "") or info["titulo"]
                continue
            if tipo not in ("user", "assistant"):
                continue
            if o.get("isSidechain"):
                continue        # subagente, nao e a conversa principal
            ts = o.get("timestamp", "")
            if ts:
                info["inicio"] = info["inicio"] or ts
                info["fim"] = ts
            info["cwd"] = info["cwd"] or o.get("cwd", "")
            t = texto_de(o.get("message"))
            if tipo == "user":
                info["n_usuario"] += 1
                # Comando de ferramenta volta como 'user' (<...>), e o
                # handshake da propria ponte nao e voce falando: nenhum dos
                # dois serve de rotulo para a conversa.
                if t and not t.startswith("<") and not t.startswith("[handshake"):
                    if len(info["amostra"]) < 3:
                        info["amostra"].append(t[:200])
                    if completa:
                        info["falas"].append(("voce", t))
            else:
                info["n_assistente"] += 1
                if completa and t:
                    info["falas"].append(("claude", t))
    return info


def indexar(forcar: bool = False) -> list[dict]:
    """Indice de todas as conversas, com cache por data de modificacao."""
    cache = {}
    if CACHE.exists() and not forcar:
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}

    saida, novo = [], {}
    if not PROJETOS.is_dir():
        print("Pasta de conversas nao encontrada: %s" % PROJETOS, file=sys.stderr)
        return []

    for caminho in sorted(PROJETOS.glob("*/*.jsonl")):
        try:
            st = caminho.stat()
        except OSError:
            continue
        chave = str(caminho)
        marca = "%d:%d" % (st.st_mtime_ns, st.st_size)
        guardado = cache.get(chave)
        if guardado and guardado.get("_marca") == marca:
            info = guardado
        else:
            info = ler_conversa(caminho)
            info["_marca"] = marca
            info.pop("falas", None)
        info["_mtime"] = st.st_mtime
        info["_tamanho"] = st.st_size
        novo[chave] = info
        saida.append(info)

    try:
        CACHE.write_text(json.dumps(novo, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    saida.sort(key=lambda i: i.get("_mtime", 0), reverse=True)
    return saida


def rotulo(info: dict) -> str:
    if info.get("titulo"):
        return info["titulo"]
    if info.get("amostra"):
        return info["amostra"][0][:70]
    return "(sem titulo)"


def quando(info: dict) -> str:
    ts = info.get("fim") or info.get("inicio") or ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime(
            "%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return "?"


def pontuar(info: dict, termos: list[str]) -> int:
    """Quanto esta conversa casa com a busca.

    Casar no titulo vale mais que no corpo. E conversa com titulo proprio e
    com muita mensagem sobe: o que voce quer retomar e quase sempre a conversa
    longa de verdade, nao um teste de oito mensagens na mesma pasta.
    """
    alvo_titulo = normalizar(rotulo(info) + " " + info.get("pasta", ""))
    alvo_corpo = normalizar(" ".join(info.get("amostra", [])))
    pontos = 0
    for t in termos:
        if t in alvo_titulo:
            pontos += 10
        if t in alvo_corpo:
            pontos += 3
        if t in normalizar(info.get("cwd", "")):
            pontos += 2
    if pontos == 0:
        return 0
    if info.get("titulo"):
        pontos += 12          # titulo proprio: alguem achou que valia nomear
    n = info.get("n_usuario", 0)
    pontos += min(15, n // 8)
    return pontos


def cmd_listar(args) -> None:
    itens = indexar(args.reindexar)
    if args.pasta:
        alvo = normalizar(args.pasta)
        itens = [i for i in itens
                 if alvo in normalizar(i["pasta"]) or alvo in normalizar(i.get("cwd", ""))]
    itens = [i for i in itens if i["n_usuario"] >= args.min_mensagens]
    print("%d conversas\n" % len(itens))
    for i in itens[:args.limite]:
        print("  %s  %-19s  %3d msgs" % (i["id"][:8], quando(i), i["n_usuario"]))
        print("      %s" % rotulo(i))
        print("      %s" % (i.get("cwd") or i["pasta"]))
        print()


def cmd_buscar(args) -> None:
    termos = [normalizar(t) for t in args.termo if t.strip()]
    itens = indexar(args.reindexar)
    marcados = [(pontuar(i, termos), i) for i in itens]
    marcados = [(p, i) for p, i in marcados if p > 0]
    marcados.sort(key=lambda x: (-x[0], -x[1].get("_mtime", 0)))
    if not marcados:
        print("Nada encontrado para: %s" % " ".join(args.termo))
        return
    print("%d conversas casam com '%s'\n" % (len(marcados), " ".join(args.termo)))
    for p, i in marcados[:args.limite]:
        print("  %s  %-19s  %3d msgs  (relevancia %d)"
              % (i["id"][:8], quando(i), i["n_usuario"], p))
        print("      %s" % rotulo(i))
        print("      %s" % (i.get("cwd") or i["pasta"]))
        if i.get("amostra"):
            print("      inicio: %s" % i["amostra"][0][:110])
        print()


def achar_arquivo(id_parcial: str) -> Path | None:
    for caminho in PROJETOS.glob("*/*.jsonl"):
        if caminho.stem.startswith(id_parcial):
            return caminho
    return None


def cmd_ler(args) -> None:
    caminho = achar_arquivo(args.id)
    if not caminho:
        print("Nao achei conversa com id comecando em '%s'." % args.id)
        return
    info = ler_conversa(caminho, completa=True)
    print("=" * 70)
    print(" %s" % rotulo(info))
    print(" id %s | %s | %d mensagens suas"
          % (info["id"], quando(info), info["n_usuario"]))
    print(" pasta: %s" % (info.get("cwd") or info["pasta"]))
    print("=" * 70)
    print()
    falas = info["falas"]
    if not args.completa:
        falas = falas[-args.ultimas:]
        if len(info["falas"]) > args.ultimas:
            print("[... %d falas anteriores omitidas; use --completa ...]\n"
                  % (len(info["falas"]) - args.ultimas))
    for quem, t in falas:
        marca = "voce " if quem == "voce" else "claude"
        t = re.sub(r"\n{3,}", "\n\n", t.strip())
        if not args.completa and len(t) > 1200:
            t = t[:1200] + " [...]"
        print("[%s] %s\n" % (marca, t))


def main() -> None:
    # O console do Windows abre em cp1252 e quebra em "→" ou "ç". Sem isto a
    # ferramenta morre com UnicodeEncodeError no meio da listagem.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("listar", help="lista as conversas, mais recentes antes")
    pl.add_argument("--pasta", default="", help="filtra pelo projeto")
    pl.add_argument("--limite", type=int, default=25)
    pl.add_argument("--min-mensagens", type=int, default=2)
    pl.add_argument("--reindexar", action="store_true")
    pl.set_defaults(func=cmd_listar)

    pb = sub.add_parser("buscar", help="procura por titulo e conteudo")
    pb.add_argument("termo", nargs="+")
    pb.add_argument("--limite", type=int, default=10)
    pb.add_argument("--reindexar", action="store_true")
    pb.set_defaults(func=cmd_buscar)

    pr = sub.add_parser("ler", help="imprime uma conversa como texto")
    pr.add_argument("id", help="o id, ou so o comeco dele")
    pr.add_argument("--ultimas", type=int, default=20)
    pr.add_argument("--completa", action="store_true")
    pr.set_defaults(func=cmd_ler)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
