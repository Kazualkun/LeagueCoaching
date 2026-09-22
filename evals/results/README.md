# Resultados versionados

`uv run python evals/run.py --with-ai --save` grava aqui um `{data}.json` com o agregado de cada
provedor.

**Estes arquivos são versionados de propósito.** É o que faz uma regressão aparecer no diff de um PR
em vez de ser descoberta por um usuário três semanas depois. Um PR que muda um prompt e derruba o F1
fica visível na revisão, e a discussão passa a ser sobre o número.

## Como ler

| Coluna | O que é |
|---|---|
| `f1` | média harmônica de precisão e cobertura — o número de comparação entre provedores |
| `f05` | o mesmo, com precisão pesando o dobro. **Este é o que importa mais neste projeto** |
| `precision` | dos findings reportados, quantos casam com o gabarito |
| `recall` | dos erros anotados, quantos foram encontrados |
| `noise_per_match` | findings sem correspondência, por partida |
| `drop_rate` | fração eliminada pelo validador — mede quanto o modelo tenta alucinar |
| `failures` | partidas em que o provedor não produziu nada |

`f05` pesa mais que `f1` porque um finding errado custa mais credibilidade do que dez certos
constroem, e o relatório determinístico já garante um piso de cobertura.

## A linha de base está sempre na tabela

A entrada `regras (sem IA)` é o motor determinístico. Um provedor abaixo dela está piorando o
produto, por mais rápido ou barato que seja — e o `run.py` diz isso explicitamente em vez de deixar
o leitor comparar sozinho.

## Ainda não há resultados aqui

Rodar contra provedores reais exige uma chave ou um Ollama local, e nenhuma das duas coisas roda no
CI hoje. A primeira publicação desta matriz depende de alguém rodar `--with-ai --save` numa máquina
com provedor configurado e abrir o PR.

O log em `~/.riftcoach/compat.jsonl` acumula o veredito de cada instalação sobre cada modelo
(só o veredito — nunca conteúdo de prompt ou resposta) e é a outra metade desta matriz.
