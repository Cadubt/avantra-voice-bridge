"""Atalhos globais no Windows via RegisterHotKey.

Usa a API do Win32 direto por ctypes: funciona com qualquer janela em foco,
nao precisa de administrador e nao instala gancho de teclado (o que faria
antivirus reclamar e poderia ser visto como keylogger).
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN,
}

# Teclas nao alfanumericas que valem a pena suportar por nome.
TECLAS = {
    "space": 0x20, "espaco": 0x20, "enter": 0x0D, "return": 0x0D,
    "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "pause": 0x13, "scrolllock": 0x91,
    "numlock": 0x90, "capslock": 0x14, "printscreen": 0x2C,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _i in range(1, 25):
    TECLAS["f%d" % _i] = 0x6F + _i


def parse(atalho: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modificadores, codigo da tecla)."""
    partes = [p.strip().lower() for p in atalho.replace(" ", "").split("+") if p.strip()]
    if not partes:
        raise ValueError("atalho vazio")
    mods = 0
    tecla = None
    for p in partes:
        if p in MODS:
            mods |= MODS[p]
        else:
            tecla = p
    if tecla is None:
        raise ValueError("atalho '%s' nao tem tecla, so modificadores" % atalho)
    if tecla in TECLAS:
        vk = TECLAS[tecla]
    elif len(tecla) == 1 and (tecla.isalnum()):
        vk = ord(tecla.upper())
    else:
        raise ValueError("tecla '%s' nao reconhecida" % tecla)
    return mods | MOD_NOREPEAT, vk


class HotkeyManager:
    """Registra atalhos e chama os callbacks numa thread propria.

    O RegisterHotKey amarra o atalho a thread que o registrou, entao tudo
    (registro e laco de mensagens) tem que acontecer na mesma thread.
    """

    def __init__(self) -> None:
        self._pedidos: list[tuple[str, object]] = []
        self._callbacks: dict[int, object] = {}
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._pronto = threading.Event()
        self.falhas: list[str] = []

    def register(self, atalho: str, callback) -> None:
        """Agenda um atalho. Chame antes do start()."""
        if atalho and atalho.strip():
            self._pedidos.append((atalho.strip(), callback))

    def start(self, timeout: float = 5.0) -> bool:
        if not self._pedidos:
            return True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._pronto.wait(timeout)

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        for i, (atalho, cb) in enumerate(self._pedidos, start=1):
            try:
                mods, vk = parse(atalho)
            except ValueError as exc:
                self.falhas.append("%s: %s" % (atalho, exc))
                continue
            if not user32.RegisterHotKey(None, i, mods, vk):
                err = ctypes.get_last_error()
                motivo = ("ja esta em uso por outro programa" if err == 1409
                          else "erro %d" % err)
                self.falhas.append("%s: %s" % (atalho, motivo))
                continue
            self._callbacks[i] = cb
        self._pronto.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                cb = self._callbacks.get(msg.wParam)
                if cb:
                    try:
                        cb()
                    except Exception as exc:
                        print("[hotkey] erro no callback: %s" % exc, flush=True)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        for i in list(self._callbacks):
            user32.UnregisterHotKey(None, i)


if __name__ == "__main__":
    import time

    hk = HotkeyManager()
    hk.register("ctrl+alt+space", lambda: print("  toggle!", flush=True))
    hk.register("ctrl+alt+v", lambda: print("  uma fala!", flush=True))
    hk.register("ctrl+alt+x", lambda: print("  cancelar!", flush=True))
    ok = hk.start()
    print("registrado: %s" % ok)
    for f in hk.falhas:
        print("  FALHA %s" % f)
    print("Aperte os atalhos. Ctrl+C aqui para sair.")
    try:
        while True:
            time.sleep(0.3)
    except KeyboardInterrupt:
        hk.stop()
