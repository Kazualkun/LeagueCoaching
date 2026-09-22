"""Testes da interface web.

O que importa aqui nao e o HTML — e o contrato entre o backend e a tela:

  - o payload carrega NIVEL e PREMISSA de cada evidencia. Se a UI nao puder
    mostrar isso, o relatorio vira "confie em mim", que e exatamente o que o
    projeto inteiro existe para nao ser.
  - recusa do client vira 409 com explicacao, nunca 500 generico. "Nao ha
    replay rodando" e um estado esperado, e a UI precisa distinguir isso de
    um bug.
  - a API NAO fala com a porta 2999. Ela passa pelo guard, sempre.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

pytest.importorskip("fastapi", reason="a interface web e um extra opcional")

from fastapi.testclient import TestClient

from riftcoach.analysis.report import analyze
from riftcoach.api import app as api
from riftcoach.knowledge.benchmarks import BenchmarkTable
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts
from riftcoach.replay.guard import BASE as GUARD_BASE

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def garen() -> MatchFacts:
    with gzip.open(FIXTURES / "sr_ranked_35min.json.gz", "rb") as f:
        d = json.load(f)
    return distill(d["match"], d["timeline"], d["match"]["info"]["participants"][0]["puuid"])


@pytest.fixture
def client(garen: MatchFacts) -> Any:
    report, _ = analyze(garen)
    api.SESSION.facts = garen
    api.SESSION.report = report
    api.SESSION.benchmarks = BenchmarkTable(patch=garen.patch).evaluate(garen)
    api.SESSION.trace = None
    api.SESSION.replay_path = None
    return TestClient(api.create_app())


@pytest.fixture
def empty_client() -> Any:
    api.SESSION.facts = None
    api.SESSION.report = None
    api.SESSION.benchmarks = []
    api.SESSION.replay_path = None
    return TestClient(api.create_app())


# --------------------------------------------------------------------------
# O contrato com a tela
# --------------------------------------------------------------------------


def test_the_report_carries_every_evidence_tier_and_assumption(client: Any) -> None:
    """A informacao mais importante da tela. Sem ela o usuario nao consegue
    discordar de forma util, e discordar de forma util e o melhor relato de
    bug que este projeto recebe."""
    d = client.get("/api/report").json()
    assert d["findings"]

    viu_derivada = False
    for f in d["findings"]:
        assert f["evidence"], f"{f['claim']} sem evidencia"
        for e in f["evidence"]:
            assert e["tier"] in ("T1", "T2", "T3")
            if e["tier"] != "T1":
                viu_derivada = True
                assert e["assumption"], "T2/T3 precisa carregar a premissa ate a tela"
    assert viu_derivada, "o payload nunca exercitou o caminho de evidencia derivada"


def test_the_seek_anchor_travels_to_the_ui(client: Any) -> None:
    """O erro e a decisao, nao o desfecho: a tela precisa do ponto de seek, nao
    so do timestamp do evento."""
    d = client.get("/api/report").json()
    for f in d["findings"]:
        assert f["seek_ms"] <= f["timestamp_ms"]
        assert f["timestamp_ms"] - f["seek_ms"] <= 8_000
        assert ":" in f["seek_at"]


def test_the_payload_says_whether_a_model_was_used(client: Any) -> None:
    """Um relatorio com IA e um que degradou precisam ser distinguiveis na
    tela, nao so no rodape do texto."""
    d = client.get("/api/report").json()
    assert d["used_ai"] is False
    assert d["model_trace"] == {}


def test_the_advantage_curve_is_one_point_per_minute(client: Any, garen: MatchFacts) -> None:
    d = client.get("/api/report").json()
    assert len(d["advantage"]) == len(garen.team_gold_diff_series)
    assert all(0 <= p["wp"] <= 100 for p in d["advantage"])


def test_benchmarks_carry_their_provenance(client: Any) -> None:
    """Um percentil do modelo embarcado e um chute educado; um do parquet e uma
    medicao. Apagar a diferenca na tela seria o mesmo que apagar no texto."""
    d = client.get("/api/report").json()
    assert d["benchmarks"]
    for b in d["benchmarks"]:
        assert b["source"] in ("model", "history", "parquet")
        assert b["source_label"]


def test_findings_arrive_already_ranked(client: Any) -> None:
    d = client.get("/api/report").json()
    sev = [f["severity"] for f in d["findings"]]
    assert sev == sorted(sev, reverse=True)


def test_the_page_is_served(client: Any) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert b"RiftCoach" in r.content


# --------------------------------------------------------------------------
# Estados vazios
# --------------------------------------------------------------------------


def test_no_session_is_a_404_not_a_crash(empty_client: Any) -> None:
    assert empty_client.get("/api/report").status_code == 404
    assert empty_client.post("/api/seek/0").status_code == 404


def test_an_out_of_range_finding_is_a_404(client: Any) -> None:
    assert client.post("/api/seek/999").status_code == 404
    assert client.post("/api/seek/-1").status_code == 404


# --------------------------------------------------------------------------
# A ponte com o client do League
# --------------------------------------------------------------------------


def test_replay_status_reports_absence_as_data_not_error(client: Any) -> None:
    """"Nao ha replay rodando" e o estado NORMAL. Transformar isso em 4xx
    faria a UI piscar erro o tempo todo."""
    r = client.get("/api/replay/status")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is False
    assert d["reason"]


@pytest.mark.parametrize(
    "falha",
    [
        httpx.ConnectError("recusada"),
        httpx.ReadTimeout("demorou"),
        httpx.ConnectTimeout("handshake pendurado"),
    ],
    ids=["conexao-recusada", "leitura-expirou", "conexao-expirou"],
)
@respx.mock
def test_a_refused_seek_is_409_with_an_explanation(
    client: Any, falha: Exception
) -> None:
    """409 e nao 500: nao ha replay rodando, e essa e uma condicao esperada que
    o usuario resolve abrindo um. A mensagem do guard vai junto.

    O transporte e mockado de proposito. A versao anterior deste teste abria
    conexao REAL com 127.0.0.1:2999 e passava so em maquina onde a porta recusa
    na hora — onde o handshake pendura (client do League aberto, ou firewall
    que descarta em vez de recusar) ela caia no ramo de timeout e falhava.
    Teste que depende do que esta ouvindo numa porta local nao prova nada.

    Parametrizar sobre os modos de falha tambem cobre a propriedade que
    interessa: TODA recusa precisa ser acionavel, nao apenas a mais comum.
    """
    respx.get(f"{GUARD_BASE}/replay/playback").mock(side_effect=falha)
    api.SESSION.replay_path = Path("fake.rofl")

    r = client.post("/api/seek/0")

    assert r.status_code == 409
    d = r.json()
    assert d["error"]
    # A mensagem precisa dizer ONDE falhou ou O QUE fazer — senao o usuario
    # nao distingue recusa esperada de bug.
    assert "2999" in d["error"] or "replay" in d["error"].lower()


def test_the_api_never_opens_its_own_connection_to_the_client() -> None:
    """Testado tambem em test_replay.py, e repetido aqui de proposito: esta e
    a propriedade que faz a promessa de capa do README ser verdade, e ela
    precisa quebrar o build de qualquer lado que for violada."""
    fonte = (Path(api.__file__)).read_text(encoding="utf-8")
    assert "open_guard" in fonte
    assert "httpx.AsyncClient" not in fonte


# --------------------------------------------------------------------------
# A pagina
# --------------------------------------------------------------------------


def test_the_page_escapes_what_the_model_wrote() -> None:
    """A claim e a fix vem de um LLM. Injetar HTML a partir dai seria XSS com
    passos extras."""
    html = (Path(api.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "const esc =" in html
    assert "esc(f.claim)" in html
    assert "esc(f.fix)" in html
    assert "esc(e.statement)" in html
    assert "esc(e.assumption)" in html


def test_the_page_distinguishes_the_three_tiers_visually() -> None:
    """Os niveis precisam ser distinguiveis SEM ler o rotulo: e a informacao
    que decide o quanto confiar em cada linha."""
    html = (Path(api.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert ".ev li.t1" in html and ".ev li.t2" in html and ".ev li.t3" in html
    assert "border-left-style: dashed" in html
    assert "border-left-style: dotted" in html


def test_the_page_needs_no_build_step() -> None:
    """O desvio consciente do plano: a pasta static/ tem UM arquivo e nenhum
    package.json. Se isso mudar, a decisao precisa ser deliberada."""
    static = Path(api.__file__).parent / "static"
    arquivos = sorted(p.name for p in static.iterdir())
    assert arquivos == ["index.html"], f"static/ ganhou arquivos: {arquivos}"
