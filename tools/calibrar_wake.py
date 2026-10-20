"""Descobre como o Whisper escreve o nome quando VOCE fala.

Nome proprio inventado e o pior caso para transcricao. Em vez de adivinhar as
grafias, esta ferramenta ouve voce dizer o nome algumas vezes, mostra o que o
modelo entendeu, e grava no config.toml. Depois disso a palavra de ativacao
passa a funcionar com a sua voz, no seu microfone.

    .venv\\Scripts\\python.exe tools\\calibrar_wake.py
    .venv\\Scripts\\python.exe tools\\calibrar_wake.py --vezes 10
"""
import argparse
import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.audio import Listener  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.stt import Transcriber, distancia, normalizar  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vezes", type=int, default=6)
    ap.add_argument("--nao-gravar", action="store_true",
                    help="so mostra, nao mexe no config.toml")
    args = ap.parse_args()

    cfg = Config.load()
    nome = cfg["wake"].get("nome", "Avantra")
    tam_wake = cfg["wake"].get("model", "tiny")

    print("Carregando os modelos ...", flush=True)
    # tam_wake vazio quer dizer "usa o principal". Carregar um segundo modelo
    # na mesma GPU estoura a VRAM, entao so faz isso se de fato for outro.
    tam_wake = (tam_wake or "").strip() or cfg["stt"]["model"]
    stt_wake = Transcriber(cfg["stt"], model_size=tam_wake)
    stt_bom = stt_wake

    falas: queue.Queue = queue.Queue()
    lis = Listener(cfg["audio"], cfg["vad"], falas.put)
    lis.start()

    print()
    print("=" * 62)
    print(" Diga o nome '%s' %d vezes, com uma pausa entre cada." % (nome, args.vezes))
    print(" Varie: sozinho, e tambem no comeco de um pedido.")
    print(" Exemplos: \"%s\"   ou   \"%s, abre a cena\"" % (nome, nome))
    print("=" * 62)
    print()

    vistos: list[str] = []
    gravadas = 0            # falas, nao grafias: uma fala rende varias grafias
    while gravadas < args.vezes:
        try:
            utt = falas.get(timeout=0.3)
        except queue.Empty:
            barras = int(min(1.0, lis.nivel * 25) * 24)
            print("\r  ouvindo [%-24s]  %d de %d"
                  % ("#" * barras, gravadas, args.vezes), end="", flush=True)
            continue

        r_wake = stt_wake.transcribe(utt.audio, aplicar_filtros=False)
        r_bom = (r_wake if stt_bom is stt_wake
                 else stt_bom.transcribe(utt.audio, aplicar_filtros=False))
        if not r_wake.texto.strip() and not r_bom.texto.strip():
            continue

        gravadas += 1
        print("\r" + " " * 50, end="\r")
        print("  %d) %-6s -> %s" % (gravadas, tam_wake, r_wake.texto))
        if stt_bom is not stt_wake:
            print("     %-6s -> %s" % (cfg["stt"]["model"], r_bom.texto))

        # A primeira palavra (ou as duas primeiras) e onde o nome mora.
        for texto in {r_wake.texto, r_bom.texto}:
            p = normalizar(texto).split()
            if not p:
                continue
            vistos.append(p[0])
            if len(p) >= 2 and len(p[0]) <= 2:
                vistos.append(p[0] + p[1])      # "a izir" -> "aizir"
                vistos.append(p[0] + " " + p[1])

    lis.close()

    alvo = normalizar(nome)
    atuais = [normalizar(x) for x in cfg["wake"].get("phrases", [])]

    # Palavras comuns do portugues. Se uma delas entrar na lista, ele acorda
    # no meio de qualquer frase.
    COMUNS = {"sair", "ser", "sei", "seis", "assim", "aqui", "ali", "ai",
              "isso", "essa", "esse", "sabe", "ouvir", "assistir", "existe",
              "serie", "serio", "disse", "deixa", "era", "ser", "hoje"}

    candidatas = []
    for v in vistos:
        if not v or v in atuais or len(v) < 3:
            continue
        if any(v == c[0] for c in candidatas):
            continue
        razao = distancia(v.replace(" ", ""), alvo) / max(len(alvo), len(v))
        candidatas.append((v, razao))

    print()
    print("-" * 62)
    if not candidatas:
        print(" Nada novo: o modelo ja acerta o nome com as grafias atuais.")
        return

    # Distancia nao separa "aze" (o nome mal transcrito) de "tosse" (barulho):
    # as duas dao 0,80. Entao quem decide e voce; eu so sugiro.
    print(" O modelo escreveu isto. Marque o que foi VOCE dizendo '%s'." % nome)
    print(" (Enter aceita a sugestao entre colchetes.)")
    print()
    escolhidas = []
    for v, razao in candidatas:
        if v in COMUNS:
            dica, padrao = "palavra comum do portugues, perigoso", "n"
        elif razao <= 0.4:
            dica, padrao = "parece o nome", "s"
        elif razao <= 0.75:
            dica, padrao = "pode ser o nome mal transcrito", "s"
        else:
            dica, padrao = "parece outra coisa", "n"
        if args.nao_gravar:
            print("   %-16s  %-34s  [%s]" % (v, "(" + dica + ")", padrao))
            if padrao == "s":
                escolhidas.append(v)
            continue
        try:
            resp = input("   %-16s  %-34s  usar? [%s] "
                         % (v, "(" + dica + ")", padrao)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            resp = ""
        if (resp or padrao).startswith("s"):
            escolhidas.append(v)

    print()
    if not escolhidas:
        print(" Nada marcado, config.toml intacto.")
        return

    if args.nao_gravar:
        print(" Rodei com --nao-gravar, nao mexi no config.toml.")
        print(" Entrariam: %s" % ", ".join(escolhidas))
        return

    todas = atuais + escolhidas
    linha = "phrases = [" + ", ".join('"%s"' % v for v in todas) + "]"
    caminho = ROOT / "config.toml"
    texto = caminho.read_text(encoding="utf-8")
    import re
    novo, n = re.subn(r"(?ms)^phrases = \[.*?\]", linha, texto, count=1)
    if n:
        caminho.write_text(novo, encoding="utf-8")
        print(" config.toml atualizado: %d grafias na lista (+%d novas)."
              % (len(todas), len(escolhidas)))
    else:
        print(" Nao achei a linha 'phrases' no config.toml. Cole isto na secao [wake]:")
        print("   " + linha)


if __name__ == "__main__":
    main()
