"""A janela do RiftCoach. Cada tela pede uma coisa so.

A PONTE COM O ASSINCRONO E A PARTE DELICADA, e vale entender antes de mexer:

    thread principal  ->  so ela toca widget. Sempre.
    thread de tarefa  ->  roda `asyncio.run(...)` e NAO toca em widget nenhum;
                          ela so deposita resultado numa fila.
    `_bombear()`      ->  roda na principal a cada 60 ms, tira da fila e chama
                          quem tinha de ser chamado.

Tkinter nao e seguro entre threads: tocar um widget de fora da principal
funciona nas suas dez primeiras tentativas e trava na decima primeira, na
maquina de outra pessoa. A fila existe para que isso nunca seja possivel, e
nao para organizar codigo.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, cast

from riftcoach.config import PLATFORM_TO_ROUTING, load_prefs, save_pref, settings
from riftcoach.core.errors import RiftCoachError
from riftcoach.gui.theme import (
    ACENTO,
    ALERTA,
    BORDA,
    ERRO,
    FUNDO,
    FUNDO_CARTAO,
    SUCESSO,
    TEXTO,
    TEXTO_FRACO,
    Botao,
    Campo,
    centralizar,
    fonte,
    paragrafo,
    titulo,
)

PORTAL = "https://developer.riotgames.com"


def _abrir_portal() -> None:
    """`webbrowser.open` devolve bool; o botao espera quem nao devolve nada."""
    webbrowser.open(PORTAL)


PORTA_WEB = 8770

LARGURA, ALTURA = 680, 690

PASSOS = ("Chave", "Conta", "Partida", "Revisar")


@dataclass
class Resultado:
    ok: bool
    dados: Any = None
    erro: str = ""
    dica: str = ""


@dataclass
class Estado:
    """O que a janela ja sabe. Sobrevive entre telas."""

    riot_id: str = ""
    plataforma: str = "br1"
    match_id: str = ""
    resumo: str = ""
    marcacoes: int = 0
    avisos: list[str] = field(default_factory=list)


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("RiftCoach AI")
        self.root.configure(bg=FUNDO)
        self.root.resizable(False, False)
        centralizar(self.root, LARGURA, ALTURA)

        self.estado = Estado()
        self._fila: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._passo = 0
        # A analise pronta, guardada para entregar ao servidor e ao overlay
        # sem refazer nada.
        self._preparado: Any = None
        self._servidor_no_ar = False
        # Id do bind de <FocusIn> que fareja a chave na area de transferencia
        # em tela_chave(). Guardado aqui para poder desligar o bind antigo
        # antes de criar outro — sem isso, cada vez que a Riot recusa uma
        # chave e a tela volta, um novo vigia se empilha sobre o anterior.
        self._vigia_area_de_transferencia: str | None = None

        self._montar_moldura()
        self.root.after(60, self._bombear)
        self.root.after(120, self._comecar)

    # ------------------------------------------------------------------
    # Moldura fixa: cabecalho, corpo, rodape
    # ------------------------------------------------------------------

    def _montar_moldura(self) -> None:
        topo = tk.Frame(self.root, bg=FUNDO)
        topo.pack(fill="x", padx=28, pady=(24, 0))
        tk.Label(topo, text="RiftCoach AI", bg=FUNDO, fg=TEXTO, font=fonte(19, negrito=True)).pack(
            anchor="w"
        )
        tk.Label(
            topo,
            text="análise das suas partidas de League — de graça, no seu computador",
            bg=FUNDO,
            fg=TEXTO_FRACO,
            font=fonte(9),
        ).pack(anchor="w", pady=(1, 0))

        self._trilha = tk.Frame(self.root, bg=FUNDO)
        self._trilha.pack(fill="x", padx=28, pady=(16, 0))
        self._chips: list[tk.Label] = []
        for i, nome in enumerate(PASSOS):
            c = tk.Label(
                self._trilha,
                text=f" {i + 1}. {nome} ",
                bg=FUNDO_CARTAO,
                fg=TEXTO_FRACO,
                font=fonte(9, negrito=True),
                padx=6,
                pady=4,
            )
            c.pack(side="left", padx=(0, 6))
            self._chips.append(c)

        self.corpo = tk.Frame(self.root, bg=FUNDO)
        self.corpo.pack(fill="both", expand=True, padx=28, pady=(18, 0))

        rodape = tk.Frame(self.root, bg=FUNDO)
        rodape.pack(fill="x", padx=28, pady=(0, 18))
        self.status = tk.Label(rodape, text="", bg=FUNDO, fg=TEXTO_FRACO, font=fonte(9), anchor="w")
        self.status.pack(fill="x")

    def _marcar_passo(self, n: int) -> None:
        self._passo = n
        for i, c in enumerate(self._chips):
            feito = i < n
            atual = i == n
            c.configure(
                bg=FUNDO_CARTAO if not atual else ACENTO,
                fg=("#ffffff" if atual else (SUCESSO if feito else TEXTO_FRACO)),
            )

    def _limpar(self) -> None:
        for w in self.corpo.winfo_children():
            w.destroy()

    def _dizer(self, texto: str, cor: str = TEXTO_FRACO) -> None:
        self.status.configure(text=texto, fg=cor)

    # ------------------------------------------------------------------
    # A ponte com o assincrono
    # ------------------------------------------------------------------

    def _tarefa(
        self,
        fabrica: Callable[[], Coroutine[Any, Any, Any]],
        quando_terminar: Callable[[Resultado], None],
    ) -> None:
        """Roda a corrotina em outra thread e devolve o resultado pela fila.

        Recebe uma FABRICA, nao uma corrotina pronta: criar a corrotina na
        thread principal e rodar em outra funciona, mas se a tarefa nunca for
        agendada o Python reclama de "coroutine was never awaited" — e a
        reclamacao apareceria no meio da tela de erro, que e o pior lugar.
        """

        def alvo() -> None:
            entregar = cast("Callable[[Any], None]", quando_terminar)
            try:
                self._fila.put((entregar, Resultado(ok=True, dados=asyncio.run(fabrica()))))
            except RiftCoachError as e:
                self._fila.put((entregar, Resultado(ok=False, erro=e.message, dica=e.hint or "")))
            except Exception as e:
                # Qualquer excecao vira tela de erro. Uma traceback no console
                # de um processo sem console e uma falha silenciosa — e a
                # pessoa fica olhando uma janela que parou.
                self._fila.put((entregar, Resultado(ok=False, erro=str(e))))

        threading.Thread(target=alvo, daemon=True).start()

    def _bombear(self) -> None:
        try:
            while True:
                fn, arg = self._fila.get_nowait()
                fn(arg)
        except queue.Empty:
            pass
        self.root.after(60, self._bombear)

    # ------------------------------------------------------------------
    # Passo 0 — o que ja sabemos
    # ------------------------------------------------------------------

    def _comecar(self) -> None:
        self._limpar()
        self._marcar_passo(0)
        titulo(self.corpo, "Conferindo o que já está pronto...").pack(anchor="w")
        paragrafo(self.corpo, "Leva uns segundos. Na próxima vez isso passa direto.").pack(
            anchor="w", pady=(6, 0)
        )
        self._dizer("verificando a sua chave da Riot")

        prefs = load_prefs()
        self.estado.riot_id = prefs.get("riot_id", "")
        self.estado.plataforma = prefs.get("platform", settings.riot_platform)

        chave = settings.resolve_api_key()
        if not chave:
            self.root.after(300, lambda: self.tela_chave())
            return

        async def testar() -> bool:
            from riftcoach.riot.client import RiotClient

            async with RiotClient(api_key=chave) as rc:
                await rc.platform_status()
            return True

        def pronto(r: Resultado) -> None:
            if r.ok:
                if self.estado.riot_id:
                    self.tela_conta(pular_se_souber=True)
                else:
                    self.tela_conta()
            else:
                self.tela_chave(motivo=r.erro)

        self._tarefa(testar, pronto)

    # ------------------------------------------------------------------
    # Passo 1 — a chave
    # ------------------------------------------------------------------

    def tela_chave(self, motivo: str = "") -> None:
        self._limpar()
        self._marcar_passo(0)
        titulo(self.corpo, "Conectar com a Riot").pack(anchor="w")

        if motivo:
            self._aviso(
                self.corpo,
                f"A chave que estava guardada não funciona mais: {motivo}",
                "Chaves de desenvolvimento expiram a cada 24 horas. Gere outra e cole abaixo.",
            )

        paragrafo(
            self.corpo,
            "O RiftCoach lê as suas partidas pela API oficial da Riot. Para isso "
            "ele precisa de uma chave, que é gratuita.",
        ).pack(anchor="w", pady=(8, 0))

        passos = tk.Frame(self.corpo, bg=FUNDO_CARTAO, padx=16, pady=13)
        passos.pack(fill="x", pady=(14, 0))
        for n, t in (
            ("1", "Clique no botão abaixo para abrir o site da Riot"),
            ("2", "Entre com a sua conta do League"),
            ("3", "Copie a chave que começa com RGAPI- — o RiftCoach detecta sozinho"),
        ):
            ln = tk.Frame(passos, bg=FUNDO_CARTAO)
            ln.pack(fill="x", pady=2)
            tk.Label(
                ln, text=n, bg=FUNDO_CARTAO, fg=ACENTO, font=fonte(10, negrito=True), width=2
            ).pack(side="left")
            tk.Label(ln, text=t, bg=FUNDO_CARTAO, fg=TEXTO, font=fonte(10)).pack(side="left")

        Botao(
            self.corpo,
            "Abrir o site da Riot",
            _abrir_portal,
            principal=False,
        ).pack(anchor="w", pady=(14, 0))

        tk.Label(self.corpo, text="Cole a chave aqui:", bg=FUNDO, fg=TEXTO, font=fonte(10)).pack(
            anchor="w", pady=(16, 4)
        )
        campo = Campo(self.corpo, senha=True)
        campo.pack(fill="x", ipady=7)
        campo.focus_set()

        def farejar_area_de_transferencia() -> None:
            # Chamado ao abrir a tela e sempre que a janela volta a ter foco
            # (ex.: a pessoa colou a chave no navegador e alt-tabou de volta).
            # So preenche um campo vazio: nunca sobrescreve o que a pessoa
            # esta digitando.
            if not campo.winfo_exists() or campo.get().strip():
                return
            try:
                conteudo = self.root.clipboard_get().strip()
            except tk.TclError:
                return
            if conteudo.startswith("RGAPI-"):
                campo.insert(0, conteudo)
                self._dizer("chave detectada — confira e clique em Continuar", SUCESSO)

        if self._vigia_area_de_transferencia:
            self.root.unbind("<FocusIn>", self._vigia_area_de_transferencia)
        farejar_area_de_transferencia()
        self._vigia_area_de_transferencia = self.root.bind(
            "<FocusIn>", lambda _e: farejar_area_de_transferencia(), add="+"
        )

        tk.Label(
            self.corpo,
            text="A chave fica no cofre de senhas do Windows. Nunca vai para "
            "arquivo nem sai do seu computador.",
            bg=FUNDO,
            fg=TEXTO_FRACO,
            font=fonte(8),
            wraplength=580,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        botao = Botao(self.corpo, "Continuar", lambda: enviar())
        botao.pack(anchor="w", pady=(16, 0))

        def enviar() -> None:
            chave = campo.get().strip()
            if not chave:
                self._dizer("cole a chave antes de continuar", ALERTA)
                return
            botao.habilitar(False)
            self._dizer("conferindo a chave com a Riot...")

            async def validar() -> str:
                from riftcoach.riot.client import RiotClient

                async with RiotClient(api_key=chave) as rc:
                    await rc.platform_status()
                return chave

            def pronto(r: Resultado) -> None:
                botao.habilitar(True)
                if not r.ok:
                    # "Unknown apikey" quase nunca e erro de quem digitou: o
                    # site TROCA a chave a cada geracao, entao a que a pessoa
                    # copiou meia hora antes ja nao vale. Dizer isso poupa a
                    # pessoa de achar que errou ao colar.
                    extra = (
                        "O site da Riot substitui a chave toda vez que você gera "
                        "uma nova. Abra o site, copie a que está lá AGORA e cole "
                        "de novo."
                        if "Unknown" in r.erro
                        else (r.dica or "Confira se copiou a linha inteira.")
                    )
                    self._dizer(f"a Riot recusou a chave: {r.erro}", ERRO)
                    self._aviso(self.corpo, f"A Riot recusou: {r.erro}", extra)
                    return
                from riftcoach.config import write_key_to_keyring

                write_key_to_keyring("riot", r.dados)
                if self._vigia_area_de_transferencia:
                    self.root.unbind("<FocusIn>", self._vigia_area_de_transferencia)
                    self._vigia_area_de_transferencia = None
                self._dizer("chave guardada", SUCESSO)
                self.tela_conta()

            self._tarefa(validar, pronto)

        campo.bind("<Return>", lambda _e: enviar())

    # ------------------------------------------------------------------
    # Passo 2 — a conta
    # ------------------------------------------------------------------

    def tela_conta(self, pular_se_souber: bool = False) -> None:
        if pular_se_souber and self.estado.riot_id:
            self.tela_partida()
            return

        self._limpar()
        self._marcar_passo(1)
        titulo(self.corpo, "Qual é a sua conta?").pack(anchor="w")
        paragrafo(
            self.corpo,
            "O Riot ID aparece no canto do client do League, no formato "
            "Nome#TAG. Não é o nome de invocador antigo, e a TAG nem sempre "
            "é BR1 — pode ser qualquer coisa que você escolheu.",
        ).pack(anchor="w", pady=(6, 0))

        tk.Label(self.corpo, text="Riot ID", bg=FUNDO, fg=TEXTO, font=fonte(10)).pack(
            anchor="w", pady=(18, 4)
        )
        campo = Campo(self.corpo)
        campo.insert(0, self.estado.riot_id or "")
        campo.pack(fill="x", ipady=7)
        campo.focus_set()
        tk.Label(
            self.corpo,
            text="exemplo:  Fulano#BR1",
            bg=FUNDO,
            fg=TEXTO_FRACO,
            font=fonte(8),
        ).pack(anchor="w", pady=(4, 0))

        tk.Label(self.corpo, text="Sua região", bg=FUNDO, fg=TEXTO, font=fonte(10)).pack(
            anchor="w", pady=(16, 4)
        )
        var = tk.StringVar(value=self.estado.plataforma)
        menu = tk.OptionMenu(self.corpo, var, *sorted(PLATFORM_TO_ROUTING))
        menu.configure(
            bg=FUNDO_CARTAO,
            fg=TEXTO,
            font=fonte(10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDA,
            activebackground=BORDA,
            width=10,
        )
        menu["menu"].configure(bg=FUNDO_CARTAO, fg=TEXTO, font=fonte(10))
        menu.pack(anchor="w")

        botao = Botao(self.corpo, "Continuar", lambda: enviar())
        botao.pack(anchor="w", pady=(22, 0))

        def enviar() -> None:
            rid = campo.get().strip()
            if "#" not in rid:
                self._dizer("faltou o # — o formato é Nome#TAG, por exemplo Fulano#BR1", ALERTA)
                return
            self.estado.riot_id = rid
            self.estado.plataforma = var.get()
            save_pref("riot_id", rid)
            save_pref("platform", var.get())
            settings.riot_platform = var.get()
            self.tela_partida()

        campo.bind("<Return>", lambda _e: enviar())

    # ------------------------------------------------------------------
    # Passo 3 — analisar
    # ------------------------------------------------------------------

    def tela_partida(self) -> None:
        self._limpar()
        self._marcar_passo(2)
        titulo(self.corpo, "Analisando a sua última partida").pack(anchor="w")
        paragrafo(
            self.corpo,
            f"Conta: {self.estado.riot_id}  ·  região: {self.estado.plataforma}",
        ).pack(anchor="w", pady=(6, 0))

        cartao = tk.Frame(self.corpo, bg=FUNDO_CARTAO, padx=18, pady=16)
        cartao.pack(fill="x", pady=(18, 0))
        linhas: dict[str, tk.Label] = {}
        for chave, texto in (
            ("dados", "Baixando nomes de itens e runas"),
            ("partida", "Procurando a sua última partida"),
            ("analise", "Medindo onde a partida virou"),
        ):
            ln = tk.Label(
                cartao,
                text=f"   {texto}",
                bg=FUNDO_CARTAO,
                fg=TEXTO_FRACO,
                font=fonte(10),
                anchor="w",
            )
            ln.pack(fill="x", pady=3)
            linhas[chave] = ln

        def andar(chave: str, estado: str) -> None:
            marca = {"fazendo": "»", "ok": "✓", "erro": "✗"}[estado]
            cor = {"fazendo": ACENTO, "ok": SUCESSO, "erro": ERRO}[estado]
            atual = linhas[chave].cget("text")[4:]
            linhas[chave].configure(text=f" {marca} {atual}", fg=cor)

        self._dizer("isso pode levar um minuto na primeira vez")
        andar("dados", "fazendo")

        rid = self.estado.riot_id

        async def trabalhar() -> Any:
            from riftcoach.overlay.prepare import preparar

            return await preparar(rid, last=1, use_ai=True)

        def pronto(r: Resultado) -> None:
            if not r.ok:
                andar("partida", "erro")
                self.tela_erro(r.erro, r.dica)
                return
            for k in ("dados", "partida", "analise"):
                andar(k, "ok")
            pre = r.dados
            self._preparado = pre
            f = pre.facts
            self.estado.match_id = f.match_id
            self.estado.marcacoes = len(pre.state.marks)
            self.estado.resumo = (
                f"{f.focus.champion} {f.focus.kills}/{f.focus.deaths}/{f.focus.assists}"
                f"  ·  {'vitoria' if f.focus.win else 'derrota'} em "
                f"{f.duration_s // 60} minutos"
            )
            self.root.after(400, self.tela_pronto)

        self._tarefa(trabalhar, pronto)

    # ------------------------------------------------------------------
    # Passo 4 — o que fazer com o resultado
    # ------------------------------------------------------------------

    def tela_pronto(self) -> None:
        self._limpar()
        self._marcar_passo(3)
        titulo(self.corpo, "Pronto. Como você quer revisar?").pack(anchor="w")
        tk.Label(
            self.corpo,
            text=self.estado.resumo,
            bg=FUNDO,
            fg=TEXTO,
            font=fonte(11, negrito=True),
        ).pack(anchor="w", pady=(8, 0))
        tk.Label(
            self.corpo,
            text=f"{self.estado.marcacoes} momentos marcados na partida",
            bg=FUNDO,
            fg=TEXTO_FRACO,
            font=fonte(9),
        ).pack(anchor="w")

        self._opcao(
            "Ler o relatório",
            "Abre no navegador. Os erros vêm ordenados pelo que mais custou, "
            "com a evidência de cada um.",
            "Abrir relatório",
            self._abrir_web,
        )
        self._opcao(
            "Marcar dentro do replay",
            "Abra o replay da partida no client do League. As marcações "
            "aparecem por cima do jogo, alguns segundos ANTES de cada erro.",
            "Abrir overlay",
            self._abrir_overlay,
        )

        Botao(
            self.corpo,
            "Analisar outra partida",
            self.tela_partida,
            principal=False,
        ).pack(anchor="w", pady=(16, 0))
        self._dizer("pode fechar esta janela quando terminar", TEXTO_FRACO)

    def _opcao(self, nome: str, explicacao: str, rotulo: str, acao: Callable[[], None]) -> None:
        c = tk.Frame(self.corpo, bg=FUNDO_CARTAO, padx=18, pady=15)
        c.pack(fill="x", pady=(16, 0))
        tk.Label(c, text=nome, bg=FUNDO_CARTAO, fg=TEXTO, font=fonte(11, negrito=True)).pack(
            anchor="w"
        )
        tk.Label(
            c,
            text=explicacao,
            bg=FUNDO_CARTAO,
            fg=TEXTO_FRACO,
            font=fonte(9),
            wraplength=580,
            justify="left",
            anchor="w",
        ).pack(anchor="w", pady=(3, 10))
        Botao(c, rotulo, acao).pack(anchor="w")

    def _abrir_web(self) -> None:
        """Sobe o servidor numa thread e abre o navegador.

        Thread, e nao processo: o relatorio ja esta em memoria aqui. Um
        processo novo refaria a analise inteira para mostrar o que a janela
        acabou de calcular.
        """
        from riftcoach.api.app import adopt, esta_no_ar, serve

        if self._preparado is None:
            self._dizer("analise ainda nao terminou", ALERTA)
            return

        # ENTREGAR A ANALISE AO SERVIDOR. Sem esta linha a pagina abre e a SPA
        # leva 404 em /api/report — que foi o "pagina nao encontrada" que
        # chegou ao usuario com o relatorio pronto na memoria ao lado.
        adopt(
            self._preparado.facts,
            self._preparado.report,
            self._preparado.benchmarks,
        )
        self._dizer("abrindo o relatório no navegador...")

        if not self._servidor_no_ar:
            self._servidor_no_ar = True

            def subir_servidor() -> None:
                try:
                    serve(port=PORTA_WEB, open_browser=False)
                except BaseException as e:
                    # uvicorn desiste com sys.exit() quando nao consegue
                    # ligar na porta — o caso comum e OUTRA janela do
                    # RiftCoach ja aberta e servindo ali. Sem isto o erro
                    # morre aqui: pythonw nao tem console, a mensagem do
                    # uvicorn nao vai a lugar nenhum, e o botao so parece
                    # nao fazer nada.
                    self._servidor_no_ar = False
                    msg = (
                        "já tem outra janela do RiftCoach aberta — feche as "
                        "outras ou use o relatório por lá"
                        if isinstance(e, SystemExit)
                        else f"o servidor não subiu: {e}"
                    )
                    self._fila.put((lambda _a: self._dizer(msg, ERRO), None))

            threading.Thread(target=subir_servidor, daemon=True).start()

        def quando_subir(tentativas: int = 40) -> None:
            """Espera a porta ACEITAR conexao, em vez de chutar um tempo fixo.

            Um tempo fixo erra na maquina lenta — que e justamente onde o
            servidor demora mais a subir, e onde abrir cedo demais mostra
            "nao foi possivel acessar o site".
            """
            if esta_no_ar(PORTA_WEB):
                webbrowser.open(f"http://127.0.0.1:{PORTA_WEB}/")
                self._dizer("relatório aberto no navegador", SUCESSO)
            elif tentativas:
                self.root.after(150, lambda: quando_subir(tentativas - 1))
            else:
                self._dizer("o servidor local não subiu; tente de novo", ERRO)

        quando_subir()

    def _abrir_overlay(self) -> None:
        """Abre o overlay como processo separado, e sem janela preta.

        Processo separado porque o overlay cria a PROPRIA janela tkinter, e
        duas raizes tkinter no mesmo processo e uma fonte de travamento que so
        aparece na maquina dos outros. `--usar-marcacoes-salvas` faz ele
        aproveitar a analise que acabou de acontecer em vez de refazer.
        """
        import subprocess

        self._dizer("procurando um replay aberto no client do League...")

        async def conferir() -> bool:
            from riftcoach.replay.guard import is_replay_running

            return await is_replay_running()

        def pronto(r: Resultado) -> None:
            if not r.dados:
                self._aviso(
                    self.corpo,
                    "Não encontrei nenhum replay rodando.",
                    "Abra o League, vá em Partidas, baixe o replay da partida e "
                    "dê play. Depois clique em Abrir overlay de novo. "
                    "Importante: o jogo precisa estar em modo 'Sem bordas' — "
                    "em tela cheia exclusiva nada aparece por cima.",
                )
                self._dizer("replay não encontrado", ALERTA)
                return

            cmd = [
                sys.executable,
                "-m",
                "riftcoach",
                "overlay",
                self.estado.riot_id,
                "--match",
                self.estado.match_id,
                "--usar-marcacoes-salvas",
            ]
            # CREATE_NO_WINDOW: sem isso o Windows abre um console preto para o
            # processo novo, que e exatamente o que esta janela existe para
            # evitar.
            bandeiras = 0x08000000 if sys.platform == "win32" else 0
            subprocess.Popen(cmd, creationflags=bandeiras)
            self._dizer("overlay aberto — CLIQUE na janela do League para vê-lo", SUCESSO)

        self._tarefa(conferir, pronto)

    # ------------------------------------------------------------------

    def tela_erro(self, mensagem: str, dica: str) -> None:
        self._limpar()
        titulo(self.corpo, "Algo não deu certo").pack(anchor="w")
        self._aviso(
            self.corpo,
            mensagem,
            dica or "Confira a sua conexão com a internet e tente de novo.",
        )
        linha = tk.Frame(self.corpo, bg=FUNDO)
        linha.pack(anchor="w", pady=(18, 0))
        Botao(linha, "Tentar de novo", self.tela_partida).pack(side="left")
        Botao(linha, "Trocar de conta", self.tela_conta, principal=False).pack(
            side="left", padx=(8, 0)
        )
        Botao(linha, "Trocar a chave", lambda: self.tela_chave(), principal=False).pack(
            side="left", padx=(8, 0)
        )
        self._dizer("nada foi perdido — nenhuma dessas opções apaga nada", TEXTO_FRACO)

    def _aviso(self, onde: tk.Misc, mensagem: str, o_que_fazer: str) -> None:
        """Erro NUNCA e beco sem saida: sempre tem o que fazer agora."""
        c = tk.Frame(onde, bg="#2d1b1b", padx=16, pady=12, highlightthickness=1)
        c.configure(highlightbackground=ERRO)
        c.pack(fill="x", pady=(14, 0))
        tk.Label(
            c,
            text=mensagem,
            bg="#2d1b1b",
            fg=ERRO,
            font=fonte(10, negrito=True),
            wraplength=560,
            justify="left",
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            c,
            text=o_que_fazer,
            bg="#2d1b1b",
            fg=TEXTO,
            font=fonte(9),
            wraplength=560,
            justify="left",
            anchor="w",
        ).pack(anchor="w", pady=(5, 0))

    def rodar(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().rodar()
