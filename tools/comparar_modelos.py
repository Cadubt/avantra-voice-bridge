"""Compara modelos e modos pelo tempo ate a PRIMEIRA FRASE falada.

Numa conversa por voz o que importa nao e o tempo total: e quanto tempo voce
fica no silencio depois de calar a boca. Esse e o numero medido aqui.

    .venv\\Scripts\\python.exe tools\\comparar_modelos.py
    .venv\\Scripts\\python.exe tools\\comparar_modelos.py --modelos haiku,sonnet
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.brain import build_brain  # noqa: E402
from bridge.config import Config  # noqa: E402

PERGUNTAS = [
    "Me explica em duas frases o que e oclusao de ambiente.",
    "Qual a diferenca entre sprite e billboard?",
    "Se eu tenho quarenta objetos numa cena, vale juntar em um so?",
]


def medir(cfg, modelo: str, tools: str, cwd: str | None = None) -> dict:
    c = dict(cfg["brain"])
    c["model"] = modelo
    c["tools"] = tools
    c["resume_session_id"] = "none"
    if cwd:
        c["cwd"] = cwd
    from bridge.config import Section
    brain = build_brain(Section(c))

    t0 = time.perf_counter()
    try:
        brain.start()
    except Exception as exc:
        print("   nao subiu: %s" % str(exc)[:160])
        return {}
    boot = time.perf_counter() - t0

    primeiras, totais = [], []
    for p in PERGUNTAS:
        marca = {}
        t = time.perf_counter()
        r = brain.ask(p, on_sentence=lambda f, m=marca, tt=t: m.setdefault(
            "t", time.perf_counter() - tt))
        if r.erro:
            print("   erro: %s" % r.erro[:120])
            continue
        primeiras.append(marca.get("t", r.ms_total / 1000) * 1000)
        totais.append(r.ms_total)
    brain.close()
    if not primeiras:
        return {}
    return {"boot": boot, "primeira": statistics.median(primeiras),
            "total": statistics.median(totais), "n": len(primeiras)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelos", default="haiku,sonnet")
    ap.add_argument("--tools", default="chat,full")
    args = ap.parse_args()

    cfg = Config.load()
    pasta_leve = str(Path(__file__).resolve().parent.parent)

    combos = []
    for tools in [t.strip() for t in args.tools.split(",") if t.strip()]:
        for modelo in [m.strip() for m in args.modelos.split(",") if m.strip()]:
            combos.append((modelo, tools, None, "projeto"))
    # Uma pasta sem MCP nem CLAUDE.md, para ver quanto o prompt grande custa.
    combos.append(("haiku", "chat", pasta_leve, "pasta vazia"))
    combos.append(("sonnet", "chat", pasta_leve, "pasta vazia"))

    linhas = []
    for modelo, tools, cwd, rotulo in combos:
        print("\n== %s | ferramentas=%s | %s" % (modelo, tools, rotulo), flush=True)
        r = medir(cfg, modelo, tools, cwd)
        if not r:
            continue
        print("   boot %.1fs | 1a frase %.0f ms | total %.0f ms (%d perguntas)"
              % (r["boot"], r["primeira"], r["total"], r["n"]))
        linhas.append((modelo, tools, rotulo, r))

    print("\n" + "=" * 70)
    print("%-8s %-6s %-12s %7s %11s %10s" % ("modelo", "tools", "pasta",
                                             "boot", "1a frase", "total"))
    print("-" * 70)
    for modelo, tools, rotulo, r in sorted(linhas, key=lambda x: x[3]["primeira"]):
        print("%-8s %-6s %-12s %6.1fs %9.0fms %8.0fms"
              % (modelo, tools, rotulo, r["boot"], r["primeira"], r["total"]))
    print("=" * 70)
    print("'1a frase' = quanto tempo voce fica no silencio. E o que importa.")


if __name__ == "__main__":
    main()
