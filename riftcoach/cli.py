"""CLI do RiftCoach.

Tudo precisa funcionar headless: usuarios avancados e o CI dependem disso.
A interface web (etapa 5) e uma casca sobre estas mesmas funcoes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from riftcoach.analysis.report import analyze as build_l5
from riftcoach.analysis.report import analyze_with_ai as build_with_ai
from riftcoach.config import (
    PLATFORM_TO_ROUTING,
    delete_key_from_keyring,
    settings,
    write_key_to_keyring,
)
from riftcoach.core.errors import ParseError, RiftCoachError
from riftcoach.knowledge.sync import PatchDB
from riftcoach.llm.hardware import probe as probe_hardware
from riftcoach.llm.router import ModelRouter, text_task
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import PARSER_VERSION, MatchFacts
from riftcoach.riot.cache import RiotCache
from riftcoach.riot.client import RiotClient

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Coach de League of Legends open source, gratuito e compativel com as regras da Riot.",
)
console = Console()


def _version() -> str:
    """Versao instalada, ou a do pyproject quando rodando do repositorio."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("riftcoach")
    except PackageNotFoundError:
        return "dev"


def _version_callback(value: bool) -> None:
    if value:
        # O template de issue pede esta saida, entao ela precisa carregar o que
        # muda um relatorio: a versao do pacote E a do parser.
        console.print(f"riftcoach {_version()} · parser v{PARSER_VERSION}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    pass


def _split_riot_id(riot_id: str) -> tuple[str, str]:
    if "#" not in riot_id:
        raise typer.BadParameter(
            f"Riot ID precisa estar no formato Nome#TAG (recebido: {riot_id!r})"
        )
    name, tag = riot_id.rsplit("#", 1)
    if not name or not tag:
        raise typer.BadParameter("Nome e TAG nao podem ser vazios.")
    return name.strip(), tag.strip()


def _run(coro: object) -> None:
    """Executa uma corrotina e traduz erros do dominio em saida amigavel."""
    try:
        asyncio.run(coro)  # type: ignore[arg-type]
    except RiftCoachError as e:
        console.print(f"[bold red]Erro:[/] {e.message}")
        if e.hint:
            console.print(f"[yellow]{e.hint}[/]")
        raise typer.Exit(code=1) from None


@app.command()
def auth(
    key: Annotated[
        str, typer.Option(prompt="Chave da API da Riot (RGAPI-...)", hide_input=True)
    ],
) -> None:
    """Guarda a chave da Riot no cofre de credenciais do sistema operacional."""
    key = key.strip()
    if not key.startswith("RGAPI-"):
        console.print("[yellow]Aviso:[/] chaves normalmente comecam com 'RGAPI-'.")
    if write_key_to_keyring("riot", key):
        console.print("[green]Chave guardada no cofre do sistema.[/]")
        console.print(
            "[dim]Se voce tinha a chave em um arquivo de texto, apague-o agora.[/]"
        )
    else:
        console.print(
            "[red]Nao foi possivel acessar o cofre de credenciais.[/]\n"
            "Use a variavel de ambiente RIOT_API_KEY."
        )
        raise typer.Exit(code=1)


@app.command()
def logout() -> None:
    """Remove a chave da Riot do cofre."""
    if delete_key_from_keyring("riot"):
        console.print("[green]Chave removida.[/]")
    else:
        console.print("[yellow]Nenhuma chave encontrada no cofre.[/]")


@app.command()
def doctor(
    riot_id: Annotated[
        str | None,
        typer.Option("--riot-id", help="Nome#TAG — testa a cadeia completa de APIs"),
    ] = None,
) -> None:
    """Diagnostico: configuracao, chave, cache e cada API que o projeto usa."""

    async def probe() -> None:
        """Testa cada API separadamente e mostra a mensagem crua da Riot.

        As APIs usam hosts diferentes (regional vs plataforma) e podem falhar
        de formas diferentes — testar em bloco esconde qual delas e o problema.
        """
        results = Table("API", "Host", "Status", "Detalhe")
        async with RiotClient() as rc:
            checks: list[tuple[str, str, object]] = [
                ("LOL-STATUS-V4", settings.riot_platform, rc.platform_status()),
            ]
            if riot_id:
                name, tag = _split_riot_id(riot_id)
                checks.append(
                    ("ACCOUNT-V1", rc.routing, rc.account_by_riot_id(name, tag))
                )

            puuid: str | None = None
            for label, host, coro in checks:
                try:
                    out = await coro  # type: ignore[misc]
                    detail = "ok"
                    if label == "ACCOUNT-V1" and isinstance(out, dict):
                        puuid = out.get("puuid")
                        detail = f"{out.get('gameName')}#{out.get('tagLine')}"
                    results.add_row(label, host, "[green]ok[/]", detail)
                except RiftCoachError as e:
                    results.add_row(label, host, "[red]falhou[/]", e.message)
                    console.print(results)
                    if e.hint:
                        console.print(f"\n[yellow]{e.hint}[/]")
                    return

            if puuid:
                for label, host, coro in (
                    ("MATCH-V5", rc.routing, rc.match_ids(puuid, count=1)),
                    ("LEAGUE-V4", settings.riot_platform, rc.league_entries(puuid)),
                ):
                    try:
                        out = await coro  # type: ignore[misc]
                        n = len(out) if isinstance(out, list) else 0
                        results.add_row(
                            label, host, "[green]ok[/]", f"{n} registro(s)"
                        )
                    except RiftCoachError as e:
                        results.add_row(label, host, "[red]falhou[/]", e.message)
        console.print(results)
        if not riot_id:
            console.print(
                "\n[dim]Passe --riot-id \"Nome#TAG\" para testar ACCOUNT-V1, "
                "MATCH-V5 e LEAGUE-V4 tambem.[/]"
            )

    async def main() -> None:
        table = Table(show_header=False, box=None)
        table.add_row("Plataforma", settings.riot_platform)
        table.add_row("Roteamento", settings.routing)
        table.add_row("Locale", settings.locale)
        table.add_row("Modo de privacidade", settings.privacy_mode)

        key = settings.resolve_api_key()
        if key:
            table.add_row("Chave da Riot", f"[green]configurada[/] ({key[:11]}...)")
        else:
            table.add_row("Chave da Riot", "[red]ausente[/] — rode: riftcoach auth")

        async with RiotCache() as c:
            st = await c.stats()
            table.add_row(
                "Cache",
                f"{st.entries} entradas · {st.stored_bytes / 1e6:.1f} MB "
                f"(de {st.raw_bytes / 1e6:.1f} MB brutos, {st.ratio:.1f}x)",
            )
            table.add_row("Arquivo de cache", str(c.path))

        console.print(table)
        console.print()

        if key:
            await probe()
        else:
            console.print("[yellow]Sem chave configurada — APIs nao testadas.[/]")

    _run(main())


@app.command("sync-patch")
def sync_patch(
    patch: Annotated[
        str | None, typer.Option("--patch", help="ex.: 16.9. Padrao: o mais recente")
    ] = None,
) -> None:
    """Baixa nomes e stats de itens/runas/campeoes do DataDragon."""

    async def main() -> None:
        db = PatchDB()
        alvo = patch or "mais recente"
        console.print(f"[dim]Sincronizando patch {alvo}...[/]")
        ddv = await db.sync(patch)
        console.print(f"[green]Patch sincronizado[/] (DataDragon {ddv})")
        console.print(f"[dim]Patches disponiveis: {', '.join(db.available_patches())}[/]")
        console.print(f"[dim]{db.path}[/]")

    _run(main())


@app.command("sync-match-patches")
def sync_match_patches() -> None:
    """Sincroniza todos os patches das partidas em cache.

    Um relatorio SEMPRE consulta o patch em que a partida foi jogada — nunca o
    mais recente. Sem isso, uma partida de seis patches atras receberia stats
    de itens que nem existiam nela.
    """

    async def main() -> None:
        async with RiotCache() as cache:
            patches = set()
            for k in await cache.keys("match:"):
                m = await cache.get(k)
                if m:
                    patches.add(
                        ".".join(m["info"].get("gameVersion", "").split(".")[:2])
                    )
        db = PatchDB()
        for p in sorted(x for x in patches if x):
            try:
                ddv = await db.sync(p)
                console.print(f"  [green]{p}[/] (DataDragon {ddv})")
            except RiftCoachError as e:
                console.print(f"  [yellow]{p}[/] {e.message}")

    _run(main())


@app.command()
def whoami(
    riot_id: Annotated[str, typer.Argument(help="Nome#TAG")],
    platform: Annotated[str | None, typer.Option("--platform", "-p")] = None,
) -> None:
    """Resolve um Riot ID para o PUUID."""

    async def main() -> None:
        if platform:
            settings.riot_platform = platform
        name, tag = _split_riot_id(riot_id)
        async with RiotClient() as rc:
            acct = await rc.account_by_riot_id(name, tag)
            console.print(
                f"[bold]{acct.get('gameName')}#{acct.get('tagLine')}[/]\n"
                f"puuid: [cyan]{acct['puuid']}[/]\n"
                f"roteamento: {rc.routing}"
            )

    _run(main())


@app.command()
def fetch(
    riot_id: Annotated[str, typer.Argument(help="Nome#TAG")],
    count: Annotated[int, typer.Option("--count", "-n", help="Partidas a baixar")] = 5,
    queue: Annotated[
        int | None, typer.Option("--queue", "-q", help="420=ranked solo, 440=flex")
    ] = None,
    platform: Annotated[str | None, typer.Option("--platform", "-p")] = None,
    timeline: Annotated[bool, typer.Option("--timeline/--no-timeline")] = True,
) -> None:
    """Baixa partidas recentes para o cache local (permanente)."""

    async def main() -> None:
        if platform:
            if platform.lower() not in PLATFORM_TO_ROUTING:
                console.print(f"[red]Plataforma desconhecida:[/] {platform}")
                raise typer.Exit(code=1)
            settings.riot_platform = platform

        name, tag = _split_riot_id(riot_id)
        async with RiotClient() as rc:
            acct = await rc.account_by_riot_id(name, tag)
            puuid = acct["puuid"]
            console.print(
                f"[bold]{acct.get('gameName')}#{acct.get('tagLine')}[/] "
                f"[dim]({rc.routing})[/]"
            )

            ids = await rc.match_ids(puuid, count=count, queue=queue)
            if not ids:
                console.print("[yellow]Nenhuma partida encontrada.[/]")
                return

            table = Table("Partida", "Dados", "Timeline", "Duracao", "Resultado")
            for mid in ids:
                cached_m = await rc.cache.has(f"match:{rc.routing}:{mid}")
                m = await rc.match(mid)
                info = m["info"]
                me = next(
                    (p for p in info["participants"] if p["puuid"] == puuid), None
                )
                tl_mark = "[dim]-[/]"
                if timeline:
                    cached_t = await rc.cache.has(f"timeline:{rc.routing}:{mid}")
                    await rc.timeline(mid)
                    tl_mark = "[dim]cache[/]" if cached_t else "[green]novo[/]"

                dur = info.get("gameDuration", 0)
                result = "?"
                if me is not None:
                    champ = me.get("championName", "?")
                    kda = f"{me['kills']}/{me['deaths']}/{me['assists']}"
                    result = (
                        f"{'[green]V[/]' if me.get('win') else '[red]D[/]'} "
                        f"{champ} {kda}"
                    )
                table.add_row(
                    mid,
                    "[dim]cache[/]" if cached_m else "[green]novo[/]",
                    tl_mark,
                    f"{dur // 60}:{dur % 60:02d}",
                    result,
                )
            console.print(table)

            st = await rc.cache.stats()
            console.print(
                f"[dim]Cache: {st.entries} entradas · "
                f"{st.stored_bytes / 1e6:.1f} MB · compressao {st.ratio:.1f}x[/]"
            )

    _run(main())


async def _tier_of(rc: RiotClient, puuid: str) -> str | None:
    """Elo do jogador, para escolher a faixa de benchmarks.

    Falhar aqui NAO e fatal e nem sequer e incomum: fora de ranked a league-v4
    nao devolve nada, e esse e o caso normal em normal/quickplay/ARAM. Sem elo,
    os benchmarks caem na faixa padrao e dizem isso no relatorio.
    """
    try:
        entradas = await rc.league_entries(puuid)
    except RiftCoachError:
        return None
    # RANKED_SOLO_5x5 primeiro; flex so se solo nao existir.
    ordem = {"RANKED_SOLO_5x5": 0, "RANKED_FLEX_SR": 1}
    ranked = sorted(
        (e for e in entradas if e.get("queueType") in ordem),
        key=lambda e: ordem[str(e["queueType"])],
    )
    return str(ranked[0]["tier"]) if ranked else None


async def _history_from_cache(
    cache: RiotCache, routing: str, puuid: str, limit: int = 50
) -> list[MatchFacts]:
    """Partidas do usuario que JA estao no cache, destiladas.

    De proposito nao busca nada na rede: o objetivo e que os benchmarks do
    proprio historico melhorem sozinhos conforme a pessoa usa `fetch`, sem
    transformar um `analyze` em dezenas de chamadas a API. Quem quiser mais
    amostra roda `fetch -n 50` uma vez.
    """
    out: list[MatchFacts] = []
    for key in await cache.keys("match:"):
        if len(out) >= limit:
            break
        match_id = key.rsplit(":", 1)[-1]
        if not await cache.has(f"timeline:{routing}:{match_id}"):
            continue
        match = await cache.get(key)
        timeline = await cache.get(f"timeline:{routing}:{match_id}")
        if not match or not timeline:
            continue
        try:
            out.append(distill(match, timeline, puuid))
        except (ParseError, KeyError, ValueError):
            # Partida de outra pessoa no mesmo cache, ou fila que o parser nao
            # cobre. Nenhuma das duas e erro: so nao entra na amostra.
            continue
    return out


@app.command()
def analyze(
    riot_id: Annotated[str, typer.Argument(help="Nome#TAG")],
    match_id: Annotated[
        str | None,
        typer.Option("--match", "-m", help="Partida especifica. Padrao: a mais recente"),
    ] = None,
    last: Annotated[
        int, typer.Option("--last", "-l", help="Analisar a N-esima partida mais recente")
    ] = 1,
    queue: Annotated[
        int | None, typer.Option("--queue", "-q", help="420=ranked solo, 440=flex")
    ] = None,
    platform: Annotated[str | None, typer.Option("--platform", "-p")] = None,
    history: Annotated[
        bool,
        typer.Option(
            "--history/--no-history",
            help="Usar o seu proprio historico em cache como referencia de percentil",
        ),
    ] = True,
    ai: Annotated[
        bool,
        typer.Option(
            "--ai/--no-ai",
            help="Usar IA quando houver provedor. --no-ai forca o relatorio deterministico",
        ),
    ] = True,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Gravar o relatorio num arquivo")
    ] = None,
) -> None:
    """Relatorio de coaching de uma partida.

    Com IA disponivel, quatro analistas especialistas rodam em paralelo sobre os
    fatos ja medidos, e um head coach unifica. Sem IA — sem GPU, sem chave, sem
    cota — sai o relatorio deterministico completo: metricas, benchmarks e motor
    de regras, calculados em Python nesta maquina.

    O rodape sempre diz qual dos dois voce recebeu.
    """

    async def main() -> None:
        if platform:
            if platform.lower() not in PLATFORM_TO_ROUTING:
                console.print(f"[red]Plataforma desconhecida:[/] {platform}")
                raise typer.Exit(code=1)
            settings.riot_platform = platform

        name, tag = _split_riot_id(riot_id)
        async with RiotClient() as rc:
            acct = await rc.account_by_riot_id(name, tag)
            puuid = acct["puuid"]

            alvo = match_id
            if alvo is None:
                ids = await rc.match_ids(puuid, count=max(1, last), queue=queue)
                if not ids:
                    console.print("[yellow]Nenhuma partida encontrada.[/]")
                    return
                if last > len(ids):
                    console.print(
                        f"[yellow]So achei {len(ids)} partidas; usando a mais antiga.[/]"
                    )
                alvo = ids[min(last, len(ids)) - 1]

            console.print(f"[dim]Analisando {alvo}...[/]")
            match = await rc.match(alvo)
            timeline = await rc.timeline(alvo)
            facts = distill(match, timeline, puuid)

            tier = await _tier_of(rc, puuid)

            anteriores: list[MatchFacts] = []
            if history:
                anteriores = [
                    f
                    for f in await _history_from_cache(rc.cache, rc.routing, puuid)
                    if f.match_id != facts.match_id
                ]

        # O relatorio consulta SEMPRE o patch em que a partida foi jogada,
        # nunca o mais recente — senao uma partida antiga receberia stats de
        # itens que nem existiam nela.
        db = PatchDB()
        db.use_patch(facts.patch)

        if ai:
            router = await ModelRouter.create()
            try:
                if not router.available:
                    console.print(
                        "[dim]Nenhum provedor de IA disponivel — relatorio deterministico.[/]"
                    )
                else:
                    console.print(f"[dim]IA: {router.describe()}[/]")
                _report, texto, _trace = await build_with_ai(
                    facts,
                    router,
                    tier=tier,
                    history=anteriores or None,
                    resolver=db,
                    patch_db=db,
                )
            finally:
                await router.aclose()
        else:
            _report, texto = build_l5(
                facts, tier=tier, history=anteriores or None, resolver=db
            )

        if output:
            output.write_text(texto, encoding="utf-8")
            console.print(f"[green]Relatorio gravado em[/] {output}")
        else:
            # print() cru, nao console.print(): o relatorio ja vem formatado e
            # a marcacao do rich comeria os colchetes dos niveis de evidencia.
            print(texto)

    _run(main())


@app.command()
def web(
    riot_id: Annotated[str, typer.Argument(help="Nome#TAG")],
    match_id: Annotated[
        str | None, typer.Option("--match", "-m", help="Partida especifica")
    ] = None,
    last: Annotated[int, typer.Option("--last", "-l")] = 1,
    ai: Annotated[bool, typer.Option("--ai/--no-ai")] = True,
    port: Annotated[int, typer.Option("--port", "-p")] = 8770,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Abre o relatorio no navegador, com linha do tempo e controle do replay.

    O servidor escuta SO em 127.0.0.1. Se o client do League estiver rodando um
    replay, clicar num finding faz o jogo pular para 8 segundos ANTES do
    momento — o erro e a decisao, nao a consequencia.
    """
    from riftcoach.api.app import prepare_session, serve

    async def preparar() -> None:
        console.print("[dim]Analisando...[/]")
        sessao = await prepare_session(
            riot_id, match_id=match_id, last=last, use_ai=ai
        )
        assert sessao.facts is not None
        f = sessao.facts
        console.print(
            f"[bold]{f.focus.champion}[/] {f.focus.kills}/{f.focus.deaths}/{f.focus.assists} "
            f"· {len(sessao.report.findings) if sessao.report else 0} findings"
        )
        if sessao.replay_path:
            console.print(f"[dim]Replay encontrado: {sessao.replay_path.name}[/]")
        else:
            console.print(
                "[dim]Nenhum .rofl encontrado — o relatorio funciona igual, "
                "so sem o botao de pular.[/]"
            )

    _run(preparar())
    console.print(f"\n[green]http://127.0.0.1:{port}/[/]  [dim](Ctrl+C para parar)[/]")
    try:
        serve(port=port, open_browser=open_browser)
    except RiftCoachError as e:
        console.print(f"[bold red]Erro:[/] {e.message}")
        if e.hint:
            console.print(f"[yellow]{e.hint}[/]")
        raise typer.Exit(code=1) from None


@app.command()
def models(
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Redetectar o hardware, ignorando o cache")
    ] = False,
) -> None:
    """Hardware detectado, provedores de IA disponiveis e por que os outros nao.

    Este comando existe porque "a IA nao rodou" precisa ter uma resposta. Sem
    ele, um provedor fora do ar e um provedor sem chave sao indistinguiveis do
    lado de fora.
    """

    async def main() -> None:
        hw = await probe_hardware(refresh=refresh)
        console.print(f"[bold]Hardware[/]  {hw.describe()}")
        if hw.ollama_up:
            baixados = ", ".join(hw.ollama_models) or "nenhum modelo baixado"
            console.print(f"[bold]Ollama[/]    no ar · {baixados}")
        else:
            console.print("[bold]Ollama[/]    [yellow]nao esta respondendo[/]")

        console.print(f"[bold]Privacidade[/] {settings.privacy_mode}", end="")
        if settings.privacy_mode == "strict":
            console.print(" [dim](provedores de nuvem sao filtrados no roteador)[/]")
        else:
            console.print()
        console.print()

        router = await ModelRouter.create(hardware=hw)
        try:
            tabela = Table("Provedor", "Custo", "Privacidade", "Contexto")
            for p in router.providers:
                perfil = p.profile
                tabela.add_row(
                    perfil.name,
                    perfil.cost_class,
                    "local" if not perfil.leaves_machine else "sai da maquina",
                    f"{perfil.ctx_tokens:,}".replace(",", "."),
                )
            if router.providers:
                console.print(tabela)
            else:
                console.print("[yellow]Nenhum provedor de IA disponivel.[/]")

            for nome, motivo in sorted(router.substitutions.items()):
                console.print(f"[yellow]{nome}:[/] {motivo}")

            tarefa = text_task("laning_analyst", 1400)
            try:
                escolhido = router.select(tarefa)
                console.print(
                    f"\n[green]Escolhido para um passe de analista:[/] "
                    f"{escolhido.profile.name}"
                )
            except RiftCoachError as e:
                console.print(f"\n[yellow]{e.hint or e.message}[/]")
                console.print(
                    "\n[dim]O relatorio sem IA nao depende de nada disto — "
                    "`riftcoach analyze` continua funcionando.[/]"
                )
        finally:
            await router.aclose()

    _run(main())


@app.command("cache")
def cache_cmd(
    prefix: Annotated[str, typer.Option("--prefix", help="match: / timeline: / account:")] = "",
) -> None:
    """Inspeciona o cache local."""

    async def main() -> None:
        async with RiotCache() as c:
            st = await c.stats()
            console.print(
                f"[bold]{st.entries}[/] entradas · "
                f"{st.stored_bytes / 1e6:.1f} MB armazenados · "
                f"{st.raw_bytes / 1e6:.1f} MB brutos · "
                f"compressao [green]{st.ratio:.1f}x[/]\n[dim]{c.path}[/]"
            )
            if prefix:
                for k in await c.keys(prefix):
                    console.print(f"  {k}")

    _run(main())


if __name__ == "__main__":
    app()
