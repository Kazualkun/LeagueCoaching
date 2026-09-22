"""Navegacao no replay: pular para um momento, com a camera certa.

A REGRA DE UX DE COACHING deste modulo, e ela justifica o resto: SEMPRE pule
para a decisao, nunca para o desfecho. Pular para o instante exato da morte
mostra a consequencia; o erro aconteceu 5 a 10 segundos antes — o pathing, a
ward que faltou, a wave empurrada.

TRES RELOGIOS, E ELES NAO CONCORDAM (docs/03-vod-review.md, 3.4):

  timeline    ms desde o inicio da partida, como a Riot conta
  replay      segundos desde o inicio do ARQUIVO, incluindo tela de
              carregamento e o periodo antes dos minions
  video       segundos desde o inicio da gravacao, que e arbitrario

O offset entre os dois primeiros e medido uma vez e reconferido depois de cada
seek. Deriva significa que o usuario navegou a mao e o mapeamento precisa ser
refeito — silenciosamente desalinhar todo finding em 40 segundos seria pior que
recusar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from riftcoach.core.errors import LiveGameRefused
from riftcoach.replay.guard import PlaybackState, ReplayGuard

# O erro e a decisao, nao o desfecho. Mesmo valor de `Finding.seek_ms`.
LEAD_IN_S = 8.0

# Acima disto, o offset medido nao bate com o guardado e a calibracao e
# refeita. Meio segundo e a folga que a propria documentacao da Riot sugere
# para leituras feitas na mesma janela.
DRIFT_TOLERANCE_S = 0.5


@dataclass
class Calibration:
    """`replay_time_s = timeline_ms / 1000 + offset`."""

    offset_s: float
    measured_at_replay_s: float

    def to_replay_s(self, timeline_ms: int) -> float:
        return max(0.0, timeline_ms / 1000.0 + self.offset_s)

    def to_timeline_ms(self, replay_s: float) -> int:
        return max(0, round((replay_s - self.offset_s) * 1000))


@dataclass
class ReplayController:
    """Controla o client do League atraves do guard — nunca direto."""

    guard: ReplayGuard
    calibration: Calibration | None = None
    _drifts: int = field(default=0, init=False)

    async def calibrate(self) -> Calibration:
        """Mede o offset entre o relogio do replay e o da timeline.

        As duas leituras precisam cair na mesma janela curta; por isso elas
        acontecem aqui, coladas, em vez de serem passadas de fora.
        """
        playback: PlaybackState = await self.guard.assert_replay_mode()
        stats = await self.guard.get("/liveclientdata/gamestats")

        game_time = _game_time_of(stats)
        if game_time is None:
            raise LiveGameRefused(
                "nao foi possivel ler o relogio do jogo em /liveclientdata/gamestats",
                hint=(
                    "Sem os dois relogios nao da para alinhar os findings com o "
                    "replay, e alinhar errado e pior que nao alinhar."
                ),
            )

        self.calibration = Calibration(
            offset_s=playback.time - game_time,
            measured_at_replay_s=playback.time,
        )
        return self.calibration

    async def ensure_calibrated(self) -> Calibration:
        return self.calibration or await self.calibrate()

    async def seek_to_ms(self, timeline_ms: int, lead_in_s: float = LEAD_IN_S) -> float:
        """Pula para `lead_in_s` ANTES do instante pedido. Devolve o alvo.

        O guard e chamado em toda requisicao aqui dentro; nao ha caminho que
        alcance o client sem passar por ele.
        """
        cal = await self.ensure_calibrated()
        alvo = max(0.0, cal.to_replay_s(timeline_ms) - lead_in_s)

        await self.guard.post(
            "/replay/playback", {"time": alvo, "paused": False, "speed": 1.0}
        )
        return alvo

    async def focus_camera(self, champion: str | None = None) -> None:
        """Camera travada, opcionalmente num campeao.

        Falhar aqui NAO e fatal: a navegacao ja aconteceu e o usuario esta
        vendo o momento certo. Camera e polimento; perder o seek por causa dela
        seria trocar o essencial pelo acessorio.
        """
        payload: dict[str, object] = {"cameraMode": "fps", "cameraLockMode": "on"}
        if champion:
            payload["selectedChampion"] = champion
        try:
            await self.guard.post("/replay/render", payload)
        except LiveGameRefused:
            return

    async def pause(self) -> None:
        estado = await self.guard.assert_replay_mode()
        await self.guard.post("/replay/playback", {"time": estado.time, "paused": True})

    async def check_drift(self) -> float:
        """Reconfere o offset. Devolve a deriva medida, em segundos.

        Deriva grande significa que o usuario navegou a mao e o mapeamento
        antigo nao vale mais. Recalibramos em vez de continuar apontando os
        findings para o lugar errado — um coach que pula 40 segundos fora
        perde a confianca na primeira vez.
        """
        antigo = self.calibration
        if antigo is None:
            await self.calibrate()
            return 0.0

        novo = await self.calibrate()
        deriva = abs(novo.offset_s - antigo.offset_s)
        if deriva > DRIFT_TOLERANCE_S:
            self._drifts += 1
        return deriva

    @property
    def drift_events(self) -> int:
        return self._drifts


def _game_time_of(stats: object) -> float | None:
    """Le `gameTime` de /liveclientdata/gamestats, tolerando variacao de forma.

    O nome do campo ja mudou entre versoes do client. Tentar alguns e melhor
    que quebrar a calibracao inteira por causa de uma chave renomeada — e,
    fracassando todos, devolvemos None e quem chama recusa.
    """
    if not isinstance(stats, dict):
        return None
    for chave in ("gameTime", "gameTimeSeconds", "time"):
        valor = stats.get(chave)
        if isinstance(valor, (int, float)):
            return float(valor)
    return None
