# Benchmarks por patch

Percentis de `cs@10`, `gd@10`, `visao/min`, `dpm`, `mortes` e `time_dead_pct`, por
`(rota, elo, patch_major)`, gerados pelo job de amostragem do mantenedor e publicados aqui como
`{patch_major}.parquet`.

So agregados sao publicados — sem PUUIDs, sem linhas por jogador.

Enquanto nao houver parquet para o patch de uma partida, `riftcoach/knowledge/benchmarks.py` cai na
tabela embarcada e, se o usuario tiver historico em cache, nos percentis do proprio historico dele.
O relatorio sempre diz qual fonte usou.
