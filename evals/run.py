"""Harness de avaliacao: pontua cada provedor contra o corpus anotado.

    uv run python evals/run.py                  # so a linha de base, sem IA
    uv run python evals/run.py --with-ai        # todos os provedores disponiveis
    uv run python evals/run.py --save           # grava em evals/results/

POR QUE ISTO EXISTE, e nao e para ter numero bonito: um projeto de IA sem
harness de avaliacao nao pode fazer afirmacao nenhuma sobre precisao, e nao tem
como saber se um PR de prompt ajudou ou piorou. Sem isto, "melhorou" e opiniao.

A LINHA DE BASE E O MOTOR DE REGRAS. Ele nao usa modelo, roda em milissegundos
e e o que o usuario recebe se tudo falhar. Um provedor que pontua abaixo dele
esta ativamente piorando o produto, e a matriz publicada precisa mostrar isso
lado a lado — e nao esconder atras de um ranking so entre modelos.

Criterio completo em evals/rubric.md.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from evals.scoring import Golden, MatchScore, ProviderScore, score_report  # noqa: E402
from riftcoach.analysis.report import analyze, analyze_with_ai  # noqa: E402
from riftcoach.core.errors import RiftCoachError  # noqa: E402
from riftcoach.knowledge.sync import PatchDB  # noqa: E402
from riftcoach.llm.router import ModelRouter  # noqa: E402
from riftcoach.parse.distill import distill  # noqa: E402
from riftcoach.parse.facts import MatchFacts  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "golden"
RESULTS_DIR = Path(__file__).parent / "results"
# As fixtures do corpus de avaliacao ficam em evals/fixtures/ quando existirem;
# enquanto o corpus e pequeno, reaproveitamos as de teste, que ja sao partidas
# reais anonimizadas.
FIXTURE_DIRS = (Path(__file__).parent / "fixtures", RAIZ / "tests" / "fixtures")

BASELINE = "regras (sem IA)"


def _load_fixture(match_id: str) -> tuple[dict, dict] | None:
    """Acha a partida de um match_id em qualquer diretorio de fixtures."""
    for d in FIXTURE_DIRS:
        if not d.exists():
            continue
        for caminho in sorted(d.glob("*.json.gz")):
            with gzip.open(caminho, "rb") as f:
                dados = json.load(f)
            m = dados.get("match", {})
            if m.get("metadata", {}).get("matchId") == match_id:
                return m, dados["timeline"]
    return None


def _facts_for(golden: Golden) -> MatchFacts | None:
    """Destila a perspectiva anotada.

    O `puuid` do golden e um prefixo: as fixtures sao anonimizadas e o valor
    completo nao e estavel entre exportacoes, mas o prefixo e suficiente para
    identificar o jogador dentro da partida.
    """
    carregado = _load_fixture(golden.match_id)
    if carregado is None:
        return None
    match, timeline = carregado
    for p in match["info"]["participants"]:
        if str(p["puuid"]).startswith(golden.puuid):
            return distill(match, timeline, p["puuid"])
    return None


def load_corpus() -> list[Golden]:
    if not GOLDEN_DIR.exists():
        return []
    return [Golden.load(p) for p in sorted(GOLDEN_DIR.glob("*.json"))]


# --------------------------------------------------------------------------
# Execucao
# --------------------------------------------------------------------------


def run_baseline(corpus: list[Golden]) -> ProviderScore:
    """O motor de regras. Sem rede, sem modelo, deterministico."""
    resultado = ProviderScore(provider=BASELINE)
    for g in corpus:
        facts = _facts_for(g)
        if facts is None:
            resultado.matches.append(
                MatchScore(match_id=g.match_id, error="fixture nao encontrada")
            )
            continue
        report, _ = analyze(facts)
        resultado.matches.append(score_report(report, g))
    return resultado


async def run_with_ai(corpus: list[Golden], router: ModelRouter) -> ProviderScore:
    nome = router.describe()
    resultado = ProviderScore(provider=nome)
    db = PatchDB()

    for g in corpus:
        facts = _facts_for(g)
        if facts is None:
            resultado.matches.append(
                MatchScore(match_id=g.match_id, error="fixture nao encontrada")
            )
            continue
        db.use_patch(facts.patch)
        try:
            report, _texto, trace = await analyze_with_ai(
                facts, router, resolver=db, patch_db=db
            )
        except RiftCoachError as e:
            # Um provedor que falha numa partida nao invalida o corpus inteiro;
            # a falha entra na conta como falha, que e informacao.
            resultado.matches.append(MatchScore(match_id=g.match_id, error=e.message))
            continue
        descartados = len(trace.violations) + len(trace.rejected)
        resultado.matches.append(score_report(report, g, dropped=descartados))
    return resultado


# --------------------------------------------------------------------------
# Saida
# --------------------------------------------------------------------------


def _bar(valor: float, largura: int = 12) -> str:
    cheio = round(valor * largura)
    return "█" * cheio + "·" * (largura - cheio)


def print_detail(score: ProviderScore) -> None:
    print(f"\n{'=' * 78}\n{score.provider}\n{'=' * 78}")
    for m in score.matches:
        if m.error:
            print(f"  {m.match_id}: FALHOU — {m.error}")
            continue
        print(
            f"  {m.match_id}: {m.matched}/{m.golden} encontrados · "
            f"{m.reported} reportados · {m.noise} sem correspondencia"
        )
        for s in m.scores:
            if s.matched_golden is None:
                print(f"      [ruido]   {s.reason}")
            elif s.grounding <= 0:
                print(f"      [DESCARTE] fundamentacao: {s.reason}")
            else:
                print(
                    f"      [ok {s.total:.2f}] ancora={s.anchor:.1f} "
                    f"categoria={s.match:.1f} utilidade={s.actionability:.1f}"
                )
        for perdido in m.missed:
            print(f"      [nao viu] {perdido}")


def print_matrix(scores: list[ProviderScore]) -> None:
    print(f"\n{'=' * 78}")
    print("MATRIZ DE COMPATIBILIDADE")
    print("=" * 78)
    print(
        f"{'provedor':<34}{'F1':>7}{'F0.5':>7}{'prec':>7}{'cob':>7}"
        f"{'ruido':>7}{'desc':>7}"
    )
    print("-" * 78)

    # Ordenado por F1, mas a linha de base SEMPRE aparece, em qualquer posicao.
    # Escondê-la seria esconder justamente a comparacao que importa.
    for s in sorted(scores, key=lambda x: -x.f1):
        marca = "  <- linha de base" if s.provider == BASELINE else ""
        print(
            f"{s.provider[:33]:<34}{s.f1:>7.3f}{s.f05:>7.3f}{s.precision:>7.3f}"
            f"{s.recall:>7.3f}{s.noise_per_match:>7.1f}{s.drop_rate:>7.1%}{marca}"
        )

    base = next((s for s in scores if s.provider == BASELINE), None)
    if base is None:
        return

    # A precisao e medida contra o que FOI anotado, e so isso. Com corpus
    # pequeno ela e pessimista por construcao: todo finding correto que o
    # anotador nao escreveu conta como ruido. Dizer isso na saida e o minimo —
    # sem essa linha, alguem vai citar "precisao 0,43" como se fosse uma
    # medida da ferramenta, quando e uma medida do corpus.
    if base.ok and base.noise_per_match > 1.0:
        anotados = sum(m.golden for m in base.ok) / len(base.ok)
        print(
            f"\nAVISO DE CORPUS: {anotados:.0f} findings anotados por partida em media.\n"
            "  Tudo o que o sistema reporta alem disso conta como ruido, mesmo estando\n"
            "  certo — entao a precisao aqui e um piso, nao uma medida da ferramenta.\n"
            "  Anotar mais partidas aperta esse numero. Ver evals/golden/README.md."
        )
    piores = [s for s in scores if s.provider != BASELINE and s.f1 < base.f1]
    if piores:
        print()
        print("ABAIXO DA LINHA DE BASE — estes provedores pioram o produto:")
        for s in piores:
            print(f"  {s.provider}  ({s.f1:.3f} contra {base.f1:.3f})")
        print(
            "  Um modelo pequeno produz finding plausivel e errado com muita\n"
            "  facilidade, e 'plausivel e errado' e exatamente o que este\n"
            "  harness existe para pegar."
        )


def print_summary(scores: list[ProviderScore]) -> None:
    print()
    for s in scores:
        print(f"{s.provider[:40]:<42} F1 {_bar(s.f1)} {s.f1:.3f}")


def save_results(scores: list[ProviderScore]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    agora = datetime.now(UTC)
    caminho = RESULTS_DIR / f"{agora:%Y-%m-%d}.json"
    caminho.write_text(
        json.dumps(
            {
                "ran_at": agora.isoformat(),
                "providers": [s.as_dict() for s in scores],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return caminho


# --------------------------------------------------------------------------


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--with-ai",
        action="store_true",
        help="avaliar tambem os provedores de IA disponiveis",
    )
    ap.add_argument("--save", action="store_true", help="gravar em evals/results/")
    ap.add_argument("--detail", action="store_true", help="mostrar finding a finding")
    args = ap.parse_args()

    corpus = load_corpus()
    if not corpus:
        print(
            "Nenhuma partida anotada em evals/golden/.\n\n"
            "Anotar uma partida e uma das contribuicoes mais valiosas do projeto e\n"
            "nao precisa de Python. Ver evals/rubric.md."
        )
        return 1

    anotados = sum(len(g.findings) for g in corpus)
    print(
        f"Corpus: {len(corpus)} perspectiva(s) anotada(s), {anotados} findings de referencia."
    )
    print(
        "NOTA: o eixo de utilidade e aproximado por heuristica quando o golden nao\n"
        "      traz `actionability_manual`. Ver evals/rubric.md, eixo 4."
    )

    scores = [run_baseline(corpus)]

    if args.with_ai:
        router = await ModelRouter.create()
        try:
            if not router.available:
                print("\nNenhum provedor de IA disponivel — so a linha de base.")
                print("Rode `riftcoach models` para ver por que.")
            else:
                for nome, motivo in sorted(router.substitutions.items()):
                    print(f"  {nome}: {motivo}")
                scores.append(await run_with_ai(corpus, router))
        finally:
            await router.aclose()

    if args.detail:
        for s in scores:
            print_detail(s)

    print_matrix(scores)
    print_summary(scores)

    if args.save:
        print(f"\nGravado em {save_results(scores)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
