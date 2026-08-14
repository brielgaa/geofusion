# Consensus Evidence Engine

## Objetivo

`src/consensus_evidence.py` é a camada final de diagnóstico do GeoFusion. Ela
faz joins somente de artefatos persistidos, verifica se as fontes apontam para
a mesma geometria e produz uma classificação auditável em
`data/processed/consensus_evidence_shadow.csv`.

O módulo não chama `StreetResolver`, `RoadGraph.route()`, ETL ou geradores de
geometria. Não importa decisões humanas para o core e não escreve aliases,
geometrias oficiais ou outputs oficiais. A única escrita é a dos dois
artefatos shadow do próprio engine.

```text
artefatos persistidos
        |
        v
normalização por schema -> EvidenceRecord
        |
        +-> snapshot/proveniência
        +-> equivalência geométrica
        +-> famílias e dependências
        +-> competição de candidatos
        v
ConsensusEvidenceResult -> CSV + relatório JSON
```

Execute com:

```bash
python src/consensus_evidence.py --shadow
python src/consensus_evidence.py --shadow --sample 30
python src/consensus_evidence.py --shadow --only-id 1008 --only-class CONSENSUS_HIGH
```

## Artefatos e população

O inventário é feito em tempo de execução. O relatório registra arquivo,
schema, coluna de ID, WKT/path, classificações, versões explícitas, timestamp,
SHA-256, duplicidade de IDs e observações de schema desconhecido.

Os adapters atuais leem:

| Fonte | Artefato | Família | Grupo independente |
| --- | --- | --- | --- |
| route quality | `route_geometry_quality_shadow.csv` | `TOPOLOGY` | `ROUTE_QUALITY_CHAIN` |
| validator | `geometry_validation_shadow.csv` | `GEOMETRY_VALIDATION` | `GEOMETRY_VALIDATION` |
| boundary audit | `boundary_contradiction_audit.csv` | `BOUNDARY_GEOMETRY` | `BOUNDARY_CHAIN` |
| name recovery | `boundary_name_recovery.csv` | `BOUNDARY_LEXICAL` | `BOUNDARY_CHAIN` |
| route audit | `route_geometry_audit.csv` | `TOPOLOGY` | `ROUTE_QUALITY_CHAIN` |
| street resolution | `street_resolution_audit.csv` | `STREET_RESOLUTION` | `STREET_RESOLUTION` |
| human review | `route_geometry_human_review.csv` | `HUMAN_REVIEW` | `HUMAN_REVIEW` |

`recape_clean.csv` é tratado como população-base e controle positivo, não como
evidência independente. A população total, os IDs comuns e a cobertura de
cada fonte são descobertos; nenhum total histórico é hard-coded.

## Independência e dependências

Uma fonte não se torna independente apenas por estar em outro arquivo.
`boundary_name_recovery` depende de `boundary_contradiction_audit` e os dois
compartilham `BOUNDARY_CHAIN`; portanto, dois lados recuperados ou duas
classificações dessa cadeia não contam como duas famílias independentes.

`geometry_validator` depende do candidato persistido por route quality, mas sua
validação geométrica é mantida como família própria. O grafo completo, usado no
relatório, expõe `evidence_family`, `source_module`, `depends_on` e
`independent_group`.

## Mesma geometria

`compare_geometry_candidates(a, b)` retorna:

- `EXACT`: hash WKT igual ou equivalência espacial exata;
- `NEAR_EQUIVALENT`: Hausdorff máximo de até 2 m, distância de endpoints de até
  3 m e diferença de comprimento de até 5%;
- `PARTIAL_OVERLAP`: sobreposição linear de pelo menos 25%, sem equivalência
  próxima;
- `DIFFERENT`: candidatos incompatíveis;
- `UNKNOWN`: WKT ausente, inválido ou não linear.

As tolerâncias ficam em `GeometryEquivalenceConfig` e são registradas no JSON.
Hash é comparado primeiro quando a geometria é válida; um hash igual não torna
um WKT inválido aceitável.

## Classes e gates

As únicas classes finais são `CONSENSUS_HIGH`, `CONSENSUS_MEDIUM`,
`CONFLICTING_EVIDENCE`, `INSUFFICIENT_EVIDENCE` e
`REJECTED_BY_CONSENSUS`.

`CONSENSUS_HIGH` exige duas famílias independentes fortes, candidato comum,
topologia/componente coerentes, sem falha crítica, sem competição severa e sem
conflito de snapshot. `CONSENSUS_MEDIUM` admite evidência forte coerente com
evidência auxiliar, mas continua bloqueada por falhas críticas.

`REJECTED_BY_CONSENSUS` só é usado quando pelo menos dois grupos independentes
rejeitam e não existe apoio positivo. Ausência de dados produz
`INSUFFICIENT_EVIDENCE`; não é uma rejeição.

O `consensus_score` é estruturado e explicativo: famílias independentes,
equivalência geométrica, topologia, limites, nome, GPS, extensão, componente e
CODLOG recebem contribuições limitadas; conflitos, hard failures e competição
aplicam penalidades. O score não substitui os gates e não é uma soma de
`HIGH=3`/`MEDIUM=2`.

## Snapshot e provenance

Cada registro guarda versão do módulo, SHA-256 do artefato e identificadores de
geração quando persistidos. `SNAPSHOT_ALIGNED`, `SNAPSHOT_PARTIAL`,
`SNAPSHOT_CONFLICT` e `SNAPSHOT_UNKNOWN` são reportados por caso. Versões de
módulos diferentes não são tratadas como conflito automaticamente; conflito
exige divergência de `snapshot_id`, geração ou hash de entrada. Artefatos com
versões mistas são listados explicitamente no inventário.

`SNAPSHOT_CONFLICT` sempre bloqueia `CONSENSUS_HIGH` e gera conflito no
resultado. Proveniência ausente permanece visível como `PARTIAL` ou `UNKNOWN`.

## Controles e ablação

Geometrias oficiais com `path` válido são controles positivos. O engine compara
a geometria oficial métrica com o candidato já persistido e mede aceitação e
falsos negativos; não injeta a geometria oficial como evidência.

Controles negativos determinísticos incluem substituição de geometria, limite,
rua paralela e competição de candidato. Eles ficam fora da população normal.
O relatório inclui taxa de falsa aceitação e intervalo/qualificação possível;
uma amostra pequena não é apresentada como precisão populacional.

A ablação recalcula as classes sem boundary, name recovery, validator e
topologia. A diferença de HIGH/MEDIUM/CONFLICTING mostra quais fontes mudam o
resultado. A matriz de concordância também marca pares dependentes, para que
alta concordância não seja confundida com informação independente.

## Cobertura projetada e limites

O relatório separa:

- `official_geometry_coverage_pct`;
- `projected_shadow_high_coverage`;
- `projected_shadow_high_medium_coverage`.

Esses números são uma simulação sobre a população-base. Um caso que já possui
geometria oficial não é contado como ganho. `official_promotions_applied` é
sempre `0`. Nenhuma classe shadow altera `recape_clean.csv`, aliases, dashboard,
StreetResolver, RoadGraph ou validators existentes.

Limitações relevantes ficam repetidas no relatório: não há um identificador de
execução comum em todos os snapshots atuais, a amostra humana é pequena e
boundary audit/name recovery compartilham uma cadeia dependente.
