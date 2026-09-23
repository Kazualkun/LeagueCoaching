"""O assistente guiado. O que abre quando alguem da dois cliques no .bat.

QUEM USA ISTO NAO SABE O QUE E TERMINAL. Nem Python, nem uv, nem chave de API.
Isso muda tudo:

  - UM passo de cada vez. Nunca pedir duas coisas na mesma tela.
  - Sempre dizer qual e o PROXIMO passo, mesmo quando deu certo.
  - Nenhum jargao sem traducao. "PUUID" nao significa nada para quem joga.
  - Erro nunca e beco sem saida: toda falha termina com o que fazer agora.
  - Idempotente. Rodar de novo nunca desfaz nada nem repete pergunta ja
    respondida — a pessoa VAI fechar no meio e reabrir.

O `riftcoach doctor` continua existindo para quem entende de terminal. Este
modulo e o contrario dele: o doctor diz o que esta errado, o assistente resolve.
"""

from __future__ import annotations

import asyncio
import webbrowser

import typer
from rich.console import Console
from rich.panel import Panel

from riftcoach.config import (
    load_prefs,
    normalize_platform,
    save_pref,
    settings,
    write_key_to_keyring,
)
from riftcoach.core.errors import RiftCoachError

console = Console()

PORTAL = "https://developer.riotgames.com"


def _titulo(n: int, total: int, texto: str) -> None:
    console.print()
    console.print(f"[bold cyan]Passo {n} de {total}[/] · {texto}")
    console.print("[dim]" + "─" * 62 + "[/]")


def _ok(texto: str) -> None:
    console.print(f"  [green]OK[/] {texto}")


def _erro(texto: str, faca: str) -> None:
    console.print(f"  [red]Problema:[/] {texto}")
    console.print(f"  [yellow]O que fazer:[/] {faca}")


# --------------------------------------------------------------------------
# Passo 1 — a chave da Riot
# --------------------------------------------------------------------------


def _pedir_chave() -> str | None:
    console.print(
        Panel(
            "O RiftCoach le suas partidas pela API oficial da Riot.\n"
            "Para isso ele precisa de uma chave — que e [bold]gratuita[/].\n\n"
            f"1. Abra  [cyan]{PORTAL}[/]\n"
            "2. Entre com a sua conta da Riot\n"
            "3. Copie a chave que comeca com [cyan]RGAPI-[/]\n\n"
            "[dim]A chave fica guardada no cofre de senhas do Windows.\n"
            "Ela nunca e gravada em arquivo nem enviada para lugar nenhum.[/]",
            title="Chave da Riot",
            border_style="cyan",
        )
    )
    if typer.confirm("  Abrir o site agora no navegador?", default=True):
        webbrowser.open(PORTAL)

    chave = str(typer.prompt("  Cole a chave aqui", hide_input=True)).strip()
    if not chave:
        return None
    if not chave.startswith("RGAPI-"):
        console.print(
            "  [yellow]Atencao:[/] chaves da Riot comecam com 'RGAPI-'. "
            "Confira se copiou a linha certa."
        )
    return chave


async def _chave_funciona(chave: str) -> tuple[bool, str]:
    from riftcoach.riot.client import RiotClient

    try:
        async with RiotClient(api_key=chave) as rc:
            await rc.platform_status()
        return True, ""
    except RiftCoachError as e:
        return False, e.message


def passo_chave() -> bool:
    _titulo(1, 4, "Conectar com a Riot")

    chave = settings.resolve_api_key()
    if chave:
        ok, motivo = asyncio.run(_chave_funciona(chave))
        if ok:
            _ok("sua chave da Riot esta funcionando")
            return True
        console.print(f"  [yellow]A chave guardada parou de funcionar:[/] {motivo}")
        console.print(
            "  [dim]Chaves de desenvolvimento expiram a cada 24h. Vamos pegar uma nova.[/]"
        )

    for tentativa in range(3):
        nova = _pedir_chave()
        if not nova:
            _erro("nenhuma chave foi colada", "rode o RiftCoach de novo quando tiver a chave")
            return False
        ok, motivo = asyncio.run(_chave_funciona(nova))
        if ok:
            write_key_to_keyring("riot", nova)
            _ok("chave validada e guardada com seguranca")
            return True
        console.print(f"  [red]A Riot recusou essa chave:[/] {motivo}")
        if "Unknown" in motivo and tentativa < 2:
            console.print(
                "  [yellow]Dica:[/] o site substitui a chave toda vez que gera "
                "uma nova. Copie a que esta la [bold]agora[/] e cole de novo."
            )
    _erro("nao consegui validar nenhuma chave", f"confira em {PORTAL} e tente mais tarde")
    return False


# --------------------------------------------------------------------------
# Passo 2 — quem e voce no jogo
# --------------------------------------------------------------------------


def passo_riot_id() -> str | None:
    _titulo(2, 4, "Identificar sua conta")

    prefs = load_prefs()
    salvo = prefs.get("riot_id")
    if salvo:
        _ok(f"analisando como [bold]{salvo}[/]")
        if not typer.confirm("  Usar outra conta?", default=False):
            return salvo

    console.print(
        Panel(
            "Precisamos do seu [bold]Riot ID[/] — aquele no formato\n"
            "[cyan]Nome#TAG[/], que aparece no canto do client do League.\n\n"
            "[dim]Nao e o nome de invocador antigo. A TAG nem sempre e BR1;\n"
            "pode ser qualquer coisa que voce escolheu.[/]",
            title="Sua conta",
            border_style="cyan",
        )
    )
    riot_id = str(typer.prompt("  Seu Riot ID (exemplo: Fulano#BR1)")).strip()
    if "#" not in riot_id:
        _erro(
            "faltou o # no meio",
            "o formato correto e Nome#TAG, por exemplo Fulano#BR1",
        )
        return None

    plataforma_digitada = typer.prompt(
        "  Sua regiao", default=prefs.get("platform", settings.riot_platform)
    ).strip()
    try:
        plataforma = normalize_platform(plataforma_digitada)
    except RiftCoachError as e:
        _erro(e.message, e.hint or "")
        return None

    save_pref("riot_id", riot_id)
    save_pref("platform", plataforma)
    settings.riot_platform = plataforma
    _ok(f"vou analisar as partidas de [bold]{riot_id}[/]")
    return riot_id


# --------------------------------------------------------------------------
# Passo 3 — dados do patch e partidas
# --------------------------------------------------------------------------


def passo_dados(riot_id: str) -> bool:
    _titulo(3, 4, "Baixar suas partidas")

    from riftcoach.knowledge.sync import PatchDB
    from riftcoach.riot.client import RiotClient

    async def baixar() -> bool:
        db = PatchDB()
        if not db.available_patches():
            console.print("  [dim]Baixando nomes de itens e runas...[/]")
            await db.sync()

        nome, tag = riot_id.split("#", 1)
        async with RiotClient() as rc:
            conta = await rc.account_by_riot_id(nome, tag)
            ids = await rc.match_ids(conta["puuid"], count=5, queue=420)
            if not ids:
                ids = await rc.match_ids(conta["puuid"], count=5)
            if not ids:
                _erro(
                    "nenhuma partida encontrada nessa conta",
                    "confira se o Riot ID e a regiao estao certos",
                )
                return False

            console.print(f"  [dim]Baixando {len(ids)} partidas...[/]")
            baixadas = 0
            for mid in ids:
                try:
                    partida = await rc.match(mid)
                    if partida["info"].get("mapId") != 11:
                        continue  # so Summoner's Rift
                    await rc.timeline(mid)
                    baixadas += 1
                except RiftCoachError:
                    continue

            if not baixadas:
                _erro(
                    "suas partidas recentes nao sao de Summoner's Rift",
                    "o RiftCoach analisa apenas o mapa classico (5v5). "
                    "Jogue uma partida normal ou ranqueada e rode de novo",
                )
                return False

            # Os dados de item precisam ser do patch da PARTIDA, nao do atual.
            for mid in ids:
                try:
                    p = await rc.match(mid)
                    await db.sync(".".join(p["info"]["gameVersion"].split(".")[:2]))
                except Exception:
                    continue

            _ok(f"{baixadas} partidas prontas para analisar")
            return True

    try:
        return asyncio.run(baixar())
    except RiftCoachError as e:
        _erro(e.message, e.hint or "tente de novo em alguns minutos")
        return False


# --------------------------------------------------------------------------
# Passo 4 — mostrar o relatorio
# --------------------------------------------------------------------------


def passo_relatorio(riot_id: str) -> None:
    _titulo(4, 4, "Abrir o seu relatorio")
    console.print(
        "  [dim]Abrindo no navegador. Ele roda so no seu computador —\n"
        "  nada e publicado na internet.[/]\n"
    )
    console.print(
        Panel(
            "[bold]Como usar a tela que vai abrir:[/]\n\n"
            "• Os erros vem ordenados: o [bold]#1 e o que mais custou[/] a partida\n"
            "• Cada um mostra a [bold]evidencia[/] e de onde ela veio\n"
            "• O botao [cyan]Pular[/] leva o replay ate o momento — mas so\n"
            "  funciona com um replay aberto no client do League\n\n"
            "[dim]Para fechar tudo depois: feche o navegador e a janela preta.[/]",
            border_style="green",
        )
    )
    typer.prompt("  Aperte ENTER para abrir", default="", show_default=False)

    import contextlib

    from riftcoach.cli import web as comando_web

    # O comando `web` termina com typer.Exit quando o servidor para. Aqui isso
    # e conclusao normal, nao erro: o assistente ainda tem a mensagem final
    # para mostrar depois.
    with contextlib.suppress(SystemExit):
        comando_web(
            riot_id=riot_id,
            match_id=None,
            last=1,
            ai=True,
            port=8770,
            open_browser=True,
        )


# --------------------------------------------------------------------------


def run() -> None:
    console.print(
        Panel(
            "[bold]Bem-vindo ao RiftCoach AI[/]\n\n"
            "Vou te guiar em 4 passos. Leva uns 2 minutos na primeira vez;\n"
            "nas proximas, abre direto.\n\n"
            "[dim]Pode fechar esta janela a qualquer momento — nada se perde,\n"
            "e ao abrir de novo voce continua de onde parou.[/]",
            border_style="bold cyan",
        )
    )

    if not passo_chave():
        return
    riot_id = passo_riot_id()
    if not riot_id:
        return
    if not passo_dados(riot_id):
        return
    passo_relatorio(riot_id)
