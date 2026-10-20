"""Cronometragem por turno. Sem isso nao da pra dizer onde o tempo foi."""
from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path

STAGES = ["fala_s", "vad_ms", "stt_ms", "claude_1a_frase_ms", "claude_total_ms",
          "tts_1a_frase_ms", "tts_total_ms", "boca_fechada_ate_som_ms", "total_ms"]


class Turn:
    """Marca instantes de um turno e devolve as duracoes entre eles."""

    def __init__(self) -> None:
        self.t: dict[str, float] = {}
        self.text_in = ""
        self.text_out = ""

    def mark(self, name: str) -> float:
        now = time.perf_counter()
        self.t.setdefault(name, now)
        return now

    def span(self, a: str, b: str) -> float | str:
        if a in self.t and b in self.t:
            return round((self.t[b] - self.t[a]) * 1000, 1)
        return ""

    def row(self) -> dict:
        fala = ""
        if "fala_inicio" in self.t and "fala_fim" in self.t:
            fala = round(self.t["fala_fim"] - self.t["fala_inicio"], 2)
        return {
            "quando": datetime.now().isoformat(timespec="seconds"),
            "fala_s": fala,
            # do fim do audio ate o VAD confirmar silencio (= silence_ms, basicamente)
            "vad_ms": self.span("fala_fim", "vad_fechou"),
            "stt_ms": self.span("stt_inicio", "stt_fim"),
            "claude_1a_frase_ms": self.span("claude_inicio", "claude_1o_texto"),
            "claude_total_ms": self.span("claude_inicio", "claude_fim"),
            "tts_1a_frase_ms": self.span("tts_1a_pedida", "tts_1o_som"),
            "tts_total_ms": self.span("tts_1a_pedida", "tts_fim"),
            # o numero que importa: voce calou a boca, quanto tempo ate ouvir algo
            "boca_fechada_ate_som_ms": self.span("fala_fim", "tts_1o_som"),
            "total_ms": self.span("fala_fim", "tts_fim"),
            "entrada": self.text_in,
            "saida": self.text_out[:300],
        }

    def resumo(self) -> str:
        r = self.row()
        return (f"stt {r['stt_ms']}ms | claude 1a frase {r['claude_1a_frase_ms']}ms "
                f"total {r['claude_total_ms']}ms | tts {r['tts_1a_frase_ms']}ms | "
                f"BOCA FECHADA -> SOM: {r['boca_fechada_ate_som_ms']}ms")


class MetricsLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.fields = ["quando", *STAGES, "entrada", "saida"]
        if not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=self.fields).writeheader()

    def write(self, turn: Turn) -> None:
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=self.fields).writerow(turn.row())
