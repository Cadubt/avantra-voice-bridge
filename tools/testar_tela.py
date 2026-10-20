"""Testa o servidor mcp_tela sem precisar da ponte inteira.

Sobe o servidor como o claude sobe (stdin/stdout em JSON-RPC), chama algumas
ferramentas e conta o que aconteceu. A captura e gravada em tmp para voce
olhar com os proprios olhos — inclusive para conferir se o que nao foi
liberado saiu mesmo coberto.

    .venv/Scripts/python.exe tools/testar_tela.py
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
TMP = RAIZ / "tmp"


class Servidor:
    def __init__(self):
        self.p = subprocess.Popen(
            [str(RAIZ / ".venv/Scripts/python.exe"), str(RAIZ / "mcp_tela.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        self.id = 0

    def chamar(self, metodo, params=None):
        self.id += 1
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self.id, "method": metodo,
             "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline()).get("result", {})

    def ferramenta(self, nome, **args):
        return self.chamar("tools/call", {"name": nome, "arguments": args})

    def fechar(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def _texto_de(r):
    return " ".join(c.get("text", "") for c in r.get("content", [])
                    if c.get("type") == "text")


def _salvar_imagem(r, nome):
    for c in r.get("content", []):
        if c.get("type") == "image":
            TMP.mkdir(exist_ok=True)
            destino = TMP / nome
            destino.write_bytes(base64.b64decode(c["data"]))
            return destino
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    s = Servidor()
    s.chamar("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "teste", "version": "1"}})
    falhas = 0

    print("1. janela na frente")
    print("   ", _texto_de(s.ferramenta("janela_ativa")))

    print("2. captura com nada liberado")
    r = s.ferramenta("ver_tela")
    print("   ", _texto_de(r).replace("\n", "\n    "))
    caminho = _salvar_imagem(r, "teste_tela_1_mascarada.jpg")
    print("    imagem:", caminho, "(%d KB)" % (caminho.stat().st_size // 1024)
          if caminho else "NENHUMA")
    if not caminho:
        falhas += 1

    print("3. clicar sem liberar nada (tem que RECUSAR)")
    r = s.ferramenta("clicar", x=700, y=200)
    recusou = bool(r.get("isError"))
    print("   ", "recusou:" if recusou else "DEIXOU PASSAR:", _texto_de(r)[:160])
    if not recusou:
        falhas += 1

    print("4. liberar o Bloco de notas")
    print("   ", _texto_de(s.ferramenta("liberar_aplicativo",
                                        apps=["Bloco de notas"],
                                        motivo="teste")))

    print("5. captura depois de liberar")
    r = s.ferramenta("ver_tela")
    print("   ", _texto_de(r).replace("\n", "\n    "))
    caminho = _salvar_imagem(r, "teste_tela_2_liberada.jpg")
    print("    imagem:", caminho)

    s.fechar()
    print("\n%s" % ("tudo certo" if not falhas else "%d checagem(ns) falhou" % falhas))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
