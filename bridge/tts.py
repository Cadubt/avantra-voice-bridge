"""Saida falada, com sintese e reproducao em paralelo.

A resposta e quebrada em frases. Enquanto a frase 1 toca, a 2 ja esta sendo
sintetizada. So a primeira frase paga o tempo de sintese; o resto fica
escondido atras do audio que ja esta tocando.

Backends, do melhor ao mais tosco:
  edge  - vozes neurais da Microsoft. Otimo sotaque, mas ONLINE (~1,4 s na
          primeira frase) e o texto sai da maquina.
  piper - neural offline, ~150 ms, roda na CPU.
  sapi  - Microsoft Maria Desktop. Robotica, mas sempre existe no Windows.
  none  - nao fala, so imprime.
"""
from __future__ import annotations

import io
import queue
import re
import time
from collections import deque
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICES_DIR = ROOT / "models" / "piper"
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

VOICE_PATHS = {
    "pt_BR-faber-medium": "pt/pt_BR/faber/medium",
    "pt_BR-cadu-medium": "pt/pt_BR/cadu/medium",
    "pt_BR-jeff-medium": "pt/pt_BR/jeff/medium",
    "pt_BR-edresson-low": "pt/pt_BR/edresson/low",
}

# Corta em fim de frase, nao em ":" ou ";" (partia citacoes no meio).
_SENT = re.compile(r"(?<=[.!?…])\s+|\n{2,}")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if s.strip()]


def clean_for_speech(text: str) -> str:
    """Tira o que nao soa bem falado. O prompt ja pede, mas nem sempre obedece."""
    t = text
    t = re.sub(r"```.*?```", " (bloco de codigo omitido) ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)           # links e imagens
    t = re.sub(r"https?://\S+", " (link) ", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)        # titulos
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)             # bullets
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"(?<!\w)[*_]([^*_\n]+)[*_](?!\w)", r"\1", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


# --------------------------------------------------------------------------
# Backends. Cada um sabe sintetizar (devolve algo opaco) e tocar esse algo.
# A separacao e o que permite sintetizar a proxima frase enquanto esta toca.
# --------------------------------------------------------------------------

class NullSpeaker:
    name = "none"
    pode_pipelinar = True

    def synthesize(self, text: str):
        return text

    def play(self, payload, on_first_sound=None) -> None:
        if on_first_sound:
            on_first_sound()

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class _PCMPlayer:
    """Toca int16 mono por sounddevice, em pedacos, para poder ser cortado."""

    def __init__(self, sd, sample_rate: int, stop_event: threading.Event) -> None:
        self._sd = sd
        self.sample_rate = sample_rate
        self._stop = stop_event

    def play_bytes(self, pedacos, on_first_sound=None) -> None:
        stream = self._sd.RawOutputStream(
            samplerate=self.sample_rate, channels=1, dtype="int16", blocksize=0)
        stream.start()
        primeiro = True
        try:
            for p in pedacos:
                if self._stop.is_set():
                    break
                if primeiro:
                    primeiro = False
                    if on_first_sound:
                        on_first_sound()
                stream.write(p)
        finally:
            try:
                if self._stop.is_set():
                    stream.abort()      # corta na hora
                else:
                    stream.stop()       # deixa drenar o buffer
            finally:
                stream.close()


class PiperSpeaker:
    """Piper local, com controle de tom e velocidade.

    Para abaixar o tom sem deixar a fala arrastada: sintetiza mais rapido
    (length_scale menor) e toca num sample rate menor. O retardo de um cancela
    a pressa do outro, e sobra so o tom mais grave.
    """

    name = "piper"
    pode_pipelinar = True

    def __init__(self, voice: str = "pt_BR-cadu-medium", pitch: float = 0.0,
                 speed: float = 1.0, volume: float = 1.0) -> None:
        import sounddevice as sd
        from piper import PiperVoice

        self.voice_name = voice
        self.model_path = ensure_voice(voice)
        self.voice = PiperVoice.load(self.model_path)
        self.pitch = float(pitch)
        self.speed = max(0.3, float(speed))
        self.volume = float(volume)
        self._stop = threading.Event()
        self._player = _PCMPlayer(sd, self.sample_rate, self._stop)

    @property
    def _k(self) -> float:
        return 2.0 ** (self.pitch / 12.0)

    @property
    def sample_rate(self) -> int:
        """Taxa em que o audio e TOCADO (menor que a nativa = mais grave)."""
        return max(4000, int(round(self.voice.config.sample_rate * self._k)))

    def synthesize(self, text: str):
        from piper.config import SynthesisConfig

        if not text.strip():
            return []
        cfg = SynthesisConfig(length_scale=self._k / self.speed, volume=self.volume)
        return [c.audio_int16_bytes for c in self.voice.synthesize(text, syn_config=cfg)]

    def play(self, payload, on_first_sound=None) -> None:
        if not payload:
            return
        self._stop.clear()
        self._player.sample_rate = self.sample_rate
        self._player.play_bytes(payload, on_first_sound)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.stop()


class EdgeSpeaker:
    """Vozes neurais da Microsoft via Edge TTS.

    Qualidade e sotaque bem melhores que o Piper, mas e um servico ONLINE:
    o texto da resposta sai da sua maquina e vai para a Microsoft, e sem
    internet nao funciona.
    """

    name = "edge"
    pode_pipelinar = True
    SR = 24000

    def __init__(self, voice: str = "pt-BR-AntonioNeural", rate: str = "+0%",
                 pitch: str = "+0Hz", volume: str = "+0%") -> None:
        import av            # noqa: F401  falha cedo se faltar
        import edge_tts      # noqa: F401
        import sounddevice as sd

        self.voice = voice
        self.rate = rate
        self.pitch = pitch
        self.volume = volume
        self.sample_rate = self.SR
        self._stop = threading.Event()
        self._player = _PCMPlayer(sd, self.SR, self._stop)

    def synthesize(self, text: str):
        import asyncio

        import edge_tts

        if not text.strip():
            return b""

        async def run() -> bytes:
            com = edge_tts.Communicate(text, self.voice, rate=self.rate,
                                       pitch=self.pitch, volume=self.volume)
            buf = bytearray()
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        return self._decodificar(asyncio.run(run()))

    def _decodificar(self, mp3: bytes) -> bytes:
        import av
        import numpy as np

        if not mp3:
            return b""
        partes = []
        with av.open(io.BytesIO(mp3)) as cont:
            res = av.AudioResampler(format="s16", layout="mono", rate=self.SR)
            for frame in cont.decode(audio=0):
                for f in res.resample(frame):
                    partes.append(f.to_ndarray().reshape(-1))
            for f in res.resample(None):
                partes.append(f.to_ndarray().reshape(-1))
        if not partes:
            return b""
        return np.concatenate(partes).astype("int16").tobytes()

    def play(self, payload, on_first_sound=None) -> None:
        if not payload:
            return
        self._stop.clear()
        passo = self.SR // 5 * 2     # 200 ms de int16
        pedacos = (payload[i:i + passo] for i in range(0, len(payload), passo))
        self._player.play_bytes(pedacos, on_first_sound)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.stop()


class SapiSpeaker:
    """Ultimo recurso. Nesta maquina so a 'Microsoft Maria Desktop' realmente
    sintetiza; as vozes 'Online' retornam sem tocar nada."""

    name = "sapi"
    pode_pipelinar = False        # o PowerShell sintetiza e toca de uma vez so

    def __init__(self, voice: str = "Microsoft Maria Desktop", rate: int = 1) -> None:
        self.voice = voice
        self.rate = rate
        self._proc = None

    def synthesize(self, text: str):
        return text

    def play(self, payload, on_first_sound=None) -> None:
        text = payload
        if not text or not text.strip():
            return
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(text)
            txt = fh.name
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "try { $s.SelectVoice('" + self.voice + "') } catch { }; "
            "$s.Rate = " + str(self.rate) + "; "
            "$t = Get-Content -Raw -Encoding UTF8 '" + txt + "'; "
            "$s.Speak($t); $s.Dispose()"
        )
        try:
            self._proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if on_first_sound:
                on_first_sound()   # o SAPI nao avisa quando o som sai de fato
            self._proc.wait()
        finally:
            self._proc = None
            Path(txt).unlink(missing_ok=True)

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

    def close(self) -> None:
        self.stop()


def ensure_voice(voice: str) -> Path:
    """Baixa o modelo do Piper na primeira vez."""
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    onnx = VOICES_DIR / (voice + ".onnx")
    cfg = VOICES_DIR / (voice + ".onnx.json")
    if onnx.exists() and cfg.exists():
        return onnx
    rel = VOICE_PATHS.get(voice)
    if not rel:
        raise FileNotFoundError(
            "Voz '" + voice + "' nao esta mapeada e nao esta em " + str(VOICES_DIR)
            + ". Baixe o .onnx e o .onnx.json e coloque la.")
    for dest, url in ((onnx, HF + "/" + rel + "/" + voice + ".onnx"),
                      (cfg, HF + "/" + rel + "/" + voice + ".onnx.json")):
        if dest.exists():
            continue
        print("[tts] baixando " + dest.name + " ...", flush=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    return onnx


class FalanteComReserva:
    """Um falante online com um offline embaixo, por frase.

    O Edge e um servico: sem internet ele falha na hora de sintetizar, nao na
    hora de subir. Sem isto a ponte simplesmente emudeceria no meio de uma
    conversa, sem dizer por que. Aqui cada frase que falhar cai para a reserva
    local e o uso continua, so com a voz pior.
    """

    pode_pipelinar = True

    def __init__(self, principal, reserva, log=print) -> None:
        self.principal = principal
        self.reserva = reserva
        self.log = log
        self.name = "%s+%s" % (principal.name, reserva.name)
        self._avisou = False

    def synthesize(self, text: str):
        try:
            return (0, self.principal.synthesize(text))
        except Exception as exc:
            if not self._avisou:
                self._avisou = True
                self.log("[tts] %s falhou (%s). Usando %s enquanto isso."
                         % (self.principal.name, str(exc)[:70], self.reserva.name))
            try:
                return (1, self.reserva.synthesize(text))
            except Exception:
                return (1, None)

    def play(self, payload, on_first_sound=None) -> None:
        if payload is None:
            return
        qual, dados = payload
        if dados is None:
            return
        (self.principal if qual == 0 else self.reserva).play(dados, on_first_sound)

    def stop(self) -> None:
        self.principal.stop()
        self.reserva.stop()

    def close(self) -> None:
        self.principal.close()
        self.reserva.close()


def build_speaker(cfg):
    """Monta o backend pedido, caindo para o proximo se ele nao subir.

    Ordem de queda: edge -> piper -> sapi -> mudo.
    """
    want = (cfg.get("backend") or "edge").lower()
    if want == "none":
        return NullSpeaker()
    if want == "edge":
        try:
            principal = EdgeSpeaker(cfg.get("edge_voice", "pt-BR-AntonioNeural"),
                                    rate=cfg.get("edge_rate", "+0%"),
                                    pitch=cfg.get("edge_pitch", "+0Hz"),
                                    volume=cfg.get("edge_volume", "+0%"))
        except Exception as exc:
            print("[tts] edge indisponivel (" + str(exc) + "); caindo para Piper",
                  flush=True)
            want = "piper"
        else:
            # O Edge so falha quando vai sintetizar, nao ao subir. A reserva
            # cobre queda de internet sem emudecer a ponte.
            nome_reserva = (cfg.get("edge_reserva") or "piper").lower()
            try:
                if nome_reserva == "sapi":
                    reserva = SapiSpeaker(
                        cfg.get("sapi_voice", "Microsoft Maria Desktop"),
                        int(cfg.get("sapi_rate", 1)))
                elif nome_reserva == "none":
                    return principal
                else:
                    reserva = PiperSpeaker(
                        cfg.get("piper_voice", "pt_BR-cadu-medium"),
                        pitch=cfg.get("piper_pitch", 0.0),
                        speed=cfg.get("piper_speed", 1.0))
            except Exception as exc:
                print("[tts] sem reserva local (" + str(exc) + ")", flush=True)
                return principal
            return FalanteComReserva(principal, reserva)
    if want == "piper":
        try:
            return PiperSpeaker(cfg.get("piper_voice", "pt_BR-cadu-medium"),
                                pitch=cfg.get("piper_pitch", 0.0),
                                speed=cfg.get("piper_speed", 1.0),
                                volume=cfg.get("piper_volume", 1.0))
        except Exception as exc:
            print("[tts] piper indisponivel (" + str(exc) + "); caindo para SAPI",
                  flush=True)
            want = "sapi"
    if want == "sapi":
        try:
            return SapiSpeaker(cfg.get("sapi_voice", "Microsoft Maria Desktop"),
                               int(cfg.get("sapi_rate", 1)))
        except Exception as exc:
            print("[tts] SAPI indisponivel (" + str(exc) + "); seguindo mudo",
                  flush=True)
    return NullSpeaker()


class SpeechQueue:
    """Duas threads: uma sintetiza, a outra toca.

    say() nao bloqueia. A frase N+1 vai sendo sintetizada enquanto a N toca,
    entao so a primeira frase de cada resposta paga o tempo de sintese.
    """

    def __init__(self, speaker, on_first_sound=None, buffer_frases: int = 2) -> None:
        self.speaker = speaker
        self.on_first_sound = on_first_sound
        self._texto: queue.Queue = queue.Queue()
        self._audio: queue.Queue = queue.Queue(maxsize=max(1, buffer_frases))
        self._pendentes = 0
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._spoke_once = False
        self._alive = True
        self._geracao = 0     # invalida o que foi enfileirado antes de um stop
        self._em_curso = ""   # texto tocando agora, para reconhecer o eco
        self._falado = deque()          # (quando, texto) das ultimas falas
        self._lock_falado = threading.Lock()

        self._t_sint = threading.Thread(target=self._loop_sintese, daemon=True)
        self._t_play = threading.Thread(target=self._loop_play, daemon=True)
        self._t_sint.start()
        self._t_play.start()

    # ---------------- threads ----------------

    def _loop_sintese(self) -> None:
        while self._alive:
            try:
                ger, texto = self._texto.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if ger != self._geracao:
                    self._fim_de_item()
                    continue
                payload = self.speaker.synthesize(texto)
                if ger != self._geracao:
                    self._fim_de_item()
                    continue
                self._audio.put((ger, payload, texto))
            except Exception as exc:
                print("[tts] erro ao sintetizar: " + str(exc), flush=True)
                self._fim_de_item()
            finally:
                self._texto.task_done()

    def texto_em_curso(self) -> str:
        """O que esta sendo falado agora, para reconhecer o proprio eco."""
        return self._em_curso

    def texto_recente(self, segundos: float = 12.0) -> str:
        """Tudo que ele falou nos ultimos N segundos, mais o que toca agora.

        Com caixa de som o microfone capta a voz dele, e o VAD so fecha a fala
        DEPOIS do silencio — ou seja, a transcricao do eco chega quando aquela
        frase ja terminou de tocar. Comparar so com o que esta tocando no
        instante exato deixaria passar quase todo eco.
        """
        corte = time.time() - segundos
        with self._lock_falado:
            recentes = [t for q, t in self._falado if q >= corte]
        if self._em_curso:
            recentes.append(self._em_curso)
        return " ".join(recentes)

    def _anotar_falado(self, texto: str) -> None:
        agora = time.time()
        with self._lock_falado:
            self._falado.append((agora, texto))
            corte = agora - 60
            while self._falado and self._falado[0][0] < corte:
                self._falado.popleft()

    def _loop_play(self) -> None:
        while self._alive:
            try:
                ger, payload, texto = self._audio.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if ger != self._geracao:
                    continue
                self._em_curso = texto
                self._anotar_falado(texto)
                cb = None
                if not self._spoke_once and self.on_first_sound:
                    cb = self._first
                self.speaker.play(payload, on_first_sound=cb)
            except Exception as exc:
                print("[tts] erro ao tocar: " + str(exc), flush=True)
            finally:
                self._em_curso = ""
                self._audio.task_done()
                self._fim_de_item()

    def _fim_de_item(self) -> None:
        with self._lock:
            self._pendentes = max(0, self._pendentes - 1)
            if self._pendentes == 0:
                self._idle.set()

    def _first(self) -> None:
        self._spoke_once = True
        if self.on_first_sound:
            self.on_first_sound()

    # ---------------- interface ----------------

    def say(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._pendentes += 1
            self._idle.clear()
            ger = self._geracao
        self._texto.put((ger, text))

    def new_turn(self) -> None:
        self._spoke_once = False

    def busy(self) -> bool:
        return not self._idle.is_set()

    def wait_idle(self, timeout=None) -> bool:
        return self._idle.wait(timeout)

    def stop_all(self) -> None:
        """Descarta o que esta na fila e corta o que esta tocando."""
        with self._lock:
            self._geracao += 1
            self._pendentes = 0
        for q in (self._texto, self._audio):
            try:
                while True:
                    q.get_nowait()
                    q.task_done()
            except queue.Empty:
                pass
        self.speaker.stop()
        self._idle.set()

    def close(self) -> None:
        self._alive = False
        self.stop_all()
        self.speaker.close()
        self._t_sint.join(timeout=2)
        self._t_play.join(timeout=2)
