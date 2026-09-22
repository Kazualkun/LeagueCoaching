"""Deteccao de hardware -> tier de modelo local.

O ERRO CLASSICO, e ele e a razao deste modulo nao ser tres linhas: classificar
a GPU so pelo tamanho dos pesos. O modelo carrega, o usuario comeca a usar, e
por volta do token 6.000 o cache KV estoura a VRAM — ai vaza para a RAM, a
velocidade cai dez vezes, e o usuario culpa o app.

Entao os pisos de TIER_REQUIREMENT_GB ja sao pesos MAIS cache: um 14B em Q4 sao
~9 GB de peso, mas pedem 12 GB de placa para rodar em 32k sem vazar.

Roda uma vez e grava em ~/.riftcoach/hardware.json. Nao exige torch, CUDA nem
nada pesado: le o que o sistema operacional ja expoe.
"""

from __future__ import annotations

import contextlib
import json
import platform
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import httpx

from riftcoach.config import data_dir

Tier = Literal["none", "tier1", "tier2", "tier3"]

OLLAMA_DEFAULT = "http://127.0.0.1:11434"

# VRAM TOTAL exigida por tier — pesos MAIS a folga de cache KV em 32k.
#
# O erro que esta tabela existe para evitar e classificar so pelos pesos. Um
# 14B em Q4 sao ~9 GB de peso e caberia numa placa de 12 GB no papel; com 32k
# de contexto, o cache KV pede mais ~3 GB e o conjunto estoura. Por isso os
# pisos abaixo ja sao o custo somado, e nao o tamanho do arquivo do modelo.
#
# Os valores batem com a tabela que o README mostra ao usuario. Se mudarem
# aqui, mudam la — prometer 8b numa placa de 8 GB e entregar "use a nuvem" e
# pior do que nunca ter prometido.
TIER_REQUIREMENT_GB: list[tuple[float, Tier]] = [
    (24.0, "tier3"),  # qwen3:30b-a3b — ~18 GB de peso + ~5 GB de cache
    (12.0, "tier2"),  # qwen3:14b     — ~9 GB de peso + ~3 GB de cache
    (8.0, "tier1"),  # qwen3:8b      — ~5 GB de peso + ~2 GB de cache
]

# Quanto do total cada tier gasta so com cache KV em 32k. Nao entra na escolha
# — ja esta embutido nos pisos acima — mas entra na explicacao para o usuario,
# que e o unico lugar onde esse numero importa depois.
KV_CACHE_RESERVE_GB: dict[Tier, float] = {
    "tier3": 5.0,
    "tier2": 3.0,
    "tier1": 2.0,
    "none": 0.0,
}


@dataclass(frozen=True)
class Hardware:
    gpu_name: str
    vram_gb: float
    unified_memory: bool
    tier: Tier
    ollama_up: bool
    ollama_models: tuple[str, ...] = ()

    @property
    def kv_reserve_gb(self) -> float:
        """Quanto da memoria vai para o cache KV em 32k, no tier escolhido."""
        return KV_CACHE_RESERVE_GB[self.tier]

    def describe(self) -> str:
        if self.tier == "none":
            faltam = TIER_REQUIREMENT_GB[-1][0] - self.vram_gb
            if not self.gpu_name:
                return "sem GPU dedicada — use um tier gratuito na nuvem"
            return (
                f"{self.gpu_name} · {self.vram_gb:.0f} GB — faltam ~{faltam:.0f} GB para o "
                "menor modelo local; use um tier gratuito na nuvem"
            )
        memoria = "memoria unificada" if self.unified_memory else "VRAM"
        return (
            f"{self.gpu_name} · {self.vram_gb:.0f} GB de {memoria} "
            f"(~{self.kv_reserve_gb:.0f} GB vao para o cache KV em 32k) -> {self.tier}"
        )


def _tier_for(vram_gb: float) -> Tier:
    for exigido, tier in TIER_REQUIREMENT_GB:
        if vram_gb >= exigido:
            return tier
    return "none"


def _run(cmd: list[str]) -> str:
    """Executa um comando de inspeccao e engole qualquer falha.

    Toda deteccao aqui e best-effort por projeto: nao achar a GPU degrada para
    nuvem, que e um caminho suportado. Estourar uma excecao porque o `wmic` nao
    existe mais no Windows 11 nao e.
    """
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
        return out.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _probe_nvidia() -> tuple[str, float] | None:
    """nvidia-smi em vez de pynvml: ja vem com o driver, entao nao adiciona
    dependencia e funciona igual nos tres sistemas."""
    saida = _run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
    )
    for linha in saida.splitlines():
        partes = [p.strip() for p in linha.split(",")]
        if len(partes) == 2 and partes[1].replace(".", "").isdigit():
            return partes[0], float(partes[1]) / 1024.0
    return None


def _probe_apple() -> tuple[str, float] | None:
    """Apple Silicon: memoria unificada, entao a RAM inteira e 'VRAM'.

    Na pratica o sistema nao cede tudo — reservamos 25% para o resto da
    maquina, que e o que a experiencia de rodar Ollama em Mac mostra.
    """
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    saida = _run(["sysctl", "-n", "hw.memsize"]).strip()
    if not saida.isdigit():
        return None
    total_gb = int(saida) / (1024**3)
    return f"Apple Silicon ({platform.processor() or 'arm64'})", total_gb * 0.75


def _probe_windows_gpu() -> tuple[str, float] | None:
    """AMD/Intel no Windows.

    PowerShell CIM em vez de `wmic`: o wmic foi descontinuado e ja nao vem em
    instalacoes recentes do Windows 11, entao ele falharia silenciosamente
    exatamente nas maquinas mais novas.
    """
    if platform.system() != "Windows":
        return None
    saida = _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress",
        ]
    )
    if not saida.strip():
        return None
    try:
        dados = json.loads(saida)
    except json.JSONDecodeError:
        return None
    if isinstance(dados, dict):
        dados = [dados]

    melhor: tuple[str, float] | None = None
    for placa in dados:
        ram = placa.get("AdapterRAM") or 0
        # AdapterRAM e int32 com sinal e satura em 4 GB: placas maiores
        # aparecem como 4095 MB ou negativas. Vale como piso, nao como medida.
        gb = (int(ram) if int(ram) > 0 else 0) / (1024**3)
        nome = str(placa.get("Name") or "GPU desconhecida")
        if melhor is None or gb > melhor[1]:
            melhor = (nome, gb)
    return melhor


def _probe_linux_gpu() -> tuple[str, float] | None:
    if platform.system() != "Linux":
        return None
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        arquivo = card / "device" / "mem_info_vram_total"
        if arquivo.exists():
            try:
                total = int(arquivo.read_text().strip())
            except (OSError, ValueError):
                continue
            nome = card.name
            vendor = card / "device" / "vendor"
            if vendor.exists():
                nome = f"{card.name} ({vendor.read_text().strip()})"
            return nome, total / (1024**3)
    return None


async def probe_ollama(base_url: str = OLLAMA_DEFAULT) -> tuple[bool, tuple[str, ...]]:
    """Ollama esta no ar, e o que ja foi baixado?

    Saber os modelos JA baixados e o que permite ao assistente de configuracao
    dizer "voce ja tem o qwen3:14b, e so usar" em vez de mandar o usuario
    baixar 9 GB de novo.
    """
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{base_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            modelos = tuple(
                str(m.get("name", "")) for m in r.json().get("models", []) if m.get("name")
            )
            return True, modelos
    except (httpx.HTTPError, ValueError, KeyError):
        return False, ()


def probe_gpu() -> tuple[str, float, bool]:
    """(nome, GB, memoria_unificada). Sincrono: so le o SO."""
    if (apple := _probe_apple()) is not None:
        return apple[0], apple[1], True
    if (nvidia := _probe_nvidia()) is not None:
        return nvidia[0], nvidia[1], False
    for sonda in (_probe_windows_gpu, _probe_linux_gpu):
        if (achado := sonda()) is not None:
            return achado[0], achado[1], False
    return "", 0.0, False


def cache_path() -> Path:
    return data_dir() / "hardware.json"


async def probe(refresh: bool = False) -> Hardware:
    """Detecta o hardware, com cache em disco.

    O estado do Ollama e reconferido SEMPRE, mesmo com cache: a GPU nao muda
    entre execucoes, mas o servico local estar no ar muda o tempo todo, e um
    cache dizendo "Ollama no ar" quando ele foi desligado mandaria o roteador
    escolher um provedor morto.
    """
    caminho = cache_path()
    base: dict[str, object] | None = None
    if not refresh and caminho.exists():
        try:
            base = json.loads(caminho.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            base = None

    if base is None:
        nome, gb, unificada = probe_gpu()
        base = {
            "gpu_name": nome,
            "vram_gb": gb,
            "unified_memory": unificada,
            "tier": _tier_for(gb),
        }
        with contextlib.suppress(OSError):
            caminho.write_text(json.dumps(base, indent=2), encoding="utf-8")

    no_ar, modelos = await probe_ollama()
    vram = _as_float(base.get("vram_gb"))
    return Hardware(
        gpu_name=str(base.get("gpu_name") or ""),
        vram_gb=vram,
        unified_memory=bool(base.get("unified_memory", False)),
        # Recalculado a partir da VRAM em vez de lido do cache. O arquivo e
        # texto num diretorio do usuario: ele pode ter sido editado a mao ou
        # ter sobrado de uma versao com outra tabela de tiers, e um tier
        # invalido aqui quebraria em KV_CACHE_RESERVE_GB muito depois, longe
        # da causa.
        tier=_tier_for(vram),
        ollama_up=no_ar,
        ollama_models=modelos,
    )


def _as_float(valor: object) -> float:
    try:
        return float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def forget() -> None:
    """Descarta o cache. O usuario trocou de placa, ou nos erramos a deteccao."""
    with contextlib.suppress(OSError):
        cache_path().unlink(missing_ok=True)


def as_dict(hw: Hardware) -> dict[str, object]:
    return asdict(hw)
