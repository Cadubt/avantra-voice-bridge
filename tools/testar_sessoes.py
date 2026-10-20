"""Testa a entrada nas sessoes do aplicativo Claude.

Sem argumento, so lista e prova a busca por nome falado — nao gasta nada.

Com --entrar <nome>, sobe o claude retomando aquela sessao e faz uma
pergunta. Por seguranca este teste entra sempre numa COPIA (fork): ele prova
o mecanismo sem escrever no historico da sua conversa. A ponte de verdade
entra sem copia, que e o que faz a conversa de voz aparecer no chat dela.

    .venv/Scripts/python.exe tools/testar_sessoes.py
    .venv/Scripts/python.exe tools/testar_sessoes.py --entrar "openclo"
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from bridge import sessoes            # noqa: E402
from bridge.comandos import interpretar  # noqa: E402

FALAS = [
    "muda para a sessão do Botivia",
    "vai para a sessão da ponte de voz",
    "entra na sessão do Opencló",
    "me leva para a sessão do site",
    "quais sessões eu tenho",
    "em que sessão estamos",
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    itens = sessoes.listar()
    print("Sessoes do aplicativo: %d\n" % len(itens))
    for i in itens:
        print("  %-34s %-12s %-14s %5.1f MB"
              % (i["titulo"][:34], i["pasta"][:12], sessoes.quando(i),
                 sessoes.tamanho(i) / 1e6))

    print("\nO que a ponte entende de cada fala:\n")
    falhas = 0
    for fala in FALAS:
        cmd = interpretar(fala, {}, apps={}, modelos={}, esforcos={})
        if not cmd:
            print("  %-38s -> NAO RECONHECIDO" % fala)
            falhas += 1
            continue
        extra = ""
        if cmd.acao == "trocar_sessao":
            alvo = sessoes.procurar(cmd.argumento, itens)
            extra = " -> " + (alvo["titulo"] if alvo else "NENHUMA SESSAO CASOU")
            if not alvo:
                falhas += 1
        print("  %-38s -> %s%s" % (fala, cmd.acao, extra))

    if "--entrar" in sys.argv:
        nome = sys.argv[sys.argv.index("--entrar") + 1]
        alvo = sessoes.procurar(nome, itens)
        if not alvo:
            print("\nNao achei sessao para '%s'" % nome)
            return 1
        print("\nEntrando em COPIA de: %s (%.1f MB de historico)"
              % (alvo["titulo"], sessoes.tamanho(alvo) / 1e6))
        from bridge.brain import PersistentBrain
        cfg = {"model": "haiku", "cwd": alvo["cwd"], "tools": "chat",
               "resume_session_id": alvo["cli"], "fork_session": True,
               "timeout_s": 300, "mode": "persistent"}
        brain = PersistentBrain(cfg)
        brain.start(warmup=False)         # como a ponte faz nas sessoes do app
        r = brain.ask("Em uma linha, do que trata esta conversa?")
        brain.close()
        print("  resposta:", (r.texto or r.erro)[:220])
        if not r.ok:
            falhas += 1

    print("\n%s" % ("tudo certo" if not falhas else "%d falha(s)" % falhas))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
