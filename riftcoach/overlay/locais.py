"""Onde cada marcacao aconteceu, no mapa.

Existe porque o minimapa so vale a pena se o que ele mostra for VERDADE. A
primeira versao desenhava um anel branco na posicao do jogador interpolada
entre frames — e a Riot so entrega uma posicao por MINUTO. Entre dois frames
um campeao atravessa meio mapa, entao aquele anel discordava visivelmente do
icone que o proprio jogo desenha. Um overlay que contradiz o jogo perde a
confianca de quem olha, e com razao.

A saida nao e desenhar melhor, e desenhar outra coisa: o que a timeline sabe
COM EXATIDAO. Morte, abate, objetivo e estrutura carregam `position` no
proprio evento. Essas sao as marcacoes que ganham lugar no mapa; as outras
ficam sem, e o cartao continua funcionando igual.

A posicao do JOGADOR continua sendo aproximada, e continua sendo util: ela
responde "onde eu estava quando isso aconteceu do outro lado do mapa?", que e
uma pergunta de macro, medida em milhares de unidades. Para essa pergunta um
erro de algumas centenas nao muda a resposta — mas quem desenha precisa
mostra-la como aproximada, e nao como um ponto.
"""

from __future__ import annotations

from typing import Any

# Eventos que carregam uma posicao que interessa a uma marcacao.
EVENTOS_COM_LUGAR = frozenset(
    {
        "CHAMPION_KILL",
        "ELITE_MONSTER_KILL",
        "BUILDING_KILL",
        "TURRET_PLATE_DESTROYED",
    }
)

# Quanto um evento pode estar distante do instante da marcacao e ainda ser
# considerado o mesmo acontecimento. Generoso o bastante para cobrir o atraso
# entre a decisao e o desfecho; apertado o bastante para nao pescar a
# teamfight seguinte.
JANELA_MS = 6_000


def _ponto(e: dict[str, Any]) -> tuple[float, float] | None:
    p = e.get("position")
    if not isinstance(p, dict):
        return None
    try:
        return float(p["x"]), float(p["y"])
    except (KeyError, TypeError, ValueError):
        return None


def local_do_evento(timeline: dict[str, Any], t_ms: int) -> tuple[float, float] | None:
    """O lugar do evento mais proximo de `t_ms`, se houver um por perto.

    Sem candidato dentro da janela devolve None — e None e uma resposta
    legitima aqui. Marcacao sem lugar simplesmente nao aparece no mapa.
    """
    melhor: tuple[int, tuple[float, float]] | None = None
    for frame in timeline.get("info", {}).get("frames", []):
        for e in frame.get("events", []):
            if e.get("type") not in EVENTOS_COM_LUGAR:
                continue
            quando = int(e.get("timestamp", 0))
            dist = abs(quando - t_ms)
            if dist > JANELA_MS:
                continue
            pos = _ponto(e)
            if pos is None:
                continue
            if melhor is None or dist < melhor[0]:
                melhor = (dist, pos)
    return melhor[1] if melhor else None


def local_do_jogador(
    track: list[tuple[int, float, float]], t_ms: int
) -> tuple[float, float] | None:
    """Onde o jogador estava, interpolado entre frames.

    Aproximado por construcao — ver o cabecalho do modulo. Quem desenha isto
    desenha uma area, nao um ponto.
    """
    if not track:
        return None
    if t_ms <= track[0][0]:
        return track[0][1], track[0][2]
    if t_ms >= track[-1][0]:
        return track[-1][1], track[-1][2]
    for i in range(len(track) - 1):
        ta, xa, ya = track[i]
        tb, xb, yb = track[i + 1]
        if ta <= t_ms <= tb:
            f = (t_ms - ta) / (tb - ta) if tb > ta else 0.0
            return xa + f * (xb - xa), ya + f * (yb - ya)
    return None
