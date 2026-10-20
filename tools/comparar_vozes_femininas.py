"""Toca as opcoes de voz FEMININA em portugues, para voce escolher ouvindo.

O quadro honesto, levantado nesta maquina:

  Piper (offline)     NAO tem voz feminina pt-BR. Os quatro datasets do
                      repositorio oficial — cadu, edresson, faber, jeff —
                      sao todos masculinos. Nao e questao de baixar outra.
  SAPI (offline)      "Microsoft Maria Desktop" e feminina e funciona, mas e
                      uma voz concatenativa antiga: robotica.
  SAPI "Online"       Francisca e Thalita aparecem instaladas e sao femininas,
                      mas NAO sintetizam por esta API nesta maquina.
  Edge TTS (ONLINE)   Francisca e Thalita de verdade, qualidade neural. Exige
                      internet e manda o texto para a Microsoft.

Entra aqui tambem o Piper masculino com o tom levantado, nao porque fique bom,
mas para voce ouvir por que nao resolve: subir o tom de uma voz masculina nao
produz voz feminina, produz voz masculina acelerada.

    .venv\\Scripts\\python.exe tools\\comparar_vozes_femininas.py
"""
import asyncio
import io
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.tts import PiperSpeaker, SapiSpeaker  # noqa: E402

FRASE = ("Opção {n}. Boa noite, Cadu. A cena da taverna está carregada e a "
         "iluminação já foi corrigida. Quer que eu renderize agora?")


def tocar_mp3(dados: bytes) -> None:
    import av

    partes = []
    with av.open(io.BytesIO(dados)) as cont:
        res = av.AudioResampler(format="s16", layout="mono", rate=24000)
        for frame in cont.decode(audio=0):
            for f in res.resample(frame):
                partes.append(f.to_ndarray().reshape(-1))
        for f in res.resample(None):
            partes.append(f.to_ndarray().reshape(-1))
    if not partes:
        return
    sd.play(np.concatenate(partes).astype("int16"), 24000)
    sd.wait()


def edge(texto: str, voz: str, pitch: str = "+0Hz"):
    import edge_tts

    async def run():
        com = edge_tts.Communicate(texto, voz, pitch=pitch)
        buf = bytearray()
        t0 = time.perf_counter()
        primeiro = None
        async for ch in com.stream():
            if ch["type"] == "audio":
                if primeiro is None:
                    primeiro = (time.perf_counter() - t0) * 1000
                buf.extend(ch["data"])
        return bytes(buf), primeiro

    return asyncio.run(run())


def main() -> None:
    n = 0

    n += 1
    print("[%d] Microsoft Maria Desktop — OFFLINE, feminina, robotica" % n,
          flush=True)
    try:
        SapiSpeaker("Microsoft Maria Desktop", 1).play(FRASE.format(n=n))
    except Exception as exc:
        print("    falhou: %s" % exc)
    time.sleep(0.4)

    for voz, pitch, rotulo in [
        ("pt-BR-FranciscaNeural", "+0Hz", "Edge Francisca — ONLINE, neural"),
        ("pt-BR-ThalitaMultilingualNeural", "+0Hz", "Edge Thalita — ONLINE, neural"),
        ("pt-BR-FranciscaNeural", "-10Hz", "Edge Francisca, um pouco mais grave"),
    ]:
        n += 1
        print("[%d] %s" % (n, rotulo), flush=True)
        try:
            dados, ms = edge(FRASE.format(n=n), voz, pitch)
            print("    primeiro audio em %.0f ms" % (ms or -1), flush=True)
            tocar_mp3(dados)
        except Exception as exc:
            print("    falhou (sem internet?): %s" % str(exc)[:80])
        time.sleep(0.4)

    n += 1
    print("[%d] Piper cadu com o tom 4 semitons ACIMA — offline, e por que "
          "isso nao resolve" % n, flush=True)
    try:
        sp = PiperSpeaker("pt_BR-cadu-medium", pitch=4.0)
        sp.play(sp.synthesize(FRASE.format(n=n)))
    except Exception as exc:
        print("    falhou: %s" % exc)

    print()
    print("Resumindo o que da para escolher:")
    print("  offline + feminina            -> so a Maria Desktop (robotica)")
    print("  feminina boa + offline        -> nao existe pronta; precisa clonar")
    print("  feminina boa, aceitando nuvem -> Edge Francisca ou Thalita")
    print()
    print("Para fixar: em config.toml, [tts] backend = \"sapi\" ou \"edge\",")
    print("e edge_voice = \"pt-BR-FranciscaNeural\".")


if __name__ == "__main__":
    main()
