# GeoFusion Operational Dashboard

## Objetivo

O dashboard é a primeira camada de produto operacional do GeoFusion. Ele torna pesquisáveis os recapes oficiais, a superfície disponível, a última intervenção conhecida, a janela de proteção e as evidências de qualidade geométrica sem promover resultados shadow para a base oficial.

## Navegação

- **Home**: resumo do acervo, cobertura oficial/shadow/estimada e entrada de busca.
- **Consulta de Via**: rua, número, coordenadas ou ID; alternativas permanecem visíveis quando há ambiguidade.
- **Proteção de Recapes**: ACTIVE, EXPIRING_SOON, EXPIRED e UNKNOWN_DATE com filtros e fila NEEDS_ATTENTION.
- **Mapa**: camadas controladas de qualidade e proteção, com filtros por subprefeitura e revestimento.
- **Auditoria**: famílias oficial, reconstrução, validator, boundary/nome e consensus, com linha do tempo.
- **Qualidade**: agregações determinísticas sobre os artefatos carregados.
- **Pipeline / Sobre**: cadeia técnica, artefatos observados, limites e princípios de uso.

## Contratos e guardrails

O app usa `OperationalQueryService` e seus serviços de StreetLookup, SurfaceLookup, ResurfacingLookup e ResurfacingProtection. O cálculo recebe uma data de referência explícita; nesta execução é `date.today()`. A data `data_termino` é tratada como data de término/conclusão do recape. Data de recebimento de notificação não é promovida a data de execução.

Uma busca ambígua retorna `DO_NOT_SELECT_SILENTLY`. Notificações associadas são apresentadas como `NEEDS_ATTENTION`; o dashboard não usa a linguagem de “violação” para essa fila.

A pesquisa experimental de image geometry permanece arquivada. Não há recuperação automática desses candidatos na camada operacional.

## Performance e cache

O lookup textual usa o índice SQLite persistido `data/processed/operational_lookup_index.sqlite` (53,4 MB; 217.212 segmentos). Ele contém apenas os campos necessários para normalização, identificação e faixas numéricas; o WKB fica armazenado para materialização lazy dos candidatos. A geometria completa e o STRtree continuam no caminho espacial.

| Operação cold em processo novo | Antes — GeoJSON | Depois — índice | Observação |
| --- | ---: | ---: | --- |
| street, operação | 25,16 s | 21,65 ms | processo legado parseia 168 MB; índice não carrega `_segments` |
| street+number, operação | 20,01 s | 2,20 ms | amostra determinística |
| street+number, processo total | 21,76 s | 1,30 s | inclui importação do Python |
| coordinate, operação | 19,61 s | 19,04 s | preservado: caminho espacial continua completo |

Em um processo já aquecido, street+number teve mediana de 0,201 ms no legado e 0,279 ms com o índice. A memória RSS após street+number foi 765,6 MB no legado contra 114,3 MB com o índice; coordinate continua em ~765 MB porque precisa do recurso espacial. A equivalência serializada foi 1.000/1.000, sem diferenças.

O app usa `st.cache_resource` com uma assinatura barata de mtime/tamanho dos artefatos, incluindo o metadata do índice. A validação evita hash da fonte no runtime; o SHA-256 é calculado apenas no build offline. Se o índice estiver ausente ou stale, o repository faz fallback ao GeoJSON e o dashboard exibe um erro operacional com o comando de rebuild.

Detalhes, perfil por componente, benchmark reproduzível e procedimento de rebuild estão em [operational-performance.md](operational-performance.md).

## Design system e revisão visual

A interface segue um sistema dark-first, técnico e de baixa distração: canvas `#080D14`, superfícies `#101827` e `#151F30`, bordas `#223149`, texto `#E8EEF7`, texto secundário `#8FA1B7` e azul de ação `#77A8FF`. Estados de proteção, geometria e disponibilidade usam badges semânticos consistentes.

O shell compartilhado concentra navegação, referência temporal, cabeçalho de página, métricas, títulos de seção, tabelas HTML compactas, painéis de detalhe, estados vazios, avisos e provenance. A hierarquia privilegia uma leitura principal por tela; barras horizontais são usadas apenas para comparações agregadas em cobertura e qualidade.

Os breakpoints revisados são 1366×768, 1920×1080 e aproximadamente 1024px. Em larguras menores, o shell reduz a largura da sidebar, empilha princípios e preserva ações sem quebra de texto. Os estados de consulta vazia, ambígua, não encontrada, proteção sem data e auditoria incompleta mantêm a incerteza explícita e oferecem contexto para o próximo passo.

Esta camada é exclusivamente de apresentação. Não altera RoadGraph, StreetResolver, reconstrução, validators, aliases, consenso, boundary, regras temporais, índice SQLite ou a separação entre oficial e shadow.

## Verificação

```powershell
.\.venv\Scripts\python.exe -m compileall -q dashboard src/operational
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

O teste de integração do dashboard confirma 5.022 recapes, 1.577 geometrias oficiais, os quatro estados de proteção e a ambiguidade de `AVENIDA PAULISTA`. Os hashes em `data/processed/operational_dashboard_protected_hashes_*.json` registram que o núcleo geoespacial, aliases, outputs oficiais e shadows protegidos permaneceram sem alteração.
