"""Hierarquia de erros do RiftCoach.

Regra geral: todo erro que o usuario final pode causar precisa carregar uma
mensagem acionavel. `RiftCoachError.hint` e o que a CLI/UI mostra — se um erro
novo nao tem hint util, provavelmente ele nao deveria ser uma excecao propria.
"""

from __future__ import annotations


class RiftCoachError(Exception):
    """Base de tudo. `hint` e a mensagem acionavel mostrada ao usuario."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------
# Conformidade (ver COMPLIANCE.md)
# --------------------------------------------------------------------------


class LiveGameRefused(RiftCoachError):
    """Levantado quando o client local NAO esta comprovadamente em modo replay.

    Esta excecao e o intertravamento de conformidade. Ela falha fechado: qualquer
    resultado ambiguo (404, timeout, corpo inesperado, conexao recusada) chega
    aqui e a operacao e abortada.
    """


# --------------------------------------------------------------------------
# API da Riot
# --------------------------------------------------------------------------


class RiotApiError(RiftCoachError):
    def __init__(self, message: str, status: int, hint: str | None = None) -> None:
        super().__init__(message, hint)
        self.status = status


class RiotKeyInvalid(RiotApiError):
    """401 — a chave nao foi reconhecida."""


class RiotKeyExpired(RiotApiError):
    """403 — chave de desenvolvimento expirada.

    Chaves de desenvolvimento da Riot expiram a cada 24h. Isso e um problema de
    UX de primeira classe, nao uma nota de rodape: a maioria dos usuarios novos
    vai bater aqui no segundo dia de uso.
    """


class RiotNotFound(RiotApiError):
    """404 — invocador, partida ou recurso inexistente."""


class RiotRateLimited(RiotApiError):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(
            message,
            status=429,
            hint=f"Limite de taxa atingido. Aguardando {retry_after:.0f}s automaticamente.",
        )
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class ParseError(RiftCoachError):
    """A partida nao pode ser destilada em MatchFacts."""


class PlayerNotInMatch(ParseError):
    pass


# --------------------------------------------------------------------------
# Roteamento de modelos (etapa 3)
# --------------------------------------------------------------------------


class NoViableProvider(RiftCoachError):
    """Nenhum provedor atende aos requisitos da tarefa.

    Nunca vira um 500 silencioso: `hint` precisa dizer o que falta
    (Ollama fora do ar? cota estourada? nenhuma chave configurada?).
    """


class SchemaExhausted(RiftCoachError):
    """O provedor nao sustentou o schema de saida apos as tentativas de reparo."""
