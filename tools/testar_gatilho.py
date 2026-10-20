"""Mede se o gatilho e reconhecido, com e sem enviesar o Whisper.

Uma palavra curta e isolada e o pior caso para transcricao: nao ha evidencia
acustica suficiente, e o modelo entrega a frase de maior probabilidade do
idioma. Medido aqui com a voz do Cadu: "Odin" virou "Bom dia", "O Dinho" e
"Ou-jin"; "Aesir" virou "Eu sei", "Azevedo" e "Azim".

O initial_prompt diz ao modelo que aquela palavra pode aparecer, e muda o
resultado. Esta ferramenta compara os dois casos para VARIOS candidatos, para
voce escolher o gatilho com dado em vez de palpite.

    .venv\\Scripts\\python.exe tools\\testar_gatilho.py
    .venv\\Scripts\\python.exe tools\\testar_gatilho.py --candidatos Odin,Avantra,Jarvis
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.config import Config  # noqa: E402
from bridge.stt import Transcriber, normalizar  # noqa: E402
from bridge.tts import PiperSpeaker  # noqa: E402

# Como a palavra costuma aparecer na pratica: sozinha e abrindo um pedido.
MOLDES = ["{g}", "{g}.", "{g}?", "{g}, me escuta", "{g}, abre a cena da taverna"]


def dica(g: str) -> str:
    return "%s. %s, me escuta. %s, abre a cena. %s, qual o status? %s." % (
        g, g, g, g, g)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidatos", default="Avantra,Odin,Jarvis")
    args = ap.parse_args()

    cfg = Config.load()
    piper = PiperSpeaker(cfg["tts"].get("piper_voice", "pt_BR-cadu-medium"))
    sr_nativo = piper.voice.config.sample_rate
    stt = Transcriber(cfg["stt"])

    def falar(texto: str):
        pcm = np.frombuffer(b"".join(piper.synthesize(texto)), dtype=np.int16)
        n = int(len(pcm) * 16000 / sr_nativo)
        x = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm)
        return (x.astype(np.float32) / 32768.0)

    print()
    print("AVISO: isto usa voz sintetica, que e MAIS FACIL que a sua. Serve")
    print("para comparar candidatos entre si, nao para prever o acerto real.")
    print()

    for g in [c.strip() for c in args.candidatos.split(",") if c.strip()]:
        alvo = normalizar(g)
        acertos_sem = acertos_com = 0
        print("=" * 70)
        print(" %s" % g)
        print("=" * 70)
        for molde in MOLDES:
            audio = falar(molde.format(g=g))
            sem = stt.transcribe(audio, aplicar_filtros=False).texto.strip()
            com = stt.transcribe(audio, aplicar_filtros=False,
                                 initial_prompt=dica(g), beam_size=5).texto.strip()
            ok_sem = alvo in normalizar(sem).split()
            ok_com = alvo in normalizar(com).split()
            acertos_sem += ok_sem
            acertos_com += ok_com
            print("   sem dica  %s  %s" % ("ok " if ok_sem else "NAO", sem[:52]))
            print("   com dica  %s  %s" % ("ok " if ok_com else "NAO", com[:52]))
        print("   --> sem dica %d/%d | com dica %d/%d"
              % (acertos_sem, len(MOLDES), acertos_com, len(MOLDES)))
        print()


if __name__ == "__main__":
    main()
