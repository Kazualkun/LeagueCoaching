"""Configuracao e segredos.

Ordem de precedencia para a chave da Riot:
    1. variavel de ambiente RIOT_API_KEY  (util para CI e desenvolvimento)
    2. cofre de credenciais do SO via keyring  (o caminho recomendado)

Chaves NUNCA sao gravadas em arquivo pelo RiftCoach. Ver COMPLIANCE.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

KEYRING_SERVICE = "riftcoach"

# Match-v5 e account-v1 usam roteamento regional (nao a plataforma).
PLATFORM_TO_ROUTING: dict[str, str] = {
    "br1": "americas",
    "la1": "americas",
    "la2": "americas",
    "na1": "americas",
    "euw1": "europe",
    "eun1": "europe",
    "tr1": "europe",
    "ru": "europe",
    "me1": "europe",
    "kr": "asia",
    "jp1": "asia",
    "oc1": "sea",
    "ph2": "sea",
    "sg2": "sea",
    "th2": "sea",
    "tw2": "sea",
    "vn2": "sea",
}


def data_dir() -> Path:
    """Diretorio de dados do usuario. Respeita RIFTCOACH_HOME se definido."""
    override = os.environ.get("RIFTCOACH_HOME")
    root = Path(override) if override else Path.home() / ".riftcoach"
    root.mkdir(parents=True, exist_ok=True)
    return root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    riot_api_key: str | None = None
    riot_platform: str = Field(default="br1", description="br1, na1, euw1, kr, ...")

    # Provedores de IA. Todos opcionais: sem nenhum deles o app entrega o
    # relatorio L5 completo (docs/01-model-routing.md, escada de degradacao).
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    openrouter_api_key: str | None = None
    mistral_api_key: str | None = None
    ollama_base_url: str = "http://127.0.0.1:11434/v1"

    # strict NAO e um filtro cosmetico: ele e aplicado no roteador ANTES de
    # qualquer outra consideracao, entao nenhum provedor de nuvem chega a ser
    # cogitado, mesmo que seja o unico disponivel. O resultado nesse caso e o
    # relatorio L5, nao uma excecao.
    privacy_mode: Literal["relaxed", "strict"] = "relaxed"
    locale: str = Field(
        default="pt_BR",
        description=(
            "Controla DataDragon, prompts, validador e UI — sempre juntos. "
            "Ver docs/04-knowledge-base.md, secao 'Locale'."
        ),
    )

    @property
    def routing(self) -> str:
        """Valor de roteamento regional para match-v5 / account-v1."""
        return PLATFORM_TO_ROUTING.get(self.riot_platform.lower(), "americas")

    def resolve_api_key(self) -> str | None:
        """Env var primeiro, depois o cofre do SO."""
        if self.riot_api_key:
            return self.riot_api_key
        return read_key_from_keyring("riot")

    def resolve_provider_key(self, provider: str) -> str | None:
        """Chave de um provedor de IA. Mesma precedencia da chave da Riot.

        Devolver None e um resultado NORMAL e esperado: significa "este
        provedor nao esta configurado", e o roteador simplesmente nao o
        considera. A maioria dos usuarios vai ter zero ou uma chave aqui.
        """
        direto = getattr(self, f"{provider}_api_key", None)
        if isinstance(direto, str) and direto.strip():
            return direto.strip()
        return read_key_from_keyring(provider)


def read_key_from_keyring(name: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:
        # Sem backend de keyring disponivel (comum em servidores headless).
        # Nao e fatal: o usuario ainda pode usar variavel de ambiente.
        return None


def write_key_to_keyring(name: str, value: str) -> bool:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, name, value)
        return True
    except Exception:
        return False


def delete_key_from_keyring(name: str) -> bool:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, name)
        return True
    except Exception:
        return False


settings = Settings()
