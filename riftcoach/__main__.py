"""Permite `python -m riftcoach`.

Existe para que a janela consiga abrir o overlay como um processo separado sem
depender de o comando `riftcoach` estar no PATH — que e justamente o que nao
esta garantido na maquina de quem instalou com dois cliques.
"""

from riftcoach.cli import app

if __name__ == "__main__":
    app()
