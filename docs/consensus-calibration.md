# Auditoria metodológica do Consensus Evidence Engine

## Escopo

`src/consensus_calibration.py` é uma camada de diagnóstico shadow. Ela lê os
artefatos persistidos e o report do consenso, mas não altera `recape_clean.csv`,
decisões humanas, cobertura oficial ou qualquer módulo de geração de
geometria. Não houve ajuste de threshold.

O artefato de entrada contém 5.022 IDs no universo do consenso e 1.577
geometrias oficiais. A auditoria também executa 200 controles negativos
reutilizados, quatro cenários com 50 IDs-base cada, e 200 controles positivos
sintéticos derivados das geometrias oficiais.

## Por que 0/1.577 era ambíguo

O número anterior misturava a existência de um registro oficial com a
possibilidade de avaliar esse registro pelo consenso. No estado atual, os
1.577 IDs oficiais têm registro mapeado de `street_resolution`, mas nenhum
`candidate_wkt` nos artefatos independentes usados pelo consenso:

| etapa | quantidade |
| --- | ---: |
| controles oficiais | 1.577 |
| com qualquer artefato mapeado | 1.577 |
| com candidate geometry | 0 |
| candidate comparável à oficial | 0 |
| snapshot alinhado | 0 |
| pelo menos duas famílias independentes | 0 |
| avaliáveis | 0 |
| não avaliáveis | 1.577 |

Portanto, a taxa de aceitação principal não é `0/1.577`: ela é
`undefined` entre avaliáveis, pois o denominador avaliável é zero. O CSV
classifica esses controles como `NOT_EVALUABLE`, com causa primária
`MISSING_CANDIDATE_GEOMETRY`. Isso é uma limitação de ligação/cobertura dos
controles, não uma rejeição demonstrada pelo consenso.

## Regra de avaliabilidade

Um controle positivo é avaliável somente quando:

- existe candidato canônico;
- o candidato é `EXACT_OFFICIAL` ou `NEAR_OFFICIAL`;
- o snapshot está `SNAPSHOT_ALIGNED`;
- há pelo menos duas famílias independentes;
- há evidência explícita de topologia e componente.

`PARTIAL_OFFICIAL`, `DIFFERENT_FROM_OFFICIAL` e `NO_CANDIDATE` são separados
como falha do desenho do controle (`POSITIVE_CONTROL_CANDIDATE_WRONG` ou
`MISSING_CANDIDATE_GEOMETRY`). Ausência de uma família não é rejeição.

As classes de saída são `NOT_EVALUABLE`, `EVALUABLE_ACCEPTED`,
`EVALUABLE_REJECTED`, `EVALUABLE_CONFLICTING` e `SNAPSHOT_INVALID`.

## Controles sintéticos

Foram construídos bundles coerentes a partir das geometrias oficiais, sem
inventar geometria:

| variante | resultado em 200 |
| --- | --- |
| completa | 200 `CONSENSUS_HIGH` |
| sem validator | 200 `CONSENSUS_MEDIUM` |
| sem boundary | 200 `CONSENSUS_MEDIUM` |
| sem topology | 200 `INSUFFICIENT_EVIDENCE` |
| sem component | 200 `INSUFFICIENT_EVIDENCE` |
| sem margem, mas `candidate_count=1` | 200 `CONSENSUS_HIGH` |

O resultado comprova que `HIGH` é alcançável e que a remoção de topologia ou
componente não pode ser tratada como evidência positiva. A ausência da margem
não implica competição quando o bundle informa um único candidato.

## Correção mínima

Foi corrigido um bug lógico em `src/consensus_evidence.py`:

- antes, `topology_ok is not False` e `component_ok is not False` permitiam
  que `None` fosse aceito como válido para `HIGH`/`MEDIUM`;
- agora ambos precisam ser explicitamente `True`;
- os loaders preservam campos ausentes como `None`, em vez de convertê-los em
  `True`.

Essa mudança não altera threshold, StreetResolver, RoadGraph, módulos de
geometria, outputs oficiais ou decisões humanas. No dataset real, as contagens
do consenso permaneceram iguais: `HIGH=0`, `MEDIUM=20`,
`CONFLICTING=1.877`, `INSUFFICIENT=2.514` e `REJECTED=611`.

## Conflitos e rejeitados

Os 1.877 conflitos foram distribuídos por motivos derivados dos campos
persistidos:

- geometry mismatch: 611;
- boundary conflict: 543;
- component conflict: 310;
- true source disagreement: 235;
- topology conflict: 95;
- codlog conflict: 63;
- candidate competition: 20.

Não houve `PSEUDO_CONFLICT_MISSING_DATA` no report atual. Os 611 rejeitados
possuem pelo menos duas famílias independentes rejeitando: 608 têm duas e 3
têm três. A auditoria não encontrou rejeitados com apenas uma família ou
evidência insuficiente para a regra de rejeição.

## Controles negativos e revisão humana

Os 200 negativos foram separados, mas nenhum é avaliável: o mesmo problema de
ligação deixa esses cenários sem candidato/famílias independentes. Assim,
`negative_false_accepted=0` é descritivo, mas a taxa de falsa aceitação entre
avaliáveis é `undefined`, não zero.

Na amostra humana de 42 linhas, houve 38 aprovadas, 3 rejeitadas e 1 deferida.
As 38 aprovadas aparecem como `CONFLICTING_EVIDENCE`; as 3 rejeitadas como
`REJECTED_BY_CONSENSUS`. A amostra é pequena e não foi usada para estimar
precisão/recall populacional.

## Limitações e decisão

A limitação dominante é a ausência de join entre as geometrias oficiais e
candidate geometries dos artefatos independentes. A auditoria demonstra que a
lógica consegue produzir todas as cinco classes solicitadas e que a correção
necessária foi pontual. Ela não demonstra poder estatístico para medir
aceitação de positivos reais até que os controles oficiais tenham candidatos
comparáveis e famílias independentes ligadas ao mesmo ID/snapshot.

Os números completos e hashes ficam em
`data/processed/consensus_calibration_report.json`; o detalhamento por ID fica
em `data/processed/consensus_positive_control_audit.csv`.
