"""Captura do microfone e deteccao de fim de fala (VAD).

O microfone fica sempre aberto; quem decide o que e fala e o VAD.
Dois backends: Silero (modelo neural, 2 MB, roda em onnxruntime na CPU) e
um medidor de volume puro, para quando o Silero nao estiver disponivel.
"""
from __future__ import annotations

import queue
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
VAD_PATH = ROOT / "models" / "silero_vad.onnx"
VAD_URL = ("https://raw.githubusercontent.com/snakers4/silero-vad/master/"
           "src/silero_vad/data/silero_vad.onnx")

# O Silero v5 so aceita janelas de 512 amostras a 16 kHz.
WINDOW = 512
SR = 16000


def list_devices() -> str:
    import sounddevice as sd
    linhas = ["Entradas disponiveis:"]
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            marca = " <- padrao" if i == sd.default.device[0] else ""
            linhas.append("  [%d] %s (%d ch, %.0f Hz)%s"
                          % (i, d["name"], d["max_input_channels"],
                             d["default_samplerate"], marca))
    return "\n".join(linhas)


# O Windows expoe o mesmo microfone uma vez por API de audio, e elas NAO sao
# equivalentes. Medido no HyperX QuadCast desta maquina:
#
#   MME          16 kHz -> silencio, sem erro    44.1 kHz (nativa) -> ok
#   DirectSound  16 kHz -> ok                    qualquer taxa     -> ok
#   WASAPI       16 kHz -> erro honesto          48 kHz (nativa)   -> ok
#   WDM-KS       nao abre
#
# O MME e o perigoso: aceita abrir numa taxa que nao e a nativa e devolve
# zeros em silencio. Por isso a escolha abaixo nunca o prefere, e quando ele
# e a unica opcao, forca a taxa nativa dele.
PRIORIDADE_API = ["windows directsound", "windows wasapi", "mme"]


def _rank_api(nome_api: str) -> int:
    n = nome_api.lower()
    for i, alvo in enumerate(PRIORIDADE_API):
        if alvo in n:
            return i
    return len(PRIORIDADE_API)


def escolher_entrada(substring: str, taxa_desejada: int = SR):
    """-> (indice, taxa_de_captura, nome, api).

    Escolhe o endpoint e a taxa em que ele REALMENTE entrega audio, nao a
    primeira que abre sem erro. Devolve (None, taxa, ...) para usar o padrao
    do Windows quando nada casa com o nome pedido.
    """
    import sounddevice as sd

    apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
    alvo = (substring or "").lower()

    candidatos = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        if alvo and alvo not in d["name"].lower():
            continue
        api = apis.get(d["hostapi"], "?")
        candidatos.append((_rank_api(api), i, d, api))

    if not candidatos:
        if alvo:
            print("[audio] nenhuma entrada com '%s' no nome; usando o padrao "
                  "do Windows" % substring, flush=True)
        return None, taxa_desejada, "(padrao do Windows)", "?"

    candidatos.sort(key=lambda c: c[0])
    for rank, i, d, api in candidatos:
        nativa = int(d["default_samplerate"])
        # No MME so a taxa nativa entrega audio de verdade.
        taxas = [nativa] if "mme" in api.lower() else [taxa_desejada, nativa]
        for taxa in taxas:
            try:
                sd.check_input_settings(device=i, samplerate=taxa, channels=1,
                                        dtype="float32")
            except Exception:
                continue
            return i, int(taxa), d["name"], api

    rank, i, d, api = candidatos[0]
    return i, int(d["default_samplerate"]), d["name"], api


def find_device(substring: str):
    """Compatibilidade: so o indice."""
    return escolher_entrada(substring)[0]


class Resampler:
    """Converte a taxa de captura para os 16 kHz que o VAD e o Whisper exigem.

    Guarda estado entre blocos: sem isso aparece um estalo na emenda de cada
    bloco, e o VAD conta esses estalos como fala.
    """

    def __init__(self, de: int, para: int = SR) -> None:
        self.de = int(de)
        self.para = int(para)
        self.ativo = self.de != self.para
        self._fase = 0.0
        self._ultimo = np.zeros(1, dtype=np.float32)
        self._fir = None
        self._cauda = None
        if self.ativo and self.para < self.de:
            # Passa-baixa antes de decimar, senao o que esta acima de 8 kHz
            # dobra para dentro da banda e vira chiado.
            self._fir = self._janela_sinc(0.45 * self.para / self.de, 63)
            self._cauda = np.zeros(len(self._fir) - 1, dtype=np.float32)

    @staticmethod
    def _janela_sinc(corte: float, taps: int) -> np.ndarray:
        n = np.arange(taps) - (taps - 1) / 2
        h = 2 * corte * np.sinc(2 * corte * n)
        h *= np.hamming(taps)
        return (h / h.sum()).astype(np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if not self.ativo:
            return x.astype(np.float32)
        x = x.astype(np.float32)
        if self._fir is not None:
            junto = np.concatenate([self._cauda, x])
            self._cauda = junto[-(len(self._fir) - 1):]
            x = np.convolve(junto, self._fir, mode="valid")
        if len(x) == 0:
            return x
        passo = self.de / self.para
        # Continua de onde o bloco anterior parou, incluindo a fracao.
        n = int(np.floor((len(x) - self._fase - 1) / passo)) + 1
        if n <= 0:
            self._fase -= len(x)
            return np.zeros(0, dtype=np.float32)
        pos = self._fase + np.arange(n) * passo
        base = np.concatenate([self._ultimo, x])
        y = np.interp(pos + 1.0, np.arange(len(base)), base).astype(np.float32)
        self._fase = pos[-1] + passo - len(x)
        self._ultimo = x[-1:]
        return y


class EnergyVAD:
    """Volume RMS puro. Simples, sem dependencia, mas confunde barulho com fala."""

    name = "energy"

    def __init__(self, threshold: float = 0.015) -> None:
        self.threshold = threshold

    def prob(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)) + 1e-9)
        return 1.0 if rms >= self.threshold else 0.0

    def reset(self) -> None:
        pass


# O Silero v5 nao analisa a janela isolada: ele espera 64 amostras de CONTEXTO
# da janela anterior grudadas na frente das 512 novas, totalizando 576. Como a
# entrada do modelo tem dimensao livre, mandar so 512 nao da erro — ele aceita
# e devolve probabilidade ~0.001 para qualquer coisa, inclusive fala limpa.
CONTEXTO = 64


class SileroVAD:
    """Silero VAD v5 em onnxruntime. Sem torch: sao 2 MB e roda na CPU."""

    name = "silero"

    def __init__(self, threshold: float = 0.6) -> None:
        import onnxruntime as ort

        ensure_vad_model()
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(str(VAD_PATH), sess_options=opts,
                                         providers=["CPUExecutionProvider"])
        self.inputs = [i.name for i in self.sess.get_inputs()]
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._contexto = np.zeros(CONTEXTO, dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        frame = frame.astype(np.float32)
        x = np.concatenate([self._contexto, frame]).reshape(1, -1)
        self._contexto = frame[-CONTEXTO:].copy()
        feed = {"input": x, "sr": np.array(SR, dtype=np.int64)}
        if "state" in self.inputs:
            feed["state"] = self._state
        out = self.sess.run(None, feed)
        if len(out) > 1:
            self._state = out[1]
        return float(np.asarray(out[0]).reshape(-1)[0])


def ensure_vad_model() -> Path:
    if VAD_PATH.exists() and VAD_PATH.stat().st_size > 100_000:
        return VAD_PATH
    VAD_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("[vad] baixando silero_vad.onnx (2 MB) ...", flush=True)
    tmp = VAD_PATH.with_suffix(".part")
    urllib.request.urlretrieve(VAD_URL, tmp)
    tmp.replace(VAD_PATH)
    return VAD_PATH


def build_vad(cfg):
    want = (cfg.backend or "silero").lower()
    if want == "silero":
        try:
            return SileroVAD(float(cfg.threshold))
        except Exception as exc:
            print("[vad] silero indisponivel (%s); usando medidor de volume" % exc,
                  flush=True)
    return EnergyVAD(float(cfg.energy_threshold))


def bip_pronto(volume: float = 0.13) -> None:
    """Toque curto de 'terminei de falar, pode falar'.

    Dois tons proximos e baixos, diferente do bipe de ligar a escuta: serve
    para voce saber quando ela REALMENTE parou e esta ouvindo, em vez de
    adivinhar pelo silencio e acabar falando por cima do fim da frase.
    """
    import sounddevice as sd

    taxa = 22050
    partes = []
    for f, dur in ((880, 0.05), (1175, 0.06)):
        n = int(taxa * dur)
        t = np.arange(n) / taxa
        env = np.minimum(1.0, np.minimum(np.arange(n), n - np.arange(n)) / (n * 0.35))
        partes.append((np.sin(2 * np.pi * f * t) * env * volume).astype(np.float32))
    try:
        sd.play(np.concatenate(partes), taxa, blocking=False)
    except Exception:
        pass


def bip(subindo: bool = True, volume: float = 0.18) -> None:
    """Dois tons curtos: subindo = escuta ligada, descendo = desligada.

    Sem isso voce tem que olhar o terminal para saber se ele esta ouvindo, e
    acaba falando com o microfone fechado. E mais rapido que falar "ligado",
    e nao atrapalha quem esta no meio de uma frase.
    """
    import sounddevice as sd

    taxa = 22050
    notas = (660, 990) if subindo else (880, 520)
    partes = []
    for f in notas:
        n = int(taxa * 0.07)
        t = np.arange(n) / taxa
        env = np.minimum(1.0, np.minimum(np.arange(n), n - np.arange(n)) / (n * 0.3))
        partes.append((np.sin(2 * np.pi * f * t) * env * volume).astype(np.float32))
    onda = np.concatenate(partes)
    try:
        sd.play(onda, taxa, blocking=False)
    except Exception:
        pass


class Utterance:
    __slots__ = ("audio", "t_inicio", "t_fim", "t_fechou")

    def __init__(self, audio, t_inicio, t_fim, t_fechou):
        self.audio = audio
        self.t_inicio = t_inicio    # perf_counter em que a fala comecou
        self.t_fim = t_fim          # perf_counter da ultima amostra com voz
        self.t_fechou = t_fechou    # perf_counter em que o VAD declarou o fim

    @property
    def duracao(self) -> float:
        return len(self.audio) / SR


class Listener:
    """Microfone aberto -> maquina de estados do VAD -> falas completas.

    Chama on_utterance(Utterance) numa thread propria. Se mute() estiver
    ligado, o audio e descartado (e o que evita ele se ouvir falando).
    """

    def __init__(self, cfg_audio, cfg_vad, on_utterance) -> None:
        self.cfg_audio = cfg_audio
        self.cfg = cfg_vad
        self.on_utterance = on_utterance
        self.vad = build_vad(cfg_vad)

        self.start_frames = max(1, int(cfg_vad.start_ms / 1000 * SR / WINDOW))
        self.silence_frames = max(1, int(cfg_vad.silence_ms / 1000 * SR / WINDOW))
        self.max_samples = int(cfg_vad.max_utterance_s * SR)
        self.min_samples = int(cfg_vad.min_utterance_ms / 1000 * SR)
        pre = max(0, int(cfg_vad.pre_roll_ms / 1000 * SR / WINDOW))
        self._preroll = deque(maxlen=pre)

        # Ganho aplicado na entrada. Serve de rede para microfone com nivel
        # baixo; o certo e subir o ganho no proprio aparelho, porque aqui o
        # ruido de fundo sobe junto.
        self.gain = float(cfg_audio.get("gain", 1.0))
        self._pico_visto = 0.0
        self._avisou_nivel = False

        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._muted = threading.Event()
        self._alive = False
        self._stream = None
        self._thread = None

        # Estado da segmentacao. Fica no objeto porque a thread que converte a
        # taxa e a que decide inicio e fim de fala sao a mesma, mas em metodos
        # diferentes.
        self._falando = False
        self._voz_seguida = 0
        self._silencio_seguido = 0
        self._buf: list = []
        self._t_inicio = 0.0
        self._t_ultima_voz = 0.0
        self._pendente = np.zeros(0, dtype=np.float32)
        self._resample = Resampler(SR, SR)
        self.taxa_captura = SR
        self.nivel = 0.0          # ultimo RMS, so para a barrinha na tela
        self.prob_fala = 0.0      # ultima probabilidade do VAD

    # ---------------- ciclo de vida ----------------

    def start(self) -> None:
        import sounddevice as sd

        device, taxa, nome, api = escolher_entrada(self.cfg_audio.input_device)
        self.taxa_captura = taxa
        self._resample = Resampler(taxa, SR)
        self._pendente = np.zeros(0, dtype=np.float32)

        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Bloco de captura de ~32 ms na taxa do dispositivo; o VAD recebe
        # janelas de 512 amostras a 16 kHz, montadas na thread de baixo.
        bloco = max(128, int(taxa * 0.032))
        self._stream = sd.InputStream(
            samplerate=taxa, blocksize=bloco, device=device, channels=1,
            dtype="float32", callback=self._on_audio)
        self._stream.start()
        extra = "" if taxa == SR else " -> %d Hz" % SR
        print("[audio] ouvindo: %s | %s @ %d Hz%s | vad=%s"
              % (nome, api, taxa, extra, self.vad.name), flush=True)

    def close(self) -> None:
        self._alive = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def mute(self) -> None:
        self._muted.set()

    def unmute(self) -> None:
        # Limpa o que entrou enquanto estava mudo, senao o eco entra na proxima fala.
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._preroll.clear()
        self._pendente = np.zeros(0, dtype=np.float32)
        self._falando = False
        self._voz_seguida = 0
        self._silencio_seguido = 0
        self._buf = []
        self.vad.reset()
        self._muted.clear()

    @property
    def muted(self) -> bool:
        return self._muted.is_set()

    @property
    def esta_falando(self) -> bool:
        """Voce esta no meio de uma frase AGORA.

        O VAD so entrega a fala depois do silencio, entao quem espera uma
        resposta precisa disto para nao desistir no meio da sua frase.
        """
        return self._falando

    # ---------------- captura ----------------

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if self._muted.is_set():
            return
        try:
            self._q.put_nowait((indata[:, 0].copy(), time.perf_counter()))
        except queue.Full:
            pass

    def _checar_nivel(self, frame) -> None:
        """Avisa uma vez se o microfone esta baixo demais para o VAD funcionar.

        Sem isso o sintoma e mudo: voce fala, nada acontece, nada e impresso,
        e nao da para saber se travou ou se nao te ouviu.
        """
        if self._avisou_nivel:
            return
        pico = float(np.abs(frame).max())
        self._pico_visto = max(self._pico_visto, pico)
        if pico < 0.08:
            return
        self._avisou_nivel = True      # pico saudavel: nao precisa avisar

    def aviso_de_nivel(self) -> str:
        """Alerta de nivel baixo, redigido para NAO acusar sem saber.

        Silencio de sala e microfone baixo produzem o mesmo pico minusculo, e
        daqui nao da para saber se voce falou. Entao o texto diz as duas
        possibilidades em vez de afirmar a errada.
        """
        if self._avisou_nivel or self._pico_visto == 0.0:
            return ""
        if self._pico_visto >= 0.05:
            return ""
        sugerido = min(20.0, round(0.3 / max(self._pico_visto, 1e-4), 1))
        return ("[audio] ainda nao ouvi nada alto (maior pico %.3f; fala "
                "normal passa de 0.1). Se voce NAO falou, ignore. Se falou, "
                "suba o ganho no proprio microfone, ou ponha gain = %.1f "
                "na secao [audio] do config.toml."
                % (self._pico_visto, sugerido))

    # ---------------- segmentacao ----------------

    def _run(self) -> None:
        while self._alive:
            try:
                bruto, t = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            # Taxa do dispositivo -> 16 kHz, e depois fatia em janelas de 512,
            # que e o unico tamanho que o Silero v5 aceita.
            convertido = self._resample(bruto)
            if len(convertido) == 0:
                continue
            self._pendente = np.concatenate([self._pendente, convertido])
            atraso = len(self._pendente) / SR

            while len(self._pendente) >= WINDOW:
                frame = self._pendente[:WINDOW]
                self._pendente = self._pendente[WINDOW:]
                self._passo(frame, t - atraso)
                atraso -= WINDOW / SR

    def _passo(self, frame, t) -> None:
        """Uma janela de 512 amostras a 16 kHz atravessa a maquina de estados."""
        if self.gain != 1.0:
            frame = np.clip(frame * self.gain, -1.0, 1.0)

        self.nivel = float(np.sqrt(np.mean(frame ** 2)))
        self._checar_nivel(frame)
        try:
            p = self.vad.prob(frame)
        except Exception:
            p = 0.0
        self.prob_fala = p
        limiar = getattr(self.vad, "threshold", 0.5)
        tem_voz = p >= limiar

        if not self._falando:
            self._preroll.append(frame)
            if tem_voz:
                self._voz_seguida += 1
                if self._voz_seguida >= self.start_frames:
                    self._falando = True
                    self._silencio_seguido = 0
                    self._buf = list(self._preroll)
                    self._preroll.clear()
                    self._t_inicio = t - len(self._buf) * WINDOW / SR
                    self._t_ultima_voz = t
            else:
                self._voz_seguida = 0
            return

        self._buf.append(frame)
        if tem_voz:
            self._silencio_seguido = 0
            self._t_ultima_voz = t
        else:
            self._silencio_seguido += 1

        estourou = len(self._buf) * WINDOW >= self.max_samples
        if self._silencio_seguido >= self.silence_frames or estourou:
            audio = (np.concatenate(self._buf) if self._buf
                     else np.zeros(0, dtype=np.float32))
            self._falando = False
            self._voz_seguida = 0
            self._buf = []
            self.vad.reset()
            if len(audio) >= self.min_samples and not self._muted.is_set():
                self.on_utterance(Utterance(audio, self._t_inicio,
                                            self._t_ultima_voz,
                                            time.perf_counter()))


if __name__ == "__main__":
    import sys
    if "--list" in sys.argv:
        print(list_devices())
        raise SystemExit(0)

    # Monitor ao vivo: mostra o VAD reagindo a sua voz, sem acionar nada.
    sys.path.insert(0, str(ROOT))
    from bridge.config import Config

    cfg = Config.load()
    vistos = []
    lis = Listener(cfg["audio"], cfg["vad"],
                   lambda u: vistos.append(u) or
                   print("\n  -> fala de %.2fs capturada" % u.duracao, flush=True))
    lis.start()
    print("Fale alguma coisa. Ctrl+C para sair.\n")
    try:
        while True:
            barras = int(min(1.0, lis.nivel * 25) * 30)
            print("\r  nivel [%-30s] p(fala)=%.2f  falas=%d"
                  % ("#" * barras, lis.prob_fala, len(vistos)), end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        lis.close()
        print("\nfim")
