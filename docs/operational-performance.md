# GeoFusion — experimento de performance do lookup operacional

## Resultado

Classificação final: **PERFORMANCE_READY** para o lookup textual operacional.

O gargalo era a carga do GeoJSON GeoSampa de 168.661.177 bytes em qualquer primeiro lookup textual. O caminho otimizado constrói offline um SQLite versionado com 217.212 registros e mantém a carga de geometria fora do startup textual. `StreetLookupService`, `StreetResolver`, `RoadGraph`, semântica de faixas, proteção, superfície, consensus, shadow e image research não foram alterados.

## Artefatos

- `data/processed/operational_lookup_index.sqlite`: índice persistido, 53.370.880 bytes.
- `data/processed/operational_lookup_index_metadata.json`: versão, schema, normalização, tamanho/mtime e SHA-256 da fonte.
- `data/processed/operational_lookup_query_sample.json`: 1.000 consultas street+number determinísticas.
- `data/processed/operational_lookup_profile_before.json` e `_after.json`: perfil por componente.
- `data/processed/operational_lookup_benchmark.json`: cold em processos novos e warm no mesmo processo.
- `data/processed/operational_lookup_equivalence.json`: comparação serializada contra o caminho legado.
- `data/processed/operational_lookup_index_determinism.json`: hashes de builds consecutivos.

## Desenho

O índice textual persiste `segment_id`, nome original, nome normalizado, CODLOG e as quatro colunas de faixa numérica. O índice `(normalized_street, source_order)` preserva a ordem determinística do GeoJSON. O campo `geometry_wkb` só é lido quando `StreetLookupService` materializa uma alternativa ou um segmento selecionado.

`OperationalRepository.segments` e `spatial_segments` permanecem no caminho legado completo. Assim, a consulta textual não paga o custo de parse/construção do STRtree, enquanto a consulta por coordenadas mantém exatamente o recurso espacial anterior.

## Números observados

O perfil detalhado do caminho legado registrou: leitura GeoJSON 0,137 s, parse JSON 4,210 s, parse de geometria/registros 17,024 s, faixas numéricas 1,000 s, dicionário 0,086 s, STRtree 0,591 s, carga integral do repository 20,718 s e primeiro lookup textual 26,578 s.

No perfil após a mudança: construção CRS 0,014 s, leitura 0,216 s, parse JSON 4,839 s, geometria 5,656 s, normalização 7,371 s, faixas 0,905 s, dicionário 0,058 s, STRtree 0,507 s, carga integral 18,062 s, primeiro lookup textual 0,021 s, primeiro lookup operacional 1,541 s e coordenada 20,437 s. Esses componentes continuam disponíveis para o caminho espacial/diagnóstico; não são executados pelo primeiro lookup textual indexado.

O benchmark de processos novos mediu street+number em 20,012 s de operação no legado e 2,20 ms com o índice; o tempo total de processo foi 21,760 s contra 1,301 s. A mediana warm foi 0,201 ms contra 0,279 ms. Coordenada mediu 19,608 s contra 19,042 s, confirmando que o índice textual não mascarou o custo espacial.

RSS após street+number: 765,566 MB no legado e 114,324 MB com o índice. Após coordenada: 764,934 MB e 765,059 MB, respectivamente.

## Equivalência e determinismo

Foram executadas 1.000 consultas com rua e número dentro de uma faixa par/ímpar disponível. A comparação usa `StreetLookupResult.to_dict()`, incluindo método, confiança, candidato, faixas, WKT, warnings e provenance: `mismatch_count = 0`.

Três hashes do SQLite foram idênticos: o original, o primeiro rebuild e o segundo rebuild. O metadata inclui `source_sha256`, mas a validação em runtime usa somente versão, schema, normalização, tamanho e `mtime_ns` da fonte, evitando re-hash de 168 MB a cada rerun.

## Build e verificação

```powershell
\.venv\Scripts\python.exe -m src.operational.build_lookup_index --root . --benchmark
\.venv\Scripts\python.exe -m src.operational.lookup_benchmark --root . --sample-only
\.venv\Scripts\python.exe -m src.operational.lookup_benchmark --root . --equivalence --output data/processed/operational_lookup_equivalence.json
\.venv\Scripts\python.exe -m src.operational.lookup_benchmark --root . --cold-repetitions 2 --repeat 20 --output data/processed/operational_lookup_benchmark.json
\.venv\Scripts\python.exe -m src.operational.lookup_benchmark --root . --determinism --output data/processed/operational_lookup_index_determinism.json
```

O índice não é gerado automaticamente pelo dashboard. Se o metadata não corresponder à fonte, o fallback é explícito e o dashboard orienta executar o primeiro comando.
