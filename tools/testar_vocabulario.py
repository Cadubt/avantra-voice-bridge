"""Mede o acerto nos nomes proprios do projeto, com e sem o vocabulario.

"TavernTale" virava "Taverneiro" porque nome proprio desconhecido perde para a
palavra comum mais parecida. O initial_prompt muda a probabilidade desses
termos. Aqui da para ver quanto muda, e se vale subir de modelo.

    .venv\\Scripts\\python.exe tools\\testar_vocabulario.py
    .venv\\Scripts\\python.exe tools\\testar_vocabulario.py --modelos medium,large-v3
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.config import Config  # noqa: E402
from bridge.stt import Transcriber, normalizar  # noqa: E402
from bridge.tts import PiperSpeaker  # noqa: E402

# (frase, termos que PRECISAM aparecer certos)
CASOS = [
    ("Abre a cena do TavernTale pra mim", ["taverntale"]),
    ("Quero continuar o desenvolvimento do TavernTale", ["taverntale"]),
    ("Avantra, renderiza a hunt no Blender", ["avantra", "blender"]),
    ("Exporta o sprite pro projeto Unity", ["sprite", "unity"]),
    ("A jogabilidade é inspirada no Tibia", ["tibia"]),
    ("Os shaders da hunt estão pesados", ["shaders"]),
    ("Isso é da Cadubt Entertainment", ["cadubt"]),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelos", default="medium")
    args = ap.parse_args()

    cfg = Config.load()
    vocab = (cfg["stt"].get("vocabulario") or "").strip()
    if not vocab:
        print("Nenhum vocabulario em [stt].vocabulario do config.toml.")
        return

    piper = PiperSpeaker(cfg["tts"].get("piper_voice", "pt_BR-cadu-medium"))
    sr = piper.voice.config.sample_rate

    def falar(texto: str):
        pcm = np.frombuffer(b"".join(piper.synthesize(texto)), dtype=np.int16)
        n = int(len(pcm) * 16000 / sr)
        x = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm)
        return (x.astype(np.float32) / 32768.0)

    audios = [(f, termos, falar(f)) for f, termos in CASOS]
    print()
    print("AVISO: voz sintetica e mais facil que a sua. Compare as COLUNAS")
    print("entre si, nao tome o numero absoluto como previsao.")

    for tam in [m.strip() for m in args.modelos.split(",") if m.strip()]:
        try:
            stt = Transcriber(cfg["stt"], model_size=tam)
        except Exception as exc:
            print("  %s nao carregou: %s" % (tam, exc))
            continue
        sem = com = 0
        total = 0
        print()
        print("=" * 72)
        print(" %s" % tam)
        print("=" * 72)
        for frase, termos, audio in audios:
            r_sem = stt.transcribe(audio, aplicar_filtros=False).texto
            r_com = stt.transcribe(audio, aplicar_filtros=False,
                                   initial_prompt=vocab).texto
            n_sem = normalizar(r_sem)
            n_com = normalizar(r_com)
            for termo in termos:
                total += 1
                sem += termo in n_sem
                com += termo in n_com
            falhou_sem = [t for t in termos if t not in n_sem]
            falhou_com = [t for t in termos if t not in n_com]
            print("  sem vocab %s %s" % ("ok " if not falhou_sem else "NAO",
                                         r_sem.strip()[:56]))
            print("  com vocab %s %s" % ("ok " if not falhou_com else "NAO",
                                         r_com.strip()[:56]))
        print("  --> termos certos: sem vocabulario %d/%d | com %d/%d"
              % (sem, total, com, total))
        del stt


if __name__ == "__main__":
    main()
