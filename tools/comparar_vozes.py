"""Toca a mesma frase em varias vozes/tons para voce escolher.

    .venv\\Scripts\\python.exe tools\\comparar_vozes.py
    .venv\\Scripts\\python.exe tools\\comparar_vozes.py --frase "outra frase"
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.tts import PiperSpeaker  # noqa: E402

FRASE = ("Opção {n}. Boa noite, Cadu. A cena da taverna está carregada e a "
         "iluminação já foi corrigida. Quer que eu renderize agora?")

# (voz, semitons de tom, velocidade, rotulo)
OPCOES = [
    ("pt_BR-faber-medium", 0.0, 1.0, "faber, tom original"),
    ("pt_BR-faber-medium", -2.0, 1.0, "faber, 2 semitons mais grave"),
    ("pt_BR-faber-medium", -4.0, 1.0, "faber, 4 semitons mais grave"),
    ("pt_BR-cadu-medium", 0.0, 1.0, "cadu, tom original"),
    ("pt_BR-cadu-medium", -3.0, 1.0, "cadu, 3 semitons mais grave"),
    ("pt_BR-jeff-medium", 0.0, 1.0, "jeff, tom original"),
    ("pt_BR-jeff-medium", -3.0, 1.0, "jeff, 3 semitons mais grave"),
    # edresson-low fica de fora: o modelo nao tem o fonema do til no mapa,
    # entao engole as vogais nasais ("nao", "informacao").
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frase", default=None)
    ap.add_argument("--so", default=None, help="filtra pelo nome da voz")
    args = ap.parse_args()

    carregadas = {}
    for i, (voz, pitch, speed, rotulo) in enumerate(OPCOES, 1):
        if args.so and args.so not in voz:
            continue
        print("[%d] %s" % (i, rotulo), flush=True)
        if voz not in carregadas:
            carregadas[voz] = PiperSpeaker(voz)
        sp = carregadas[voz]
        sp.pitch, sp.speed = pitch, speed
        texto = args.frase or FRASE.format(n=i)
        sp.speak(texto)
        time.sleep(0.35)

    print("\nPara fixar a escolha, em config.toml:")
    print('  piper_voice = "pt_BR-<voz>-medium"')
    print("  piper_pitch = -3.0   # semitons, negativo = mais grave")
    print("  piper_speed = 1.0    # 1.1 fala mais rapido")


if __name__ == "__main__":
    main()
