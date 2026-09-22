"""Destilacao: match-v5 + timeline -> MatchFacts.

~237.000 tokens de JSON bruto viram ~1.100 tokens de evidencia citavel.

Este modulo NAO importa nada de LLM e nao faz rede. Se essa fronteira se
mantiver, o parser continua testavel offline e correto.

Armadilhas descobertas em dados reais (tools/dump_event_shapes.py):

  - `participantId`/`killerId` podem ser **0** (minion, torre, indefinido).
    `participants[0 - 1]` indexaria o ULTIMO jogador em silencio.
  - `WARD_PLACED`/`WARD_KILL` **nao tem posicao**. Qualquer afirmacao sobre onde
    uma ward foi colocada e impossivel a partir de telemetria.
  - Nao existe evento de recall. Recalls sao inferidos (T2).
  - `gameVersion` tem quatro componentes (`16.9.772.8292`).
  - Arena (mapId 30) traz os `challenges` zerados — rejeitamos na entrada.
"""

from __future__ import annotations

from typing import Any

from riftcoach.core.errors import ParseError, PlayerNotInMatch
from riftcoach.core.zones import MapZone, side_relative, to_zone
from riftcoach.parse.facts import (
    PARSER_VERSION,
    DeathContext,
    KillContext,
    LaneSnapshot,
    MatchFacts,
    ObjectiveEvent,
    PlayerSummary,
    RecallEvent,
    VisionSummary,
    WaveProxy,
)

SUMMONERS_RIFT = 11
FRAME_MS = 60_000
SKILL_SLOTS = {1: "Q", 2: "W", 3: "E", 4: "R"}

# Distancia abaixo da qual consideramos dois campeoes "juntos". 2000 unidades e
# aproximadamente o alcance de uma habilidade de longo alcance.
NEARBY_UNITS = 2000
# Um objetivo e "disputado" quando houve abate perto dele no TEMPO **e** no
# ESPACO. So o tempo nao serve: numa partida de 35 min com 77 abates, quase
# todo objetivo tem algum abate a 25s de distancia, e o marcador vira ruido
# constante — 26 de 28 objetivos apareciam como disputados.
CONTESTED_MS = 25_000
CONTESTED_UNITS = 2_500
# Nascimento do primeiro dragao/arauto, para a janela de objetivo nas mortes.
DRAGON_FIRST_MS = 300_000
DRAGON_RESPAWN_MS = 300_000


def mmss(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


def normalize_patch(game_version: str) -> str:
    """'16.9.772.8292' -> '16.9'.

    O DataDragon so conhece major.minor; passar a string crua nao acha nada.
    """
    parts = game_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else game_version


def _round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


class _Timeline:
    """Acesso indexado a timeline, para nao varrer os frames varias vezes."""

    def __init__(self, timeline: dict[str, Any]) -> None:
        info = timeline["info"]
        self.frames: list[dict[str, Any]] = info["frames"]
        self.events: list[dict[str, Any]] = [
            e for f in self.frames for e in f["events"]
        ]
        self.events.sort(key=lambda e: e["timestamp"])

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] in types]

    def frame_at(self, t_ms: int) -> dict[str, Any]:
        """Frame mais proximo de t_ms. Frames sao de minuto em minuto, entao
        isto e sempre uma aproximacao — toda metrica derivada dele e T2."""
        idx = min(len(self.frames) - 1, max(0, round(t_ms / FRAME_MS)))
        return self.frames[idx]

    def pframe(self, t_ms: int, pid: int) -> dict[str, Any] | None:
        pf: dict[str, Any] | None = self.frame_at(t_ms)["participantFrames"].get(
            str(pid)
        )
        return pf

    def pframe_exact(self, frame_idx: int, pid: int) -> dict[str, Any] | None:
        if not 0 <= frame_idx < len(self.frames):
            return None
        pf: dict[str, Any] | None = self.frames[frame_idx]["participantFrames"].get(
            str(pid)
        )
        return pf



def _team_diff(
    pframes: dict[str, Any],
    by_pid: dict[int, dict[str, Any]],
    team_id: int,
    field: str,
) -> int:
    """Soma do time em foco menos a do inimigo, para um campo do frame.

    Funcao de modulo em vez de closure dentro do laco: fechar sobre a variavel
    do laco e a armadilha classica em que todas as chamadas acabam enxergando
    a ULTIMA iteracao.
    """
    own = other = 0
    for k, v in pframes.items():
        valor = v.get(field, 0)
        if by_pid.get(int(k), {}).get("teamId") == team_id:
            own += valor
        else:
            other += valor
    return own - other


def _valid_pid(pid: Any) -> int | None:
    """Converte um id de participante da Riot, tratando 0 como 'ninguem'.

    Esta guarda e obrigatoria: `participants[0 - 1]` devolve o ULTIMO jogador,
    o que atribuiria abates de torre e compras orfas ao jogador errado sem
    nenhum erro visivel.
    """
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 10 else None


def _summary(p: dict[str, Any]) -> PlayerSummary:
    return PlayerSummary(
        participant_id=p["participantId"],
        puuid=p["puuid"],
        champion=p.get("championName", "?"),
        team_id=p["teamId"],
        position=p.get("teamPosition") or p.get("individualPosition") or "",
        win=bool(p.get("win")),
        kills=p.get("kills", 0),
        deaths=p.get("deaths", 0),
        assists=p.get("assists", 0),
        cs=p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
        gold=p.get("goldEarned", 0),
        damage_to_champions=p.get("totalDamageDealtToChampions", 0),
        vision_score=p.get("visionScore", 0),
        level=p.get("champLevel", 0),
        items=[p.get(f"item{i}", 0) for i in range(7)],
    )


def _find_opponent(
    focus: dict[str, Any], participants: list[dict[str, Any]], tl: _Timeline
) -> tuple[dict[str, Any] | None, str]:
    """Resolve o oponente de rota.

    `teamPosition` e o caminho feliz, mas e NOTORIAMENTE vazio ou errado em
    troca de rota, remake e partidas com desconexao. O plano B mede proximidade
    real entre os minutos 2 e 8 — quem passou mais tempo perto do jogador em
    foco e o oponente de rota, independentemente do que a Riot rotulou.
    """
    enemies = [p for p in participants if p["teamId"] != focus["teamId"]]
    pos = focus.get("teamPosition")
    if pos:
        for p in enemies:
            if p.get("teamPosition") == pos:
                return p, "team_position"

    best: tuple[int, dict[str, Any]] | None = None
    fid = focus["participantId"]
    for p in enemies:
        score = 0
        for idx in range(2, 9):
            a = tl.pframe_exact(idx, fid)
            b = tl.pframe_exact(idx, p["participantId"])
            if not a or not b or "position" not in a or "position" not in b:
                continue
            dx = a["position"]["x"] - b["position"]["x"]
            dy = a["position"]["y"] - b["position"]["y"]
            if dx * dx + dy * dy < 2500**2:
                score += 1
        if score and (best is None or score > best[0]):
            best = (score, p)
    if best:
        return best[1], "proximity"
    return None, "none"


def _wave_proxy(tl: _Timeline, pid: int, t_ms: int, team_id: int) -> WaveProxy:
    """Estado da wave inferido de ritmo de CS + posicao.

    SEMPRE T2, nunca T1: a API da Riot nao tem campo de estado de wave. Quatro
    estados grosseiros sao o limite honesto da telemetria — distinguir
    freeze/slow push/crash de verdade exige ver a contagem de minions, o que so
    a visao (Modos B/C) entrega, e ainda assim como T3.
    """
    idx = round(t_ms / FRAME_MS)
    cur = tl.pframe_exact(idx, pid)
    prev = tl.pframe_exact(idx - 1, pid)
    if not cur or "position" not in cur:
        return "UNKNOWN"

    zone = to_zone(cur["position"]["x"], cur["position"]["y"])
    rel = side_relative(zone, team_id)
    if "LANE" not in zone.name:
        return "NOT_IN_LANE"

    cs_rate = 0
    if prev:
        cs_rate = cur.get("minionsKilled", 0) - prev.get("minionsKilled", 0)

    if rel.startswith("ENEMY_"):
        return "PUSHING_TO_ENEMY" if cs_rate >= 3 else "NOT_IN_LANE"
    if rel.startswith("OWN_"):
        return "HELD_IN_OWN_HALF"
    return "HOLDING_MID"


def _objective_window(t_ms: int, objectives: list[dict[str, Any]]) -> str | None:
    """Havia um objetivo nascendo ou disponivel perto deste instante?

    Sem tracking de respawn exato (a Riot nao expoe timers), usamos o proximo
    objetivo REALMENTE tomado como ancora. E retrospectivo, mas e verdade
    medida: se um dragao caiu 38s depois da morte, o dragao estava em disputa.
    """
    for e in objectives:
        delta = e["timestamp"] - t_ms
        if 0 <= delta <= 60_000:
            kind = e.get("monsterType", "OBJETIVO")
            return f"{kind}_EM_{delta // 1000}s"
    return None


def distill(
    match: dict[str, Any], timeline: dict[str, Any], puuid: str
) -> MatchFacts:
    """Transforma match-v5 + timeline em MatchFacts para um jogador."""
    info = match["info"]

    if info.get("mapId") != SUMMONERS_RIFT:
        raise ParseError(
            f"mapId {info.get('mapId')} nao e Summoner's Rift.",
            hint=(
                "Modos como Arena (mapId 30) trazem os challenges zerados e nao "
                "tem objetivos de SR. Analisar geraria metricas falsas."
            ),
        )

    participants: list[dict[str, Any]] = info["participants"]
    focus_raw = next((p for p in participants if p["puuid"] == puuid), None)
    if focus_raw is None:
        raise PlayerNotInMatch(
            f"puuid nao participou de {match['metadata']['matchId']}."
        )

    tl = _Timeline(timeline)
    fid = focus_raw["participantId"]
    team_id = focus_raw["teamId"]
    by_pid = {p["participantId"]: p for p in participants}

    opp_raw, opp_source = _find_opponent(focus_raw, participants, tl)
    oid = opp_raw["participantId"] if opp_raw else None

    duration_s = info.get("gameDuration", 0)
    ch = focus_raw.get("challenges", {})

    # ---- series por minuto -------------------------------------------
    lane_series: list[LaneSnapshot] = []
    gold_diff_series: list[int] = []
    xp_diff_series: list[int] = []
    for idx, frame in enumerate(tl.frames):
        pf_all = frame["participantFrames"]

        gold_diff_series.append(
            _round_to(_team_diff(pf_all, by_pid, team_id, "totalGold"), 50)
        )
        xp_diff_series.append(_round_to(_team_diff(pf_all, by_pid, team_id, "xp"), 50))

        # Por minuto ate 18, depois a cada 3: decisoes de rota sao de escala de
        # minuto; late game e de escala de evento e ja esta em deaths/objectives.
        if idx > 18 and idx % 3:
            continue
        a = pf_all.get(str(fid))
        if not a:
            continue
        zone = (
            side_relative(to_zone(a["position"]["x"], a["position"]["y"]), team_id)
            if "position" in a
            else "?"
        )
        b = pf_all.get(str(oid)) if oid else None
        lane_series.append(
            LaneSnapshot(
                minute=idx,
                gd=_round_to(a.get("totalGold", 0) - b.get("totalGold", 0), 25)
                if b
                else 0,
                xpd=_round_to(a.get("xp", 0) - b.get("xp", 0), 50) if b else 0,
                csd=(
                    a.get("minionsKilled", 0)
                    + a.get("jungleMinionsKilled", 0)
                    - b.get("minionsKilled", 0)
                    - b.get("jungleMinionsKilled", 0)
                )
                if b
                else 0,
                zone=zone,
            )
        )

    def cs_at(minute: int, pid: int) -> int | None:
        pf = tl.pframe_exact(minute, pid)
        if pf is None:
            return None
        return int(pf.get("minionsKilled", 0)) + int(pf.get("jungleMinionsKilled", 0))

    def diff_at(minute: int, field: str) -> int | None:
        if oid is None:
            return None
        a, b = tl.pframe_exact(minute, fid), tl.pframe_exact(minute, oid)
        if not a or not b:
            return None
        return _round_to(a.get(field, 0) - b.get(field, 0), 25)

    # ---- mortes e abates ----------------------------------------------
    monster_kills = tl.of_type("ELITE_MONSTER_KILL")
    deaths: list[DeathContext] = []
    kills: list[KillContext] = []
    for e in tl.of_type("CHAMPION_KILL"):
        t_ms = e["timestamp"]
        killer = _valid_pid(e.get("killerId"))
        victim = _valid_pid(e.get("victimId"))
        assists = [
            p for p in (_valid_pid(a) for a in e.get("assistingParticipantIds", [])) if p
        ]
        pos = e.get("position", {})
        zone = (
            side_relative(to_zone(pos["x"], pos["y"]), team_id)
            if "x" in pos
            else MapZone.UNKNOWN.value
        )
        swing = e.get("bounty", 0) + e.get("shutdownBounty", 0)

        if victim == fid:
            pf = tl.pframe(t_ms, fid)
            near = 0
            if pf and "position" in pf:
                for other_pid, other in tl.frame_at(t_ms)["participantFrames"].items():
                    opid = int(other_pid)
                    if opid == fid or by_pid.get(opid, {}).get("teamId") != team_id:
                        continue
                    if "position" not in other:
                        continue
                    dx = other["position"]["x"] - pf["position"]["x"]
                    dy = other["position"]["y"] - pf["position"]["y"]
                    if dx * dx + dy * dy <= NEARBY_UNITS**2:
                        near += 1
            opp_pf = tl.pframe(t_ms, oid) if oid else None
            deaths.append(
                DeathContext(
                    n=len(deaths) + 1,
                    t_ms=t_ms,
                    t=mmss(t_ms),
                    zone=zone,
                    killers=[
                        by_pid[p].get("championName", "?")
                        for p in ([killer] if killer else []) + assists
                        if p in by_pid
                    ],
                    gold_swing=-swing,
                    allies_within_2000u=near,
                    objective_window=_objective_window(t_ms, monster_kills),
                    wave_proxy=_wave_proxy(tl, fid, t_ms, team_id),
                    gold_at_death=pf.get("currentGold", 0) if pf else 0,
                    level_diff_vs_opponent=(
                        (pf.get("level", 0) - opp_pf.get("level", 0))
                        if pf and opp_pf
                        else 0
                    ),
                )
            )
        elif killer == fid or fid in assists:
            kills.append(
                KillContext(
                    n=len(kills) + 1,
                    t_ms=t_ms,
                    t=mmss(t_ms),
                    zone=zone,
                    victim=by_pid[victim].get("championName", "?")
                    if victim in by_pid
                    else "?",
                    assisted=killer != fid,
                    gold_swing=swing,
                )
            )

    # ---- build (aplicando ITEM_UNDO) ----------------------------------
    purchases: list[tuple[int, int]] = []
    for e in tl.of_type("ITEM_PURCHASED"):
        if _valid_pid(e.get("participantId")) == fid:
            purchases.append((e["timestamp"], e["itemId"]))
    for e in tl.of_type("ITEM_UNDO"):
        if _valid_pid(e.get("participantId")) != fid:
            continue
        before = e.get("beforeId")
        # Um desfazer anula a compra mais recente daquele item: a compra nunca
        # aconteceu, entao manter no caminho de build mentiria sobre o gasto.
        for i in range(len(purchases) - 1, -1, -1):
            if purchases[i][1] == before and purchases[i][0] <= e["timestamp"]:
                purchases.pop(i)
                break
    build_path = [(mmss(t), item) for t, item in purchases]

    skills = "".join(
        SKILL_SLOTS.get(e.get("skillSlot", 0), "?")
        for e in tl.of_type("SKILL_LEVEL_UP")
        if _valid_pid(e.get("participantId")) == fid
    )

    # ---- recalls (inferidos — a Riot nao emite evento) -----------------
    recalls = _detect_recalls(tl, fid, team_id, deaths, purchases)

    # ---- objetivos -----------------------------------------------------
    ward_times = [
        e["timestamp"]
        for e in tl.of_type("WARD_PLACED")
        if _valid_pid(e.get("creatorId")) == fid
    ]
    objectives: list[ObjectiveEvent] = []
    # (instante, x, y) — precisamos da posicao para julgar disputa de verdade.
    kill_points: list[tuple[int, int, int]] = [
        (e["timestamp"], e["position"]["x"], e["position"]["y"])
        for e in tl.of_type("CHAMPION_KILL")
        if "position" in e
    ]

    def _contested(t_ms: int, pos: dict[str, Any]) -> bool:
        if "x" not in pos:
            return False
        for kt, kx, ky in kill_points:
            if abs(kt - t_ms) > CONTESTED_MS:
                continue
            dx, dy = kx - pos["x"], ky - pos["y"]
            if dx * dx + dy * dy <= CONTESTED_UNITS**2:
                return True
        return False
    for e in tl.of_type("ELITE_MONSTER_KILL", "BUILDING_KILL"):
        t_ms = e["timestamp"]
        pos = e.get("position", {})
        zone = to_zone(pos["x"], pos["y"]).value if "x" in pos else "?"
        pf = tl.pframe(t_ms, fid)
        if e["type"] == "ELITE_MONSTER_KILL":
            kind = e.get("monsterType", "?")
            subtype = e.get("monsterSubType")
            mine = e.get("killerTeamId") == team_id
        else:
            kind = e.get("buildingType", "?")
            subtype = e.get("towerType") or e.get("laneType")
            # teamId em BUILDING_KILL e o time DONO da construcao destruida.
            mine = e.get("teamId") != team_id
        objectives.append(
            ObjectiveEvent(
                t=mmss(t_ms),
                t_ms=t_ms,
                kind=kind,
                subtype=subtype,
                taken_by_focus_team=mine,
                contested=_contested(t_ms, pos),
                zone=zone,
                focus_player_distance_u=(
                    round(
                        (
                            (pf["position"]["x"] - pos["x"]) ** 2
                            + (pf["position"]["y"] - pos["y"]) ** 2
                        )
                        ** 0.5
                    )
                    if pf and "position" in pf and "x" in pos
                    else None
                ),
                focus_player_zone=(
                    side_relative(
                        to_zone(pf["position"]["x"], pf["position"]["y"]), team_id
                    )
                    if pf and "position" in pf
                    else "?"
                ),
                team_gold_diff_at=gold_diff_series[
                    min(len(gold_diff_series) - 1, round(t_ms / FRAME_MS))
                ]
                if gold_diff_series
                else 0,
                wards_placed_60s_before=sum(
                    1 for w in ward_times if 0 <= t_ms - w <= 60_000
                ),
            )
        )

    # ---- visao (so contagens: wards nao tem posicao) --------------------
    n_buckets = max(1, duration_s // 300 + 1)
    buckets = [0] * n_buckets
    for w in ward_times:
        buckets[min(n_buckets - 1, w // 300_000)] += 1
    vision = VisionSummary(
        wards_placed=focus_raw.get("wardsPlaced", 0),
        wards_killed=focus_raw.get("wardsKilled", 0),
        control_wards_bought=focus_raw.get("visionWardsBoughtInGame", 0),
        vision_score=focus_raw.get("visionScore", 0),
        vision_per_min=ch.get("visionScorePerMinute", 0.0),
        by_5min_bucket=buckets,
    )

    minutes = max(1.0, duration_s / 60)
    time_dead = focus_raw.get("totalTimeSpentDead", 0)

    return MatchFacts(
        match_id=match["metadata"]["matchId"],
        patch=normalize_patch(info.get("gameVersion", "")),
        game_version_raw=info.get("gameVersion", ""),
        queue_id=info.get("queueId", 0),
        map_id=info.get("mapId", 0),
        duration_s=duration_s,
        parser_version=PARSER_VERSION,
        focus=_summary(focus_raw),
        opponent=_summary(opp_raw) if opp_raw else None,
        opponent_source=opp_source,  # type: ignore[arg-type]
        team=[_summary(p) for p in participants if p["teamId"] == team_id],
        enemy=[_summary(p) for p in participants if p["teamId"] != team_id],
        cs_at_10=cs_at(10, fid) or 0,
        cs_at_14=cs_at(14, fid) or 0,
        csd_at_10=(
            (cs_at(10, fid) or 0) - (cs_at(10, oid) or 0) if oid is not None else None
        ),
        gd_at_10=diff_at(10, "totalGold"),
        gd_at_14=diff_at(14, "totalGold"),
        xpd_at_10=diff_at(10, "xp"),
        dpm=round(focus_raw.get("totalDamageDealtToChampions", 0) / minutes, 1),
        damage_share=round(ch.get("teamDamagePercentage", 0.0), 4),
        kill_participation=round(ch.get("killParticipation", 0.0), 4),
        gold_per_min=round(focus_raw.get("goldEarned", 0) / minutes, 1),
        time_dead_s=time_dead,
        time_dead_pct=round(100 * time_dead / max(1, duration_s), 1),
        plates_taken=ch.get("turretPlatesTaken", 0),
        solo_kills=ch.get("soloKills", 0),
        lane_series=lane_series,
        team_gold_diff_series=gold_diff_series,
        team_xp_diff_series=xp_diff_series,
        deaths=deaths,
        kills=kills,
        recalls=recalls,
        objectives=objectives,
        vision=vision,
        build_path=build_path,
        skill_order=">".join(skills),
        runes=[
            sel["perk"]
            for style in focus_raw.get("perks", {}).get("styles", [])
            for sel in style.get("selections", [])
        ],
        summoners=(focus_raw.get("summoner1Id", 0), focus_raw.get("summoner2Id", 0)),
    )


# Compras separadas por menos que isto pertencem a mesma visita a loja.
SHOP_CLUSTER_GAP_MS = 15_000
# Dois sinais de recall mais proximos que isto sao o mesmo recall.
RECALL_DEDUPE_MS = 45_000


def _detect_recalls(
    tl: _Timeline,
    fid: int,
    team_id: int,
    deaths: list[DeathContext],
    purchases: list[tuple[int, int]],
) -> list[RecallEvent]:
    """Infere recalls. A timeline da Riot NAO tem evento de recall.

    Dois sinais, unidos:

    1. AGRUPAMENTO DE COMPRAS (principal). So da para comprar na loja, e a loja
       fica na base — entao um grupo de `ITEM_PURCHASED` PROVA que o jogador
       estava na base naquele instante, com precisao de milissegundo. Bem mais
       forte que amostrar posicao.
    2. TRANSICAO DE POSICAO (complementar). Pega os recalls sem compra (voltar
       so para curar, ou sem ouro), que o sinal 1 nao ve.

    O sinal 1 sozinho ja corrige a subcontagem grave da deteccao so por posicao:
    frames sao de 60 em 60 segundos, entao um recall que vai e volta dentro de
    um minuto era invisivel.

    Continua sendo T2: 'estava na loja' nao e literalmente 'deu recall'
    (teleporte e morte tambem levam a base), mas mortes sao descontadas e o
    restante e recall com altissima probabilidade.
    """
    base = {MapZone.BLUE_BASE, MapZone.RED_BASE}
    death_times = {d.t_ms for d in deaths}

    def in_base(idx: int) -> bool | None:
        pf = tl.pframe_exact(idx, fid)
        if not pf or "position" not in pf:
            return None
        return to_zone(pf["position"]["x"], pf["position"]["y"]) in base

    # Nao existe recall antes de o jogador ter SAIDO da base pela primeira vez.
    # A compra inicial nem sempre acontece em t=0 — dados reais mostram compras
    # aos 10-12s, ainda na fase inicial. Filtrar so por t==0 contava isso como
    # recall e poluia toda a analise de economia da fase de rota.
    #
    # O frame 0 e IGNORADO de proposito. Em t=0 o jogador esta no spawn por
    # definicao, mas nos dados reais 4 dos 10 ja aparecem fora do raio da base
    # nesse frame: eles comecam a andar imediatamente. Confiar no frame 0
    # colocaria first_exit em 0 e deixaria a compra inicial passar. E seguro
    # comecar do frame 1: um recall de verdade antes dos 60s nao existe, ja que
    # os minions so nascem aos 65s.
    first_exit: int | None = None
    for idx in range(1, len(tl.frames)):
        if in_base(idx) is False:
            first_exit = tl.frames[idx]["timestamp"]
            break
    if first_exit is None:
        return []  # nunca saiu da base (remake, AFK) -> nenhum recall

    # --- sinal 1: agrupamento de compras ---
    candidates: list[int] = []
    cluster_start: int | None = None
    prev_t: int | None = None
    for t, _item in sorted(purchases):
        if t <= first_exit:
            continue
        if prev_t is None or t - prev_t > SHOP_CLUSTER_GAP_MS:
            if cluster_start is not None:
                candidates.append(cluster_start)
            cluster_start = t
        prev_t = t
    if cluster_start is not None:
        candidates.append(cluster_start)

    # --- sinal 2: transicoes de posicao para a base ---

    for idx in range(1, len(tl.frames)):
        if in_base(idx - 1) is False and in_base(idx) is True:
            candidates.append(tl.frames[idx]["timestamp"])

    # --- unir: ordena e funde sinais proximos ---
    out: list[RecallEvent] = []
    last_t = -RECALL_DEDUPE_MS
    for t_ms in sorted(candidates):
        if t_ms - last_t < RECALL_DEDUPE_MS:
            continue
        last_t = t_ms

        # Morte leva a base sem ser recall. Marcamos como forcado em vez de
        # descartar: 'voltou porque morreu' tambem e informacao de tempo.
        forced = any(0 <= t_ms - d <= 25_000 for d in death_times)
        pf = tl.pframe(t_ms, fid)
        bought = [
            item
            for t, item in purchases
            if t_ms <= t <= t_ms + SHOP_CLUSTER_GAP_MS
        ]
        back_at: float | None = None
        start = round(t_ms / FRAME_MS)
        for j in range(start + 1, min(start + 4, len(tl.frames))):
            if in_base(j) is False:
                back_at = (tl.frames[j]["timestamp"] - t_ms) / 1000
                break

        out.append(
            RecallEvent(
                t=mmss(t_ms),
                t_ms=t_ms,
                gold_on_back=pf.get("currentGold", 0) if pf else 0,
                bought=bought,
                wave_proxy_before=_wave_proxy(tl, fid, t_ms - FRAME_MS, team_id),
                seconds_to_return=back_at,
                was_forced=forced,
            )
        )
    return out
