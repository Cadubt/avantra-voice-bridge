"""Fala um texto com o backend configurado. Util para testar a voz.

    .venv\\Scripts\\python.exe tools\\dizer.py "texto aqui"
    .venv\\Scripts\\python.exe tools\\dizer.py --arquivo tmp\\status.txt
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.config import Config  # noqa: E402
from bridge.tts import (SpeechQueue, build_speaker, clean_for_speech,  # noqa: E402
                        split_sentences)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("texto", nargs="*")
    ap.add_argument("--arquivo")
    ap.add_argument("--backend", help="sobrescreve o backend do config")
    args = ap.parse_args()

    if args.arquivo:
        texto = Path(args.arquivo).read_text(encoding="utf-8")
    else:
        texto = " ".join(args.texto) or sys.stdin.read()

    cfg = Config.load()
    if args.backend:
        cfg["tts"]["backend"] = args.backend
    sp = build_speaker(cfg["tts"])

    t0 = time.perf_counter()
    marcas = []
    fila = SpeechQueue(sp, on_first_sound=lambda: marcas.append(time.perf_counter()))
    for frase in split_sentences(clean_for_speech(texto)):
        fila.say(frase)
    fila.wait_idle(600)
    prim = (marcas[0] - t0) * 1000 if marcas else -1
    print("[%s] primeiro som em %.0f ms | %.1f s no total"
          % (sp.name, prim, time.perf_counter() - t0))
    fila.close()


if __name__ == "__main__":
    main()
