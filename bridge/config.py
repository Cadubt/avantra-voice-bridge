"""Leitura do config.toml, com defaults embutidos."""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "audio": {"input_device": "", "samplerate": 16000, "block_ms": 32,
              "gain": 1.0},
    "vad": {
        "backend": "silero", "threshold": 0.6, "energy_threshold": 0.015,
        "start_ms": 160, "silence_ms": 750, "max_utterance_s": 45,
        "min_utterance_ms": 450, "pre_roll_ms": 320,
    },
    "stt": {
        "model": "small", "device": "cpu", "compute_type": "int8",
        "language": "pt", "beam_size": 1, "cpu_threads": 6,
        "min_chars": 3, "max_no_speech_prob": 0.6, "min_avg_logprob": -1.1,
        "vocabulario": "",
    },
    "brain": {
        "mode": "persistent", "model": "haiku", "cwd": "", "tools": "full",
        "extra_dirs": [],
        "permission_mode": "acceptEdits", "resume_session_id": "",
        "fork_session": True, "max_reply_chars": 1400, "timeout_s": 300,
        "settings_json": "", "session_name": "Avantra (voz)", "effort": "",
        "mcp_config": "mcp.json",
        "system_prompt": "Responda em portugues do Brasil, curto, sem markdown.",
    },
    "tts": {
        "backend": "piper", "piper_voice": "pt_BR-faber-medium",
        "sapi_voice": "Microsoft Maria Desktop", "sapi_rate": 1,
        "stream_sentences": True, "bip_ao_terminar": True, "interromper_pelo_nome": True,
    },
    "wake": {
        "enabled": True, "nome": "Avantra", "gatilho": "Odin",
        "model": "", "device": "cpu",
        "phrases": ["avantra", "aezir", "esir", "ezir", "asir", "aizir"],
        "sleep_after_s": 180, "ack": "Oi.",
    },
    # Pastas alternaveis por voz. Vazio = so a pasta de [brain].cwd.
    "workspaces": {},
    "modelos": {}, "esforcos": {},
    "apps": {},
    "guarda": {
        "enabled": True, "espera_s": 25, "tentativas": 2,
        "espera_minima_s": 10, "espera_maxima_s": 180,
        "confirmar_tela": False,
        "desligar_plugins": [], "pastas_livres": [],
    },
    "hotkeys": {
        "armed_on_start": True, "toggle": "pause",
        "oneshot": "ctrl+alt+v", "cancel": "ctrl+alt+x",
    },
    "log": {"pasta": "logs", "verbose": True},
}


class Section(dict):
    """dict que tambem responde a acesso por atributo."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class Config(Section):
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path) if path else ROOT / "config.toml"
        if not path.exists() and path.name == "config.toml":
            # Clone recem-feito: o config.toml real fica fora do git (guarda a
            # senha falada e caminhos pessoais). Sobe com o exemplo e avisa.
            exemplo = path.with_name("config.example.toml")
            if exemplo.exists():
                print("[config] sem config.toml; usando config.example.toml. "
                      "Copie para config.toml e ajuste.", flush=True)
                path = exemplo
        raw = {}
        if path.exists():
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
        cfg = cls()
        for section, defaults in DEFAULTS.items():
            merged = dict(defaults)
            merged.update(raw.get(section, {}))
            cfg[section] = Section(merged)
        # Secoes do arquivo que nao tem default (nomes de workspace novos, etc).
        for section, valores in raw.items():
            if section not in cfg and isinstance(valores, dict):
                cfg[section] = Section(valores)
        cfg.path = path
        return cfg
