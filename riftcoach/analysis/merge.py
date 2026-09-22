"""Deduplicacao, ranqueamento e selecao do top 3.

Dois produtores de findings vao passar por aqui:

  agora      o motor de regras (rules.py), no caminho L5 sem IA
  etapa 3    os quatro analistas especialistas, rodando em paralelo

Eles tem o mesmo modo de falha, e e por isso que este modulo existe antes de
qualquer modelo: quatro passes independentes olhando a mesma partida vao
descrever a MESMA morte quatro vezes, com palavras diferentes. O motor de
regras faz igual — a morte que custou 11pp aparece como erro medido, e de novo
como morte em janela de objetivo.

Um relatorio com doze findings nao e doze vezes mais util que um com cinco. Ele
e menos util, porque ninguem le ate o fim e o leitor perde a capacidade de
distinguir o que era grave. Cortar e funcionalidade, nao economia.
"""

from __future__ import annotations

from riftcoach.core.schema import Category, CoachingReport, Evidence, Finding
from riftcoach.parse.facts import PARSER_VERSION, MatchFacts

# Dois findings da mesma categoria dentro desta janela falam do mesmo momento.
# 30s e aproximadamente o tempo de uma luta inteira comecar e terminar.
DEDUPE_WINDOW_MS = 30_000

# Quantos findings o relatorio carrega. Acima disso o leitor para de ler, e o
# que foi cortado sempre pode ser recuperado — os dados nao somem, so a linha.
MAX_FINDINGS = 8

# Categorias GENERICAS: sao o que sobra quando sabemos que algo custou caro mas
# nao sabemos dizer por que. Vem do motor de vantagem, que MEDE o custo de um
# momento sem explica-lo.
GENERIC: frozenset[Category] = frozenset({"decision", "positioning"})

# Categorias que EXPLICAM um momento — e so nelas um generico pode ser fundido.
#
# A restricao e estreita de proposito. `macro` (rule_objective_deaths) descreve
# literalmente a mesma morte que o motor de vantagem mediu, entao fundir e
# lossless: um lado traz o custo, o outro traz o porque.
#
# Tudo o mais descreve uma TENDENCIA da partida inteira e so tem timestamp
# porque o schema exige um ponto de seek. O CS@10 esta ancorado no minuto 10; a
# regra da Sentinela de Controle, no segundo recall. Deixar essas absorverem
# faria uma observacao de checklist engolir uma morte critica que por acaso
# aconteceu perto — o que ja aconteceu em teste antes desta restricao existir.
EXPLAINS_A_MOMENT: frozenset[Category] = frozenset({"macro"})


def _information(f: Finding) -> tuple[int, int, float]:
    """Quanto um finding informa. Usado para escolher entre duplicatas.

    Evidencia primeiro, depois gravidade, depois confianca: entre dois
    findings sobre o mesmo momento, o que cita mais coisa e o que o usuario
    consegue conferir.
    """
    return (len(f.evidence), f.severity, f.confidence)


# Acima desta sobreposicao de palavras, duas evidencias contam o mesmo fato.
# 0.6 e empirico: abaixo disso as duas descricoes da mesma morte deixavam de
# casar, acima disso a janela de objetivo comecava a casar com a morte em si.
SAME_FACT_OVERLAP = 0.6


def _words(statement: str) -> set[str]:
    return {w for w in statement.lower().replace(",", " ").replace(":", " ").split() if w}


def _says_the_same(a: Evidence, b: Evidence) -> bool:
    """Duas evidencias contam o mesmo fato?

    Jaccard sobre palavras, restrito ao mesmo nivel e ao mesmo instante — sem
    esse recorte, duas frases genericas parecidas de momentos diferentes
    casariam e uma delas sumiria do relatorio.
    """
    if a.tier is not b.tier or a.timestamp_ms != b.timestamp_ms:
        return False
    pa, pb = _words(a.statement), _words(b.statement)
    if not pa or not pb:
        return False
    return len(pa & pb) / len(pa | pb) >= SAME_FACT_OVERLAP


def _absorb(especifico: Finding, generico: Finding) -> Finding:
    """Funde um finding generico no especifico que descreve o mesmo momento.

    Absorver, e nao descartar, porque os dois carregam metade da historia:

      o generico  tem a gravidade MEDIDA pelo motor de vantagem ("custou 16pp")
      o especifico tem a explicacao ("com o barao a 43s de nascer")

    Descartar qualquer um dos dois perde informacao que o usuario precisa. Um
    descarte ingenuo pelo mais grave apagaria justamente o POR QUE; o contrario
    rebaixaria um erro critico a uma observacao.
    """
    # Do generico, so entra o que o especifico ainda NAO diz. Casar texto exato
    # nao serve: duas regras descrevem a mesma morte com frases parecidas e nao
    # identicas ("morte D4 em 18:15, em X, para Y" contra "morte em 18:15:
    # morreu em X para Y"). Casar por (nivel, instante) seria o outro extremo e
    # apagaria fatos distintos do mesmo momento — a janela de objetivo e o
    # estado de wave sao ambos T1/T2 as 18:15 e ambos precisam sobreviver.
    evidencias = list(especifico.evidence)
    for e in generico.evidence:
        if not any(_says_the_same(e, ja) for ja in evidencias):
            evidencias.append(e)
    severidade = max(especifico.severity, generico.severity)

    # Se a fusao ELEVA a gravidade, a claim precisa passar a sustentar isso. Um
    # finding marcado como gravidade 4 cuja frase so descreve o contexto deixa
    # o leitor sem saber de onde veio o 4 — e "confie em mim" e exatamente o
    # que este projeto nao faz.
    claim = (
        f"{especifico.claim} {generico.claim}"
        if severidade > especifico.severity
        else especifico.claim
    )

    return especifico.model_copy(
        update={
            "severity": severidade,
            "claim": claim,
            "evidence": evidencias,
            "confidence": max(especifico.confidence, generico.confidence),
        }
    )


def dedupe(findings: list[Finding], window_ms: int = DEDUPE_WINDOW_MS) -> list[Finding]:
    """Colapsa findings que descrevem o mesmo momento.

    Duas passadas, nesta ordem:

      1. mesma categoria + mesmo instante  -> fica o que informa mais
      2. generica que coincide com uma especifica -> funde na especifica

    A ordem importa: fazer (2) antes de (1) deixaria duas genericas
    sobreviverem uma a outra e so depois seriam comparadas com a especifica.
    """
    mantidos: list[Finding] = []
    for f in sorted(findings, key=lambda x: (-_information(x)[0], -x.severity)):
        dup = next(
            (
                m
                for m in mantidos
                if m.category == f.category and abs(m.timestamp_ms - f.timestamp_ms) <= window_ms
            ),
            None,
        )
        if dup is None:
            mantidos.append(f)
        elif _information(f) > _information(dup):
            mantidos[mantidos.index(dup)] = f

    resultado = [m for m in mantidos if m.category not in GENERIC]
    for g in (m for m in mantidos if m.category in GENERIC):
        alvo = next(
            (
                e
                for e in resultado
                if e.category in EXPLAINS_A_MOMENT
                and abs(e.timestamp_ms - g.timestamp_ms) <= window_ms
            ),
            None,
        )
        if alvo is None:
            resultado.append(g)
        else:
            resultado[resultado.index(alvo)] = _absorb(alvo, g)
    return resultado


def rank(findings: list[Finding]) -> list[Finding]:
    """Gravidade, depois qualidade da evidencia, depois confianca.

    Mesmo criterio de `CoachingReport.ranked()`, de proposito: a ordem que a
    UI mostra e a ordem em que o relatorio foi montado.
    """
    return sorted(findings, key=lambda f: (-f.severity, f.best_tier.value, -f.confidence))


def pick_top_three(findings: list[Finding]) -> list[int]:
    """Indices dos tres findings de abertura, com categorias distintas.

    Tres findings sobre mortes ensinam uma coisa so. Variar a categoria cobre
    mais superficie do jogo e e o que faz o topo do relatorio valer a leitura.
    Se nao houver tres categorias, completa pela ordem de ranqueamento.
    """
    escolhidos: list[int] = []
    vistas: set[Category] = set()
    for i, f in enumerate(findings):
        if len(escolhidos) == 3:
            break
        if f.category not in vistas:
            escolhidos.append(i)
            vistas.add(f.category)
    for i in range(len(findings)):
        if len(escolhidos) == 3:
            break
        if i not in escolhidos:
            escolhidos.append(i)
    return sorted(escolhidos)


def merge(
    findings: list[Finding],
    limit: int = MAX_FINDINGS,
    window_ms: int = DEDUPE_WINDOW_MS,
) -> list[Finding]:
    """Deduplica, ranqueia e corta. O pipeline completo, em ordem."""
    return rank(dedupe(findings, window_ms))[:limit]


# Vagas garantidas para os findings do modelo quando a IA roda. Ver
# `merge_sources` para o porque de isto existir.
MODEL_RESERVE = 3


def merge_sources(
    measured: list[Finding],
    interpreted: list[Finding],
    limit: int = MAX_FINDINGS,
    reserve: int = MODEL_RESERVE,
    window_ms: int = DEDUPE_WINDOW_MS,
) -> list[Finding]:
    """Junta os dois produtores garantindo espaco para o interpretado.

    POR QUE A RESERVA EXISTE, e ela nao e um favor ao modelo: o criterio de
    ranqueamento coloca medicao acima de interpretacao de proposito — mesma
    gravidade, ganha quem tem confianca maior, e as regras sempre tem. Com
    limite unico, o resultado observado foi o modelo produzir um finding bom e
    ele ser cortado por findings de regra que ja estavam la sem IA nenhuma.

    Ou seja: ligar a IA nao mudava nada visivel no relatorio. Uma
    funcionalidade que nao muda a saida nao e uma funcionalidade.

    A reserva nao rebaixa o medido — ele continua no topo e continua vindo
    primeiro. Ela so impede que o interpretado seja zerado pelo corte. Se o
    modelo nao produzir nada, o resultado e identico ao L5.
    """
    juntos = dedupe([*measured, *interpreted], window_ms)

    # A fusao pode ter produzido objetos novos, entao a origem e reconhecida
    # pelo conteudo: um finding que sobreviveu carrega a claim de um dos lados.
    claims_medidas = {f.claim for f in measured}
    do_modelo = [f for f in juntos if f.claim not in claims_medidas]
    medidos = [f for f in juntos if f.claim in claims_medidas]

    if not do_modelo:
        return rank(juntos)[:limit]

    vagas_modelo = min(reserve, len(do_modelo))
    escolhidos = [
        *rank(medidos)[: max(0, limit - vagas_modelo)],
        *rank(do_modelo)[:vagas_modelo],
    ]
    return rank(escolhidos)[:limit]


def build_report(
    facts: MatchFacts,
    findings: list[Finding],
    model_trace: dict[str, str] | None = None,
    limit: int = MAX_FINDINGS,
    interpreted: list[Finding] | None = None,
) -> CoachingReport:
    """Monta o `CoachingReport` final a partir de findings crus.

    `interpreted` separa o que veio de modelo do que veio de medicao, para que
    a reserva de `merge_sources` possa ser aplicada. Sem ele, o comportamento e
    o do caminho L5: um unico conjunto, um unico criterio.

    `model_trace` fica vazio no caminho L5 — e assim que o usuario sabe que
    nenhum modelo tocou neste relatorio.
    """
    finais = (
        merge_sources(findings, interpreted, limit=limit)
        if interpreted
        else merge(findings, limit=limit)
    )
    return CoachingReport(
        match_id=facts.match_id,
        patch=facts.patch,
        puuid=facts.focus.puuid,
        parser_version=PARSER_VERSION,
        model_trace=model_trace or {},
        findings=finais,
        top_three=pick_top_three(finais),
    )
