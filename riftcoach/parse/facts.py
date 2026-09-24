"""A representacao intermediaria `MatchFacts`.

Saida da camada de destilacao, entrada de tudo que vem depois. Contem apenas
estrutura — nenhum nome de item ou campeao resolvido, porque isso depende do
patch e pertence a camada de conhecimento (docs/04-knowledge-base.md). Ids
crus aqui, nomes na renderizacao.

Toda metrica derivada e calculada em Python, nunca pelo modelo.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Incrementar quando a destilacao mudar de forma que invalide resultados
# anteriores. Entra na chave de cache e no CoachingReport, para que o usuario
# consiga explicar por que o relatorio de ontem difere do de hoje.
# v2: posicao por ancoras de evento (parse/posicoes.py) em vez do frame de
# minuto mais proximo; participacao, numeros e junglers nos objetivos.
PARSER_VERSION = 2

WaveProxy = Literal["PUSHING_TO_ENEMY", "HOLDING_MID", "HELD_IN_OWN_HALF", "NOT_IN_LANE", "UNKNOWN"]


class PlayerSummary(BaseModel):
    participant_id: int
    puuid: str
    champion: str
    team_id: int
    position: str  # TOP / JUNGLE / MIDDLE / BOTTOM / UTILITY
    win: bool
    kills: int
    deaths: int
    assists: int
    cs: int
    gold: int
    damage_to_champions: int
    vision_score: int
    level: int
    items: list[int]
    # Id numerico: o nome em portugues sai do DataDragon por ele. O
    # `champion` acima e o identificador em ingles do match-v5.
    champion_id: int = 0


class TeamProfile(BaseModel):
    """O que um time fez de dano, cura e controle — para ler a composicao.

    Medido no fim da partida (match-v5), nao estimado: "o time inimigo deu 70%
    de dano fisico" e fato, e e o que decide se armadura rendia mais que
    resistencia magica, ou se faltou anti-cura.
    """

    physical_share: float
    magic_share: float
    true_share: float
    heal_total: int
    cc_seconds: int
    # (campeao, cura) dos que mais curaram, maior primeiro.
    top_healers: list[tuple[str, int]] = Field(default_factory=list)


class LaneSnapshot(BaseModel):
    """Deltas por minuto entre foco e oponente.

    Deltas, nunca absolutos: o modelo se importa com a diferenca, e deltas
    comprimem muito melhor (inteiros pequenos, muitos zeros).
    """

    minute: int
    gd: int  # diferenca de ouro, arredondada para multiplos de 25
    xpd: int  # diferenca de xp, arredondada para multiplos de 50
    csd: int
    zone: str  # zona do jogador em foco, relativa ao lado


class DeathContext(BaseModel):
    """Uma morte, com todo o contexto tatico ja calculado.

    Estrutura de maior sinal-por-token do sistema: ~35 tokens por morte, e cada
    uma diretamente treinavel.
    """

    n: int
    t_ms: int
    t: str  # "14:22"
    zone: str  # relativo ao lado: OWN_/ENEMY_
    killers: list[str]  # nomes de campeao
    gold_swing: int  # bounty entregue + shutdown
    allies_within_2000u: int  # do participantFrame mais proximo (T2)
    objective_window: str | None  # "DRAKE_IN_38s" | "BARON_UP" | None
    wave_proxy: WaveProxy  # T2
    gold_at_death: int  # morreu segurando ouro nao gasto?
    level_diff_vs_opponent: int
    wards_placed_60s_before: int = 0
    # O proximo objetivo que caiu depois da morte, de forma estruturada: as
    # regras decidem pelo PAPEL do jogador se ele importa (a ADC nao responde
    # pelo Arauto). `objective_window` continua existindo, agora legivel.
    next_objective_kind: str | None = None
    next_objective_in_s: int | None = None
    # Quantos vivos em cada time no instante da morte (T2: tempo de morte
    # estimado pelo nivel e pelo minuto da partida).
    allies_alive: int | None = None
    enemies_alive: int | None = None


class KillContext(BaseModel):
    n: int
    t_ms: int
    t: str
    zone: str
    victim: str
    assisted: bool
    gold_swing: int


class RecallEvent(BaseModel):
    """A Riot NAO emite evento de recall.

    Isto e inferido de transicoes de posicao para a base sem morte associada —
    sempre T2, nunca T1. Ver `distill._detect_recalls`.
    """

    t: str
    t_ms: int
    gold_on_back: int
    bought: list[int]  # itemIds; nomes vem da camada de conhecimento
    wave_proxy_before: WaveProxy
    seconds_to_return: float | None  # tempo ate voltar a rota (T2)
    was_forced: bool  # precedido por morte


class ObjectivePresence(BaseModel):
    """Quem estava no objetivo — medido pelo evento e pelas ancoras de posicao.

    Existe porque a versao anterior julgava "voce estava longe" pelo frame de
    minuto mais proximo, e dizia isso do Barao em que o jogador tinha
    ASSISTENCIA. O evento da Riot diz quem participou; isso e T1.
    """

    # Golpe final ou assistencia no evento: T1, a Riot diz quem participou.
    focus_participated: bool = False
    # Raio de duvida de `focus_player_distance_u`, em unidades
    # (parse/posicoes.py). Zero quando participou.
    focus_distance_err_u: int | None = None
    focus_dead: bool = False
    focus_respawn_in_s: float | None = None
    # Quando a distancia e incerta demais: onde foi visto por ultimo, e quando.
    focus_last_seen_zone: str | None = None
    focus_last_seen_s_before: int | None = None
    # Vivos 15 s antes do golpe final: a luta pelo objetivo vem antes dele.
    allies_alive: int = 5
    enemies_alive: int = 5
    # Campeoes que participaram (golpe final + assistencias) do time que pegou.
    participants: list[str] = Field(default_factory=list)
    own_jungler_participated: bool | None = None
    enemy_jungler_participated: bool | None = None
    enemy_jungler_distance_u: int | None = None
    enemy_jungler_distance_err_u: int | None = None


class ObjectiveEvent(BaseModel):
    t: str
    t_ms: int
    kind: str  # DRAGON / BARON_NASHOR / RIFTHERALD / HORDE / TOWER / INHIBITOR
    subtype: str | None  # elemento do dragao, tipo de torre
    taken_by_focus_team: bool
    contested: bool  # algum abate de campeao em +/- 25 s
    zone: str
    focus_player_zone: str  # onde o jogador ESTAVA quando isso aconteceu
    # Distancia do jogador ate o objetivo, em unidades do mapa. E o que
    # separa 'estava na luta' de 'estava do outro lado do mapa' — a zona
    # sozinha nao serve, porque NEUTRAL_MID_LANE nao diz se voce estava a
    # 1.000 ou a 9.000 unidades do pit. None quando a posicao nao foi lida.
    focus_player_distance_u: int | None = None
    team_gold_diff_at: int
    wards_placed_60s_before: int  # SO contagem — a Riot nao da posicao de ward
    focus_wave_proxy: WaveProxy = "UNKNOWN"
    # Distancia com o raio de duvida, e quem estava la. So nos objetivos
    # epicos (dragao, barao, arauto, larvas); torre e inibidor ficam sem.
    presence: ObjectivePresence | None = None


class VisionSummary(BaseModel):
    """Visao, com a limitacao declarada.

    `WARD_PLACED` e `WARD_KILL` na timeline NAO carregam posicao — so tipo e
    instante. Qualquer afirmacao sobre ONDE uma ward foi colocada e impossivel a
    partir de telemetria; so o modo de visao (B/C) pode inferir isso, e ainda
    assim como T3. Contagens temporais sao tudo o que existe aqui.
    """

    wards_placed: int
    wards_killed: int
    control_wards_bought: int
    vision_score: float
    vision_per_min: float
    by_5min_bucket: list[int]  # wards colocadas por bloco de 5 min


class MatchFacts(BaseModel):
    # --- identidade ---
    match_id: str
    patch: str  # normalizado para major.minor
    game_version_raw: str
    queue_id: int
    map_id: int
    duration_s: int
    parser_version: int = PARSER_VERSION

    # --- jogadores ---
    focus: PlayerSummary
    opponent: PlayerSummary | None  # None em troca de rota nao resolvida
    opponent_source: Literal["team_position", "proximity", "none"]
    team: list[PlayerSummary]
    enemy: list[PlayerSummary]

    # --- metricas derivadas (todas T1/T2, calculadas em Python) ---
    cs_at_10: int
    cs_at_14: int
    csd_at_10: int | None
    gd_at_10: int | None
    gd_at_14: int | None
    xpd_at_10: int | None
    dpm: float
    damage_share: float
    kill_participation: float
    gold_per_min: float
    time_dead_s: int
    time_dead_pct: float
    plates_taken: int
    solo_kills: int

    # --- series ---
    lane_series: list[LaneSnapshot] = Field(default_factory=list)
    team_gold_diff_series: list[int] = Field(default_factory=list)
    # XP de time: alimenta o termo de nivel do motor de vantagem. Ouro e XP
    # sao correlacionados, mas nao identicos — um time pode estar atras em
    # ouro e a frente em nivel depois de uma wave grande.
    team_xp_diff_series: list[int] = Field(default_factory=list)

    # --- eventos ---
    deaths: list[DeathContext] = Field(default_factory=list)
    kills: list[KillContext] = Field(default_factory=list)
    recalls: list[RecallEvent] = Field(default_factory=list)
    objectives: list[ObjectiveEvent] = Field(default_factory=list)
    vision: VisionSummary

    # --- build ---
    build_path: list[tuple[str, int]] = Field(default_factory=list)  # ("06:12", itemId)
    skill_order: str = ""  # "Q>W>E>Q>Q>R>..."
    runes: list[int] = Field(default_factory=list)
    # (arvore principal, arvore secundaria) — a runa-chave e runes[0].
    rune_styles: tuple[int, int] = (0, 0)
    ally_profile: TeamProfile | None = None
    enemy_profile: TeamProfile | None = None
    summoners: tuple[int, int] = (0, 0)
