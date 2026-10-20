"""Mede a transcricao sem depender do microfone.

Sintetiza frases com o Piper, reamostra para 16 kHz e passa pelo Whisper.
Nao e igual a sua voz real (voz sintetica e mais facil), mas da a medida de
TEMPO por tamanho de modelo, que e o que importa para escolher.

    .venv\\Scripts\\python.exe tools\\testar_stt.py
    .venv\\Scripts\\python.exe tools\\testar_stt.py --modelos tiny,small,medium
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge.config import Config  # noqa: E402
from bridge.stt import Transcriber, normalizar  # noqa: E402
from bridge.tts import PiperSpeaker  # noqa: E402

FRASES = [
    "Avantra, me mostra como está a cena da taverna.",
    "Quantos objetos tem na coleção de iluminação do arquivo principal?",
    "Renderiza um frame de teste em resolução baixa e me avisa quando terminar.",
    "Não, deixa pra depois. Vamos falar do sistema de combate.",
]


def reamostrar(pcm16: np.ndarray, de: int, para: int) -> np.ndarray:
    """Interpolacao linear. Suficiente para alimentar o Whisper."""
    if de == para:
        return pcm16.astype(np.float32) / 32768.0
    n = int(round(len(pcm16) * para / de))
    x = np.linspace(0, len(pcm16) - 1, n)
    return np.interp(x, np.arange(len(pcm16)), pcm16).astype(np.float32) / 32768.0


def erro_palavras(ref: str, hip: str) -> float:
    """WER simples por distancia de edicao em palavras."""
    r, h = normalizar(ref).split(), normalizar(hip).split()
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=np.int32)
    d[:, 0] = np.arange(len(r) + 1)
    d[0, :] = np.arange(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            custo = 0 if r[i - 1] == h[j - 1] else 1
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + custo)
    return d[len(r), len(h)] / max(1, len(r))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelos", default="tiny,small")
    ap.add_argument("--device", default=None, help="cpu ou cuda")
    args = ap.parse_args()

    cfg = Config.load()
    if args.device:
        cfg["stt"]["device"] = args.device
        cfg["stt"]["compute_type"] = ("float16" if args.device == "cuda"
                                      else "int8")
    print("Gerando audio de teste com o Piper ...", flush=True)
    piper = PiperSpeaker(cfg["tts"].get("piper_voice", "pt_BR-cadu-medium"))
    sr_nativo = piper.voice.config.sample_rate

    amostras = []
    for frase in FRASES:
        pedacos = piper.synthesize(frase)
        pcm = np.frombuffer(b"".join(pedacos), dtype=np.int16)
        amostras.append((frase, reamostrar(pcm, sr_nativo, 16000)))
    total_audio = sum(len(a) for _, a in amostras) / 16000
    print("  %d frases, %.1fs de audio\n" % (len(amostras), total_audio))

    for tam in [m.strip() for m in args.modelos.split(",") if m.strip()]:
        print("=" * 60)
        try:
            t0 = time.perf_counter()
            stt = Transcriber(cfg["stt"], model_size=tam)
            carga = time.perf_counter() - t0
        except Exception as exc:
            print("  %s nao carregou: %s" % (tam, exc))
            continue

        tempos, wers = [], []
        for frase, audio in amostras:
            r = stt.transcribe(audio)
            w = erro_palavras(frase, r.texto)
            tempos.append(r.ms)
            wers.append(w)
            marca = "ok " if w == 0 else "DIF"
            print("  [%s] %5.0f ms  wer %.0f%%  %s" % (marca, r.ms, w * 100, r.texto))
        print("  --- %s em %s: carga %.1fs | %.0f ms por fala (mediana) | "
              "WER medio %.0f%% | %.1fx tempo real"
              % (tam, stt.device, carga, float(np.median(tempos)),
                 float(np.mean(wers)) * 100,
                 total_audio / (sum(tempos) / 1000)))
        del stt


if __name__ == "__main__":
    main()
