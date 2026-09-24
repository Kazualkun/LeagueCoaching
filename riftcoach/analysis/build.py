"""Build, runas, matchup e composicao — a sua escolha contra a dos melhores.

Pedido literal: "qual runa eu escolhi e o que eu buildei vs runa recomendada
e build recomendada; taxa de vitoria naquela matchup". Recomendada, aqui,
tem definicao operacional e verificavel: o que os jogadores de Challenger,
Grao-Mestre e Mestre do servidor usam com o mesmo campeao no mesmo papel,
no mesmo patch (knowledge/meta.py). Nao e opiniao de ninguem, e contagem —
e sempre com a amostra junto do numero.

Duas regras para nao virar ruido:

  1. Divergir do meta NAO e erro por si. So vira achado quando a amostra e
     grande, a escolha comum e MUITO comum, e a sua e rara. Build diferente
     com motivo (contra a composicao inimiga) e boa build.
  2. A composicao e lida do que aconteceu na partida (dano, cura e controle
     medidos no fim), e as sugestoes saem dela — "o time deles curou 40 mil
     e voce nao fez anti-cura" e fato, nao gosto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from riftcoach.core.schema import Evidence, EvidenceTier, Finding
from riftcoach.knowledge.meta import AMOSTRA_MINIMA, MetaDB, Opcao, Taxa
from riftcoach.knowledge.sync import PatchDB, classes_de_itens, stats_do_item
from riftcoach.parse.facts import MatchFacts

# Itens com Feridas Dolorosas (anti-cura) em Summoner's Rift. Ids estaveis ha
# muitas temporadas; se algum sair do jogo, so deixa de ser reconhecido.
ANTICURA = frozenset({3123, 3033, 6609, 3076, 3075, 3916, 3165, 3011})

# Cura inimiga a partir da qual anti-cura deixa de ser opcional, por minuto de
# partida. ~600/min e o que um Soraka/Aatrox/Vladimir faz numa partida normal.
CURA_ALTA_POR_MIN = 600

# Uma escolha e "a do meta" quando aparece em pelo menos esta fracao das
# partidas do campeao no papel; a sua e "rara" abaixo desta.
ESCOLHA_DOMINANTE = 0.40
ESCOLHA_RARA = 0.10


@dataclass
class Item:
    nome: str
    taxa: Taxa | None = None
    escolha: float | None = None
    seu: bool = False


@dataclass
class Build:
    campeao: str
    papel: str
    patches: tuple[str, ...]
    fonte: str
    campeao_taxa: Taxa
    oponente: str | None = None
    matchup: Taxa | None = None
    matchup_rota: float | None = None
    runa_sua: str = ""
    runa_sua_taxa: Opcao | None = None
    runas_meta: list[Item] = field(default_factory=list)
    itens_seus: list[str] = field(default_factory=list)
    itens_meta: list[Item] = field(default_factory=list)
    botas_meta: list[Item] = field(default_factory=list)
    composicao: list[str] = field(default_factory=list)
    observacoes: list[str] = field(default_factory=list)
    # O componente de anti-cura que combina com o SEU tipo de dano: sugerir
    # Colete de Espinhos para uma atiradora e conselho que ninguem segue.
    anticura_sugerida: str = "um componente de anti-cura"
    achados: list[Finding] = field(default_factory=list)

    @property
    def tem_amostra(self) -> bool:
        return self.campeao_taxa.jogos > 0


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def analisar(facts: MatchFacts, db: PatchDB, meta: MetaDB | None = None) -> Build:
    """Monta a comparacao. Funciona sem amostra nenhuma (so composicao)."""
    meta = meta or MetaDB()
    db.use_patch(facts.patch)
    foco = facts.focus
    papel = foco.position or "MIDDLE"
    nome = db.champion(foco.champion_id) or foco.champion
    patches = meta.patches_para(facts.patch, foco.champion, papel)
    classes = classes_de_itens(db, facts.patch)

    b = Build(
        campeao=nome,
        papel=papel,
        patches=patches,
        fonte="Challenger, Grão-Mestre e Mestre do servidor",
        campeao_taxa=meta.campeao(foco.champion, papel, patches),
    )

    # --- matchup ---------------------------------------------------------
    if facts.opponent is not None:
        b.oponente = db.champion(facts.opponent.champion_id) or facts.opponent.champion
        b.matchup, b.matchup_rota = meta.matchup(
            foco.champion, papel, facts.opponent.champion, patches
        )

    # --- runas -------------------------------------------------------------
    pedra = facts.runes[0] if facts.runes else 0
    sub = facts.rune_styles[1]
    b.runa_sua = " + ".join(x for x in (db.rune(pedra), db.rune(sub)) if x)
    opcoes = meta.runas(foco.champion, papel, patches)
    for o in opcoes:
        nome_runa = " + ".join(x for x in (db.rune(o.chave[0]), db.rune(o.chave[1])) if x)
        seu = o.chave == (pedra, sub)
        b.runas_meta.append(Item(nome_runa, o.taxa, o.escolha, seu))
        if seu:
            b.runa_sua_taxa = o

    # --- itens ---------------------------------------------------------------
    finais = [i for i in foco.items if i in classes["lendario"]]
    ordem = [i for _, i in facts.build_path if i in classes["lendario"]]
    seus = list(dict.fromkeys([*ordem, *finais]))  # ordem de compra, sem repetir
    b.itens_seus = [db.item(i) or str(i) for i in seus]
    for o in meta.itens(foco.champion, papel, patches, classes["lendario"], top=8):
        b.itens_meta.append(
            Item(db.item(o.chave[0]) or str(o.chave[0]), o.taxa, o.escolha, o.chave[0] in seus)
        )
    for o in meta.itens(foco.champion, papel, patches, classes["botas"], top=3):
        b.botas_meta.append(
            Item(
                db.item(o.chave[0]) or str(o.chave[0]), o.taxa, o.escolha, o.chave[0] in foco.items
            )
        )

    _composicao(facts, db, b, set(foco.items))
    _achados(facts, b, pedra, sub, seus)
    return b


def _composicao(facts: MatchFacts, db: PatchDB, b: Build, itens: set[int]) -> None:
    inimigo = facts.enemy_profile
    if inimigo is None:
        return
    minutos = max(1.0, facts.duration_s / 60)
    maior = max(
        (("físico", inimigo.physical_share), ("mágico", inimigo.magic_share)),
        key=lambda x: x[1],
    )
    b.composicao.append(
        f"Dano do time inimigo: {_pct(inimigo.physical_share)} físico, "
        f"{_pct(inimigo.magic_share)} mágico, {_pct(inimigo.true_share)} verdadeiro"
        + (f" — predominantemente {maior[0]}" if maior[1] >= 0.6 else " — misto")
    )
    if inimigo.top_healers:
        quem, cura = inimigo.top_healers[0]
        b.composicao.append(
            f"Cura do time inimigo: {inimigo.heal_total:,} no total".replace(",", ".")
            + f"; quem mais curou: {quem} ({cura:,})".replace(",", ".")
        )
    b.composicao.append(
        f"Controle (CC) aplicado pelo time inimigo: {inimigo.cc_seconds}s na partida"
    )
    # A build respondeu ao tipo de dano? Resistencia contra o dano que domina
    # e a resposta mais barata que existe, e vale reconhecer quando ela veio.
    stat = "FlatSpellBlockMod" if maior[0] == "mágico" else "FlatArmorMod"
    nome_stat = "resistência mágica" if maior[0] == "mágico" else "armadura"
    if maior[1] >= 0.6:
        defensivos = [
            db.item(i) or str(i)
            for i in itens
            if i and stats_do_item(db, i, facts.patch).get(stat, 0) > 0
        ]
        if defensivos:
            b.observacoes.append(
                f"Contra dano {maior[0]} dominante, você fez {nome_stat} "
                f"({', '.join(defensivos)}) — a build respondeu à composição."
            )
        elif b.papel in ("TOP", "JUNGLE", "UTILITY"):
            b.observacoes.append(
                f"O dano inimigo foi {_pct(maior[1])} {maior[0]} e você não fez "
                f"nenhum item com {nome_stat}."
            )
    fez_anticura = bool(itens & ANTICURA)
    meu = facts.ally_profile
    foco = facts.focus
    fisico = foco.damage_to_champions > 0 and (
        b.papel in ("BOTTOM",) or (meu is not None and meu.physical_share >= meu.magic_share)
    )
    if b.papel in ("TOP", "UTILITY") and not fisico:
        sugestao = (3076, "Colete de Espinhos")
    elif fisico:
        sugestao = (3123, "Chamado do Carrasco")
    else:
        sugestao = (3916, "Orbe do Esquecimento")
    b.anticura_sugerida = db.item(sugestao[0]) or sugestao[1]
    if inimigo.heal_total / minutos >= CURA_ALTA_POR_MIN:
        if fez_anticura:
            b.observacoes.append("Você fez anti-cura contra um time que curava muito — certo.")
        elif b.papel in ("TOP", "MIDDLE", "BOTTOM", "JUNGLE"):
            b.observacoes.append(
                "O time inimigo curou muito e você não fez nenhum item de anti-cura."
            )


def _achados(facts: MatchFacts, b: Build, pedra: int, sub: int, seus: list[int]) -> None:
    """So o que a amostra sustenta. Ver a regra 1 no topo do modulo."""
    tem_amostra = b.campeao_taxa.jogos >= AMOSTRA_MINIMA
    ev_amostra = Evidence(
        tier=EvidenceTier.T2_DERIVED,
        timestamp_ms=0,
        statement=(
            f"{b.campeao} {b.papel.lower()} na amostra: {b.campeao_taxa.texto()} "
            f"— patch {'+'.join(b.patches)}"
        ),
        source="benchmark",
        assumption=(
            f"amostra de partidas ranqueadas de {b.fonte}, coletada pela API da Riot; "
            "popularidade nao prova que a escolha e a melhor para a SUA partida"
        ),
    )

    # Runas: a sua e rara e uma outra domina.
    if tem_amostra and b.runas_meta:
        dominante = b.runas_meta[0]
        sua_escolha = b.runa_sua_taxa.escolha if b.runa_sua_taxa else 0.0
        if (
            not dominante.seu
            and (dominante.escolha or 0) >= ESCOLHA_DOMINANTE
            and sua_escolha < ESCOLHA_RARA
            and dominante.taxa is not None
            and dominante.taxa.suficiente
        ):
            b.achados.append(
                Finding(
                    category="itemization",
                    phase="early",
                    severity=2,
                    timestamp_ms=0,
                    claim=(
                        f"Suas runas ({b.runa_sua or 'desconhecidas'}) são raras em "
                        f"{b.campeao}: {dominante.nome} aparece em "
                        f"{_pct(dominante.escolha or 0)} das partidas dos melhores."
                    ),
                    evidence=[
                        ev_amostra,
                        Evidence(
                            tier=EvidenceTier.T2_DERIVED,
                            timestamp_ms=0,
                            statement=(
                                f"{dominante.nome}: escolhida em {_pct(dominante.escolha or 0)}, "
                                f"vitória {dominante.taxa.texto()}; a sua: escolhida em "
                                f"{_pct(sua_escolha)}"
                            ),
                            source="benchmark",
                            assumption="taxas suavizadas em direção a 50% (amostra declarada)",
                        ),
                    ],
                    fix=(
                        f"Teste {dominante.nome} nas próximas partidas de {b.campeao}. Se você "
                        "escolheu diferente por causa do matchup ou da composição, ótimo — "
                        "só confira se o motivo ainda vale."
                    ),
                    drill=(
                        f"Nas próximas 3 partidas de {b.campeao}, jogue com {dominante.nome} "
                        "e compare a sua fase de rotas."
                    ),
                    confidence=0.55,
                )
            )

    # Itens: nenhum dos essenciais, com build ja formada.
    if tem_amostra and len(seus) >= 3 and facts.duration_s >= 25 * 60:
        essenciais = [
            i for i in b.itens_meta[:4] if (i.escolha or 0) >= 0.35 and i.taxa and i.taxa.suficiente
        ]
        faltaram = [i for i in essenciais if not i.seu]
        if essenciais and len(faltaram) == len(essenciais):
            b.achados.append(
                Finding(
                    category="itemization",
                    phase="mid",
                    severity=2,
                    timestamp_ms=0,
                    claim=(
                        f"Sua build não teve nenhum dos itens mais usados em {b.campeao} "
                        f"pelos melhores: {', '.join(i.nome for i in faltaram)}."
                    ),
                    evidence=[
                        ev_amostra,
                        Evidence(
                            tier=EvidenceTier.T2_DERIVED,
                            timestamp_ms=0,
                            statement=" · ".join(
                                f"{i.nome}: em {_pct(i.escolha or 0)} das builds, "
                                f"vitória {i.taxa.texto() if i.taxa else '?'}"
                                for i in faltaram
                            ),
                            source="benchmark",
                            assumption="inventario final das partidas da amostra",
                        ),
                        Evidence(
                            tier=EvidenceTier.T1_MEASURED,
                            timestamp_ms=0,
                            statement="sua build: " + ", ".join(b.itens_seus),
                            source="match",
                        ),
                    ],
                    fix=(
                        "Build fora do padrão só se paga quando responde à partida (anti-cura, "
                        "armadura contra dano físico). Se não foi o caso, volte ao núcleo "
                        f"de {b.campeao}."
                    ),
                    drill=(
                        f"Nas próximas 3 partidas de {b.campeao}, feche primeiro "
                        f"{faltaram[0].nome} e compare o seu dano aos 20 minutos."
                    ),
                    confidence=0.5,
                )
            )

    if any("não fez nenhum item de anti-cura" in o for o in b.observacoes):
        inimigo = facts.enemy_profile
        assert inimigo is not None
        quem, cura = inimigo.top_healers[0] if inimigo.top_healers else ("?", 0)
        b.achados.append(
            Finding(
                category="itemization",
                phase="mid",
                severity=2,
                timestamp_ms=15 * 60_000,
                claim=(
                    f"O time inimigo curou {inimigo.heal_total:,} na partida ".replace(",", ".")
                    + f"({quem}: {cura:,}) e você não fez anti-cura.".replace(",", ".")
                ),
                evidence=[
                    Evidence(
                        tier=EvidenceTier.T1_MEASURED,
                        timestamp_ms=0,
                        statement=(
                            f"cura total do time inimigo: {inimigo.heal_total} em "
                            f"{facts.duration_s // 60} minutos"
                        ),
                        source="match",
                    ),
                ],
                fix=(
                    f"Contra cura alta, {b.anticura_sugerida} cedo corta a cura deles "
                    "pela metade nas lutas — e é um componente barato, não um item inteiro."
                ),
                drill=(
                    "Na tela de carregamento, identifique quem cura no time inimigo. Se "
                    "houver dois, o componente de anti-cura entra no segundo recall."
                ),
                confidence=0.6,
            )
        )


def texto_curto(b: Build) -> str:
    """Versao de ~150 tokens para o passe unico, que roda contra um teto de
    8.000 tokens/min e ja chega perto dele (analysis/analysts.py)."""
    partes = [f"BUILD vs MELHORES ({b.campeao} {b.papel}, {b.campeao_taxa.jogos} partidas):"]
    if b.matchup and b.matchup.jogos and b.oponente:
        partes.append(f"matchup vs {b.oponente} {b.matchup.texto()}")
    runa = next((r for r in b.runas_meta if r.seu), None)
    partes.append(
        f"runas do jogador {b.runa_sua or '?'} (usadas por {_pct(runa.escolha or 0)} dos melhores)"
        if runa
        else f"runas do jogador {b.runa_sua or '?'} (raras na amostra)"
    )
    nucleo = [f"{i.nome}{' (fez)' if i.seu else ''}" for i in b.itens_meta[:4]]
    if nucleo:
        partes.append("itens mais usados: " + ", ".join(nucleo))
    partes += b.observacoes
    return "; ".join(partes)


def texto_para_ia(b: Build) -> str:
    """O bloco que vai para o modelo: tudo calculado, com amostra declarada."""
    linhas = [
        f"BUILD, RUNAS E MATCHUP ({b.campeao} {b.papel}; amostra de {b.fonte}, "
        f"patch {'+'.join(b.patches)}):"
    ]
    if b.tem_amostra:
        linhas.append(f"  {b.campeao} neste papel: vitória {b.campeao_taxa.texto()}")
    else:
        linhas.append("  sem partidas deste campeão/papel na amostra coletada")
    if b.oponente:
        if b.matchup and b.matchup.jogos:
            rota = f"; venceu a fase de rotas em {_pct(b.matchup_rota)}" if b.matchup_rota else ""
            linhas.append(f"  matchup contra {b.oponente}: vitória {b.matchup.texto()}{rota}")
        else:
            linhas.append(f"  matchup contra {b.oponente}: sem partidas na amostra")
    linhas.append(f"  runas do jogador: {b.runa_sua or '?'}")
    for r in b.runas_meta[:3]:
        linhas.append(
            f"    meta: {r.nome} — escolhida em {_pct(r.escolha or 0)}, vitória "
            f"{r.taxa.texto() if r.taxa else '?'}{' (a do jogador)' if r.seu else ''}"
        )
    linhas.append(f"  itens do jogador (ordem): {', '.join(b.itens_seus) or '?'}")
    for i in b.itens_meta[:6]:
        linhas.append(
            f"    meta: {i.nome} — em {_pct(i.escolha or 0)} das builds, vitória "
            f"{i.taxa.texto() if i.taxa else '?'}{' (o jogador fez)' if i.seu else ''}"
        )
    if b.composicao:
        linhas.append("COMPOSICAO INIMIGA (medida no fim da partida):")
        linhas += [f"  {c}" for c in b.composicao]
    linhas += [f"  observacao: {o}" for o in b.observacoes]
    linhas.append(
        "Use estes numeros como evidencia T2 citando a amostra. Popularidade nao e prova "
        "de que a escolha e melhor NESTA partida; build que responde a composicao e valida."
    )
    return "\n".join(linhas)


def para_pagina(b: Build) -> dict[str, object]:
    def item(i: Item) -> dict[str, object]:
        return {
            "name": i.nome,
            "pick": round(100 * (i.escolha or 0)),
            "wr": round(100 * i.taxa.suavizada) if i.taxa else None,
            "wr_margin": round(100 * i.taxa.margem) if i.taxa else None,
            "games": i.taxa.jogos if i.taxa else 0,
            "yours": i.seu,
        }

    return {
        "champion": b.campeao,
        "patches": list(b.patches),
        "source": b.fonte,
        "champion_wr": b.campeao_taxa.texto(),
        "champion_games": b.campeao_taxa.jogos,
        "opponent": b.oponente,
        "matchup": b.matchup.texto() if b.matchup else None,
        "matchup_games": b.matchup.jogos if b.matchup else 0,
        "matchup_lane": round(100 * b.matchup_rota) if b.matchup_rota is not None else None,
        "runes_yours": b.runa_sua,
        "runes_meta": [item(i) for i in b.runas_meta],
        "items_yours": b.itens_seus,
        "items_meta": [item(i) for i in b.itens_meta],
        "boots_meta": [item(i) for i in b.botas_meta],
        "composition": b.composicao,
        "notes": b.observacoes,
    }
