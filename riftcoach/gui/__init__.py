"""A janela que abre com dois cliques. Nada de terminal.

O assistente de texto (`riftcoach/wizard.py`) continua existindo e continua
correto — mas ele so serve para quem ja aceitou a ideia de digitar num terminal
preto. Quem joga League nao aceitou, e nao tem por que aceitar.

As cinco regras sao as mesmas do assistente de texto, porque elas nao eram
sobre o terminal e sim sobre a pessoa:

  - UM passo de cada vez. Nunca pedir duas coisas na mesma tela.
  - Sempre dizer qual e o PROXIMO passo, mesmo quando deu certo.
  - Nenhum jargao sem traducao.
  - Erro nunca e beco sem saida: toda falha termina com o que fazer agora.
  - Fechar no meio nao perde nada.

E uma regra a mais, que so existe aqui: A JANELA NUNCA CONGELA. Toda tarefa
que fala com a rede roda em outra thread e volta pela fila. Uma janela travada
com "(Não está respondendo)" na barra de titulo e, para quem nao e tecnico, um
programa quebrado — e ela apareceria justamente no passo mais demorado.
"""
