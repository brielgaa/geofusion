# Boundary Contradiction Diagnostic Engine

## Propósito

`src/boundary_contradiction_audit.py` investiga, em modo exclusivamente shadow,
os casos em que a validação geométrica independente marcou `De` ou `Até` como
`BOUNDARY_CONTRADICTION`. A população atual é derivada de
`data/processed/geometry_validation_shadow.csv`; o cadastro de resolução atual é
lido de `route_geometry_quality_shadow.csv` apenas para comparação.

O módulo não importa `geometry_validator.py`, não chama `RoadGraph.route()`, não
usa `StreetResolver`, não altera RoadGraph, ETL, aliases, dashboard, decisões
humanas, geometrias oficiais ou classes oficiais. Caminhos entre interseções são
temporários e servem apenas para comparação diagnóstica.

## Execução

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& .\.venv\Scripts\python.exe src\boundary_contradiction_audit.py --shadow --sample 30
& .\.venv\Scripts\python.exe src\boundary_contradiction_audit.py --shadow --sample 100
& .\.venv\Scripts\python.exe src\boundary_contradiction_audit.py --shadow --resume
```

Também estão disponíveis `--only-root-cause`, `--only-id`, `--resume` e
`--reset-cache`. O cache é local à auditoria e nunca é lido pelo pipeline.

## Resolução contextual restrita

Para cada limite, os candidatos são limitados a ruas encontradas:

- nos segmentos próximos à via principal;
- nos segmentos próximos à geometria e à extremidade analisada;
- nas ruas que intersectam ou quase intersectam a via principal;
- no nome atual, quando há correspondência exata no grafo.

A comparação lexical ocorre somente nesse conjunto local. Não há fuzzy global ou
consulta ao StreetResolver. Cada candidato mantém nome, CODLOG, similaridade,
distância à extremidade, tipo de interseção, gap, componente e margem local.

## Evidências

São registrados separadamente para `De` e `Até`:

- nome original, nome atual e normalização local;
- `VALID`, `PLAUSIBLE`, `AMBIGUOUS`, `CONTRADICTORY` ou `NOT_FOUND`;
- interseção em nó, interseção geométrica sem nó, gap pequeno, paralelo próximo
  ou ausência de interseção;
- distância, snap requerido, CODLOG, componente, GPS e candidatos alternativos;
- hipótese de limites invertidos;
- caminho temporário entre interseções e comparação de overlap, comprimento e
  Hausdorff com a geometria candidata.

As causas primárias e secundárias incluem problemas de nome, transversal ausente,
rua paralela, componente incorreto, interseção sem nó, gap topológico, múltiplas
interseções, `SAME_TRANSVERSAL`, `VIA_INTEIRA`, `FIM_DA_VIA`, limites invertidos,
somente um limite válido e contradição de dados.

## Calibração

Os controles positivos são geometrias oficiais com pelo menos um limite
informado, divididas deterministicamente em calibração e validação. Distâncias
de extremidade, gaps, score e quantidade de evidências usam quantis dos
controles positivos. Controles negativos sintéticos — troca de De, troca de Até,
inversão de um limite, rua paralela, componente errado e transversal próxima
incorreta — são avaliados separadamente para medir falsa aceitação.

Os thresholds efetivos e as taxas de aceitação/rejeição ficam em
`boundary_contradiction_report.json`. A auditoria não usa `geometry_score` nem
qualquer score/threshold do gerador.

## Score e recomendações

O `boundary_validation_score` combina, independentemente, evidência de De,
evidência de Até, topologia, GPS, extensão e margem entre candidatos. As
recomendações são diagnósticas:

- `BOUNDARIES_VALIDATED_HIGH`;
- `BOUNDARIES_VALIDATED_MEDIUM`;
- `ONE_BOUNDARY_VALIDATED`;
- `BOUNDARIES_REVERSED`;
- `KEEP_CONTRADICTION`;
- `DATA_INSUFFICIENT`.

Nenhuma delas promove ou rebaixa uma geometria oficial.

## Artefatos

- `data/processed/boundary_contradiction_audit.csv`: uma linha por contradição;
- `data/processed/boundary_contradiction_report.json`: métricas, controles,
  thresholds, causas, priorização e impacto shadow;
- `data/processed/boundary_contradiction_audit_cache.json`: cache local da
  calibração e hashes de entrada;
- `docs/boundary-contradictions.md`: metodologia e limitações.

O impacto reportado como ganho de cobertura é apenas limite superior da
simulação. Sem revisão humana suficiente, não é precision/recall populacional e
não autoriza implementação de heurística.
