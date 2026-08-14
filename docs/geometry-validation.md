# Validação geométrica independente — modo shadow

## Escopo

`src/geometry_validator.py` é uma camada exploratória para os registros cujo
`geometry_confidence` permanece `ESTIMATED` em
`data/processed/route_geometry_quality_shadow.csv`. Ela não altera o ETL, o
`RoadGraph`, o `StreetResolver`, os geradores, decisões humanas, aliases,
geometrias oficiais ou qualquer saída oficial.

O grafo GeoSampa é aberto somente para leitura espacial. O módulo não chama
`route`, resolução de logradouro, heurística de geração ou escritor de output.
O score oficial é exportado somente em `official_geometry_score_comparison_only`
e não entra no score independente.

## Execução

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& .\.venv\Scripts\python.exe src\geometry_validator.py --shadow --sample 30
& .\.venv\Scripts\python.exe src\geometry_validator.py --shadow --sample 100 --resume
& .\.venv\Scripts\python.exe src\geometry_validator.py --shadow --resume
```

Opções disponíveis: `--only-strategy`, `--only-root-cause`, `--resume` e
`--reset-cache`. O último remove exclusivamente os dois artefatos da própria
validação antes de recomeçar.

## Evidências independentes

Cada caso recebe WKT, hash SHA-256 da geometria analisada, ID, classe e score
independente, além de:

- alinhamento e distância à via principal encontrada por comparação exata no
  grafo;
- continuidade, lacunas, loop/interseção e componentes topológicos;
- distância do GPS à geometria e posição ao longo do caminho;
- desvio de extensão nas faixas `<=10`, `10-25`, `25-50` e `>50`;
- confirmação independente de `De` e `Até`, quando o nome existe no grafo;
- quantidade de alternativas e margem independente entre o candidato e a
  melhor alternativa;
- falhas duras e avisos.

Os nomes não passam por fuzzy match nem por aliases. Se `via` não existir por
comparação exata, o campo persistido `main_street` pode ser usado como segunda
chave de consulta, sempre sem correção textual da produção.

## Calibração e controles

As geometrias com `path` preenchido em `recape_clean.csv` são controles positivos.
Elas são separadas deterministicamente em calibração (70%) e validação (30%).
Os limites são quantis observados nos controles positivos; as perturbações
negativas de validação são:

- `WRONG_STREET_OFFSET`;
- `WRONG_COMPONENT_OFFSET`;
- `CRITICAL_GAP`;
- `LOOP`;
- `EXTREME_EXTENSION`.

Os negativos são controles sintéticos, não exemplos de produção e não podem ser
usados como evidência de recuperação. Os valores e taxas de aceitação/rejeição
ficam registrados em `geometry_validation_report.json`.

## Classes e interpretação

- `VALIDATED_HIGH`: evidências independentes acima do quantil alto dos controles,
  sem falha dura e com quantidade mínima de evidências.
- `VALIDATED_MEDIUM`: sinal consistente, mas abaixo do nível de HIGH ou com
  cobertura de evidência menor.
- `INSUFFICIENT_EVIDENCE`: sinal incompleto ou abaixo do quantil de controle.
- `REJECTED`: falha geométrica, topológica, de continuidade, extensão ou limite
  contradito. Isso rejeita a validação shadow, não rebaixa o output oficial.

Toda recomendação é `PROMOTE_HIGH`, `PROMOTE_MEDIUM`, `KEEP_ESTIMATED` ou `REJECT`. O relatório sempre
registra `official_promotions: 0`, `official_status_changed: false` e hashes dos
arquivos oficiais antes/depois.

## Validação estatística

O relatório junta, quando disponível, os rótulos da revisão humana. Precisão e
recall são exibidos com intervalo de Wilson de 95%; com menos de 30 casos
rotulados, o resultado é explicitamente insuficiente para promoção. A ausência
de rótulos não é tratada como aprovação.

`shadow_proposals` descreve oportunidades de investigação com complexidade,
risco, casos afetados, dependências, falsos positivos e ROI por limite superior.
Nenhuma proposta é implementada automaticamente. Ganho de cobertura é apenas um
limite superior da simulação, nunca uma previsão oficial.

## Artefatos

- `data/processed/geometry_validation_shadow.csv`: resultados por caso ESTIMATED;
- `data/processed/geometry_validation_report.json`: calibração, controles,
  simulação, métricas humanas, padrões e propostas;
- este documento: contrato operacional e limites da análise.

## Critérios de aceite

```powershell
& .\.venv\Scripts\python.exe -m py_compile src\geometry_validator.py
& .\.venv\Scripts\pytest.exe -q
```

Além dos testes, a execução deve manter os hashes oficiais iguais, produzir WKT
e hashes para os casos processados, manter o score oficial apenas como campo de
comparação e não alterar nenhuma decisão ou classificação oficial.
