"""Painel da Avantra: uma janela com a conversa, as imagens e os links.

Por que pywebview: e uma janela nativa do Windows com o WebView2 (o mesmo
motor do Edge) dentro. HTML e CSS dao o visual de painel de nave sem
reinventar renderizacao de markdown, tabela, imagem e rolagem — tudo o que o
WPF exigiria escrever na mao. E tudo continua em Python, no mesmo processo da
ponte, entao a janela le o estado dela direto, sem servidor no meio.

Seguranca, porque a pagina tem uma ponte para o Python (js_api):
  - todo HTML que vem da resposta do modelo passa pelo nh3 com uma lista
    fechada de tags; script, iframe, evento inline e estilo nao sobrevivem;
  - link so abre se for http, https ou mailto, e abre no navegador padrao,
    nunca dentro da janela;
  - imagem so carrega de http(s) ou de data:image; arquivo local vira
    data:image aqui no Python, e so se for extensao de imagem.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import queue
import re
import sys
import threading
import time
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
PAGINA = ROOT / "painel" / "index.html"

# ---------------------------------------------------------------------------
# markdown -> HTML seguro
# ---------------------------------------------------------------------------

_TAGS = {"p", "br", "hr", "strong", "em", "b", "i", "u", "s", "del", "code",
         "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5",
         "h6", "a", "img", "table", "thead", "tbody", "tr", "th", "td", "sup",
         "sub", "kbd", "mark"}
_ATRIBUTOS = {"a": {"href", "title"}, "img": {"src", "alt", "title"},
              "th": {"align"}, "td": {"align"}, "code": {"class"}}
_EXT_IMAGEM = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
               ".avif", ".ico"}
_LIMITE_IMAGEM_LOCAL = 8 * 1024 * 1024

_URL_SOLTA = re.compile(
    r"(?<![(<\[\"'=/])\b(https?://[^\s<>()\[\]\"'`]+[^\s<>()\[\]\"'`.,;:!?])")
_SRC = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")')
_FENCE = re.compile(r"(^```.*?^```[ \t]*$|^~~~.*?^~~~[ \t]*$)", re.S | re.M)


def _filtro_atributo(tag: str, atributo: str, valor: str):
    v = (valor or "").strip()
    baixo = v.lower()
    if tag == "a" and atributo == "href":
        return v if baixo.startswith(("http://", "https://", "mailto:")) else None
    if tag == "img" and atributo == "src":
        if baixo.startswith(("http://", "https://", "data:image/")):
            return v
        return None
    if tag == "code" and atributo == "class":
        return v if re.fullmatch(r"language-[\w+#.-]{1,30}", v) else None
    return v


def _imagem_local_para_data(src: str) -> str:
    """file:///X:/a%20b.png ou X:\\a.png -> data:image/png;base64,...

    O WebView nao deixa uma pagina carregar arquivo do disco, e e bom que nao
    deixe. Quem decide aqui e o Python: so imagem, so ate 8 MB.
    """
    bruto = src.strip()
    if bruto.lower().startswith("file:"):
        caminho = unquote(urlparse(bruto).path or "")
        if re.match(r"^/[A-Za-z]:", caminho):
            caminho = caminho[1:]
    elif re.match(r"^[A-Za-z]:[\\/]", bruto):
        caminho = unquote(bruto)
    else:
        return src
    p = Path(caminho)
    try:
        if (p.suffix.lower() not in _EXT_IMAGEM or not p.is_file()
                or p.stat().st_size > _LIMITE_IMAGEM_LOCAL):
            return src
        tipo = mimetypes.guess_type(p.name)[0] or "image/png"
        return "data:%s;base64,%s" % (tipo, base64.b64encode(p.read_bytes()).decode())
    except OSError:
        return src


def _linkificar(texto: str) -> str:
    """URL solta vira <url>, que o markdown transforma em link. Fora de codigo."""
    partes = _FENCE.split(texto)
    for i in range(0, len(partes), 2):          # pares = fora de bloco de codigo
        partes[i] = _URL_SOLTA.sub(r"<\1>", partes[i])
    return "".join(partes)


def renderizar(texto: str) -> str:
    import markdown
    import nh3

    html = markdown.markdown(
        _linkificar(texto or ""),
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html")
    html = _SRC.sub(lambda m: m.group(1) + _imagem_local_para_data(
        m.group(2).replace("&amp;", "&")) + m.group(3), html)
    return nh3.clean(html, tags=_TAGS, attributes=_ATRIBUTOS,
                     url_schemes={"http", "https", "mailto", "data"},
                     attribute_filter=_filtro_atributo,
                     link_rel="noopener noreferrer")


class _Midia(HTMLParser):
    """Recolhe imagens e links de um HTML ja limpo, para a coluna da direita."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.imagens: list[dict] = []
        self.links: list[dict] = []
        self._link: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "img" and a.get("src"):
            self.imagens.append({"src": a["src"], "alt": a.get("alt") or ""})
        elif tag == "a" and a.get("href", "").startswith(("http://", "https://")):
            self._link = {"href": a["href"], "texto": ""}

    def handle_data(self, data):
        if self._link is not None:
            self._link["texto"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            self.links.append(self._link)
            self._link = None


def extrair_midia(html: str) -> dict:
    m = _Midia()
    try:
        m.feed(html)
    except Exception:
        pass
    vistos, imagens = set(), []
    for im in m.imagens:
        if im["src"] not in vistos:
            vistos.add(im["src"])
            imagens.append(im)
    links = []
    for ln in m.links:
        href = ln["href"]
        if href in vistos:
            continue
        vistos.add(href)
        caminho = urlparse(href).path.lower()
        if any(caminho.endswith(e) for e in _EXT_IMAGEM):
            imagens.append({"src": href, "alt": ln["texto"].strip()})
            continue
        dominio = urlparse(href).netloc.lower()
        if dominio.startswith("www."):
            dominio = dominio[4:]
        texto = ln["texto"].strip()
        links.append({"href": href, "dominio": dominio,
                      "titulo": texto if texto and texto != href else dominio})
    return {"imagens": imagens, "links": links}


# ---------------------------------------------------------------------------
# espelho do console: o que a ponte imprime aparece tambem no "Registro"
# ---------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MEDIDOR = re.compile(r"\[[# ]{24}\] p=")


class _Espelho:
    def __init__(self, original, destino) -> None:
        self._original = original
        self._destino = destino
        self._buf = ""

    def write(self, s):
        n = self._original.write(s)
        try:
            self._buf += s
            while "\n" in self._buf:
                linha, self._buf = self._buf.split("\n", 1)
                linha = _ANSI.sub("", linha.rsplit("\r", 1)[-1]).rstrip()
                if linha.strip() and not _MEDIDOR.search(linha):
                    self._destino(linha)
            if len(self._buf) > 4000:
                self._buf = self._buf[-4000:]
        except Exception:
            pass
        return n

    def flush(self):
        self._original.flush()

    def __getattr__(self, nome):
        return getattr(self._original, nome)


# ---------------------------------------------------------------------------
# o que a pagina pode chamar
# ---------------------------------------------------------------------------

class _Api:
    """Exposto ao JavaScript como window.pywebview.api.

    Atributos com _ na frente NAO sao expostos pelo pywebview; por isso o
    painel fica em self._painel, e a pagina nao alcanca a ponte inteira.
    """

    def __init__(self, painel: "Painel") -> None:
        self._painel = painel

    def enviar(self, texto):
        texto = str(texto or "").strip()[:8000]
        if texto and self._painel.ponte:
            self._painel.ponte.receber_texto(texto)
        return True

    def abrir_link(self, url):
        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://", "mailto:")):
            return False
        webbrowser.open(url)
        return True

    def alternar_escuta(self):
        if self._painel.ponte:
            self._painel.ponte._toggle()
        return True

    def parar(self):
        if self._painel.ponte:
            self._painel.ponte.parar_pelo_painel()
        return True

    def responder(self, decisao):
        if self._painel.ponte:
            self._painel.ponte.resposta_digitada(bool(decisao))
        return True


# ---------------------------------------------------------------------------
# a janela
# ---------------------------------------------------------------------------

class Painel:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.ponte = None
        self.janela = None
        self._fila: queue.Queue = queue.Queue()
        self._pronto = threading.Event()
        self._fechou = threading.Event()
        self._lock = threading.Lock()
        self._resp = {"id": 0, "texto": "", "sujo": False, "t": 0.0}
        self._tele_anterior: dict = {}
        self._info_anterior: dict = {}

    # ---------------- ciclo de vida ----------------

    def abrir(self, trabalho) -> None:
        """Abre a janela e roda `trabalho` numa thread. Bloqueia ate fechar."""
        import webview

        sys.stdout = _Espelho(sys.stdout, self._console)
        pcfg = self.cfg["painel"]
        self.janela = webview.create_window(
            "Avantra",
            html=PAGINA.read_text(encoding="utf-8"),
            js_api=_Api(self),
            width=int(pcfg.get("largura", 1500)),
            height=int(pcfg.get("altura", 940)),
            min_size=(960, 620),
            background_color="#04070C",
            text_select=True,
        )
        self.janela.events.loaded += self._carregou
        self.janela.events.closed += self._fechou.set

        def _rodar():
            threading.Thread(target=self._bomba, name="painel-bomba",
                             daemon=True).start()
            trabalho()

        webview.start(_rodar, debug=bool(pcfg.get("debug", False)))

    @property
    def fechado(self) -> bool:
        return self._fechou.is_set()

    def _carregou(self) -> None:
        self._pronto.set()

    # ---------------- eventos que a ponte manda ----------------

    def _emitir(self, **ev) -> None:
        self._fila.put(ev)

    def _console(self, linha: str) -> None:
        self._emitir(t="log", texto=linha[:500])

    def voce(self, texto: str, digitado: bool = False) -> None:
        self._emitir(t="voce", texto=texto, origem="texto" if digitado else "voz",
                     hora=time.strftime("%H:%M"))

    def resposta_inicio(self) -> None:
        with self._lock:
            self._resp = {"id": self._resp["id"] + 1, "texto": "",
                          "sujo": False, "t": 0.0}
            rid = self._resp["id"]
        self._emitir(t="resp_ini", id=rid, hora=time.strftime("%H:%M"))

    def resposta_delta(self, acumulado: str) -> None:
        with self._lock:
            self._resp["texto"] = acumulado
            self._resp["sujo"] = True

    def resposta_fim(self, texto: str, erro: str = "",
                     latencia_ms=None) -> None:
        with self._lock:
            self._resp["texto"] = texto or self._resp["texto"]
            self._resp["sujo"] = False
            rid, final = self._resp["id"], self._resp["texto"]
        html = renderizar(final) if final.strip() else ""
        self._emitir(t="resp_fim", id=rid, html=html, erro=erro or "",
                     midia=extrair_midia(html), latencia=latencia_ms,
                     hora=time.strftime("%H:%M"))

    def ferramenta(self, nome: str) -> None:
        with self._lock:
            rid = self._resp["id"]
        self._emitir(t="ferramenta", id=rid, nome=nome,
                     hora=time.strftime("%H:%M:%S"))

    def fala_local(self, texto: str) -> None:
        """Frases curtas que a propria ponte fala (comandos, avisos)."""
        self._emitir(t="local", texto=texto, hora=time.strftime("%H:%M"))

    def sistema(self, texto: str, nivel: str = "info") -> None:
        self._emitir(t="sistema", texto=texto, nivel=nivel)

    def confirmacao(self, titulo: str, ouvi: str, acao: str) -> None:
        self._emitir(t="confirma", titulo=titulo, ouvi=ouvi, acao=acao)

    def confirmacao_fim(self) -> None:
        self._emitir(t="confirma_fim")

    # ---------------- bomba: fila -> pagina ----------------

    def _bomba(self) -> None:
        """Junta eventos e manda em lote, ~20 vezes por segundo.

        evaluate_js e sincrono e custa uma ida e volta ao WebView: um por
        pedaco de texto do streaming afogaria a janela. Em lote, nao.
        """
        self._pronto.wait()
        while not self._fechou.is_set():
            lote = []
            try:
                lote.append(self._fila.get(timeout=0.05))
                while len(lote) < 200:
                    lote.append(self._fila.get_nowait())
            except queue.Empty:
                pass

            with self._lock:
                agora = time.time()
                if self._resp["sujo"] and agora - self._resp["t"] > 0.09:
                    self._resp["sujo"] = False
                    self._resp["t"] = agora
                    rid, texto = self._resp["id"], self._resp["texto"]
                else:
                    rid, texto = None, None
            if rid is not None:
                try:
                    lote.append({"t": "resp", "id": rid, "html": renderizar(texto)})
                except Exception:
                    pass

            lote.extend(self._telemetria())
            if not lote:
                continue
            try:
                self.janela.evaluate_js(
                    "window.avantra && window.avantra.receber(%s)"
                    % json.dumps(lote, ensure_ascii=True))
            except Exception:
                if self._fechou.is_set():
                    return

    def _telemetria(self) -> list:
        p = self.ponte
        if p is None:
            return []
        saida = []
        try:
            estado = p.estado_visivel()
            nivel = 0.0
            if getattr(p, "listener", None) is not None:
                nivel = round(min(1.0, float(p.listener.nivel) * 12) * 20) / 20
            tele = {"estado": estado, "nivel": nivel}
            if tele != self._tele_anterior:
                self._tele_anterior = tele
                saida.append({"t": "tele", **tele})
            info = p.info_visivel()
            if info != self._info_anterior:
                self._info_anterior = info
                saida.append({"t": "info", **info})
        except Exception:
            pass
        return saida
