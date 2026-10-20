"""Transcricao com faster-whisper (CTranslate2), e deteccao do nome de ativacao.

Dois modelos podem viver ao mesmo tempo: o grande, usado quando a escuta esta
ligada, e um minusculo que fica so esperando voce chamar pelo nome.
"""
from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


def _registrar_dlls_cuda() -> None:
    """Deixa o CTranslate2 achar o cuBLAS e o cuDNN que vieram pelo pip.

    Sem isso a GPU carrega o modelo e depois estoura com
    'cublas64_12.dll is not found'.
    """
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not base.is_dir():
        return
    for sub in base.iterdir():
        binario = sub / "bin"
        if not binario.is_dir():
            continue
        try:
            os.add_dll_directory(str(binario))
        except OSError:
            pass
        os.environ["PATH"] = str(binario) + os.pathsep + os.environ.get("PATH", "")


_registrar_dlls_cuda()


@dataclass
class Resultado:
    texto: str = ""
    no_speech_prob: float = 1.0
    avg_logprob: float = -10.0
    duracao_audio: float = 0.0
    ms: float = 0.0
    idioma: str = ""
    descartado: str = ""          # motivo, quando nao passou nos filtros
    segmentos: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.texto) and not self.descartado


class Transcriber:
    def __init__(self, cfg_stt, model_size: str | None = None) -> None:
        from faster_whisper import WhisperModel

        self.cfg = cfg_stt
        self.model_size = model_size or cfg_stt.model
        device = cfg_stt.device
        compute = cfg_stt.compute_type
        if device == "cuda" and compute == "int8":
            compute = "int8_float16"       # int8 puro nao existe na GPU
        t0 = time.perf_counter()
        try:
            self.model = WhisperModel(
                self.model_size, device=device, compute_type=compute,
                cpu_threads=int(cfg_stt.cpu_threads), num_workers=1)
            self.device = device
        except Exception as exc:
            if device == "cuda":
                print("[stt] CUDA falhou (%s); caindo para CPU" % exc, flush=True)
                self.model = WhisperModel(
                    self.model_size, device="cpu", compute_type="int8",
                    cpu_threads=int(cfg_stt.cpu_threads), num_workers=1)
                self.device = "cpu"
            else:
                raise
        self.load_s = time.perf_counter() - t0
        print("[stt] modelo '%s' em %s carregado em %.1fs"
              % (self.model_size, self.device, self.load_s), flush=True)

    def transcribe(self, audio, aplicar_filtros: bool = True,
                   initial_prompt: str | None = None,
                   beam_size: int | None = None) -> Resultado:
        """Transcreve um trecho de audio.

        initial_prompt enviesa o decodificador para um vocabulario esperado.
        E o que salva a palavra de ativacao: uma palavra curta e isolada nao
        da evidencia acustica suficiente, e o Whisper entrega a frase de maior
        probabilidade em portugues — "Odin" virava "Bom dia", "Avantra" virava
        "Eu sei". Dizendo antes que aquela palavra pode aparecer, ela deixa de
        ser improvavel.
        """
        t0 = time.perf_counter()
        segs, info = self.model.transcribe(
            audio,
            language=self.cfg.language or None,
            beam_size=int(beam_size or self.cfg.beam_size),
            vad_filter=False,              # o VAD ja cortou a fala
            condition_on_previous_text=False,
            initial_prompt=initial_prompt,
            temperature=0.0,
        )
        segs = list(segs)
        texto = " ".join(s.text.strip() for s in segs).strip()
        texto = re.sub(r"\s+", " ", texto)

        res = Resultado(
            texto=texto,
            no_speech_prob=min((s.no_speech_prob for s in segs), default=1.0),
            avg_logprob=max((s.avg_logprob for s in segs), default=-10.0),
            duracao_audio=len(audio) / 16000,
            ms=(time.perf_counter() - t0) * 1000,
            idioma=getattr(info, "language", "") or "",
            segmentos=segs,
        )
        if aplicar_filtros:
            res.descartado = self._motivo_descarte(res)
        return res

    def _motivo_descarte(self, r: Resultado) -> str:
        """Filtros anti-lixo. Whisper alucina texto em cima de silencio e ruido."""
        if len(r.texto) < int(self.cfg.min_chars):
            return "curto demais"
        if r.no_speech_prob > float(self.cfg.max_no_speech_prob):
            return "p(sem fala)=%.2f" % r.no_speech_prob
        if r.avg_logprob < float(self.cfg.min_avg_logprob):
            return "confianca baixa (%.2f)" % r.avg_logprob
        if _eh_alucinacao(r.texto):
            return "alucinacao conhecida"
        return ""


# Frases que o Whisper inventa quando ouve silencio ou ruido. Sao legendas
# de treino que vazaram no modelo; aparecem sozinhas e nao significam nada.
_LIXO = {
    "obrigado", "obrigada", "tchau", "legendas pela comunidade amara.org",
    "legendado pela comunidade amara.org", "amara.org", "subtitles by the amara.org community",
    "...", ".", "..", "!", "?", "muito obrigado", "ate a proxima", "e ai",
    "subscribe", "inscreva-se", "legendas", "obrigado por assistir",
}


def normalizar(texto: str) -> str:
    """minusculo, sem acento, so letras e espaco."""
    t = unicodedata.normalize("NFD", texto.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _eh_alucinacao(texto: str) -> bool:
    n = normalizar(texto)
    if n in _LIXO:
        return True
    # "ah ah ah ah", "sim sim sim sim": a mesma palavra repetida e ruido.
    palavras = n.split()
    if len(palavras) >= 4 and len(set(palavras)) == 1:
        return True
    return False


def distancia(a: str, b: str) -> int:
    """Levenshtein. Duas linhas de matriz bastam."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    anterior = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        atual = [i]
        for j, cb in enumerate(b, 1):
            atual.append(min(anterior[j] + 1, atual[j - 1] + 1,
                             anterior[j - 1] + (ca != cb)))
        anterior = atual
    return anterior[-1]


class WakeWord:
    """Procura o nome de ativacao numa transcricao e devolve o que vem depois.

    Nome proprio inventado e o pior caso para o Whisper: nos testes "Avantra"
    saiu como "Aze", "A Izir", "A Izzy", "Aizuki" e "A Isaac". Listar todas as
    grafias nao escala, entao tem duas camadas: a lista exata do config e,
    depois, uma comparacao por semelhanca nas primeiras palavras da fala.
    Rode tools/calibrar_wake.py para alimentar a lista com a SUA voz.
    """

    def __init__(self, cfg_wake) -> None:
        self.cfg = cfg_wake
        # O NOME e como ele se apresenta. O GATILHO e a palavra que o acorda.
        # Sao coisas diferentes de proposito: um nome inventado como "Avantra"
        # nao existe no vocabulario do Whisper, entao ele chuta a palavra
        # portuguesa mais parecida, e chuta diferente a cada entonacao —
        # medido: Aizir, Aisir, Azevedo, Azeite, Aze, "A Izir", "Eu sei".
        # Nao da para consertar com mais grafias ("eu sei" e frase comum).
        # O gatilho precisa ser uma palavra que o Whisper JA conhece.
        self.nome = cfg_wake.get("nome", "Avantra")
        self.gatilho = (cfg_wake.get("gatilho") or "").strip() or self.nome
        self.frases = [normalizar(p) for p in cfg_wake.get("phrases", []) if p.strip()]
        self.frases = [f for f in self.frases if f]
        if not self.frases:
            self.frases = [normalizar(self.gatilho)]
        # Ordena da mais longa para a mais curta: "havantra" antes de "esir".
        self.frases.sort(key=len, reverse=True)
        self.alvo = normalizar(self.gatilho)
        # 0.35 deixa passar 1 erro num nome de 5 letras, mas nao 2. Com 0.4,
        # "sair" (2 erros) viraria falso positivo.
        self.tolerancia = float(cfg_wake.get("tolerancia", 0.35))
        # Em quantas palavras do inicio da fala procurar o nome.
        self.janela = int(cfg_wake.get("janela_palavras", 3))

    def detectar(self, texto: str):
        """-> (achou, resto_da_frase). O resto ja vale como primeiro pedido.

        Chamar o nome duas ou tres vezes seguidas e natural quando nao se tem
        certeza de ter sido ouvido, entao as repeticoes sao descartadas em vez
        de virarem um pedido sem sentido.
        """
        achou, resto = self._detectar_uma(texto)
        if not achou:
            return False, ""
        for _ in range(4):
            de_novo, menos = self._detectar_uma(resto)
            if not de_novo:
                break
            resto = menos
        return True, resto

    def _detectar_uma(self, texto: str):
        n = normalizar(texto)
        if not n:
            return False, ""

        for frase in self.frases:
            # Limite de palavra dos dois lados, para "esir" nao casar em "pesquisar".
            m = re.search(r"(?:^|\s)" + re.escape(frase) + r"(?:\s|$)", n)
            if m:
                return True, _resto_apos(texto, n, m.end()).strip(" ,.!?;:")

        achou, consumidas = self._por_semelhanca(n)
        if achou:
            palavras = n.split()
            resto = " ".join(palavras[consumidas:])
            return True, resto.strip(" ,.!?;:")
        return False, ""

    def _por_semelhanca(self, n: str) -> tuple[bool, int]:
        """Compara as primeiras palavras com o nome, juntando ate 2 delas.

        Pega os casos em que a transcricao parte o nome em dois ("a izir") ou
        troca uma letra ("aezir"), sem precisar de tudo listado.
        """
        palavras = n.split()
        for inicio in range(min(2, len(palavras))):
            for quantas in (1, 2):
                fim = inicio + quantas
                if fim > len(palavras) or fim > self.janela + inicio:
                    break
                cand = "".join(palavras[inicio:fim])
                if abs(len(cand) - len(self.alvo)) > 2:
                    continue
                razao = distancia(cand, self.alvo) / max(len(self.alvo), len(cand))
                if razao <= self.tolerancia:
                    return True, fim
        return False, 0


def _resto_apos(original: str, normalizado: str, pos_norm: int) -> str:
    """Mapeia uma posicao do texto normalizado de volta para o original."""
    i_norm = 0
    for i_orig, ch in enumerate(original):
        if i_norm >= pos_norm:
            return original[i_orig:]
        c = normalizar(ch)
        if c:
            i_norm += len(c)
        elif normalizado[i_norm:i_norm + 1] == " ":
            i_norm += 1
    return ""
