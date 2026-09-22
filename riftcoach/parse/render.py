"""MatchFacts -> o formato de texto compacto que vai para o modelo.

NAO mandamos JSON. JSON gasta ~40% dos tokens com chaves, aspas e nomes de
campo repetidos — e paga esse custo em TODA linha. Cabecalho declarado uma vez
e linhas posicionais custam uma fracao disso.

Duas regras que valem mais que qualquer micro-otimizacao:

1. Cada analista recebe so as SECOES de que precisa. O analista de rota nao
   ganha nada vendo a lista de objetivos, e prompts curtos de objetivo unico
   sao exatamente onde modelos pequenos empatam com os grandes.
2. Ids de item/runa sao resolvidos por um `NameResolver` opcional, injetado
   pela camada de conhecimento (etapa 2). Este modulo continua independente de
   patch — ele nao sabe o que e um "Companheiro de Luden".
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from riftcoach.parse.facts import MatchFacts

# Nomes de fila que realmente aparecem. Ids desconhecidos caem no proprio
# numero em vez de virar "?" — o numero pelo menos e pesquisavel.
QUEUE_NAMES = {
    400: "NORMAL DRAFT",
    420: "RANKED SOLO",
    430: "NORMAL BLIND",
    440: "RANKED FLEX",
    490: "QUICKPLAY",
    450: "ARAM",
}

POSITION_PT = {
    "TOP": "TOP",
    "JUNGLE": "SELVA",
    "MIDDLE": "MEIO",
    "BOTTOM": "ADC",
    "UTILITY": "SUP",
    "": "?",
}

# Zonas viram uma letra na tabela por minuto. Uma coluna de
# "NEUTRAL_MID_LANE" por minuto custaria mais tokens que a serie inteira.
ZONE_LETTER = [
    ("OWN_", "P"),  # Propria metade
    ("ENEMY_", "I"),  # metade Inimiga
    ("NEUTRAL_", "N"),  # Neutro (entre as torres externas)
]
ZONE_LEGEND = "P=sua metade I=metade inimiga N=meio da rota J=selva R=rio B=base"


class NameResolver(Protocol):
    """Implementado pela camada de conhecimento (etapa 2, DataDragon)."""

    def item(self, item_id: int) -> str: ...
    def rune(self, perk_id: int) -> str: ...
    def summoner(self, spell_id: int) -> str: ...


class Section(StrEnum):
    HEADER = "header"
    LANE = "lane"
    DEATHS = "deaths"
    KILLS = "kills"
    RECALLS = "recalls"
    OBJECTIVES = "objectives"
    BUILD = "build"
    VISION = "vision"
    TEMPO = "tempo"


# Fatia de cada analista. Isto e o que mantem cada passe em ~1,2k tokens.
ANALYST_SECTIONS: dict[str, tuple[Section, ...]] = {
    "laning": (Section.HEADER, Section.LANE, Section.DEATHS, Section.RECALLS),
    "macro": (Section.HEADER, Section.OBJECTIVES, Section.TEMPO, Section.VISION),
    "economy": (Section.HEADER, Section.RECALLS, Section.BUILD, Section.LANE),
    "fights": (Section.HEADER, Section.DEATHS, Section.KILLS, Section.TEMPO),
    "head_coach": (Section.HEADER,),
}


def _zone_letter(zone: str) -> str:
    for prefix, letter in ZONE_LETTER:
        if zone.startswith(prefix):
            return "J" if "JUNGLE" in zone else ("B" if "BASE" in zone else letter)
    if "RIVER" in zone:
        return "R"
    if "PIT" in zone:
        return "R"
    return "?"


def _ids(values: list[int], resolver: NameResolver | None, kind: str) -> list[str]:
    """Sem resolver, os ids crus sao mantidos.

    Nunca inventamos um nome: um id nao resolvido e informacao ausente, e o
    modelo precisa ver isso como ausente em vez de receber um palpite.
    """
    if resolver is None:
        return [str(v) for v in values]
    fn = getattr(resolver, kind)
    return [fn(v) or str(v) for v in values]


def _header(f: MatchFacts, resolver: NameResolver | None) -> list[str]:
    queue = QUEUE_NAMES.get(f.queue_id, str(f.queue_id))
    result = "VITORIA" if f.focus.win else "DERROTA"
    dur = f"{f.duration_s // 60}:{f.duration_s % 60:02d}"
    p = f.focus
    lines = [
        f"=== {f.match_id} · patch {f.patch} · {queue} · {dur} · {result} ===",
        f"VOCE {p.champion} {POSITION_PT.get(p.position, p.position)} "
        f"{p.kills}/{p.deaths}/{p.assists} {p.cs}cs "
        f"{f.dpm:.0f}dpm dano={100 * f.damage_share:.0f}% "
        f"kp={100 * f.kill_participation:.0f}% "
        f"visao={f.vision.vision_per_min:.1f}/min morto={f.time_dead_pct:.0f}%",
    ]
    if f.opponent:
        o = f.opponent
        origem = "" if f.opponent_source == "team_position" else " (por proximidade)"
        lines.append(
            f"ADV  {o.champion} {POSITION_PT.get(o.position, o.position)} "
            f"{o.kills}/{o.deaths}/{o.assists} {o.cs}cs{origem}"
        )
    else:
        lines.append("ADV  nao identificado (troca de rota?)")
    return lines


def _lane(f: MatchFacts) -> list[str]:
    if not f.opponent or not f.lane_series:
        return []
    # Fase de rota apenas. Depois do minuto 18 a decisao e de escala de evento e
    # ja esta em MORTES/OBJETIVOS — repetir aqui seria pagar duas vezes.
    rows = [s for s in f.lane_series if s.minute <= 18]
    if not rows:
        return []

    def row(label: str, values: list[str]) -> str:
        return label.ljust(5) + "".join(v.rjust(6) for v in values)

    out = [
        "",
        f"ROTA vs {f.opponent.champion} — diferencas por minuto (voce menos ele)",
        row("min", [str(s.minute) for s in rows]),
        row("ouro", [str(s.gd) for s in rows]),
        row("xp", [str(s.xpd) for s in rows]),
        row("cs", [str(s.csd) for s in rows]),
        row("onde", [_zone_letter(s.zone) for s in rows]),
        ZONE_LEGEND,
    ]
    marcos = []
    if f.cs_at_10:
        marcos.append(f"cs@10={f.cs_at_10}")
    if f.cs_at_14:
        marcos.append(f"cs@14={f.cs_at_14}")
    for label, v in (("gd@10", f.gd_at_10), ("gd@14", f.gd_at_14), ("xpd@10", f.xpd_at_10)):
        # None significa "a partida nao chegou la", nao "empatados". Dizer 0
        # afirmaria um empate que nunca foi medido.
        marcos.append(f"{label}={v}" if v is not None else f"{label}=N/D")
    out.append(" ".join(marcos))
    return out


def _deaths(f: MatchFacts) -> list[str]:
    if not f.deaths:
        return ["", "MORTES (0)"]
    out = ["", f"MORTES ({len(f.deaths)})"]
    for d in f.deaths:
        bits = [
            f"D{d.n}",
            d.t,
            d.zone,
            f"por={'+'.join(d.killers) or '?'}",
            f"swing={d.gold_swing}",
            f"aliados={d.allies_within_2000u}",
            f"wave={d.wave_proxy}",
        ]
        if d.gold_at_death >= 500:
            bits.append(f"ouro_parado={d.gold_at_death}")
        if d.level_diff_vs_opponent:
            bits.append(f"lvl={d.level_diff_vs_opponent:+d}")
        if d.objective_window:
            bits.append(f"obj={d.objective_window}")
        out.append(" ".join(bits))
    return out


def _kills(f: MatchFacts) -> list[str]:
    if not f.kills:
        return ["", "ABATES (0)"]
    out = ["", f"ABATES/ASSIST ({len(f.kills)})"]
    for k in f.kills:
        tipo = "assist" if k.assisted else "abate"
        out.append(f"K{k.n} {k.t} {k.zone} {tipo}={k.victim} swing=+{k.gold_swing}")
    return out


def _recalls(f: MatchFacts, resolver: NameResolver | None) -> list[str]:
    if not f.recalls:
        return ["", "RECALLS (0)"]
    out = ["", f"RECALLS ({len(f.recalls)}) — inferidos de compras+posicao (T2)"]
    for i, r in enumerate(f.recalls, 1):
        bits = [f"R{i}", r.t, f"ouro={r.gold_on_back}"]
        if r.bought:
            bits.append(f"comprou=[{','.join(_ids(r.bought, resolver, 'item'))}]")
        bits.append(f"wave_antes={r.wave_proxy_before}")
        if r.seconds_to_return is not None:
            bits.append(f"fora={r.seconds_to_return:.0f}s")
        if r.was_forced:
            bits.append("forcado=morte")
        out.append(" ".join(bits))
    return out


def _objectives(f: MatchFacts) -> list[str]:
    """Filtra o ruido.

    A lista crua tem 20-38 entradas (toda torre dos dois times). Torre externa
    sem briga em volta nao ensina nada; monstro epico e inibidor sempre ensinam.
    """
    keep = [
        o
        for o in f.objectives
        if o.kind in ("DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE")
        or (o.subtype or "") in ("INHIBITOR_BUILDING", "NEXUS_TURRET")
        or o.contested
    ]
    if not keep:
        return ["", "OBJETIVOS (0)"]
    out = ["", f"OBJETIVOS ({len(keep)} de {len(f.objectives)} — filtrados)"]
    for o in keep:
        quem = "NOSSO" if o.taken_by_focus_team else "DELES"
        bits = [o.t, f"{o.kind}{'/' + o.subtype if o.subtype else ''}", quem]
        if o.contested:
            bits.append("disputado")
        bits.append(f"voce={o.focus_player_zone}")
        bits.append(f"ouro_time={o.team_gold_diff_at:+d}")
        if o.wards_placed_60s_before:
            bits.append(f"suas_wards_60s={o.wards_placed_60s_before}")
        out.append(" ".join(bits))
    return out


def _tempo(f: MatchFacts) -> list[str]:
    if not f.team_gold_diff_series:
        return []
    serie = f.team_gold_diff_series
    # Uma amostra a cada 2 minutos basta para ver a forma da curva; por minuto
    # dobra o custo sem mudar nenhuma conclusao.
    amostra = serie[::2]
    pico = max(serie)
    vale = min(serie)
    return [
        "",
        "TEMPO — diferenca de ouro do TIME, a cada 2 min",
        " ".join(str(v) for v in amostra),
        f"pico={pico:+d} vale={vale:+d} final={serie[-1]:+d}",
    ]


def _vision(f: MatchFacts) -> list[str]:
    v = f.vision
    return [
        "",
        f"VISAO score={v.vision_score:.0f} ({v.vision_per_min:.2f}/min) "
        f"wards={v.wards_placed} destruidas={v.wards_killed} "
        f"controle_compradas={v.control_wards_bought}",
        f"wards por bloco de 5min: {' '.join(str(b) for b in v.by_5min_bucket)}",
        "NOTA: a telemetria da Riot nao informa ONDE uma ward foi colocada — so quantas e quando.",
    ]


def _build(f: MatchFacts, resolver: NameResolver | None) -> list[str]:
    out = [""]
    if f.build_path:
        itens = [
            f"{t} {n}"
            for (t, i), n in zip(
                f.build_path,
                _ids([i for _, i in f.build_path], resolver, "item"),
                strict=True,
            )
        ]
        out.append("BUILD " + " | ".join(itens))
    if f.skill_order:
        out.append("SKILLS " + f.skill_order)
    if f.runes:
        out.append("RUNAS " + " ".join(_ids(f.runes, resolver, "rune")))
    if any(f.summoners):
        out.append("SUMMONERS " + " ".join(_ids(list(f.summoners), resolver, "summoner")))
    return out


_RENDERERS = {
    Section.HEADER: lambda f, r: _header(f, r),
    Section.LANE: lambda f, r: _lane(f),
    Section.DEATHS: lambda f, r: _deaths(f),
    Section.KILLS: lambda f, r: _kills(f),
    Section.RECALLS: lambda f, r: _recalls(f, r),
    Section.OBJECTIVES: lambda f, r: _objectives(f),
    Section.TEMPO: lambda f, r: _tempo(f),
    Section.VISION: lambda f, r: _vision(f),
    Section.BUILD: lambda f, r: _build(f, r),
}


def render(
    facts: MatchFacts,
    sections: tuple[Section, ...] | None = None,
    resolver: NameResolver | None = None,
) -> str:
    """Renderiza MatchFacts no formato compacto.

    `sections=None` gera o relatorio completo (para a UI e para o modo L5 sem
    IA). Os analistas passam a propria fatia, via ANALYST_SECTIONS.
    """
    wanted = sections if sections is not None else tuple(Section)
    out: list[str] = []
    for s in wanted:
        if (fn := _RENDERERS.get(s)) is not None:
            out.extend(fn(facts, resolver))
    return "\n".join(out).strip() + "\n"


def render_for_analyst(
    facts: MatchFacts, analyst: str, resolver: NameResolver | None = None
) -> str:
    return render(facts, ANALYST_SECTIONS.get(analyst), resolver)


def estimate_tokens(text: str) -> int:
    """Estimativa grosseira (~4 caracteres por token).

    NAO e uma contagem real: tokenizadores diferem entre modelos e nenhum deles
    e 4 chars/token exatos. Serve para acompanhar tendencia e travar regressao
    de tamanho, nao para planejar orcamento no limite.
    """
    return len(text) // 4
