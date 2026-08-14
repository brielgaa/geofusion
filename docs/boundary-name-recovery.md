# Boundary Name Recovery (shadow)

`src/boundary_name_recovery.py` é uma camada diagnóstica independente para investigar os casos `NAME_PROBLEM` apontados pela auditoria de contradições De/Até. Ela não altera `RoadGraph`, `StreetResolver`, o ETL, o dashboard, o arquivo oficial de aliases ou qualquer geometria oficial.

## Fluxo

1. Lê `data/processed/boundary_contradiction_audit.csv` e mantém as linhas cujo `root_cause` ou `secondary_causes` indicam problema de nome.
2. Junta o contexto persistido da geometria shadow e da coordenada GPS.
3. Localiza candidatos somente na vizinhança da via principal, da geometria candidata e do endpoint De/Até. Não existe fuzzy global da cidade.
4. Compara os nomes depois da confirmação espacial, preservando a diferença entre tokens estruturais, auxiliares e críticos.
5. Registra score, margem top-2, interseção, distância GPS, componente, tipologia lexical e advertências.

## Critério conservador

`NAME_RECOVERED_HIGH` exige tokens críticos exatos, interseção real ou geométrica, mesmo componente e margem suficiente. `NAME_RECOVERED_MEDIUM` admite evidência geométrica forte com incerteza controlada. Similaridade lexical sem confirmação espacial, troca de token crítico, componente errado ou margem pequena permanecem como `NAME_AMBIGUOUS`, `NAME_DATA_CONTRADICTION` ou `NAME_NOT_FOUND`.

O score é independente de `geometry_score` e de `boundary_validation_score`. A margem top-2 impede que um nome apenas parecido seja tratado como único.

## Tipos e aliases

Os tipos lexicais são apenas classificatórios: abreviação, typo, truncamento, token faltante/extra, tipo de logradouro, acento, ordem de tokens e variação numérica. A ausência de fonte histórica não é convertida artificialmente em `OLD_OR_ALTERNATIVE_NAME`.

`data/processed/boundary_alias_candidates.csv` é uma lista de oportunidades. `GLOBAL_ALIAS` só é sugerido com recorrência, baixa ambiguidade, confirmações geográficas e coerência dos tokens críticos; os demais ficam como `CONTEXTUAL_ALIAS` ou `DO_NOT_ALIAS`. Nenhuma linha é escrita em `data/config/street_aliases.csv`.

## Controles

Os controles positivos usam geometrias oficiais somente como referência conhecida e degradam nomes de forma determinística. Os negativos exercitam troca de token crítico, nomes semelhantes, ausência de interseção e componente errado. O relatório apresenta `recovery_rate`, acurácia top-1/top-2, taxa de rejeição, falsa aceitação, intervalos de Wilson e o tamanho mínimo de 30 casos.

## Execução

```powershell
python src/boundary_name_recovery.py --shadow --sample 30
python src/boundary_name_recovery.py --shadow --sample 100
python src/boundary_name_recovery.py --shadow --resume
python src/boundary_name_recovery.py --shadow --only-side DE --only-problem-type TYPO
```

Saídas:

- `data/processed/boundary_name_recovery.csv`
- `data/processed/boundary_name_recovery_report.json`
- `data/processed/boundary_alias_candidates.csv`
- `data/processed/boundary_name_recovery_cache.json`

Todas são shadow. O relatório inclui hashes antes/depois dos arquivos protegidos e exige igualdade para considerar a execução íntegra.

## Resultado da execução completa

Na execução final sobre a população oficial de 661 casos (1.322 lados), o engine encontrou 86 lados `NAME_RECOVERED_HIGH`, 138 `NAME_RECOVERED_MEDIUM`, 780 ambíguos, 11 não encontrados e 307 em contradição de dados. Em projeção por caso, isso correspondeu a 2 casos potencialmente HIGH e 218 potencialmente MEDIUM; nenhuma decisão oficial foi aplicada.

Os controles positivos tiveram 720 casos, recovery rate de 33,47%, acurácia top-1 de 67,50% e top-2 de 67,50%. A baixa taxa de promoção é intencional: o candidato correto frequentemente foi identificado, mas não recebeu evidência suficiente para promoção. Os 250 controles negativos foram todos rejeitados (falsa aceitação de 0%; limite superior de Wilson de 1,51%). Cada tipo de controle teve pelo menos 30 casos.

Foram gerados 305 candidatos de alias, dos quais 18 ficaram como `CONTEXTUAL_ALIAS` e nenhum como `GLOBAL_ALIAS`. Portanto, nenhum alias global tem evidência suficiente nesta execução. O principal sinal lexical observado foi a combinação de abreviação, acento e variações de tokens; isso não constitui validação para uma nova heurística oficial.

## Limitações

O engine não inventa nomes antigos, não transforma uma aproximação lexical em alias e não corrige a base. Controles sintéticos não substituem revisão humana; resultados com poucos casos ou falsa aceitação relevante não devem virar heurística oficial.
