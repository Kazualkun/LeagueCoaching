"""Coleta das partidas que alimentam as estatisticas do patch (meta.py).

A semente sao as ligas de topo do servidor da pessoa (Challenger, Grao-Mestre,
Mestre): e dali que sai o que se chama de "build recomendada". De cada jogador
vem o historico ranqueado recente; de cada partida, as dez linhas.

CUSTO, que e o que limita tudo aqui: uma chave pessoal da Riot aceita 100
requisicoes a cada 2 minutos. Cada partida custa UMA (so match-v5, sem
timeline), e cada jogador mais uma pela lista. Na pratica, ~45 partidas por
minuto — 500 partidas em ~12 minutos. O limitador do RiotClient cuida do
ritmo; aqui so se decide o que pedir.

Tudo passa pelo RiotClient e pela API oficial. Nada de raspar sites de
estatistica (docs/04-knowledge-base.md, L3).
"""

from __future__ import annotations

import random
from collections.abc import Callable

from riftcoach.knowledge.meta import MetaDB, patch_de
from riftcoach.riot.client import RiotClient

LIGAS = ("challenger", "grandmaster", "master")
PARTIDAS_POR_JOGADOR = 15


def _versao(patch: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in patch.split("."))
    except ValueError:
        return (0,)


async def semear_do_cache(rc: RiotClient, meta: MetaDB) -> int:
    """Aproveita as partidas que ja estao no cache local (o historico da
    propria pessoa e de quem ela analisou). Custo zero de API."""
    novas = 0
    for chave in await rc.cache.keys("match:"):
        mid = chave.rsplit(":", 1)[-1]
        if meta.conhece(mid):
            continue
        partida = await rc.cache.get(chave)
        if isinstance(partida, dict) and meta.ingerir(partida, fonte="historico"):
            novas += 1
    return novas


async def coletar(
    rc: RiotClient,
    meta: MetaDB,
    *,
    alvo: int = 300,
    patch: str | None = None,
    ligas: tuple[str, ...] = LIGAS,
    log: Callable[[str], None] = print,
    parar: Callable[[], bool] | None = None,
) -> int:
    """Coleta ate `alvo` partidas novas do `patch` (o atual, se None).

    Devolve quantas entraram. Pode ser interrompida (`parar`) sem perder o
    que ja entrou: cada partida e gravada assim que chega.
    """
    puuids: list[tuple[str, str]] = []
    for liga in ligas:
        entradas = await rc.apex_league(liga)
        puuids += [(str(e["puuid"]), liga) for e in entradas if e.get("puuid")]
        log(f"{liga}: {len(entradas)} jogadores")
    # Embaralha para a amostra nao ser so do topo da Challenger: o 1o colocado
    # joga campeoes e horarios que nao representam o servidor.
    random.shuffle(puuids)

    novas = 0
    alvo_patch = patch
    for puuid, liga in puuids:
        if novas >= alvo or (parar is not None and parar()):
            break
        try:
            ids = await rc.match_ids(puuid, count=PARTIDAS_POR_JOGADOR, queue=420)
        except Exception as e:  # um jogador com erro nao derruba a coleta
            log(f"historico indisponivel ({e.__class__.__name__}); seguindo")
            continue
        for mid in ids:
            if novas >= alvo or (parar is not None and parar()):
                break
            if meta.conhece(mid):
                continue
            try:
                partida = await rc.match(mid, guardar=False)
            except Exception as e:
                log(f"partida {mid} indisponivel ({e.__class__.__name__}); seguindo")
                continue
            p = patch_de(str(partida.get("info", {}).get("gameVersion", "")))
            if alvo_patch is None:
                alvo_patch = p
                log(f"coletando o patch {p}")
            if _versao(p) > _versao(alvo_patch):
                continue  # mais novo que o pedido: pula e segue para tras
            if _versao(p) < _versao(alvo_patch):
                break  # historico e cronologico: dai para tras e patch velho
            if meta.ingerir(partida, fonte=liga):
                novas += 1
                if novas % 25 == 0:
                    log(f"{novas}/{alvo} partidas do patch {alvo_patch}")
    return novas
