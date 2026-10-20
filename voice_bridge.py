"""voice-bridge: conversa por voz com o Claude Code.

    .venv\\Scripts\\python.exe voice_bridge.py
    .venv\\Scripts\\python.exe voice_bridge.py --list-devices
    .venv\\Scripts\\python.exe voice_bridge.py --texto     (digita em vez de falar)

Fluxo de um turno:
    microfone -> VAD decide que voce parou -> Whisper transcreve ->
    claude responde em streaming -> cada frase pronta ja vira audio.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bridge import comandos  # noqa: E402
from bridge.audio import Listener, bip, bip_pronto, list_devices  # noqa: E402
from bridge.brain import build_brain  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.guarda import Guarda  # noqa: E402
from bridge.hotkeys import HotkeyManager  # noqa: E402
from bridge.metrics import MetricsLog, Turn  # noqa: E402
from bridge.stt import Transcriber, WakeWord  # noqa: E402
from bridge.tts import (SpeechQueue, build_speaker, clean_for_speech,  # noqa: E402
                        split_sentences)

DORMINDO = "dormindo"
ESCUTANDO = "escutando"
OCUPADO = "ocupado"

# Respostas que contam como sim e como nao. Lista fechada de proposito: se a
# transcricao vier com qualquer outra coisa, a ponte pergunta de novo em vez
# de adivinhar. Numa confirmacao de acao perigosa, adivinhar e o erro caro.
_SIM = {"sim", "claro", "pode", "pode sim", "isso", "confirmo", "confirmado",
        "autorizo", "ok", "okay", "beleza", "manda", "manda ver", "vai",
        "faz", "faca", "faz sim", "positivo", "exato", "certo", "concordo",
        "pode fazer", "sim pode", "sim por favor", "afirmativo", "yes"}
_NAO = {"nao", "nao nao", "negativo", "cancela", "cancelar", "para", "parar",
        "deixa", "deixa pra la", "esquece", "nem pensar", "de jeito nenhum",
        "nao faz", "nao faca", "nao pode", "nao quero", "no", "espera",
        "melhor nao", "nao por favor", "cancela isso"}


class _FalaPronta:
    """Fala que ja veio transcrita (ex.: o resto de 'Avantra, para com isso').

    Entra na mesma fila das falas normais para nao duplicar o laco, e o
    processamento pula a transcricao quando ve este tipo.
    """

    __slots__ = ("texto", "t_inicio", "t_fim", "t_fechou", "audio")

    def __init__(self, texto: str, origem) -> None:
        self.texto = texto
        self.audio = getattr(origem, "audio", None)
        self.t_inicio = getattr(origem, "t_inicio", time.perf_counter())
        self.t_fim = getattr(origem, "t_fim", time.perf_counter())
        self.t_fechou = getattr(origem, "t_fechou", time.perf_counter())


def _sim_ou_nao(texto: str):
    """-> True, False, ou None quando a resposta nao foi clara."""
    from bridge.stt import normalizar

    n = normalizar(texto).strip(" .!?")
    if not n:
        return None
    if n in _SIM:
        return True
    if n in _NAO:
        return False
    palavras = n.split()
    # "nao" em qualquer lugar pesa mais que um "sim" solto: na duvida, recusa.
    if any(p in _NAO for p in palavras):
        return False
    if palavras and palavras[0] in _SIM:
        return True
    return None


class Ponte:
    def __init__(self, cfg: Config, modo_texto: bool = False) -> None:
        self.cfg = cfg
        self.modo_texto = modo_texto
        self.estado = DORMINDO
        self.rodando = True
        # A sessao do aplicativo em que estamos, se entramos em alguma.
        self.sessao_app = None
        self.oneshot = False               # desarma sozinho apos uma fala
        self.ultimo_uso = time.time()
        self.cancelar = threading.Event()
        self.fila: queue.Queue = queue.Queue()
        self.fila_confirmacao: queue.Queue = queue.Queue()
        self.aguardando_confirmacao = threading.Event()
        self._lock_confirmacao = threading.Lock()
        self.guarda = None
        self.ultima_fala = ""   # a sua frase, como foi transcrita

        # Um arquivo por dia, numa pasta so. Facilita achar "o que eu pedi na
        # terca" e evita um transcript.log de megabytes.
        hoje = time.strftime("%Y-%m-%d")
        pasta_logs = ROOT / (cfg["log"].get("pasta") or "logs")
        pasta_logs.mkdir(parents=True, exist_ok=True)
        self.transcript = pasta_logs / ("conversa-%s.log" % hoje)
        self.metrics = MetricsLog(pasta_logs / ("latencia-%s.csv" % hoje))
        self.log_acoes = pasta_logs / ("acoes-%s.log" % hoje)
        self.turno_atual: Turn | None = None
        self._avisou_ferramenta = False

    # ------------------------------------------------------------------
    # montagem
    # ------------------------------------------------------------------

    def montar(self) -> None:
        t0 = time.perf_counter()
        print("=" * 64)
        print(" voice-bridge  —  conversa por voz com o Claude Code")
        print("=" * 64)

        self.speaker = build_speaker(self.cfg["tts"])
        self.voz = SpeechQueue(self.speaker, on_first_sound=self._primeiro_som)
        print("[tts] %s pronto" % self.speaker.name)

        self.stt = Transcriber(self.cfg["stt"])
        self.wake = None
        wcfg = self.cfg["wake"]
        if wcfg.get("enabled", True) and not self.modo_texto:
            self.wake = WakeWord(wcfg)
            tam = (wcfg.get("model") or "").strip()
            if not tam or tam == self.stt.model_size:
                # Sem segundo modelo: o principal ja esta carregado, e dois
                # modelos na mesma GPU estouram a VRAM.
                self.stt_wake = self.stt
            else:
                cfg_wake = dict(self.cfg["stt"])
                cfg_wake["device"] = wcfg.get("device", "cpu")
                cfg_wake["compute_type"] = ("float16"
                                            if cfg_wake["device"] == "cuda"
                                            else "int8")
                from bridge.config import Section
                self.stt_wake = Transcriber(Section(cfg_wake), model_size=tam)
            print("[wake] acorda dizendo \"%s\"" % self.wake.gatilho)

        self._montar_guarda()

        self.brain = build_brain(self.cfg["brain"])
        print("[brain] subindo o claude (isso leva uns segundos) ...", flush=True)
        self.brain.start()

        if not self.modo_texto:
            self.listener = Listener(self.cfg["audio"], self.cfg["vad"],
                                     self._nova_fala)
            self.listener.start()
            self._montar_atalhos()
        else:
            self.listener = None

        if self.cfg["hotkeys"].get("armed_on_start", True) or not self.wake:
            self.estado = ESCUTANDO

        print("-" * 64)
        print(self.ajuda())
        print("-" * 64)
        if self.estado == ESCUTANDO:
            aviso = "ESCUTANDO — pode falar (%s desliga)" % self.cfg["hotkeys"].get(
                "toggle", "?")
        elif self.wake:
            aviso = "dormindo — aperte %s, ou diga \"%s\"" % (
                self.cfg["hotkeys"].get("toggle", "?"), self.wake.gatilho)
        else:
            aviso = "dormindo — aperte %s para falar" % self.cfg["hotkeys"].get(
                "toggle", "?")
        print("pronto em %.1fs | %s\n" % (time.perf_counter() - t0, aviso))

    def _montar_guarda(self) -> None:
        """Sobe o servidor que intercepta cada ferramenta antes de executar."""
        gcfg = self.cfg["guarda"]
        if not gcfg.get("enabled", True):
            print("[guarda] DESLIGADO: nada sera confirmado antes de rodar.",
                  flush=True)
            return
        if self.modo_texto:
            print("[guarda] modo texto: confirmacao por voz indisponivel, "
                  "acao perigosa sera bloqueada direto.", flush=True)

        # Onde escrever sem perguntar. NAO e o extra_dirs: aquele diz onde ele
        # pode chegar, este diz onde pode agir sozinho. Se fossem a mesma
        # lista, liberar o computador inteiro desligaria o guarda.
        livres = [self.cfg["brain"].get("cwd") or str(ROOT)]
        for d in gcfg.get("pastas_livres", []) or []:
            p = Path(os.path.expandvars(str(d))).expanduser()
            if p.is_dir():
                livres.append(str(p))

        def confirmar(descricao, motivo=""):
            self._registrar_acao("PERGUNTOU", descricao, motivo)
            if self.modo_texto or not self.listener:
                return False
            ok = self.confirmar_acao(descricao, motivo)
            self._registrar_acao("AUTORIZOU" if ok else "RECUSOU", descricao, motivo)
            return ok

        self.guarda = Guarda(confirmar, pastas_livres=livres, log=self._log,
                             ativo=True,
                             proteger_ponte=bool(gcfg.get("proteger_a_ponte",
                                                          False)),
                             confirmar_tela=bool(gcfg.get("confirmar_tela",
                                                          False)))
        self.guarda.start()
        self.cfg["brain"]["settings_json"] = self.guarda.settings_json(
            timeout=int(gcfg.get("espera_s", 25)) * 4 + 60,
            desligar_plugins=gcfg.get("desligar_plugins", []))
        print("[guarda] ligado em %s | pastas livres: %d"
              % (self.guarda.url, len(livres)), flush=True)

    def _registrar_acao(self, o_que: str, descricao: str, motivo: str) -> None:
        try:
            with open(self.log_acoes, "a", encoding="utf-8") as fh:
                fh.write("[%s] %-9s %s (%s)\n"
                         % (time.strftime("%H:%M:%S"), o_que, descricao, motivo))
        except OSError:
            pass

    def _montar_atalhos(self) -> None:
        hk = self.cfg["hotkeys"]
        self.hotkeys = HotkeyManager()
        self.hotkeys.register(hk.get("toggle"), self._toggle)
        self.hotkeys.register(hk.get("oneshot"), self._armar_uma_vez)
        self.hotkeys.register(hk.get("cancel"), self._cancelar)
        self.hotkeys.start()
        if self.hotkeys.falhas:
            for f in self.hotkeys.falhas:
                print("[hotkey] NAO registrado -> %s" % f)
            if len(self.hotkeys.falhas) >= 2:
                # Atalho global e exclusivo: quem registra primeiro fica com
                # ele. Varios falharem de uma vez quase sempre quer dizer que
                # outra copia da ponte ficou aberta.
                print("[hotkey] varios de uma vez: outra janela da ponte "
                      "provavelmente esta aberta. Feche-a e reinicie.")
            else:
                print("[hotkey] troque o atalho em config.toml, secao "
                      "[hotkeys].")

    def ajuda(self) -> str:
        hk = self.cfg["hotkeys"]
        tecla = hk.get("toggle", "?")
        linhas = [
            " %-18s liga/desliga a escuta" % tecla,
            " %-18s (falando) para tudo na hora e te escuta" % "",
            " %-18s (confirmando) cala a boca, a pergunta continua" % "",
            " %-18s escuta uma fala so" % hk.get("oneshot", "?"),
            " %-18s cala a boca e descarta o turno" % hk.get("cancel", "?"),
        ]
        if self.wake:
            pelo_nome = self.cfg["tts"].get("interromper_pelo_nome", False)
            mesma = (self.wake.gatilho.lower() == self.wake.nome.lower())
            alvo = "acorda" if mesma else ("acorda o " + self.wake.nome)
            linhas.append(" %-18s %s%s"
                          % ("\"%s\"" % self.wake.gatilho, alvo,
                             ", e interrompe" if pelo_nome else
                             " (so quando ele esta calado)"))
        linhas.append(" por voz:           encerrar conversa | liga as ferramentas | "
                      "modo conversa | trabalhar no <pasta> | status")
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    # atalhos
    # ------------------------------------------------------------------

    def _toggle(self) -> None:
        # Falando ou pensando, o Pause significa "para tudo e me escuta".
        # E a interrupcao confiavel: nao depende de transcricao nenhuma, entao
        # funciona com caixa de som, com barulho, no meio de uma palavra.
        if self.aguardando_confirmacao.is_set():
            # No meio de uma confirmacao: so cala a boca para voce responder
            # ja. Nao cancela a pergunta, que ainda espera seu sim ou nao.
            self.voz.stop_all()
            self._log("· calei, pode responder")
            return

        if self.voz.busy() or self.estado == OCUPADO:
            self.voz.stop_all()
            self.cancelar.set()        # corta tambem o turno do Claude
            self.oneshot = False
            self.estado = ESCUTANDO
            self.ultimo_uso = time.time()
            bip(subindo=True)
            self._log("· parei. pode falar")
            return

        if self.estado == ESCUTANDO:
            self.estado = DORMINDO
            bip(subindo=False)
            self._log("· escuta DESLIGADA (bip descendo)")
        else:
            self.oneshot = False
            self.estado = ESCUTANDO
            self.ultimo_uso = time.time()
            bip(subindo=True)
            self._log("· escutando — pode falar (bip subindo)")

    def _armar_uma_vez(self) -> None:
        self.oneshot = True
        self.estado = ESCUTANDO
        self.ultimo_uso = time.time()
        bip(subindo=True)
        self._log("· escutando uma fala")

    def _cancelar(self) -> None:
        self.cancelar.set()
        self.voz.stop_all()
        self._log("· cancelado")

    # ------------------------------------------------------------------
    # entrada
    # ------------------------------------------------------------------

    def _nova_fala(self, utt) -> None:
        """Chamado pela thread do VAD. So enfileira; o trabalho e no laco."""
        # Esperando um sim ou nao: a fala vai para o guarda, nao para o Claude.
        if self.aguardando_confirmacao.is_set():
            self.fila_confirmacao.put(utt)
            return
        if self.voz.busy():
            # Falando: pode ser voce mandando ele calar a boca.
            self._talvez_interromper(utt)
            if (self.cfg["log"].get("verbose")
                    and not self.cfg["tts"].get("interromper_pelo_nome", False)):
                self._log("  (falei por cima — fala descartada; use o %s para "
                          "me interromper)" % self.cfg["hotkeys"].get("toggle"))
            return
        if self.estado == OCUPADO:
            if self.cfg["log"].get("verbose"):
                self._log("  (ainda pensando — fala descartada; %s cancela)"
                          % self.cfg["hotkeys"].get("toggle"))
            return
        if self.estado == DORMINDO and not self.wake:
            return
        self.fila.put(utt)

    # ------------------------------------------------------------------
    # confirmacao falada de acao perigosa
    # ------------------------------------------------------------------

    def _talvez_interromper(self, utt) -> None:
        """Chamar o nome dele enquanto ele fala faz ele parar e te ouvir.

        Para isso o microfone fica aberto durante a fala, e aí ele se ouve
        pelas caixas. Como a ponte sabe exatamente o texto que está sendo
        falado, o eco é reconhecido comparando a transcrição com esse texto —
        se a maior parte das palavras já está no que ele está dizendo, foi
        ele mesmo. Com fone de ouvido o problema nem existe.
        """
        if not self.cfg["tts"].get("interromper_pelo_nome", True) or not self.wake:
            return

        # Chamar alguem no meio da frase e curto: "Avantra", "Avantra, para".
        # O eco de uma resposta inteira e longo. Cortar pela duracao evita
        # transcrever o alto-falante a cada frase — com caixa de som isso
        # aconteceria o turno todo.
        limite = float(self.cfg["tts"].get("interrupcao_max_s", 4.0))
        if utt.duracao > limite:
            return

        try:
            res = self.stt_wake.transcribe(utt.audio, aplicar_filtros=False)
        except Exception:
            return
        texto = (res.texto or "").strip()
        if not texto:
            return

        if self._parece_eco(texto):
            if self.cfg["log"].get("verbose"):
                print("\r  (eco ignorado: %s)" % texto[:50], flush=True)
            return

        achou, resto = self.wake.detectar(texto)
        if not achou:
            return

        # Se ele acabou de dizer o proprio nome ("eu sou o Avantra"), o que o
        # microfone pegou foi ele, nao voce. Nao se acorda sozinho.
        from bridge.stt import normalizar
        recente = normalizar(self.voz.texto_recente(12))
        if any(" %s " % f in " %s " % recente for f in self.wake.frases):
            if self.cfg["log"].get("verbose"):
                print("\r  (ele disse o proprio nome; ignorando)", flush=True)
            return

        self.voz.stop_all()
        self.cancelar.set()          # corta tambem o turno do Claude em curso
        self._log("· você me chamou, parei de falar")
        bip(subindo=True)
        self.estado = ESCUTANDO
        self.ultimo_uso = time.time()
        if resto.strip():
            # "Avantra, para" ja traz o pedido junto: aproveita.
            self.fila.put(_FalaPronta(resto.strip(), utt))

    def _parece_eco(self, texto: str) -> bool:
        """A transcricao e a propria voz dele voltando pelas caixas de som?

        Compara PALAVRA INTEIRA, nao trecho: com substring, "sim" casa dentro
        de "assim" e a sua autorizacao vira eco descartado.
        """
        from bridge.stt import normalizar

        janela = float(self.cfg["tts"].get("eco_janela_s", 12.0))
        falando = set(normalizar(self.voz.texto_recente(janela)).split())
        if not falando:
            return False
        palavras = [p for p in normalizar(texto).split() if len(p) > 2]
        if not palavras:
            return False
        dentro = sum(1 for p in palavras if p in falando)
        return dentro / len(palavras) >= float(
            self.cfg["tts"].get("eco_limiar", 0.6))

    def confirmar_acao(self, descricao: str, motivo: str = "") -> bool:
        """Fala o que o Claude quer fazer e espera voce autorizar.

        Chamado pela thread do servidor do guarda, no meio de um turno. Repete
        a acao em voz alta de proposito: assim voce ouve o que foi ENTENDIDO,
        nao o que voce acha que pediu.
        """
        with self._lock_confirmacao:
            espera = float(self.cfg["guarda"].get("espera_s", 25))
            tentativas = int(self.cfg["guarda"].get("tentativas", 2))

            self.voz.stop_all()          # cala o que estiver falando
            self.voz.new_turn()

            # Repetir o COMANDO nao ajuda voce a conferir nada. O que importa
            # e ouvir a sua propria frase como ela foi transcrita: e ali que o
            # erro mora. A acao em portugues vem depois, como consequencia.
            ouvi = (self.ultima_fala or "").strip()
            partes = []
            if ouvi:
                partes.append("Você me pediu, e eu ouvi assim: %s." % ouvi)
            partes.append("Para fazer isso eu vou %s." % descricao)
            # A pergunta NAO pode conter as palavras "sim" e "nao": se contiver,
            # a sua resposta curta vira eco da propria pergunta e e descartada.
            partes.append("Posso?")

            print("\n  [guarda] ouvi: %s" % (ouvi or "(nada)"), flush=True)
            print("  [guarda] vou: %s" % descricao, flush=True)

            estava_mudo = bool(self.listener and self.listener.muted)
            if estava_mudo:
                self.listener.unmute()

            try:
                for tentativa in range(tentativas):
                    # Armar ANTES de falar: voce costuma responder antes de a
                    # pergunta terminar, e so assim a resposta e capturada.
                    try:
                        while True:
                            self.fila_confirmacao.get_nowait()
                    except queue.Empty:
                        pass
                    self.aguardando_confirmacao.set()

                    self.voz.new_turn()
                    if tentativa == 0:
                        for parte in partes:
                            self.voz.say(parte)
                    else:
                        self.voz.say("Não entendi. Posso?")
                    self.voz.wait_idle(120)
                    bip(subindo=True)

                    utt = self._esperar_resposta(espera)
                    if utt is None:
                        self._dizer("Não ouvi resposta. Não vou fazer nada.")
                        return False

                    res = self.stt.transcribe(utt.audio, aplicar_filtros=False)
                    texto = (res.texto or "").strip()
                    print("  [guarda] voce respondeu: %s" % texto, flush=True)
                    # Quando voce responde em cima da pergunta, o VAD junta
                    # tudo numa fala so e a transcricao vira "Posso? Não!".
                    # Tira o que foi eco antes de classificar, senao um "sim"
                    # grudado no "Posso?" nao e reconhecido.
                    limpo = self._sem_eco(texto)
                    if limpo != texto:
                        print("  [guarda] sem o eco: %s" % (limpo or "(nada)"),
                              flush=True)
                    decisao = _sim_ou_nao(limpo or texto)
                    if decisao is True:
                        if not self._senha_ok(espera):
                            self._dizer("Senha errada ou não ouvi. Não vou fazer.")
                            return False
                        self._dizer("Ok, fazendo.")
                        return True
                    if decisao is False:
                        self._dizer("Certo, deixa pra lá.")
                        return False
                self._dizer("Não entendi. Na dúvida, não vou fazer nada.")
                return False
            finally:
                self.aguardando_confirmacao.clear()
                # Devolve o microfone ao estado anterior: se o turno estava
                # com ele mudo, o resto da resposta nao pode ficar captando a
                # caixa de som so porque houve uma confirmacao no meio.
                if estava_mudo and self.listener and not self.listener.muted:
                    self.listener.mute()

    def _senha_ok(self, espera: float) -> bool:
        """Pede a senha de confirmacao. Sem senha configurada, passa direto."""
        import unicodedata

        def _norm(s: str) -> str:
            s = unicodedata.normalize("NFKD", s or "")
            return "".join(c for c in s if not unicodedata.combining(c)).lower()

        senha = _norm(self.cfg["guarda"].get("senha_confirmacao", "")).strip()
        if not senha:
            return True
        try:
            while True:
                self.fila_confirmacao.get_nowait()
        except queue.Empty:
            pass
        self.voz.new_turn()
        self.voz.say("Qual é a senha?")
        self.voz.wait_idle(60)
        bip(subindo=True)
        utt = self._esperar_resposta(espera)
        if utt is None:
            return False
        res = self.stt.transcribe(utt.audio, aplicar_filtros=False)
        return senha in _norm(res.texto or "")

    def _vocabulario(self) -> str | None:
        return (self.cfg["stt"].get("vocabulario") or "").strip() or None

    def _dica_do_gatilho(self) -> str:
        """Frases de exemplo que ensinam o modelo a esperar o gatilho.

        O initial_prompt funciona como contexto anterior: o modelo continua
        dele. Por isso vale escrever o gatilho algumas vezes, em frases
        parecidas com as que voce diz de verdade.
        """
        g = self.wake.gatilho
        return ("%s. %s, me escuta. %s, abre a cena. %s, qual o status? %s."
                % (g, g, g, g, g))

    def perguntar_sim_ou_nao(self, pergunta: str, tentativas: int = 2) -> bool:
        """Fala uma pergunta e espera voce responder sim ou nao.

        Mesma maquina que o guarda usa. A pergunta NAO pode conter as palavras
        "sim" e "nao": com caixa de som, responder em cima dela faz o VAD
        juntar o alto-falante com a sua voz, e ai a resposta vira eco.
        """
        with self._lock_confirmacao:
            espera = float(self.cfg["guarda"].get("espera_s", 25))
            self.voz.stop_all()
            self.voz.new_turn()
            estava_mudo = bool(self.listener and self.listener.muted)
            if estava_mudo:
                self.listener.unmute()
            try:
                for tentativa in range(tentativas):
                    try:
                        while True:
                            self.fila_confirmacao.get_nowait()
                    except queue.Empty:
                        pass
                    self.aguardando_confirmacao.set()
                    self.voz.new_turn()
                    self.voz.say(pergunta if tentativa == 0
                                 else "Não entendi. " + pergunta)
                    self.voz.wait_idle(90)
                    bip(subindo=True)

                    utt = self._esperar_resposta(espera)
                    if utt is None:
                        return False
                    res = self.stt.transcribe(utt.audio, aplicar_filtros=False)
                    texto = (res.texto or "").strip()
                    print("  [confirmação] você respondeu: %s" % texto, flush=True)
                    decisao = _sim_ou_nao(self._sem_eco(texto) or texto)
                    if decisao is not None:
                        return decisao
                return False
            finally:
                self.aguardando_confirmacao.clear()
                if estava_mudo and self.listener and not self.listener.muted:
                    self.listener.mute()

    def _sem_eco(self, texto: str) -> str:
        """Tira da transcricao as palavras que ele acabou de falar.

        Com caixa de som, responder em cima da pergunta faz o VAD juntar o
        alto-falante e a sua voz numa fala so. Sobra o que e seu.
        """
        from bridge.stt import normalizar

        janela = float(self.cfg["tts"].get("eco_janela_s", 12.0))
        dele = set(normalizar(self.voz.texto_recente(janela)).split())
        if not dele:
            return texto
        sobrou = [p for p in normalizar(texto).split() if p not in dele]
        return " ".join(sobrou)

    def _esperar_resposta(self, espera: float):
        """Pega a proxima fala que nao seja eco da propria pergunta.

        Nao desiste enquanto voce estiver falando. O VAD so entrega a fala
        DEPOIS de 750 ms de silencio, entao um prazo seco estourava no meio da
        sua frase e ela concluia que nao tinha ouvido nada — falando por cima
        de voce. Aqui o prazo e um minimo, nao um maximo: passado ele, so
        desiste quando houver silencio de verdade.
        """
        minimo = max(float(espera),
                     float(self.cfg["guarda"].get("espera_minima_s", 10)))
        teto = time.time() + float(self.cfg["guarda"].get("espera_maxima_s", 180))
        limite = time.time() + minimo
        while True:
            agora = time.time()
            if agora >= limite:
                # Passou do minimo: so desiste se voce nao estiver falando.
                if agora >= teto:
                    return None
                if not (self.listener and self.listener.esta_falando):
                    return None
            try:
                utt = self.fila_confirmacao.get(timeout=0.25)
            except queue.Empty:
                continue
            if getattr(utt, "audio", None) is None:
                continue
            # Falas capturadas enquanto a pergunta ainda tocava podem ser o
            # proprio alto-falante. "sim" e "nao" nao aparecem na pergunta, de
            # proposito, entao uma resposta de verdade nunca cai aqui.
            try:
                res = self.stt_wake.transcribe(utt.audio, aplicar_filtros=False)
                if self._parece_eco(res.texto or ""):
                    if self.cfg["log"].get("verbose"):
                        self._log("  (eco da pergunta ignorado: %s)"
                                  % (res.texto or "")[:40])
                    # Eco nao gasta o seu tempo de resposta: devolve o minimo.
                    limite = max(limite, time.time() + minimo)
                    continue
            except Exception:
                pass
            return utt

    def _primeiro_som(self) -> None:
        if self.turno_atual:
            self.turno_atual.mark("tts_1o_som")

    # ------------------------------------------------------------------
    # laco principal
    # ------------------------------------------------------------------

    def rodar(self) -> None:
        dorme_apos = float(self.cfg["wake"].get("sleep_after_s", 0) or 0)
        proximo_aviso = time.time() + 25
        while self.rodando:
            try:
                utt = self.fila.get(timeout=0.3)
            except queue.Empty:
                self._linha_de_status()
                if (dorme_apos and self.estado == ESCUTANDO and self.wake
                        and time.time() - self.ultimo_uso > dorme_apos):
                    self.estado = DORMINDO
                    self._log("· dormindo por inatividade")
                # Microfone baixo demais tem sintoma mudo: voce fala e nada
                # acontece. Melhor dizer isso do que deixar parecer travado.
                # So vale a pena avisar com a escuta ligada: parado, silencio
                # e o esperado e o aviso vira falso alarme.
                if (self.listener and proximo_aviso
                        and self.estado == ESCUTANDO
                        and time.time() > proximo_aviso):
                    aviso = self.listener.aviso_de_nivel()
                    if aviso:
                        self._log(aviso)
                        proximo_aviso = time.time() + 60
                    else:
                        proximo_aviso = time.time() + 25
                continue
            try:
                self._processar_fala(utt)
            except MemoryError:
                # Tipicamente VRAM: dois modelos na GPU, ou outra copia da
                # ponte aberta, ou um jogo segurando a memoria.
                self._log("! a GPU ficou sem memoria ao transcrever.")
                self._log("  Feche outra janela da ponte, se houver. Se "
                          "persistir, em config.toml [stt]: model = \"small\","
                          " ou device = \"cpu\".")
                self.estado = ESCUTANDO if not self.oneshot else DORMINDO
            except Exception as exc:
                import traceback
                traceback.print_exc()
                self._log("! erro no turno: %s" % exc)
                self.estado = ESCUTANDO if not self.oneshot else DORMINDO

    def _processar_fala(self, utt) -> None:
        t = Turn()
        t.t["fala_inicio"] = utt.t_inicio
        t.t["fala_fim"] = utt.t_fim
        t.t["vad_fechou"] = utt.t_fechou

        if isinstance(utt, _FalaPronta):
            # Veio de "Avantra, <pedido>" dito enquanto ele falava: ja transcrito.
            texto = utt.texto
            self.estado = ESCUTANDO
            self.ultimo_uso = time.time()
            self.ultima_fala = texto
            print("\n[voce] %s" % texto, flush=True)
            t.text_in = texto
            cmd = comandos.interpretar(texto, self.cfg["workspaces"],
                                       self.cfg["apps"], self.cfg["modelos"],
                                       self.cfg["esforcos"])
            if cmd and self._executar_comando(cmd):
                return
            self._perguntar(texto, t)
            return

        dormindo = self.estado == DORMINDO
        stt = self.stt_wake if (dormindo and self.wake) else self.stt

        t.mark("stt_inicio")
        # Dormindo, os filtros ficam DESLIGADOS: uma palavra solta quase sempre
        # tem confianca baixa, e com eles ligados a ativacao morria antes de
        # ser testada. Lixo nao e problema aqui, porque so o que parecer com o
        # nome acorda; o resto e ignorado de qualquer jeito.
        if dormindo and self.wake:
            # Dormindo so interessa saber se voce chamou. Vale enviesar o
            # modelo para o gatilho e gastar um beam maior: o audio e curto,
            # entao custa pouco, e sem isso a palavra isolada nao e
            # reconhecida (vira "Bom dia", "Eu sei", "O Dinho").
            res = stt.transcribe(utt.audio, aplicar_filtros=False,
                                 initial_prompt=self._dica_do_gatilho(),
                                 beam_size=5)
        else:
            # O vocabulario do projeto entra aqui: sem ele "TavernTale" vira
            # "Taverneiro", porque nome proprio desconhecido perde para a
            # palavra comum mais parecida.
            res = stt.transcribe(utt.audio, aplicar_filtros=True,
                                 initial_prompt=self._vocabulario())
        t.mark("stt_fim")

        if dormindo and self.wake:
            if not res.texto.strip():
                return
        elif not res.ok:
            if self.cfg["log"].get("verbose") and res.texto:
                self._log("  (descartado: %s) %s" % (res.descartado, res.texto[:60]))
            return

        texto = res.texto
        if dormindo:
            # Dormindo o microfone continua aberto, entao a voz dela volta
            # pela caixa de som e e transcrita. Sem este filtro ela acordaria
            # sozinha ao dizer o proprio nome numa frase.
            if self._parece_eco(texto):
                if self.cfg["log"].get("verbose"):
                    self._log("  (eco dela mesma, ignorado: %s)" % texto[:50])
                return
            achou, resto = self.wake.detectar(texto)
            if not achou:
                # Sem isto a falha e muda: voce chama, o VAD ouve, o nome nao
                # casa e nada aparece — impossivel saber se foi o microfone,
                # o VAD ou a grafia. Mostrar o que foi entendido e o que torna
                # a palavra de ativacao ajustavel.
                if self.cfg["log"].get("verbose") and texto:
                    self._log("  (dormindo — ouvi \"%s\", não é \"%s\")"
                              % (texto[:70], self.wake.gatilho))
                return
            self.estado = ESCUTANDO
            self.ultimo_uso = time.time()
            self._log("· acordei")
            if not resto:
                # Bipe, nao fala. Enquanto ela dizia "Oi" o microfone ficava
                # mudo, e a pergunta que voce emendava logo depois era
                # descartada — justo o jeito natural de usar: "Avantra" e ja
                # continuar falando.
                bip(subindo=True)
                ack = self.cfg["wake"].get("ack", "")
                if ack:
                    self.voz.new_turn()
                    self.voz.say(ack)
                return
            texto = resto            # "Avantra, abre a cena" -> "abre a cena"
            # Reaproveita o audio no modelo bom: o tiny erra demais em comando.
            if stt is not self.stt:
                melhor = self.stt.transcribe(utt.audio, aplicar_filtros=True)
                if melhor.ok:
                    achou2, resto2 = self.wake.detectar(melhor.texto)
                    texto = resto2 if (achou2 and resto2) else melhor.texto
                t.mark("stt_fim2")

        self.ultimo_uso = time.time()
        self.ultima_fala = texto     # o guarda repete isto na confirmacao
        print("\n[voce] %s" % texto, flush=True)
        t.text_in = texto

        # "Avantra, dormir" tem que funcionar igual a "dormir". Chamar pelo
        # nome antes do pedido e o jeito natural de falar, e sem tirar o nome
        # da frente nenhum comando local casaria.
        texto_cmd = texto
        if self.wake:
            achou, resto = self.wake.detectar(texto)
            if achou and resto.strip():
                texto_cmd = resto.strip()

        cmd = comandos.interpretar(texto_cmd, self.cfg["workspaces"],
                                   self.cfg["apps"], self.cfg["modelos"],
                                   self.cfg["esforcos"])
        if cmd and self._executar_comando(cmd):
            return

        self._perguntar(texto, t)
        if self.oneshot:
            self.oneshot = False
            self.estado = DORMINDO
            self._log("· voltei a dormir")

    # ------------------------------------------------------------------
    # turno com o Claude
    # ------------------------------------------------------------------

    def _perguntar(self, texto: str, t: Turn) -> None:
        self.estado = OCUPADO
        self.cancelar.clear()
        self.voz.new_turn()
        self._avisou_ferramenta = False
        self.turno_atual = t
        # Com interrupcao pelo nome o microfone FICA ABERTO enquanto ele fala:
        # e a unica forma de voce conseguir cortar no meio. O preco e ele
        # poder captar as proprias caixas de som, tratado em _parece_eco().
        if self.listener and not self.cfg["tts"].get("interromper_pelo_nome", True):
            self.listener.mute()

        primeira = {"marcada": False}

        def falar(frase: str) -> None:
            if not primeira["marcada"]:
                primeira["marcada"] = True
                t.mark("tts_1a_pedida")
            limpa = clean_for_speech(frase)
            if limpa:
                print("[avantra] %s" % limpa, flush=True)
                for parte in split_sentences(limpa) or [limpa]:
                    self.voz.say(parte)

        def usou_ferramenta(nome: str) -> None:
            # Resposta com ferramenta pode levar dezenas de segundos. Avisa uma
            # vez, para o silencio nao parecer travamento.
            if not self._avisou_ferramenta:
                self._avisou_ferramenta = True
                t.mark("tts_1a_pedida")
                self.voz.say("Só um segundo, deixa eu olhar.")
            print("   [%s]" % nome, flush=True)

        t.mark("claude_inicio")
        r = self.brain.ask(texto, on_sentence=falar, cancel=self.cancelar,
                           on_tool=usou_ferramenta)
        t.mark("claude_fim")
        if r.ms_primeiro_texto:
            t.t["claude_1o_texto"] = t.t["claude_inicio"] + r.ms_primeiro_texto / 1000

        if r.erro:
            self._log("! claude: %s" % r.erro)
            self.voz.new_turn()
            self.voz.say("Olha, %s" % r.erro)
        t.text_out = r.texto

        self.voz.wait_idle(600)
        t.mark("tts_fim")
        self.turno_atual = None

        self.metrics.write(t)
        if self.cfg["log"].get("verbose"):
            print("   %s" % t.resumo(), flush=True)
        self._gravar_transcript(texto, r.texto)

        if self.listener and self.listener.muted:
            self.listener.unmute()
        self.estado = ESCUTANDO
        # Avisa que terminou e ja esta ouvindo. Sem isto voce tem que adivinhar
        # pelo silencio quando ela parou, e acaba falando por cima do fim da
        # frase — o que fazia a sua fala ser descartada.
        if self.cfg["tts"].get("bip_ao_terminar", True):
            bip_pronto()

    # ------------------------------------------------------------------
    # comandos locais
    # ------------------------------------------------------------------

    def _executar_comando(self, cmd) -> bool:
        if cmd.acao == "encerrar_conversa":
            # Confirma antes de desligar: uma transcricao errada nao pode
            # deixar voce falando sozinho sem perceber.
            if self.perguntar_sim_ou_nao(
                    "Você quer encerrar a conversa? Eu paro de escutar até "
                    "você me chamar de novo."):
                self.estado = DORMINDO
                self.voz.new_turn()
                self.voz.say("Certo. É só me chamar.")
                self.voz.wait_idle(30)
                self._log("· conversa encerrada — diga \"%s\" para voltar"
                          % self.wake.gatilho if self.wake else "· encerrada")
            else:
                self._dizer("Continuo aqui.")
            return True

        if cmd.acao == "cancelar":
            self._cancelar()
            return True

        if cmd.acao in ("ferramentas_on", "ferramentas_off"):
            novo = "full" if cmd.acao == "ferramentas_on" else "chat"
            if self.cfg["brain"].get("tools") == novo:
                self._dizer("Já estou nesse modo.")
                return True
            self.cfg["brain"]["tools"] = novo
            self._dizer("Mudando o modo. Um instante.")
            self._reiniciar_brain()
            self._dizer("Pronto. Ferramentas %s."
                        % ("ligadas" if novo == "full" else "desligadas"))
            return True

        if cmd.acao == "trocar_modelo":
            novo = self.cfg["modelos"].get(cmd.argumento, "")
            if not novo:
                self._dizer("Não conheço esse modelo.")
                return True
            if self.cfg["brain"].get("model") == novo:
                self._dizer("Já estou no %s." % novo)
                return True
            self.cfg["brain"]["model"] = novo
            self._dizer("Trocando para o %s. Um instante." % novo)
            self._reiniciar_brain()
            self._dizer("Pronto, agora no %s." % novo)
            return True

        if cmd.acao == "trocar_esforco":
            novo = self.cfg["esforcos"].get(cmd.argumento, "")
            if not novo:
                self._dizer("Não conheço esse nível.")
                return True
            if (self.cfg["brain"].get("effort") or "medium") == novo:
                self._dizer("O esforço já está em %s." % cmd.argumento)
                return True
            self.cfg["brain"]["effort"] = novo
            self._dizer("Mudando o esforço para %s. Um instante."
                        % cmd.argumento)
            self._reiniciar_brain()
            # Esforco alto deixa a resposta bem mais lenta: melhor voce saber
            # disso agora do que estranhar o silencio depois.
            extra = (" Vou demorar mais para responder."
                     if novo in ("high", "xhigh", "max") else "")
            self._dizer("Pronto." + extra)
            return True

        if cmd.acao == "qual_modelo":
            esforco = self.cfg["brain"].get("effort") or "normal"
            self._dizer("Estou no %s, com esforço %s."
                        % (self.cfg["brain"].get("model"), esforco))
            return True

        if cmd.acao == "abrir_app":
            caminho = self.cfg["apps"].get(cmd.argumento, "")
            if not caminho:
                self._dizer("Não tenho %s na lista." % cmd.argumento)
                return True
            alvo = Path(os.path.expandvars(caminho)).expanduser()
            if not alvo.exists() and not shutil.which(str(caminho)):
                self._dizer("O caminho de %s não existe mais." % cmd.argumento)
                self._log("! %s -> %s (nao encontrado)" % (cmd.argumento, caminho))
                return True
            try:
                # Sem shell e sem esperar: a ponte nao segura o programa aberto.
                subprocess.Popen([str(alvo) if alvo.exists() else str(caminho)],
                                 cwd=str(alvo.parent) if alvo.exists() else None,
                                 close_fds=True)
            except Exception as exc:
                self._log("! nao consegui abrir %s: %s" % (cmd.argumento, exc))
                self._dizer("Não consegui abrir %s." % cmd.argumento)
                return True
            self._log("· abrindo %s (%s)" % (cmd.argumento, caminho))
            self._dizer("Abrindo %s." % cmd.argumento)
            return True

        if cmd.acao == "listar_sessoes":
            self._listar_sessoes()
            return True

        if cmd.acao == "qual_sessao":
            self._qual_sessao()
            return True

        if cmd.acao == "trocar_sessao":
            self._trocar_sessao(cmd.argumento)
            return True

        if cmd.acao == "retomar_conversa":
            self._retomar_conversa(cmd.argumento)
            return True

        if cmd.acao == "trocar_pasta":
            pasta = self.cfg["workspaces"].get(cmd.argumento, "")
            if not pasta or not Path(pasta).is_dir():
                self._dizer("Não achei essa pasta.")
                return True
            self.cfg["brain"]["cwd"] = pasta
            # Cada pasta tem a sua conversa: nao arrasta o contexto da outra.
            self.cfg["brain"]["resume_session_id"] = ""
            self.sessao_app = None
            self._dizer("Mudando para %s. Um instante." % cmd.argumento)
            self._reiniciar_brain(sessao_por_pasta=cmd.argumento)
            self._dizer("Pronto, estou em %s." % cmd.argumento)
            return True

        if cmd.acao == "status":
            modo = ("com ferramentas" if self.cfg["brain"].get("tools") == "full"
                    else "só conversa")
            self._dizer("Estou em %s, %s."
                        % (Path(self.brain.cwd).name, modo))
            return True

        if cmd.acao == "sair":
            self._dizer("Fechando.")
            self.rodando = False
            return True

        return False

    # ------------------------------------------------------------------
    # Sessoes do aplicativo Claude
    #
    # A barra lateral do aplicativo so lista o que nasceu dentro dele, mas o
    # TEXTO dessas conversas mora no mesmo lugar que o claude de linha de
    # comando usa. Entao a ponte consegue entrar numa delas de verdade — e,
    # sem fork, o que voce falar e o que ela fizer entram no transcrito
    # daquela sessao, e aparecem no chat de texto quando voce a abrir.
    # ------------------------------------------------------------------

    def _listar_sessoes(self, itens=None) -> None:
        from bridge import sessoes

        itens = sessoes.listar() if itens is None else itens
        if not itens:
            self._dizer("Não achei nenhuma sessão do aplicativo.")
            return
        # Dez titulos seguidos cansam de ouvir; os cinco mais recentes bastam.
        nomes = ["%s, de %s" % (i["titulo"], sessoes.quando(i))
                 for i in itens[:5]]
        self._dizer("Você tem %d sessões. As mais recentes: %s."
                    % (len(itens), "; ".join(nomes)))

    def _qual_sessao(self) -> None:
        atual = getattr(self, "sessao_app", None)
        if atual:
            self._dizer("Estamos na sessão %s. O que a gente falar aqui "
                        "aparece no chat de texto dela." % atual["titulo"])
        else:
            self._dizer("Não estou em nenhuma sessão do aplicativo. Esta "
                        "conversa é só da ponte, na pasta %s."
                        % Path(self.brain.cwd).name)

    def _trocar_sessao(self, busca: str) -> None:
        """Entra numa sessao que nasceu no aplicativo Claude.

        Sem fork, de proposito: e isso que faz a conversa de voz e as acoes
        da Avantra aparecerem no chat de texto daquela sessao.
        """
        from bridge import sessoes

        itens = sessoes.listar()
        if not itens:
            self._dizer("Não achei nenhuma sessão do aplicativo.")
            return
        if not busca.strip():
            self._listar_sessoes(itens)
            return

        alvo = sessoes.procurar(busca, itens)
        if not alvo:
            nomes = "; ".join(i["titulo"] for i in itens[:4])
            self._dizer("Não achei sessão com esse nome. As mais recentes "
                        "são: %s." % nomes)
            return

        mb = sessoes.tamanho(alvo) / 1e6
        # Entrar reenvia o historico inteiro uma vez. Numa sessao grande isso
        # demora e custa: melhor voce saber antes de estranhar o silencio.
        if mb >= 8:
            self._dizer("Entrando na sessão %s, de %s. Ela é grande, uns %d "
                        "megabytes de histórico: a primeira resposta vai "
                        "demorar." % (alvo["titulo"], sessoes.quando(alvo),
                                      round(mb)))
        else:
            self._dizer("Entrando na sessão %s, de %s."
                        % (alvo["titulo"], sessoes.quando(alvo)))

        if alvo["cwd"] and Path(alvo["cwd"]).is_dir():
            self.cfg["brain"]["cwd"] = alvo["cwd"]
        self.cfg["brain"]["resume_session_id"] = alvo["cli"]
        self.cfg["brain"]["fork_session"] = False
        try:
            # Sem aquecimento: o handshake da ponte iria parar no chat dela.
            self._reiniciar_brain(warmup=False)
        except Exception as exc:
            self._log("! nao consegui entrar na sessao: %s" % exc)
            self._dizer("Não consegui entrar nessa sessão.")
            return

        self.sessao_app = alvo
        self._log("· entrei na sessao %s (%s)"
                  % (alvo["cli"][:8], alvo["titulo"]))
        # Se ela foi usada hoje, provavelmente esta aberta no aplicativo —
        # e dois processos escrevendo no mesmo transcrito se atrapalham.
        aviso = (" Se ela estiver aberta no aplicativo, feche a aba antes de"
                 " continuarmos." if sessoes.quando(alvo) == "hoje" else "")
        self._dizer("Pronto, estou na %s. O que a gente falar daqui em "
                    "diante entra no chat de texto dela.%s"
                    % (sessoes.rotulo(alvo), aviso))

    def _retomar_conversa(self, busca: str) -> None:
        """Entra numa conversa antiga do Claude Code e continua dela.

        Diferente de so LER o historico: aqui o processo claude e reiniciado
        retomando aquela sessao, entao ele de fato continua de onde parou. Vai
        numa copia (fork), para nao escrever por cima da conversa que voce
        ainda usa no aplicativo.
        """
        sys.path.insert(0, str(ROOT / "tools"))
        from conversas import indexar, pontuar, quando, rotulo  # noqa: E402
        from bridge.stt import normalizar

        if not busca.strip():
            self._dizer("Qual conversa? Me diga sobre o que ela era.")
            return

        self._dizer("Procurando.")
        termos = [t for t in normalizar(busca).split() if len(t) > 2]
        if not termos:
            termos = [normalizar(busca)]
        itens = indexar()
        marcados = [(pontuar(i, termos), i) for i in itens]
        marcados = [(p, i) for p, i in marcados if p > 0]
        if not marcados:
            self._dizer("Não achei nenhuma conversa sobre %s." % busca)
            return
        marcados.sort(key=lambda x: (-x[0], -x[1].get("_mtime", 0)))
        _, alvo = marcados[0]

        nome = rotulo(alvo)
        n = alvo.get("n_usuario", 0)
        # Retomar reenvia o historico inteiro uma vez. Medido numa conversa de
        # 297 mensagens: 29 s e 607 mil tokens de cache, custando dolares. Os
        # turnos seguintes sao baratos, mas o primeiro doi, entao avisa antes.
        if n > 60:
            self._dizer("Achei: %s, de %s, com %d mensagens. É grande: vai "
                        "levar uns trinta segundos e custa caro, porque o "
                        "histórico inteiro é reenviado. Entrando."
                        % (nome, quando(alvo), n))
        else:
            self._dizer("Achei: %s, de %s, com %d mensagens. Entrando nela."
                        % (nome, quando(alvo), n))

        # A sessao so abre na pasta em que foi criada.
        if alvo.get("cwd") and Path(alvo["cwd"]).is_dir():
            self.cfg["brain"]["cwd"] = alvo["cwd"]
        self.cfg["brain"]["resume_session_id"] = alvo["id"]
        self.cfg["brain"]["fork_session"] = True
        self.sessao_app = None
        try:
            self._reiniciar_brain()
        except Exception as exc:
            self._log("! nao consegui retomar: %s" % exc)
            self._dizer("Não consegui abrir essa conversa.")
            return
        self._log("· retomada a conversa %s (%s)" % (alvo["id"][:8], nome))
        self._dizer("Pronto, estou nela. Me diz o que você quer continuar.")

    def _reiniciar_brain(self, sessao_por_pasta: str = "",
                         warmup: bool = True) -> None:
        from bridge import brain as brain_mod

        if sessao_por_pasta:
            # Uma sessao salva por pasta, para voltar de onde parou.
            brain_mod.SESSION_FILE = ROOT / (".session_" + sessao_por_pasta)
        try:
            self.brain.close()
        except Exception:
            pass
        self.brain = build_brain(self.cfg["brain"])
        if getattr(self.brain, "modo", "") == "persistent":
            # Dentro de uma sessao do aplicativo o aquecimento sai caro de
            # outro jeito: a mensagem de handshake apareceria no chat dela.
            self.brain.start(warmup=warmup)
        else:
            self.brain.start()

    def _dizer(self, texto: str, avisar_fim: bool = True) -> None:
        print("[avantra] %s" % texto, flush=True)
        self.voz.new_turn()
        self.voz.say(texto)
        self.voz.wait_idle(60)
        if avisar_fim and self.cfg["tts"].get("bip_ao_terminar", True):
            bip_pronto()

    # ------------------------------------------------------------------

    def _linha_de_status(self) -> None:
        """Medidor ao vivo do que o microfone esta entregando AGORA.

        Sem isso, 'falei e nao aconteceu nada' nao distingue microfone mudo,
        VAD surdo e escuta desligada. Com isso, da para ver na hora.
        """
        if not self.listener or self.estado == OCUPADO:
            return
        agora = time.time()
        if agora - getattr(self, "_t_status", 0) < 0.1:
            return
        self._t_status = agora
        nivel = self.listener.nivel
        p = self.listener.prob_fala
        limiar = getattr(self.listener.vad, "threshold", 0.5)
        barras = int(min(1.0, nivel * 12) * 24)
        marca = "VOZ" if p >= limiar else "   "
        estado = "ESCUTANDO" if self.estado == ESCUTANDO else "dormindo "
        linha = ("  %s [%-24s] p=%.2f/%.2f %s"
                 % (estado, "#" * barras, p, limiar, marca))
        if linha == getattr(self, "_status_anterior", None):
            return
        self._status_anterior = linha
        # \x1b[2K apaga a linha inteira antes de reescrever. So o \r nao basta:
        # no painel de terminal do app ele nao limpa, e as atualizacoes vao se
        # acumulando lado a lado ate virar uma parede de texto.
        print("\x1b[2K\r" + linha, end="", flush=True)

    def _log(self, msg: str) -> None:
        # Apaga o medidor antes de escrever, senao a mensagem cola nele.
        print("\x1b[2K\r" + msg, flush=True)
        self._status_anterior = None    # forca redesenhar o medidor depois

    def _gravar_transcript(self, entrada: str, saida: str) -> None:
        try:
            with open(self.transcript, "a", encoding="utf-8") as fh:
                fh.write("[%s]\nvoce : %s\navantra: %s\n\n"
                         % (time.strftime("%Y-%m-%d %H:%M:%S"), entrada, saida))
        except OSError:
            pass
        self._gravar_resumo_no_projeto(entrada, saida)

    def _gravar_resumo_no_projeto(self, entrada: str, saida: str) -> None:
        """Deixa o que foi falado legivel DENTRO da pasta do projeto.

        Conversa criada pela linha de comando nao aparece na barra lateral do
        aplicativo Claude — ele so lista as que nasceram nele (verificado: 7 de
        ~44). Entao o jeito de voce reencontrar isto quando for continuar por
        texto e um arquivo na propria pasta do projeto, que o CLAUDE.md de la
        aponta.
        """
        destino = (self.cfg["log"].get("resumo_no_projeto") or "").strip()
        if not destino:
            return
        pasta = Path(self.brain.cwd) / destino
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            arq = pasta / ("voz-%s.md" % time.strftime("%Y-%m-%d"))
            novo = not arq.exists()
            with open(arq, "a", encoding="utf-8") as fh:
                if novo:
                    fh.write("# Conversa por voz com a Avantra — %s\n\n"
                             "Gravado pela ponte em %s.\n"
                             "Esta conversa **não** aparece na barra lateral do "
                             "app Claude; este arquivo é como ela chega até lá.\n\n"
                             % (time.strftime("%d/%m/%Y"), ROOT))
                fh.write("### %s\n\n**Cadu:** %s\n\n**Avantra:** %s\n\n"
                         % (time.strftime("%H:%M"), entrada.strip(),
                            (saida or "").strip()[:2000]))
        except OSError as exc:
            self._log("! nao consegui gravar o resumo no projeto: %s" % exc)

    def fechar(self) -> None:
        self.rodando = False
        for alvo in ("listener", "hotkeys", "voz", "brain"):
            obj = getattr(self, alvo, None)
            if obj is None:
                continue
            try:
                (obj.close if hasattr(obj, "close") else obj.stop)()
            except Exception:
                pass


def loop_texto(p: Ponte) -> None:
    """Modo sem microfone: voce digita, ele fala. Bom para testar o resto."""
    print("Modo texto. Digite e de enter. 'sair' encerra.\n")
    while p.rodando:
        try:
            linha = input("voce> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not linha:
            continue
        if linha.lower() in ("sair", "quit", "exit"):
            break
        t = Turn()
        t.t["fala_fim"] = time.perf_counter()
        t.text_in = linha
        cmd = comandos.interpretar(linha, p.cfg["workspaces"], p.cfg["apps"],
                                   p.cfg["modelos"], p.cfg["esforcos"])
        if cmd and p._executar_comando(cmd):
            continue
        p._perguntar(linha, t)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--texto", action="store_true",
                    help="digita em vez de falar (testa tudo menos o microfone)")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    if args.list_devices:
        print(list_devices())
        return

    cfg = Config.load(args.config)
    p = Ponte(cfg, modo_texto=args.texto)
    try:
        p.montar()
        if args.texto:
            loop_texto(p)
        else:
            p.rodar()
    except KeyboardInterrupt:
        print("\nencerrando ...")
    finally:
        p.fechar()


if __name__ == "__main__":
    main()
