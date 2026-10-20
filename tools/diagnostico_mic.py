"""Descobre QUAL entrada de audio realmente entrega a sua voz.

O Windows lista o mesmo microfone varias vezes, uma por API de audio (MME,
DirectSound, WASAPI, WDM-KS). Elas nao sao equivalentes: algumas aceitam
abrir numa taxa de amostragem que nao e a nativa e devolvem silencio em vez
de dar erro. Esta ferramenta abre cada uma, mede o que chega, e diz qual usar.

    .venv\\Scripts\\python.exe tools\\diagnostico_mic.py
    .venv\\Scripts\\python.exe tools\\diagnostico_mic.py --filtro quadcast

FALE SEM PARAR enquanto ela roda. Cada combinacao e testada por 3 segundos.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DURACAO = 3.0
TAXAS = [16000, 48000, 44100]


def medir(indice: int, taxa: int, canais: int, duracao: float) -> dict:
    """Abre o dispositivo e devolve o pico e a media do que chegou."""
    picos = []
    def cb(indata, frames, t, status):
        picos.append(float(np.sqrt(np.mean(indata[:, 0] ** 2))))
    try:
        with sd.InputStream(samplerate=taxa, device=indice, channels=canais,
                            dtype="float32", blocksize=1024, callback=cb):
            time.sleep(duracao)
    except Exception as exc:
        return {"erro": str(exc)[:70]}
    if not picos:
        return {"erro": "nenhum bloco de audio chegou"}
    arr = np.array(picos)
    return {"pico": float(arr.max()), "medio": float(arr.mean()),
            "blocos": len(picos)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filtro", default="",
                    help="so testa dispositivos com esse trecho no nome")
    ap.add_argument("--duracao", type=float, default=DURACAO)
    args = ap.parse_args()
    dur = args.duracao

    apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
    alvo = args.filtro.lower()

    candidatos = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        if alvo and alvo not in d["name"].lower():
            continue
        candidatos.append((i, d))

    if not candidatos:
        print("Nenhuma entrada com '%s' no nome." % args.filtro)
        return

    total = sum(len(TAXAS) for _ in candidatos)
    print("=" * 74)
    print(" FALE SEM PARAR ate o fim. Vou testar %d combinacoes, %.0fs cada"
          " (~%.0fs no total)." % (total, dur, total * dur))
    print("=" * 74)
    print()

    resultados = []
    for i, d in candidatos:
        api = apis.get(d["hostapi"], "?")
        nativa = int(d["default_samplerate"])
        print(" [%d] %s  (%s, nativa %d Hz)" % (i, d["name"], api, nativa))
        for taxa in TAXAS:
            r = medir(i, taxa, 1, dur)
            if "erro" in r:
                print("      %6d Hz  -> nao abriu: %s" % (taxa, r["erro"]))
                continue
            barras = int(min(1.0, r["pico"] * 20) * 28)
            print("      %6d Hz  pico %.4f  medio %.4f  [%-28s]"
                  % (taxa, r["pico"], r["medio"], "#" * barras))
            resultados.append((r["pico"], i, d["name"], api, taxa, nativa))
        print()

    if not resultados:
        print("Nada abriu. O microfone pode estar mudo no hardware.")
        return

    resultados.sort(reverse=True)
    print("=" * 74)
    melhor = resultados[0]
    pico, idx, nome, api, taxa, nativa = melhor

    if pico < 0.05:
        print(" NENHUMA combinacao recebeu sua voz (o maior pico foi %.4f)." % pico)
        print(" Isso nao e configuracao: nao esta chegando audio nenhum.")
        print()
        print(" Verifique, nesta ordem:")
        print("   1. O botao capacitivo no TOPO do QuadCast. LED apagado = mudo.")
        print("   2. Windows: Configuracoes > Sistema > Som > Microfone,")
        print("      veja se a barrinha se move quando voce fala.")
        print("   3. Privacidade > Microfone: aplicativos de area de trabalho")
        print("      precisam ter acesso liberado.")
        print("   4. Algum outro programa (OBS, Discord) pode ter pegado o")
        print("      dispositivo em modo exclusivo.")
        return

    print(" MELHOR: [%d] %s" % (idx, nome))
    print("         API %s | abrir a %d Hz | pico %.4f" % (api, taxa, pico))
    print()
    print(" Em config.toml, secao [audio]:")
    print('   input_device = "%s"' % nome)
    if taxa != 16000:
        print("   samplerate = %d        # e reamostrado para 16 kHz internamente"
              % taxa)
    print()
    ruins = [r for r in resultados if r[0] < pico * 0.1]
    if ruins:
        print(" Entregaram pouco ou nada (nao use):")
        for p, i, n, a, t, _ in ruins[:6]:
            print("   [%d] %s a %d Hz (%s): pico %.4f" % (i, n[:34], t, a, p))


if __name__ == "__main__":
    main()
