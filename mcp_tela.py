"""mcp_tela — o servidor que deixa a Avantra ver e usar a maquina do Cadu.

Por que isto existe
-------------------
O "computer use" do aplicativo Claude Desktop e um servidor interno DELE: a
propria linha de comando do app lista "computer-use" entre os nomes de
servidor reservados, e nao ha executavel publicado para ser chamado de fora.
A ponte de voz roda o `claude` pela linha de comando, em outro processo —
entao ela nao herda aquelas ferramentas. Este arquivo refaz o essencial.

As travas sao as mesmas que o app tem, de proposito:

  - lista de aplicativos liberados por sessao. Nada de clicar ou digitar num
    programa que o Cadu nao liberou. Quem pede a liberacao e o modelo; quem
    autoriza e ele, falando, porque o guarda da ponte intercepta
    `liberar_aplicativo` antes de ela rodar.
  - janela de programa nao liberado sai MASCARADA da captura, respeitando o
    empilhamento: o que esta por cima continua por cima.
  - clique e rolagem olham a janela SOB o ponto; digitacao e tecla olham a
    janela em primeiro plano. Assim nao da para digitar no programa errado.

Fala MCP em JSON-RPC pelo stdin/stdout, uma mensagem por linha. Nao usa o SDK
de proposito: sao tres metodos, e assim o venv da ponte nao ganha pydantic,
starlette e o resto so por causa disto.

Depende de mss (captura) e pillow (redimensionar, mascarar, JPEG).
Registrado em mcp.json; quem sobe e fecha e o proprio claude.
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import sys
import time
import unicodedata
from ctypes import wintypes
from pathlib import Path

import mss
from PIL import Image, ImageDraw

VERSAO = "1.0"

# Lado maior da imagem devolvida. A API ja reduz qualquer coisa acima de 1568;
# 1400 deixa margem e corta um terco dos tokens de uma tela ultrawide.
MAX_LADO = 1400

# Cor com que janela nao liberada e coberta. Cinza escuro, nao preto: na tela
# escura do Cadu, preto se confunde com fundo de aplicativo.
COR_MASCARA = (38, 38, 42)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _dpi_ciente() -> None:
    """Sem isto o Windows mente sobre o tamanho da tela quando ha escala.

    Com 150% de zoom, uma tela de 2560 seria relatada como 1707 e todo clique
    cairia deslocado. Pedimos consciencia por monitor; se a versao do Windows
    nao tiver a V2, a antiga ja resolve o caso de um monitor so.
    """
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def _normal(texto: str) -> str:
    """Minusculas e sem acento, para comparar 'Bloco de Notas' com o que o
    reconhecimento de voz entregou ('bloco de notas')."""
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower().strip()


# --------------------------------------------------------------------------
# Janelas
# --------------------------------------------------------------------------

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAKED = 14
GA_ROOT = 2

# Processos que seguram a area de trabalho e a barra de tarefas. Mascarar
# estes deixaria a captura inteira cinza e o modelo cego para onde clicar.
SHELL = {"explorer.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
         "searchhost.exe", "searchapp.exe", "dwm.exe"}


class Janela:
    __slots__ = ("hwnd", "titulo", "exe", "caixa")

    def __init__(self, hwnd, titulo, exe, caixa):
        self.hwnd = hwnd
        self.titulo = titulo
        self.exe = exe
        self.caixa = caixa          # (esq, topo, dir, baixo) em pixels reais

    @property
    def nome(self) -> str:
        return self.titulo or self.exe or "?"


def _titulo(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _exe_do_hwnd(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(h)
    return ""


def _escondida(hwnd) -> bool:
    """Janela 'cloaked': existe, tem tamanho, mas o Windows nao desenha.

    E o estado normal de aplicativo da loja em segundo plano. Sem este teste
    elas apareceriam como retangulos mascarados no meio da tela, cobrindo o
    que o Cadu esta realmente vendo.
    """
    valor = ctypes.c_int(0)
    try:
        ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(valor), ctypes.sizeof(valor))
    except Exception:
        return False
    return valor.value != 0


def janelas_visiveis() -> list:
    """Janelas de topo visiveis, da que esta MAIS NA FRENTE para a de tras."""
    achadas = []

    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            r = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            if r.right - r.left < 2 or r.bottom - r.top < 2:
                return True
            if _escondida(hwnd):
                return True
            achadas.append(Janela(hwnd, _titulo(hwnd), _exe_do_hwnd(hwnd),
                                  (r.left, r.top, r.right, r.bottom)))
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return achadas


def janela_em_primeiro_plano():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return Janela(hwnd, _titulo(hwnd), _exe_do_hwnd(hwnd),
                  (r.left, r.top, r.right, r.bottom))


def janela_no_ponto(x: int, y: int):
    pt = wintypes.POINT(int(x), int(y))
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return None
    raiz = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    r = wintypes.RECT()
    user32.GetWindowRect(raiz, ctypes.byref(r))
    return Janela(raiz, _titulo(raiz), _exe_do_hwnd(raiz),
                  (r.left, r.top, r.right, r.bottom))


# --------------------------------------------------------------------------
# Lista de aplicativos liberados
# --------------------------------------------------------------------------

# Atalhos para o que o Cadu provavelmente vai pedir pelo nome falado antes de
# o programa estar aberto. O resto se resolve sozinho: ao liberar, o servidor
# procura o termo no titulo e no executavel das janelas que ja existem.
APELIDOS = {
    "bloco de notas": "notepad.exe",
    "notepad": "notepad.exe",
    "explorador": "explorer.exe",
    "explorador de arquivos": "explorer.exe",
    "paint": "mspaint.exe",
    "blender": "blender.exe",
    "unity": "unity.exe",
    "discord": "discord.exe",
    "steam": "steam.exe",
    "tibia": "tibia.exe",
    "medivia": "medivia.exe",
}


class Liberados:
    """Quem a Avantra pode controlar nesta sessao. Comeca vazia, sempre."""

    def __init__(self) -> None:
        self.exes = set()
        self.termos = set()

    def libera(self, janela) -> bool:
        """Pode AGIR nesta janela? O shell nao tem passe: a area de trabalho e
        a barra de tarefas abrem qualquer programa com dois cliques, entao
        mexer nelas e pedido como qualquer outro ("explorador")."""
        if janela is None:
            return False
        if janela.exe and janela.exe in self.exes:
            return True
        alvo = _normal(janela.exe + " " + janela.titulo)
        return any(t in alvo for t in self.termos if t)

    def adicionar(self, pedidos):
        """-> (resolvidos agora, guardados para quando o programa abrir)."""
        janelas = janelas_visiveis()
        resolvidos, pendentes = [], []
        for pedido in pedidos:
            termo = _normal(pedido)
            if not termo:
                continue
            self.termos.add(termo)
            achou = False
            apelido = APELIDOS.get(termo)
            if apelido:
                self.exes.add(apelido)
                achou = True
            for j in janelas:
                if termo in _normal(j.exe + " " + j.titulo) and j.exe:
                    self.exes.add(j.exe)
                    achou = True
            (resolvidos if achou else pendentes).append(pedido)
        return resolvidos, pendentes

    def resumo(self) -> str:
        if not self.termos:
            return "nenhum aplicativo liberado ainda"
        exes = sorted(self.exes)
        return "liberados: %s%s" % (
            ", ".join(sorted(self.termos)),
            (" (executaveis: " + ", ".join(exes) + ")") if exes else "")


LIBERADOS = Liberados()


# --------------------------------------------------------------------------
# Captura
# --------------------------------------------------------------------------

# O que a ultima captura devolveu. As coordenadas que o modelo manda sao as
# da IMAGEM que ele viu; e aqui que elas voltam a ser pixels da tela.
_FRAME = {"mon": None, "img_w": 0, "img_h": 0}


def _monitores():
    with mss.mss() as sct:
        return [dict(m) for m in sct.monitors[1:]]


def _monitor_ativo(indice):
    mons = _monitores()
    if not mons:
        raise RuntimeError("nao achei nenhum monitor")
    if indice:
        if not 1 <= int(indice) <= len(mons):
            raise ValueError("so existem %d monitores" % len(mons))
        return int(indice), mons[int(indice) - 1]
    frente = janela_em_primeiro_plano()
    if frente:
        cx = (frente.caixa[0] + frente.caixa[2]) // 2
        cy = (frente.caixa[1] + frente.caixa[3]) // 2
        for i, m in enumerate(mons, 1):
            if m["left"] <= cx < m["left"] + m["width"] and \
               m["top"] <= cy < m["top"] + m["height"]:
                return i, m
    maior = max(range(len(mons)), key=lambda i: mons[i]["width"] * mons[i]["height"])
    return maior + 1, mons[maior]


def _mascarar(img, mon):
    """Cobre as janelas de programas nao liberados. -> nomes cobertos.

    O empilhamento importa: percorro da janela MAIS AO FUNDO para a da frente,
    marcando a area das nao liberadas e DESMARCANDO a das liberadas. Assim uma
    janela liberada por cima de uma proibida continua visivel, em vez de
    sumir dentro do retangulo da de baixo.
    """
    mascara = Image.new("L", img.size, 0)
    pincel = ImageDraw.Draw(mascara)
    cobertos = []
    fator = img.size[0] / float(mon["width"])

    for j in reversed(janelas_visiveis()):
        esq, topo, dir_, baixo = j.caixa
        cx0 = int((esq - mon["left"]) * fator)
        cy0 = int((topo - mon["top"]) * fator)
        cx1 = int((dir_ - mon["left"]) * fator)
        cy1 = int((baixo - mon["top"]) * fator)
        if cx1 <= 0 or cy1 <= 0 or cx0 >= img.size[0] or cy0 >= img.size[1]:
            continue                      # esta em outro monitor
        caixa = (max(0, cx0), max(0, cy0),
                 min(img.size[0], cx1), min(img.size[1], cy1))
        if j.exe in SHELL or LIBERADOS.libera(j):
            # O shell aparece sempre: cobrir a area de trabalho e a barra de
            # tarefas deixaria a captura inutil. Aparecer nao e poder clicar.
            pincel.rectangle(caixa, fill=0)
        else:
            pincel.rectangle(caixa, fill=255)
            nome = (j.titulo or j.exe)[:40]
            if nome and nome not in cobertos:
                cobertos.append(nome)

    if mascara.getbbox():
        img.paste(Image.new("RGB", img.size, COR_MASCARA), mask=mascara)
    return cobertos


def capturar(indice=None, mascarar=True):
    """-> (imagem, texto descrevendo o que ela e)."""
    n, mon = _monitor_ativo(indice)
    with mss.mss() as sct:
        bruto = sct.grab(mon)
    img = Image.frombytes("RGB", bruto.size, bruto.bgra, "raw", "BGRX")

    fator = min(1.0, MAX_LADO / float(max(img.size)))
    if fator < 1.0:
        img = img.resize((max(1, int(img.size[0] * fator)),
                          max(1, int(img.size[1] * fator))), Image.LANCZOS)

    cobertos = _mascarar(img, mon) if mascarar else []
    _FRAME.update({"mon": mon, "img_w": img.size[0], "img_h": img.size[1]})

    total = len(_monitores())
    texto = ("Monitor %d de %d (%dx%d reais). A imagem tem %dx%d: use ESTAS "
             "coordenadas, que eu converto." %
             (n, total, mon["width"], mon["height"], img.size[0], img.size[1]))
    if cobertos:
        texto += ("\nCoberto porque nao esta liberado: %s. Se precisar de "
                  "algum, chame liberar_aplicativo." % ", ".join(cobertos[:8]))
    return img, texto


def _para_tela(x, y):
    """Coordenada da imagem -> pixel real da tela."""
    mon = _FRAME["mon"]
    if not mon or not _FRAME["img_w"]:
        raise RuntimeError("chame ver_tela antes: sem uma captura eu nao sei "
                           "a que ponto da tela essas coordenadas correspondem")
    fx = mon["width"] / float(_FRAME["img_w"])
    fy = mon["height"] / float(_FRAME["img_h"])
    return int(mon["left"] + float(x) * fx), int(mon["top"] + float(y) * fy)


def _imagem_mcp(img):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75, optimize=True)
    return {"type": "image",
            "data": base64.b64encode(buf.getvalue()).decode("ascii"),
            "mimeType": "image/jpeg"}


# --------------------------------------------------------------------------
# Mouse e teclado
#
# Tudo passa pelo SendInput do Windows, a mesma porta por onde entram o mouse
# e o teclado de verdade. Nada de mandar mensagem direto para a janela: isso
# funciona num programa e falha no outro, e jogo nenhum aceita.
# --------------------------------------------------------------------------

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF = {
    "move": 0x0001, "esq_baixo": 0x0002, "esq_cima": 0x0004,
    "dir_baixo": 0x0008, "dir_cima": 0x0010,
    "meio_baixo": 0x0020, "meio_cima": 0x0040,
    "roda": 0x0800, "roda_h": 0x1000, "absoluto": 0x8000, "virtual": 0x4000,
}
KEYEVENTF_EXTENDED, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004

VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "espaco": 0x20, "backspace": 0x08, "delete": 0x2E,
    "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "cima": 0x26, "baixo": 0x28,
    "esquerda": 0x25, "direita": 0x27,
}
VK.update({"f%d" % i: 0x6F + i for i in range(1, 13)})
ESTENDIDAS = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _UNIAO(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNIAO)]


def _enviar(*entradas):
    n = len(entradas)
    arr = (_INPUT * n)(*entradas)
    user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))


def _mouse(flags, x=None, y=None, dado=0):
    dx = dy = 0
    if x is not None:
        # Coordenada absoluta da area de trabalho inteira, normalizada em
        # 65535. E o unico jeito que acerta com varios monitores.
        vx = user32.GetSystemMetrics(76)
        vy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78)
        vh = user32.GetSystemMetrics(79)
        dx = int(round((x - vx) * 65535.0 / max(1, vw - 1)))
        dy = int(round((y - vy) * 65535.0 / max(1, vh - 1)))
        flags |= MOUSEEVENTF["absoluto"] | MOUSEEVENTF["virtual"]
    return _INPUT(type=INPUT_MOUSE,
                  u=_UNIAO(mi=_MOUSEINPUT(dx, dy, dado & 0xFFFFFFFF, flags, 0, None)))


def _tecla(vk, solta=False):
    flags = KEYEVENTF_KEYUP if solta else 0
    if vk in ESTENDIDAS:
        flags |= KEYEVENTF_EXTENDED
    return _INPUT(type=INPUT_KEYBOARD,
                  u=_UNIAO(ki=_KEYBDINPUT(vk, 0, flags, 0, None)))


def _caractere(codigo, solta=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if solta else 0)
    return _INPUT(type=INPUT_KEYBOARD,
                  u=_UNIAO(ki=_KEYBDINPUT(0, codigo, flags, 0, None)))


def _modificadores(texto):
    return [p for p in _normal(texto).replace("+", " ").split()
            if p in ("ctrl", "control", "shift", "alt", "win", "windows")]


def mover(x, y):
    _enviar(_mouse(MOUSEEVENTF["move"], x, y))


def clicar_em(x, y, botao="esquerdo", duplo=False, modificadores=""):
    mods = [VK[m] for m in _modificadores(modificadores)]
    baixo, cima = {"esquerdo": ("esq_baixo", "esq_cima"),
                   "direito": ("dir_baixo", "dir_cima"),
                   "meio": ("meio_baixo", "meio_cima")}.get(
                       botao, ("esq_baixo", "esq_cima"))
    mover(x, y)
    time.sleep(0.03)
    for vk in mods:
        _enviar(_tecla(vk))
    _enviar(_mouse(MOUSEEVENTF[baixo]), _mouse(MOUSEEVENTF[cima]))
    if duplo:
        time.sleep(0.06)
        _enviar(_mouse(MOUSEEVENTF[baixo]), _mouse(MOUSEEVENTF[cima]))
    for vk in reversed(mods):
        _enviar(_tecla(vk, solta=True))


def arrastar_de(x0, y0, x1, y1):
    mover(x0, y0)
    time.sleep(0.05)
    _enviar(_mouse(MOUSEEVENTF["esq_baixo"]))
    # Em passos: salto instantaneo e ignorado por muita interface, que so
    # reage ao movimento continuo.
    passos = 14
    for i in range(1, passos + 1):
        mover(int(x0 + (x1 - x0) * i / passos),
              int(y0 + (y1 - y0) * i / passos))
        time.sleep(0.012)
    _enviar(_mouse(MOUSEEVENTF["esq_cima"]))


def rolar_em(x, y, direcao, quantidade):
    mover(x, y)
    passo = 120 * max(1, int(quantidade))
    if direcao in ("cima", "up"):
        _enviar(_mouse(MOUSEEVENTF["roda"], dado=passo))
    elif direcao in ("baixo", "down"):
        _enviar(_mouse(MOUSEEVENTF["roda"], dado=-passo))
    elif direcao in ("direita", "right"):
        _enviar(_mouse(MOUSEEVENTF["roda_h"], dado=passo))
    else:
        _enviar(_mouse(MOUSEEVENTF["roda_h"], dado=-passo))


def digitar_texto(texto):
    """Manda o texto como unicode puro: nao depende do layout do teclado e
    nao mexe na area de transferencia do Cadu."""
    for ch in texto:
        if ch == "\n":
            _enviar(_tecla(0x0D), _tecla(0x0D, solta=True))
        elif ch == "\t":
            _enviar(_tecla(0x09), _tecla(0x09, solta=True))
        elif ch == "\r":
            continue
        else:
            codigo = ord(ch)
            if codigo > 0xFFFF:          # fora do plano basico (emoji)
                codigo -= 0x10000
                alto = 0xD800 + (codigo >> 10)
                baixo = 0xDC00 + (codigo & 0x3FF)
                _enviar(_caractere(alto), _caractere(alto, solta=True),
                        _caractere(baixo), _caractere(baixo, solta=True))
            else:
                _enviar(_caractere(codigo), _caractere(codigo, solta=True))
        time.sleep(0.004)


def teclar_combo(combo):
    partes = [p for p in _normal(combo).split("+") if p]
    if not partes:
        raise ValueError("combo vazio")
    mods, final = partes[:-1], partes[-1]
    vks = [VK[m] for m in mods if m in VK]
    if final in VK:
        alvo = VK[final]
    elif len(final) == 1:
        alvo = user32.VkKeyScanW(ctypes.c_wchar(final)) & 0xFF
    else:
        raise ValueError("nao conheco a tecla '%s'" % final)
    for vk in vks:
        _enviar(_tecla(vk))
    _enviar(_tecla(alvo))
    _enviar(_tecla(alvo, solta=True))
    for vk in reversed(vks):
        _enviar(_tecla(vk, solta=True))


# --------------------------------------------------------------------------
# Abrir programa
# --------------------------------------------------------------------------

MENUS_INICIAR = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]


def _atalho_do_menu(nome):
    """Acha o .lnk do Menu Iniciar que corresponde ao nome falado."""
    alvo = _normal(nome)
    melhor = None
    for raiz in MENUS_INICIAR:
        if not raiz.is_dir():
            continue
        try:
            for lnk in raiz.rglob("*.lnk"):
                n = _normal(lnk.stem)
                if n == alvo:
                    return lnk
                if alvo in n and (melhor is None or
                                  len(n) < len(_normal(melhor.stem))):
                    melhor = lnk
        except OSError:
            continue
    return melhor


# --------------------------------------------------------------------------
# As ferramentas
# --------------------------------------------------------------------------

def _texto(msg):
    return {"content": [{"type": "text", "text": msg}]}


def _erro(msg):
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _exigir_liberado(janela, acao):
    """-> mensagem de recusa, ou string vazia se pode seguir."""
    if janela is None:
        return "Nao consegui identificar a janela para %s." % acao
    if LIBERADOS.libera(janela):
        return ""
    return ("Nao posso %s: a janela e do %s (%s), que o Cadu nao liberou. "
            "Chame liberar_aplicativo com esse nome; ele autoriza por voz."
            % (acao, janela.titulo or janela.exe, janela.exe or "?"))


def t_ver_tela(p):
    img, texto = capturar(p.get("monitor"))
    r = _texto(texto)
    r["content"].append(_imagem_mcp(img))
    return r


def t_ver_perto(p):
    if not _FRAME["mon"]:
        return _erro("chame ver_tela primeiro.")
    x0, y0 = float(p["x0"]), float(p["y0"])
    x1, y1 = float(p["x1"]), float(p["y1"])
    img, _ = capturar(None)
    caixa = (max(0, int(min(x0, x1))), max(0, int(min(y0, y1))),
             min(img.size[0], int(max(x0, x1))), min(img.size[1], int(max(y0, y1))))
    if caixa[2] - caixa[0] < 4 or caixa[3] - caixa[1] < 4:
        return _erro("regiao pequena demais.")
    recorte = img.crop(caixa)
    fator = min(4.0, MAX_LADO / float(max(recorte.size)))
    if fator > 1:
        recorte = recorte.resize((int(recorte.size[0] * fator),
                                  int(recorte.size[1] * fator)), Image.LANCZOS)
    r = _texto("Recorte de (%d,%d) a (%d,%d). Para clicar, continue usando as "
               "coordenadas da tela inteira, nao as deste recorte." % caixa)
    r["content"].append(_imagem_mcp(recorte))
    return r


def t_liberar_aplicativo(p):
    apps = p.get("apps") or []
    if isinstance(apps, str):
        apps = [apps]
    if not apps:
        return _erro("diga quais aplicativos.")
    resolvidos, pendentes = LIBERADOS.adicionar([str(a) for a in apps])
    partes = []
    if resolvidos:
        partes.append("Liberado: " + ", ".join(resolvidos) + ".")
    if pendentes:
        partes.append("Anotado para quando abrir: " + ", ".join(pendentes) + ".")
    return _texto(" ".join(partes) + " " + LIBERADOS.resumo())


def t_aplicativos_liberados(_p):
    return _texto(LIBERADOS.resumo())


def t_janela_ativa(_p):
    j = janela_em_primeiro_plano()
    if not j:
        return _texto("nao ha janela em primeiro plano")
    return _texto("Na frente: %s (%s). %s"
                  % (j.titulo or "(sem titulo)", j.exe or "?",
                     "Liberado." if LIBERADOS.libera(j) else "NAO liberado."))


def t_mover_mouse(p):
    x, y = _para_tela(p["x"], p["y"])
    mover(x, y)
    return _texto("mouse em (%s, %s) da imagem" % (p["x"], p["y"]))


def t_clicar(p):
    x, y = _para_tela(p["x"], p["y"])
    recusa = _exigir_liberado(janela_no_ponto(x, y), "clicar ai")
    if recusa:
        return _erro(recusa)
    clicar_em(x, y, p.get("botao", "esquerdo"), bool(p.get("duplo")),
              p.get("modificadores", ""))
    return _texto("cliquei em (%s, %s)" % (p["x"], p["y"]))


def t_arrastar(p):
    x0, y0 = _para_tela(p["x0"], p["y0"])
    x1, y1 = _para_tela(p["x1"], p["y1"])
    recusa = _exigir_liberado(janela_no_ponto(x0, y0), "arrastar dai")
    if recusa:
        return _erro(recusa)
    arrastar_de(x0, y0, x1, y1)
    return _texto("arrastei de (%s,%s) ate (%s,%s)"
                  % (p["x0"], p["y0"], p["x1"], p["y1"]))


def t_rolar(p):
    x, y = _para_tela(p["x"], p["y"])
    recusa = _exigir_liberado(janela_no_ponto(x, y), "rolar ai")
    if recusa:
        return _erro(recusa)
    rolar_em(x, y, _normal(p.get("direcao", "baixo")), int(p.get("quantidade", 3)))
    return _texto("rolei para %s" % p.get("direcao", "baixo"))


def t_digitar(p):
    texto = str(p.get("texto", ""))
    if not texto:
        return _erro("nada para digitar.")
    recusa = _exigir_liberado(janela_em_primeiro_plano(), "digitar")
    if recusa:
        return _erro(recusa)
    digitar_texto(texto)
    return _texto("digitei %d caracteres" % len(texto))


def t_teclar(p):
    combo = str(p.get("combo", ""))
    recusa = _exigir_liberado(janela_em_primeiro_plano(), "usar o teclado")
    if recusa:
        return _erro(recusa)
    try:
        for _ in range(max(1, min(50, int(p.get("repeticoes", 1))))):
            teclar_combo(combo)
            time.sleep(0.02)
    except ValueError as exc:
        return _erro(str(exc))
    return _texto("teclei %s" % combo)


def t_abrir_aplicativo(p):
    nome = str(p.get("nome", "")).strip()
    if not nome:
        return _erro("diga qual aplicativo.")
    fingida = Janela(0, nome, APELIDOS.get(_normal(nome), ""), (0, 0, 0, 0))
    if not LIBERADOS.libera(fingida):
        return _erro("O Cadu ainda nao liberou o %s. Chame liberar_aplicativo "
                     "antes de abrir." % nome)
    alvo = Path(os.path.expandvars(nome))
    try:
        if alvo.exists():
            os.startfile(str(alvo))
        else:
            atalho = _atalho_do_menu(nome)
            os.startfile(str(atalho) if atalho else nome)
    except OSError as exc:
        return _erro("nao consegui abrir %s: %s" % (nome, exc))
    time.sleep(1.2)
    LIBERADOS.adicionar([nome])           # a janela existe agora: fixa o exe
    return _texto("abri %s" % nome)


def t_esperar(p):
    time.sleep(max(0.0, min(10.0, float(p.get("segundos", 1)))))
    return _texto("esperei")


FERRAMENTAS = [
    ("ver_tela",
     "Tira uma foto da tela e devolve a imagem. Comece SEMPRE por aqui: as "
     "coordenadas das outras ferramentas sao as desta imagem. Janela de "
     "programa que o Cadu nao liberou aparece coberta.",
     {"type": "object", "properties": {
         "monitor": {"type": "integer",
                     "description": "qual monitor (1, 2...). Sem isto, o do "
                                    "programa que esta na frente."}}},
     t_ver_tela),
    ("ver_perto",
     "Amplia um pedaco da tela, para ler texto pequeno ou conferir um icone. "
     "Nao clica em nada.",
     {"type": "object", "properties": {
         "x0": {"type": "number"}, "y0": {"type": "number"},
         "x1": {"type": "number"}, "y1": {"type": "number"}},
      "required": ["x0", "y0", "x1", "y1"]},
     t_ver_perto),
    ("liberar_aplicativo",
     "Pede ao Cadu permissao para controlar um programa. Sem isto nao da para "
     "clicar nem digitar nele, e ele sai coberto na foto da tela. Ele "
     "autoriza em voz alta. Peca so o que a tarefa precisa.",
     {"type": "object", "properties": {
         "apps": {"type": "array", "items": {"type": "string"},
                  "description": "nomes como o Cadu fala: Bloco de notas, Blender"},
         "motivo": {"type": "string", "description": "o que voce vai fazer ali"}},
      "required": ["apps"]},
     t_liberar_aplicativo),
    ("aplicativos_liberados", "Diz o que ja esta liberado nesta sessao.",
     {"type": "object", "properties": {}}, t_aplicativos_liberados),
    ("janela_ativa", "Diz qual programa esta na frente e se esta liberado.",
     {"type": "object", "properties": {}}, t_janela_ativa),
    ("mover_mouse", "Move o cursor sem clicar (revela menu ou dica de botao).",
     {"type": "object", "properties": {"x": {"type": "number"},
                                       "y": {"type": "number"}},
      "required": ["x", "y"]}, t_mover_mouse),
    ("clicar", "Clica num ponto da imagem da tela.",
     {"type": "object", "properties": {
         "x": {"type": "number"}, "y": {"type": "number"},
         "botao": {"type": "string", "enum": ["esquerdo", "direito", "meio"]},
         "duplo": {"type": "boolean"},
         "modificadores": {"type": "string",
                           "description": "ctrl, shift, alt, ou ctrl+shift"}},
      "required": ["x", "y"]}, t_clicar),
    ("arrastar", "Segura o botao esquerdo de um ponto ao outro.",
     {"type": "object", "properties": {
         "x0": {"type": "number"}, "y0": {"type": "number"},
         "x1": {"type": "number"}, "y1": {"type": "number"}},
      "required": ["x0", "y0", "x1", "y1"]}, t_arrastar),
    ("rolar", "Gira a roda do mouse sobre um ponto.",
     {"type": "object", "properties": {
         "x": {"type": "number"}, "y": {"type": "number"},
         "direcao": {"type": "string",
                     "enum": ["cima", "baixo", "esquerda", "direita"]},
         "quantidade": {"type": "integer"}},
      "required": ["x", "y", "direcao"]}, t_rolar),
    ("digitar", "Digita texto no programa que esta na frente.",
     {"type": "object", "properties": {"texto": {"type": "string"}},
      "required": ["texto"]}, t_digitar),
    ("teclar", "Aperta uma tecla ou combinacao: enter, ctrl+s, alt+tab, f2.",
     {"type": "object", "properties": {
         "combo": {"type": "string"},
         "repeticoes": {"type": "integer"}},
      "required": ["combo"]}, t_teclar),
    ("abrir_aplicativo",
     "Abre um programa pelo nome. So funciona depois de o Cadu liberar esse "
     "programa.",
     {"type": "object", "properties": {"nome": {"type": "string"}},
      "required": ["nome"]}, t_abrir_aplicativo),
    ("esperar", "Espera alguns segundos, para a tela terminar de mudar.",
     {"type": "object", "properties": {"segundos": {"type": "number"}}},
     t_esperar),
]

MAPA = {nome: fn for nome, _d, _s, fn in FERRAMENTAS}


# --------------------------------------------------------------------------
# MCP em JSON-RPC pelo stdin/stdout
#
# Uma mensagem por linha. O stdout e so do protocolo: qualquer coisa que a
# gente queira ver vai para o stderr, senao o claude engasga no meio do JSON.
# --------------------------------------------------------------------------

def _log(msg):
    print("[mcp_tela] " + msg, file=sys.stderr, flush=True)


def _responder(id_, resultado=None, erro=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if erro is not None:
        msg["error"] = erro
    else:
        msg["result"] = resultado
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _tratar(pedido):
    metodo = pedido.get("method", "")
    id_ = pedido.get("id")

    if metodo == "initialize":
        pedida = (pedido.get("params") or {}).get("protocolVersion")
        _responder(id_, {
            "protocolVersion": pedida or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tela", "version": VERSAO}})
        return

    if metodo.startswith("notifications/"):
        return                                 # notificacao: nao se responde

    if metodo == "ping":
        _responder(id_, {})
        return

    if metodo == "tools/list":
        _responder(id_, {"tools": [
            {"name": n, "description": d, "inputSchema": s}
            for n, d, s, _fn in FERRAMENTAS]})
        return

    if metodo == "tools/call":
        params = pedido.get("params") or {}
        nome = params.get("name", "")
        fn = MAPA.get(nome)
        if fn is None:
            _responder(id_, _erro("nao existe a ferramenta '%s'" % nome))
            return
        try:
            _responder(id_, fn(params.get("arguments") or {}))
        except Exception as exc:
            # Erro de ferramenta volta como resultado, nao como erro de
            # protocolo: assim o modelo le o motivo e tenta outro caminho.
            _log("%s falhou: %r" % (nome, exc))
            _responder(id_, _erro("%s falhou: %s" % (nome, exc)))
        return

    if id_ is not None:
        _responder(id_, erro={"code": -32601, "message": "metodo desconhecido"})


def main():
    _dpi_ciente()
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    _log("pronto (versao %s)" % VERSAO)
    for linha in sys.stdin:
        linha = linha.strip()
        if not linha:
            continue
        try:
            pedido = json.loads(linha)
        except json.JSONDecodeError:
            continue
        try:
            _tratar(pedido)
        except Exception as exc:
            _log("erro tratando %s: %r" % (linha[:80], exc))


if __name__ == "__main__":
    main()
