"""Compara o Piper local com a voz neural do Edge (online), para o sotaque.

    .venv\\Scripts\\python.exe tools\\comparar_sotaque.py
"""
import asyncio
import io
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.tts import PiperSpeaker  # noqa: E402

FRASE = ("{n}. Boa noite, Cadu. A cena da taverna está carregada e a iluminação "
         "já foi corrigida. Quer que eu renderize agora? A próxima exportação "
         "vai demorar três minutos.")


def tocar_mp3(dados: bytes) -> None:
    """Decodifica mp3 em memoria com PyAV e toca."""
    import av

    with av.open(io.BytesIO(dados)) as cont:
        stream = cont.streams.audio[0]
        sr = stream.rate
        quadros = [f.to_ndarray() for f in cont.decode(audio=0)]
    if not quadros:
        return
    pcm = np.concatenate([q.reshape(-1) if q.ndim == 1 else q.T.reshape(-1, q.shape[0])[:, 0]
                          for q in quadros])
    if pcm.dtype != np.float32:
        pcm = pcm.astype(np.float32) / np.iinfo(pcm.dtype).max
    sd.play(pcm, sr)
    sd.wait()


async def edge(texto: str, voz: str, rate: str = "+0%", pitch: str = "+0Hz"):
    import edge_tts

    com = edge_tts.Communicate(texto, voz, rate=rate, pitch=pitch)
    buf = bytearray()
    t0 = time.perf_counter()
    primeiro = None
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            if primeiro is None:
                primeiro = (time.perf_counter() - t0) * 1000
            buf.extend(chunk["data"])
    return bytes(buf), primeiro, (time.perf_counter() - t0) * 1000


def main() -> None:
    print("[1] Piper cadu-medium (local, offline)", flush=True)
    sp = PiperSpeaker("pt_BR-cadu-medium")
    t0 = time.perf_counter()
    sp.speak(FRASE.format(n="Opção um"))
    print("    tempo total: %.0f ms\n" % ((time.perf_counter() - t0) * 1000))
    time.sleep(0.4)

    for n, (voz, rate, pitch, rotulo) in enumerate([
        ("pt-BR-AntonioNeural", "+0%", "+0Hz", "Antonio, tom original"),
        ("pt-BR-AntonioNeural", "+0%", "-25Hz", "Antonio, mais grave"),
    ], start=2):
        print("[%d] Edge %s (online)" % (n, rotulo), flush=True)
        dados, ms_primeiro, ms_total = asyncio.run(
            edge(FRASE.format(n="Opção %d" % n), voz, rate, pitch))
        print("    primeiro byte de áudio: %.0f ms | síntese completa: %.0f ms"
              % (ms_primeiro or -1, ms_total), flush=True)
        tocar_mp3(dados)
        time.sleep(0.4)


if __name__ == "__main__":
    main()
