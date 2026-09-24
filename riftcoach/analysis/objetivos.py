"""Revisao dos objetivos epicos — com o PAPEL do jogador no centro.

A versao anterior era uma lista de campos crus por evento:

    objetivo · HORDE · 8:17 · inimigo
    Pode lutar: voce estava longe para contestar · +1800 ouro para seu time
    Visao: ... · Wave: HOLDING_MID · seu jogador nao tinha Smite · Numeros:
    nao disponivel · Jungler: nao disponivel

Quatro problemas, e todos reclamados por quem usou:

  1. Nao olhava o papel. "Nao tinha Smite" para uma ADC; "estava longe" das
     Vastilarvas para quem joga na rota de baixo — que nao tinha como estar.
  2. Errava a posicao: frame de minuto mais proximo. Dizia "longe" do Barao
     em que o jogador tinha ASSISTENCIA.
  3. Repetia: tres Vastilarvas, tres entradas iguais.
  4. Falava em codigo: HORDE, HOLDING_MID, "+1800 ouro para seu time" (que
     parecia o premio do objetivo, e era a vantagem do time naquele minuto).

Agora cada objetivo responde, em portugues, as perguntas que um coach faz:
era seu? voce estava la? dava para lutar (quantos vivos de cada lado)? o
jungler deles estava? — e fecha com UM veredito.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from riftcoach.core.zones import objetivo_legivel, zona_legivel
from riftcoach.parse.distill import mmss
from riftcoach.parse.facts import MatchFacts, ObjectiveEvent

EPICOS = frozenset({"DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE", "ATAKHAN"})

# Fim da fase de rotas, para efeito de "quem deveria estar la". Grosseiro de
# proposito, como as fases de rules.py.
FIM_DAS_ROTAS_MS = 14 * 60_000

# Distancia a partir da qual "estava la" deixa de valer. Com o raio de duvida:
# so se afirma LONGE quando ate o melhor caso fica alem disto, e PERTO quando
# ate o pior caso fica aquem.
PERTO_U = 3_500

# Vastilarvas nascem juntas; abates com menos que isto de diferenca sao a
# mesma disputa.
MESMA_DISPUTA_MS = 120_000

Papel = Literal["principal", "secundario", "fora"]
Tom = Literal["bom", "ruim", "neutro", "incerto"]

PAPEL_LEGIVEL = {
    "TOP": "topo",
    "JUNGLE": "caçador",
    "MIDDLE": "meio",
    "BOTTOM": "atirador",
    "UTILITY": "suporte",
}


def papel_no_objetivo(role: str, kind: str, t_ms: int, subtype: str | None = None) -> Papel:
    """De quem e este objetivo, pelo papel e pela fase da partida.

    Nao e regra do jogo, e o consenso de coaching: na fase de rotas o dragao
    e da rota de baixo e do caçador, o lado de cima (larvas, arauto) e do
    topo e do caçador, e o meio ajuda os dois. Depois das rotas, e no Barao,
    no Atakhan e no Ancião, e de todo mundo.
    """
    if kind in ("BARON_NASHOR", "ATAKHAN") or subtype == "ELDER_DRAGON":
        return "principal"
    if role == "JUNGLE":
        return "principal"
    rotas = t_ms < FIM_DAS_ROTAS_MS
    if kind == "DRAGON":
        if role in ("BOTTOM", "UTILITY"):
            return "principal"
        if role == "MIDDLE":
            return "secundario" if rotas else "principal"
        return "fora" if rotas else "secundario"  # TOP
    if kind in ("HORDE", "RIFTHERALD"):
        if role == "TOP":
            return "principal"
        if role == "MIDDLE":
            return "secundario" if rotas else "principal"
        if role == "UTILITY":
            return "secundario"
        return "fora" if rotas else "secundario"  # BOTTOM
    return "secundario"


@dataclass(frozen=True)
class Item:
    rotulo: str
    texto: str
    tom: Tom = "neutro"


@dataclass
class RevisaoDeObjetivo:
    t_ms: int
    nome: str
    seu_time: bool
    papel: Papel
    veredito: str
    tom: Tom
    itens: list[Item] = field(default_factory=list)

    @property
    def t(self) -> str:
        return mmss(self.t_ms)


def _agrupar(objetivos: list[ObjectiveEvent]) -> list[list[ObjectiveEvent]]:
    """Larvas seguidas viram UMA disputa; o resto fica um por um."""
    grupos: list[list[ObjectiveEvent]] = []
    for o in objetivos:
        anterior = grupos[-1] if grupos else None
        if (
            anterior is not None
            and o.kind == "HORDE"
            and anterior[-1].kind == "HORDE"
            and o.t_ms - anterior[-1].t_ms <= MESMA_DISPUTA_MS
        ):
            anterior.append(o)
        else:
            grupos.append([o])
    return grupos


def _onde_voce(o: ObjectiveEvent) -> tuple[str, Tom, str]:
    """(texto, tom, situacao) sobre o jogador neste objetivo.

    situacao: participou | morto | perto | longe | meio | incerto
    """
    p = o.presence
    if p is None:
        return "sem dados de presença", "incerto", "incerto"
    if p.focus_participated:
        return "participou (golpe final ou assistência)", "bom", "participou"
    if p.focus_dead:
        extra = f" — renascia em {p.focus_respawn_in_s:.0f}s" if p.focus_respawn_in_s else ""
        return f"estava morto{extra}", "ruim", "morto"
    d, e = o.focus_player_distance_u, p.focus_distance_err_u
    if d is None or e is None:
        visto = ""
        if p.focus_last_seen_zone and p.focus_last_seen_s_before is not None:
            visto = (
                f" — visto por último: {zona_legivel(p.focus_last_seen_zone)}, "
                f"{p.focus_last_seen_s_before}s antes"
            )
        return (
            "não dá para afirmar a distância (nenhum registro seu perto deste instante)" + visto,
            "incerto",
            "incerto",
        )
    onde = zona_legivel(o.focus_player_zone)
    aprox = f"~{_milhar(d)} unidades (±{_milhar(e)}), {onde}"
    if d - e >= PERTO_U:
        return f"longe: {aprox}", "ruim", "longe"
    if d + e <= PERTO_U:
        return f"perto: {aprox}", "neutro", "perto"
    return f"a {aprox} — nem no fosso, nem do outro lado do mapa", "incerto", "meio"


def _milhar(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def revisar(f: MatchFacts) -> list[RevisaoDeObjetivo]:
    role = f.focus.position or "MIDDLE"
    epicos = [o for o in f.objectives if o.kind in EPICOS]
    out: list[RevisaoDeObjetivo] = []
    for grupo in _agrupar(epicos):
        o = grupo[0]
        ultimo = grupo[-1]
        nossos = sum(1 for x in grupo if x.taken_by_focus_team)
        deles = len(grupo) - nossos
        nome = objetivo_legivel(o.kind, o.subtype)
        if len(grupo) > 1:
            nome = f"{nome} ({len(grupo)})"
        seu_time = nossos > deles
        papel = papel_no_objetivo(role, o.kind, o.t_ms, o.subtype)

        # Da disputa, vale o que aconteceu com VOCE no melhor momento: se
        # participou de uma das larvas, participou da disputa.
        situacoes = [_onde_voce(x) for x in grupo]
        ordem = ["participou", "perto", "meio", "morto", "longe", "incerto"]
        texto_voce, tom_voce, situacao = min(situacoes, key=lambda s: ordem.index(s[2]))
        if papel == "fora" and tom_voce == "ruim":
            tom_voce = "neutro"  # longe do que nao era seu nao e erro

        p = o.presence
        itens: list[Item] = []
        if len(grupo) > 1 and nossos and deles:
            itens.append(Item("Resultado", f"seu time {nossos} x {deles} inimigo", "neutro"))
        itens.append(Item("Você", texto_voce, tom_voce))

        vivos_nossos = vivos_deles = 5
        if p is not None:
            vivos_nossos, vivos_deles = p.allies_alive, p.enemies_alive
            tom_n: Tom = (
                "bom"
                if vivos_nossos > vivos_deles
                else "ruim"
                if vivos_nossos < vivos_deles
                else "neutro"
            )
            itens.append(
                Item(
                    "Vivos 15s antes",
                    f"seu time {vivos_nossos} x {vivos_deles} inimigo",
                    tom_n,
                )
            )
            participantes = sorted(
                {c for x in grupo if x.presence for c in x.presence.participants}
            )
            if participantes:
                quem = "seu time" if seu_time else "inimigo"
                itens.append(Item(f"Quem fez ({quem})", ", ".join(participantes)))
            if not seu_time and p.enemy_jungler_participated:
                itens.append(Item("Caçador inimigo", "estava lá (participou)", "neutro"))
            elif p.enemy_jungler_distance_u is not None and p.enemy_jungler_distance_err_u:
                d = p.enemy_jungler_distance_u
                longe_jg = d - p.enemy_jungler_distance_err_u >= PERTO_U
                itens.append(
                    Item(
                        "Caçador inimigo",
                        f"~{_milhar(d)} unidades do objetivo"
                        + (" — longe: era a janela para fazer" if longe_jg else ""),
                        "bom" if longe_jg else "neutro",
                    )
                )
            if role != "JUNGLE" and p.own_jungler_participated is not None:
                itens.append(
                    Item(
                        "Seu caçador",
                        "participou" if p.own_jungler_participated else "não participou",
                    )
                )

        vantagem = o.team_gold_diff_at
        if vantagem:
            lado = "à frente" if vantagem > 0 else "atrás"
            itens.append(
                Item(
                    "Ouro do time",
                    f"seu time estava {_milhar(abs(vantagem))} de ouro {lado} naquele minuto",
                    "bom" if vantagem > 0 else "ruim",
                )
            )
        if role in ("UTILITY", "JUNGLE") or papel == "principal":
            w = o.wards_placed_60s_before
            itens.append(
                Item(
                    "Suas wards (60s antes)",
                    f"{w} colocada{'s' if w != 1 else ''}"
                    + (" — a Riot não informa onde" if w else ""),
                    "bom" if w else ("ruim" if role == "UTILITY" else "neutro"),
                )
            )

        veredito, tom = _veredito(
            seu_time, papel, situacao, vivos_nossos, vivos_deles, role, o.t_ms
        )
        out.append(
            RevisaoDeObjetivo(
                t_ms=o.t_ms,
                nome=nome if len(grupo) == 1 else f"{nome} · até {mmss(ultimo.t_ms)}",
                seu_time=seu_time,
                papel=papel,
                veredito=veredito,
                tom=tom,
                itens=itens,
            )
        )
    return out


def _veredito(
    seu_time: bool,
    papel: Papel,
    situacao: str,
    vivos_nossos: int,
    vivos_deles: int,
    role: str,
    t_ms: int,
) -> tuple[str, Tom]:
    """UMA frase, na ordem em que um coach pensaria."""
    papel_nome = PAPEL_LEGIVEL.get(role, role.lower())
    if seu_time:
        if situacao == "participou":
            return "Garantido, com você.", "bom"
        if papel == "fora":
            return f"Garantido pelo time — não era objetivo do {papel_nome} neste momento.", "bom"
        if situacao == "longe" and papel == "principal":
            return (
                "Garantido sem você. Deu certo desta vez, mas era objetivo seu: "
                "vale ver o que você fazia do outro lado.",
                "neutro",
            )
        return "Garantido pelo time.", "bom"

    if vivos_nossos <= vivos_deles - 2:
        return (
            f"Cedido em desvantagem numérica ({vivos_nossos} x {vivos_deles}): lutar seria pior. "
            "O erro, se houve, foi antes — nas mortes que abriram a janela.",
            "neutro",
        )
    if papel == "fora":
        return (
            f"Não era objetivo do {papel_nome} neste momento — não entra na sua conta.",
            "neutro",
        )
    if situacao == "morto":
        return (
            "Você estava morto quando caiu: a morte antes do objetivo é o que revisar.",
            "ruim",
        )
    if situacao == "participou":
        return "Você estava na disputa e o time perdeu — reveja o início da luta.", "neutro"
    if situacao == "longe":
        if papel == "principal":
            return (
                "Era objetivo seu e você estava longe. A correção é chegar antes: "
                + _como_chegar(role, t_ms),
                "ruim",
            )
        return "Você estava longe; como papel de apoio aqui, vale só conferir o timing.", "neutro"
    if situacao == "perto":
        return (
            "Você estava perto e o time não contestou — veja no replay se dava para lutar.",
            "neutro",
        )
    return "Não dá para afirmar onde você estava — confira no replay.", "incerto"


def _como_chegar(role: str, t_ms: int) -> str:
    rotas = t_ms < FIM_DAS_ROTAS_MS
    return {
        "BOTTOM": "empurre a wave de baixo e chegue ao rio com o suporte 45-60s antes de nascer.",
        "UTILITY": "puxe a visão do rio 60-90s antes e leve o atirador junto.",
        "MIDDLE": "empurre a wave do meio antes: prioridade no meio é o que deixa chegar primeiro.",
        "TOP": (
            "empurre a wave e guarde o Teleporte para a luta."
            if not rotas
            else "empurre a wave antes de o objetivo nascer para poder ajudar."
        ),
        "JUNGLE": "planeje a rota para terminar no objetivo quando ele nascer.",
    }.get(role, "antecipe a rota em vez de reagir ao objetivo.")


def papel_para_ia(role: str) -> str:
    """O papel, dito ao modelo, com o que ele implica. Sem isto o modelo
    cobrava Smite de ADC e Arauto de suporte — ver o topo do modulo."""
    nome = PAPEL_LEGIVEL.get(role, role.lower())
    return (
        f"PAPEL DO JOGADOR: {nome} ({role}). Julgue TUDO por este papel: so o cacador usa "
        "Smite; na fase de rotas (ate ~14 min) o lado de cima (Vastilarvas, Arauto) e do "
        "topo/cacador/meio e o Dragao e da rota de baixo/cacador/meio; Barao, Atakhan e "
        "Anciao sao de todos. Nao cobre do jogador objetivo que nao era do papel dele."
    )


def texto_para_ia(revisao: list[RevisaoDeObjetivo]) -> str:
    """Uma linha curta por disputa, ja com o veredito — o modelo explica, nao
    recalcula. Curta de proposito: a pergunta inteira precisa caber na cota de
    tokens por minuto do tier gratuito (tests/test_pergunta.py)."""
    if not revisao:
        return ""
    linhas = ["OBJETIVOS (ja avaliados pelo papel; nao contradiga):"]
    for r in revisao[:12]:
        voce = next((i.texto for i in r.itens if i.rotulo == "Você"), "?")
        vivos = next((i.texto for i in r.itens if i.rotulo.startswith("Vivos")), "")
        vivos = vivos.replace("seu time ", "").replace(" inimigo", "")
        linhas.append(
            f"  {r.t} {r.nome} {'nosso' if r.seu_time else 'deles'} [{r.papel}] "
            f"voce: {voce.split(' — ')[0]}; vivos {vivos} => {r.veredito.split('. ')[0]}"
        )
    return "\n".join(linhas)
