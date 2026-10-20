"""Mede a ponte com o Claude sozinha, sem microfone nem voz.

    .venv\\Scripts\\python.exe tools\\testar_brain.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.brain import build_brain  # noqa: E402
from bridge.config import Config  # noqa: E402

PERGUNTAS = [
    "Diga apenas: pronto.",
    "Em uma frase, o que e o projeto Tavern Tale?",
    "Qual foi a primeira coisa que eu te pedi nesta conversa?",
]


def main() -> None:
    cfg = Config.load()
    cfg["brain"]["resume_session_id"] = "none"      # conversa limpa para medir
    brain = build_brain(cfg["brain"])

    t0 = time.perf_counter()
    brain.start()
    boot = time.perf_counter() - t0
    print("\nboot do processo: %.2fs  (pago uma vez so)\n" % boot)

    for i, p in enumerate(PERGUNTAS, 1):
        print("[%d] voce: %s" % (i, p))
        frases = []
        marca = {}

        def on_sentence(f, marca=marca, frases=frases):
            if "primeira" not in marca:
                marca["primeira"] = time.perf_counter()
            frases.append(f)
            print("      -> %s" % f)

        t = time.perf_counter()
        r = brain.ask(p, on_sentence=on_sentence,
                      on_tool=lambda n: print("      [usou %s]" % n))
        if r.erro:
            print("      ERRO: %s" % r.erro)
            continue
        prim = ((marca.get("primeira", t) - t) * 1000)
        print("      1a frase pronta: %.0f ms | 1o texto: %.0f ms | total: %.0f ms"
              % (prim, r.ms_primeiro_texto, r.ms_total))
        if r.ferramentas:
            print("      ferramentas: %s" % ", ".join(r.ferramentas))
        print()

    print("session_id: %s" % brain.session_id)
    brain.close()


if __name__ == "__main__":
    main()
