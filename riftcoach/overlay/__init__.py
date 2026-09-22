"""A camada de desenho por cima do replay.

Isto e o que transforma o RiftCoach de "relatorio sobre a partida" em "coach
dentro da partida". O relatorio ja existia e era preciso; o que faltava era
estar no lugar onde a pessoa esta olhando.

A divisao aqui e proposital:

    geometry.py  mundo -> pixel. Puro, testavel, medido contra print real.
    scene.py     estado + instante -> lista de primitivas. Puro, testavel.
    window.py    onde esta a janela do League. Windows, impuro.
    render.py    primitivas -> pixels na tela. tkinter, impuro.
    run.py       o laco que junta tudo com o relogio do replay.

As duas primeiras concentram toda a logica e nao importam tkinter nem win32,
entao a parte que pode estar ERRADA e a parte que da para testar sem abrir o
jogo. A parte que precisa do jogo aberto e burra de proposito.
"""
