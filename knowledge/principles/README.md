# Corpus de princípios

Fundamentos de coaching independentes de patch. Eles mudam numa escala de anos, não de duas semanas —
por isso ficam aqui, escritos à mão, em vez de virem do DataDragon.

**Esta é a principal superfície de contribuição do projeto, e ela não exige Python.** Um jogador de
elo alto que escreve bem `waves/bounce-mechanics.md` contribui mais do que a maioria dos PRs de
código, porque esse arquivo melhora todos os relatórios que a ferramenta vai gerar, para sempre.

O formato, o que faz um bom princípio e onde colocar cada arquivo estão em
[`CONTRIBUTING.md` → Escrevendo princípios](../../CONTRIBUTING.md#escrevendo-princípios).

## O que já existe

| Arquivo | Assunto |
|---|---|
| [`waves/freezing.md`](waves/freezing.md) | Congelar a wave: como criar, por que é forte, quando prende você |
| [`waves/slow-push.md`](waves/slow-push.md) | Construir uma wave grande de propósito e escolher quando ela crasha |
| [`macro/objective-setup.md`](macro/objective-setup.md) | A janela de visão de 60 a 90 s antes do objetivo nascer |
| [`economy/recall-thresholds.md`](economy/recall-thresholds.md) | Voltar por componente, não por item completo |

Estes quatro são sementes, não uma coleção. As pastas previstas no projeto cobrem bem mais:

```
waves/      slow-push ✅  freezing ✅  crashing-and-recall  bounce-mechanics
trading/    stance-and-spacing  minion-aggro  level-spike-windows
macro/      objective-setup ✅  tempo-and-prio  crossmap-and-tradeoffs
economy/    recall-thresholds ✅  death-timers
vision/     deep-vs-defensive
fights/     target-selection
```

Qualquer um dos itens sem ✅ é um PR bem-vindo hoje.

## Licença

Este diretório é **CC-BY-SA-4.0** ([texto completo](LICENSE)), e não AGPL como o resto do
repositório. A separação é proposital: o corpus de coaching deve poder circular independentemente do
código.
