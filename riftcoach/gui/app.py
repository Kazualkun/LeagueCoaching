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
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlencode

from riftcoach.config import PLATFORM_TO_ROUTING, load_prefs, save_pref, settings
from riftcoach.core.errors import LiveGameRefused, RiftCoachError
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

LARGURA, ALTURA = 800, 780

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
        self.root.resizable(True, True)
        self.root.minsize(760, 700)
        centralizar(self.root, LARGURA, ALTURA)

        self.estado = Estado()
        self._fila: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._passo = 0
        # A analise pronta, guardada para entregar ao servidor e ao overlay
        # sem refazer nada.
        self._preparado: Any = None
        self._servidor_no_ar = False
        self._porta_web: int | None = None
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
        titulo(self.corpo, "Escolha a partida").pack(anchor="w")
        paragrafo(
            self.corpo,
            f"Conta: {self.estado.riot_id}  ·  região: {self.estado.plataforma}\n"
            "Você pode escolher Solo/Duo ou Flex, inclusive uma partida que não seja a última.",
        ).pack(anchor="w", pady=(6, 0))
        Botao(
            self.corpo,
            "Selecionar outro jogador/conta",
            self.tela_conta,
            principal=False,
        ).pack(anchor="w", pady=(10, 0))

        area = tk.Frame(self.corpo, bg=FUNDO_CARTAO, padx=18, pady=16)
        area.pack(fill="x", pady=(18, 0))
        tk.Label(
            area, text="Partidas recentes", bg=FUNDO_CARTAO, fg=TEXTO, font=fonte(10, negrito=True)
        ).pack(anchor="w")
        lista = tk.Listbox(
            area,
            height=7,
            width=78,
            bg=FUNDO,
            fg=TEXTO,
            selectbackground=BORDA,
            relief="flat",
            highlightthickness=0,
        )
        lista.pack(fill="x", pady=(8, 8))
        status = tk.Label(
            area,
            text="Aguarde, buscando suas partidas...",
            bg=FUNDO_CARTAO,
            fg=TEXTO_FRACO,
            font=fonte(9),
        )
        status.pack(anchor="w")
        filtros = tk.Frame(area, bg=FUNDO_CARTAO)
        filtros.pack(anchor="w", pady=(8, 0))
        filtro = tk.StringVar(value="todas")
        candidatos: list[dict[str, Any]] = []
        for valor, texto in (("todas", "Todas"), ("solo", "Solo/Duo"), ("flex", "Flex")):
            tk.Radiobutton(
                filtros,
                text=texto,
                value=valor,
                variable=filtro,
                command=lambda: preencher(),
                bg=FUNDO_CARTAO,
                fg=TEXTO,
                selectcolor=FUNDO,
                activebackground=FUNDO_CARTAO,
                activeforeground=TEXTO,
            ).pack(side="left", padx=(0, 10))

        def visiveis() -> list[dict[str, Any]]:
            if filtro.get() == "todas":
                return candidatos
            return [c for c in candidatos if c["fila"] == filtro.get()]

        def preencher() -> None:
            lista.delete(0, tk.END)
            for c in visiveis():
                lista.insert(tk.END, c["texto"])

        botao = Botao(area, "Gerar relatório desta partida", lambda: iniciar())
        botao.pack(anchor="w", pady=(10, 0))

        cartao = tk.Frame(self.corpo, bg=FUNDO_CARTAO, padx=18, pady=16)
        cartao.pack(fill="x", pady=(12, 0))
        linhas: dict[str, tk.Label] = {}
        for chave, texto in (
            ("dados", "Baixando nomes de itens e runas"),
            ("partida", "Baixando a partida escolhida"),
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

        rid = self.estado.riot_id

        async def buscar() -> list[dict[str, Any]]:
            from riftcoach.riot.client import RiotClient

            nome, _, tag = rid.partition("#")
            async with RiotClient() as rc:
                conta = await rc.account_by_riot_id(nome.strip(), tag.strip())
                ids = await rc.match_ids(conta["puuid"], count=20)
                import asyncio as _asyncio

                partidas = await _asyncio.gather(*(rc.match(mid) for mid in ids))
            out = []
            for mid, partida in zip(ids, partidas, strict=True):
                info = partida.get("info", {})
                fila_id = info.get("queueId")
                if fila_id not in (420, 440):
                    continue
                participante = next(
                    (p for p in info.get("participants", []) if p.get("puuid") == conta["puuid"]),
                    {},
                )
                fila = "solo" if fila_id == 420 else "flex"
                data = info.get("gameStartTimestamp", 0)
                quando = (
                    time.strftime("%d/%m %H:%M", time.localtime(data / 1000)) if data else "data?"
                )
                resultado = "vitória" if participante.get("win") else "derrota"
                out.append(
                    {
                        "id": mid,
                        "fila": fila,
                        "texto": f"{quando} · {'Solo/Duo' if fila == 'solo' else 'Flex'} · "
                        f"{participante.get('championName', '?')} · {resultado}",
                    }
                )
            return out

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

        self._dizer("aguarde, buscando suas partidas...")

        def partidas_prontas(r: Resultado) -> None:
            if not r.ok:
                status.configure(text=f"Não consegui buscar partidas: {r.erro}", fg=ERRO)
                return
            candidatos.extend(r.dados)
            preencher()
            status.configure(text=f"{len(candidatos)} partidas Solo/Duo/Flex encontradas.")
            botao.habilitar(True)

        botao.habilitar(False)
        self._tarefa(buscar, partidas_prontas)

        def iniciar() -> None:
            vis = visiveis()
            sel = lista.curselection()
            if not sel or sel[0] >= len(vis):
                self._dizer("selecione uma partida antes de gerar o relatório", ALERTA)
                return
            escolhido = vis[sel[0]]
            botao.habilitar(False)
            self._dizer("aguarde, estamos gerando seu relatório...")
            andar("dados", "fazendo")
            progresso = {"ativo": True, "indice": 0}
            mensagens = (
                "aguarde, baixando os dados completos da partida...",
                "aguarde, calculando mortes, objetivos, wave e visão...",
                "aguarde, gerando os achados determinísticos...",
                "aguarde, a IA está comparando macro, economia e lutas...",
                "aguarde, validando e montando o relatório final...",
            )

            def atualizar_progresso() -> None:
                if not progresso["ativo"]:
                    return
                self._dizer(mensagens[progresso["indice"] % len(mensagens)])
                progresso["indice"] += 1
                self.root.after(2500, atualizar_progresso)

            atualizar_progresso()

            async def trabalhar() -> Any:
                from riftcoach.overlay.prepare import preparar

                return await preparar(rid, match_id=escolhido["id"], use_ai=True)

            def terminou(resultado: Resultado) -> None:
                progresso["ativo"] = False
                if resultado.ok:
                    andar("dados", "ok")
                    andar("partida", "ok")
                    andar("analise", "ok")
                else:
                    andar("analise", "erro")
                pronto(resultado)

            self._tarefa(trabalhar, terminou)

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

        self._opcao(
            "Estatísticas do patch (matchup, runas e itens)",
            "Coleta partidas ranqueadas dos melhores do servidor com a sua chave da "
            "Riot. É daí que o relatório tira a taxa de vitória do matchup e compara "
            "suas runas e itens com os deles. Uns 5 minutos; pode repetir para "
            "aumentar a amostra.",
            "Atualizar estatísticas",
            self._atualizar_meta,
        )

        Botao(
            self.corpo,
            "Analisar outra partida",
            self.tela_partida,
            principal=False,
        ).pack(anchor="w", pady=(16, 0))
        self._dizer("pode fechar esta janela quando terminar", TEXTO_FRACO)

    def _atualizar_meta(self) -> None:
        """Coleta ~200 partidas do patch atual em segundo plano.

        200 cabe em ~5 minutos no limite de uma chave pessoal (100 pedidos a
        cada 2 minutos). Cada clique soma na mesma base; a amostra cresce.
        """
        self._dizer("coletando estatísticas do patch... (uns 5 minutos)")

        async def coletar_agora() -> Any:
            from riftcoach.knowledge.coleta import coletar, semear_do_cache
            from riftcoach.knowledge.meta import MetaDB
            from riftcoach.riot.client import RiotClient

            meta = MetaDB()
            async with RiotClient() as rc:
                await semear_do_cache(rc, meta)
                novas = await coletar(rc, meta, alvo=200, log=lambda _m: None)
            return novas, meta.partidas()

        def terminou(r: Resultado) -> None:
            if r.ok and r.dados:
                novas, total = r.dados
                self._dizer(
                    f"{novas} partidas novas — {total} na base. Reabra o relatório para ver.",
                    SUCESSO,
                )
            else:
                self._dizer(f"não consegui coletar: {r.erro}", ERRO)

        self._tarefa(coletar_agora, terminou)

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
            self._dizer("aguarde, estamos gerando seu relatório...", ALERTA)
            return

        # ENTREGAR A ANALISE AO SERVIDOR. Sem esta linha a pagina abre e a SPA
        # leva 404 em /api/report — que foi o "pagina nao encontrada" que
        # chegou ao usuario com o relatorio pronto na memoria ao lado.
        adopt(
            self._preparado.facts,
            self._preparado.report,
            self._preparado.benchmarks,
            review=getattr(self._preparado, "session", None),
        )
        self._dizer("abrindo o relatório no navegador...")

        if not self._servidor_no_ar:
            if esta_no_ar(PORTA_WEB):
                self._dizer(
                    "já existe um relatório aberto nesta sessão; feche a janela antiga "
                    "e abra o relatório novamente",
                    ERRO,
                )
                return
            self._servidor_no_ar = True
            # Um unico servidor representa o relatorio atual. Criar outra
            # porta deixava uma aba antiga viva e confundia a sessao.
            porta = PORTA_WEB
            self._porta_web = porta

            def subir_servidor() -> None:
                try:
                    serve(port=porta, open_browser=False)
                except BaseException as e:
                    # Todo erro daqui morria em silencio, e foi o que fez o
                    # botao "nao fazer nada" por tanto tempo: sob pythonw nao
                    # ha stderr, entao nem a excecao nem o log do uvicorn
                    # chegavam a lugar nenhum. Qualquer falha precisa voltar
                    # para a barra de status, que e a unica saida que existe.
                    # SystemExit e como o uvicorn desiste quando nao consegue
                    # ligar na porta — em geral outro programa ja esta nela.
                    self._servidor_no_ar = False
                    msg = (
                        f"a porta {porta} está ocupada por outro relatório — "
                        "feche esse relatório antigo e tente de novo"
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
            porta_atual = self._porta_web or PORTA_WEB
            if esta_no_ar(porta_atual):
                url = f"http://127.0.0.1:{porta_atual}/?{urlencode({'session': time.time_ns()})}"
                webbrowser.open(url)
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

        self._dizer("aguarde, estamos gerando o overlay...")

        async def conferir() -> LiveGameRefused | None:
            from riftcoach.replay.gamecfg import replay_api_status
            from riftcoach.replay.guard import replay_refusal

            # A Replay API desligada nao vira ligada por insistir: exige
            # editar o game.cfg e reabrir o jogo. Sondar 90s antes de contar
            # isso e 90s esperando por uma resposta que ja temos aqui.
            habilitada, explicacao = replay_api_status()
            if habilitada is False:
                return LiveGameRefused("A Replay API do League está desligada.", hint=explicacao)

            # O client pode levar alguns segundos entre abrir a janela do
            # replay e publicar /replay/playback. Uma unica sonda transforma
            # esse estado normal de carregamento em um falso "nao encontrei".
            recusa: LiveGameRefused | None = None
            for tentativa in range(45):
                recusa = await replay_refusal()
                if recusa is None:
                    return None
                if tentativa < 44:
                    await asyncio.sleep(2.0)
            return recusa

        def pronto(r: Resultado) -> None:
            if not r.ok:
                self._aviso(self.corpo, r.erro, r.dica or "Tente de novo.")
                self._dizer("não consegui checar o replay", ERRO)
                return
            if (recusa := r.dados) is not None:
                from riftcoach.replay.gamecfg import replay_patch_mismatch

                # Replay de patch anterior NAO abre, e mandar "baixe e de play"
                # nesse caso manda a pessoa tentar uma coisa impossivel.
                facts = getattr(self._preparado, "facts", None)
                problema = replay_patch_mismatch(facts.patch) if facts is not None else None
                # A recusa do guard ja diz o que fazer ("ligue a Replay API",
                # "de play"). Trocar isso por um texto fixo de "nao encontrei"
                # manda a pessoa repetir o que ela ja fez.
                detalhe = (
                    problema
                    or recusa.hint
                    or "Abra o League, vá em Partidas, baixe o replay da partida e "
                    "dê play. Depois clique em Abrir overlay de novo."
                )
                nota = (
                    "O jogo também precisa estar em modo 'Sem bordas' — em "
                    "tela cheia exclusiva nada aparece por cima."
                )
                self._aviso(self.corpo, recusa.message, f"{detalhe}\n\n{nota}")
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
            # Sem console, tudo o que o overlay escreve — inclusive o erro que
            # o derruba — ia para lugar nenhum. Num arquivo, ao menos da para
            # abrir e ver por que ele sumiu. PYTHONIOENCODING porque, com a
            # saida num arquivo, o filho escreveria em cp1252 e um caractere
            # fora dele derrubaria o overlay por causa do proprio log.
            from riftcoach.config import data_dir

            with (data_dir() / "overlay.log").open("w", encoding="utf-8") as saida:
                subprocess.Popen(
                    cmd,
                    creationflags=bandeiras,
                    stdout=saida,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
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


def _garante_saidas() -> None:
    """Da a `sys.stdout`/`sys.stderr` um destino valido quando nao ha console.

    Aberto por dois cliques, o RiftCoach roda sob `pythonw.exe`, e ali os dois
    sao None — nao um arquivo fechado, None mesmo. Qualquer biblioteca que
    assuma que existe um stream quebra, e quebra longe daqui: o uvicorn
    estourava em `sys.stdout.isatty()` montando o log colorido, ANTES de ligar
    na porta. O servidor nunca subia, e como nao havia stderr, a mensagem nao
    tinha para onde ir — o botao do relatorio simplesmente nao fazia nada.
    """
    for nome in ("stdout", "stderr"):
        if getattr(sys, nome, None) is None:
            # Sem `with`: este stream precisa durar o processo inteiro, e
            # fecha-lo traria o bug de volta. Um arquivo de verdade, e nao uma
            # classe falsa, porque bibliotecas chamam mais que write() —
            # fileno() e flush() entre elas.
            setattr(sys, nome, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def main() -> None:
    _garante_saidas()
    App().rodar()
