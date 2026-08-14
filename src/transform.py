import argparse
import pandas as pd
import os
import re
import math
import sys
import json
import time
import shutil
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
import unicodedata
from rapidfuzz import process, fuzz

try:
    import geopandas as gpd
    import requests
    from shapely.geometry import Point
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer
    try:
        from road_graph import RoadGraph
    except ImportError:
        from .road_graph import RoadGraph
except ImportError:
    gpd = None
    requests = None
    RoadGraph = None

try:
    from street_resolution_overrides import load_human_review_overrides, HumanReviewOverrides
except ImportError:
    from .street_resolution_overrides import load_human_review_overrides, HumanReviewOverrides

_ROUTE_WORKER_GRAPH = None


def _init_route_worker(graph):
    global _ROUTE_WORKER_GRAPH
    _ROUTE_WORKER_GRAPH = graph
    _ROUTE_WORKER_GRAPH._rebuild_spatial_index()


def _route_worker(task):
    index, values = task
    via, de, ate, reference, expected, codlog = values
    before_intersections = set(_ROUTE_WORKER_GRAPH.intersection_cache)
    before_resolutions = set(_ROUTE_WORKER_GRAPH.resolution_cache)
    before_paths = set(_ROUTE_WORKER_GRAPH.path_cache)
    routed = _ROUTE_WORKER_GRAPH.route(
        via, de, ate, reference=reference, expected_length=expected, codlog=codlog
    )
    return index, routed, {
        'intersections': {k: _ROUTE_WORKER_GRAPH.intersection_cache[k] for k in set(_ROUTE_WORKER_GRAPH.intersection_cache) - before_intersections},
        'resolutions': {k: _ROUTE_WORKER_GRAPH.resolution_cache[k] for k in set(_ROUTE_WORKER_GRAPH.resolution_cache) - before_resolutions},
        'paths': {k: _ROUTE_WORKER_GRAPH.path_cache[k] for k in set(_ROUTE_WORKER_GRAPH.path_cache) - before_paths},
    }


def _failure_diagnosis(status, metadata, row):
    """Converte o estado técnico da rota em uma causa operacional explícita."""
    metadata = metadata if isinstance(metadata, dict) else {}
    if status == 'HUMAN_UNRESOLVED' or metadata.get('human_review_decision') == 'MARCAR_COMO_NAO_RESOLVIDO':
        return 'HUMAN_UNRESOLVED', 'Decisão humana marcou o recape como não resolvido; fuzzy automático foi bloqueado.'
    via_found = bool(metadata.get('rua_via_resolvida'))
    codlog_status = metadata.get('codlog_status', 'NAO_INFORMADO')
    method_via = metadata.get('method_via', '')
    method_de = metadata.get('method_de', '')
    method_ate = metadata.get('method_ate', '')

    if not via_found:
        if codlog_status == 'INEXISTENTE':
            reason = 'CODLOG_INEXISTENTE'
            detail = 'CODLOG informado não foi encontrado no índice GeoSampa e a via também não foi resolvida.'
        elif method_via in ('SEM_NOME', 'SEM_GEOMETRIA'):
            reason = 'FUZZY_NAO_RESOLVEU' if method_via == 'SEM_GEOMETRIA' else 'SEM_RUA'
            detail = 'A via não foi encontrada por nome exato nem pelo limite de resolução fuzzy.'
        else:
            reason = 'SEM_RUA'
            detail = 'A via principal não possui correspondência no GeoSampa.'
    elif status in ('SEM_RUA_DE', 'SEM_RUA_ATE'):
        campo = 'De' if status == 'SEM_RUA_DE' else 'Até'
        method = method_de if campo == 'De' else method_ate
        reason = 'FUZZY_NAO_RESOLVEU' if method == 'SEM_GEOMETRIA' else 'SEM_RUA'
        detail = f'O logradouro de referência "{campo}" não foi resolvido no GeoSampa.'
    elif status == 'SEM_INTERSECAO_DE':
        reason = 'SEM_INTERSECAO_DE'
        detail = 'A via foi encontrada, mas não há interseção topológica com o logradouro informado em De.'
    elif status == 'SEM_INTERSECAO_ATE':
        reason = 'SEM_INTERSECAO_ATE'
        detail = 'A via foi encontrada, mas não há interseção topológica com o logradouro informado em Até.'
    elif status == 'SEM_CAMINHO':
        reason = 'SEM_CAMINHO'
        detail = 'As interseções foram encontradas, porém os nós pertencem a componentes desconectados ou não há caminho válido.'
    elif status in ('SEM_GEOMETRIA', 'GEOMETRIA_INVALIDA'):
        reason = 'GEOMETRIA_INVALIDA'
        detail = 'A rota foi resolvida, mas a geometria final ficou vazia ou inválida.'
    else:
        reason = 'OUTROS'
        detail = f'Falha não classificada no estado técnico {status!r}.'

    evidence = (
        f'via_resolvida={metadata.get("rua_via_resolvida") or "NÃO"}; '
        f'segmentos_via={metadata.get("segment_count_via", 0)}; '
        f'interseções_de={metadata.get("intersection_count_de", 0)}; '
        f'interseções_ate={metadata.get("intersection_count_ate", 0)}; '
        f'componente={"SIM" if metadata.get("component_connected") else "NÃO"}; '
        f'caminho={"SIM" if metadata.get("path_found") else "NÃO"}; '
        f'métodos=via:{method_via or "-"},de:{method_de or "-"},ate:{method_ate or "-"}'
    )
    return reason, f'{detail} Evidências: {evidence}'

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

PROJECT_DIR = os.path.dirname(os.path.dirname(__file__))
RAW_DIR = os.path.join(PROJECT_DIR, 'data', 'raw')
PROCESSED_DIR = os.path.join(PROJECT_DIR, 'data', 'processed')
CACHE_DIR = os.path.join(PROJECT_DIR, 'data', 'cache')
GEOSAMPA_SEGMENTOS = os.path.join(CACHE_DIR, 'geosampa_segmento_logradouro.geojson')
PIPELINE_RUN_PATH = os.path.join(PROCESSED_DIR, 'pipeline_run.json')
DEFAULT_HUMAN_REVIEW_PATH = os.path.join(PROCESSED_DIR, 'street_resolution_human_review.csv')
HUMAN_OVERRIDE_REPORT_PATH = os.path.join(PROCESSED_DIR, 'street_resolution_override_report.json')
HUMAN_OVERRIDE_ERRORS_PATH = os.path.join(PROCESSED_DIR, 'street_resolution_override_errors.csv')
HUMAN_OVERRIDE_SHADOW_PATH = os.path.join(PROCESSED_DIR, 'street_resolution_override_shadow.csv')

# Códigos persistidos sem elementos visuais. Rótulos, cores e ícones pertencem
# à interface; CSVs antigos com emojis seguem legíveis no dashboard.
SITUACAO_CONCLUIDO = 'CONCLUIDO'
SITUACAO_PLANEJADO = 'PLANEJADO'
SITUACAO_EM_ANDAMENTO = 'EM_ANDAMENTO'
SITUACAO_SEM_COBERTURA = 'SEM_COBERTURA'
SITUACAO_REVISAO = 'REVISAO'

# ─────────────────────────────────────────
# NORMALIZAÇÃO DE NOME DE RUA
# ─────────────────────────────────────────
_MOJIBAKE_MARKERS = ('Ã', 'Â', '�', '‰', 'Š', 'Œ', 'Ž', 'š', 'œ', 'ž', 'Ÿ')

def corrigir_texto(valor):
    if not isinstance(valor, str):
        return valor
    if not any(marker in valor for marker in _MOJIBAKE_MARKERS):
        return valor
    for encoding in ('cp1252', 'latin-1'):
        try:
            return valor.encode(encoding).decode('utf-8')
        except UnicodeError:
            continue
    return valor


def corrigir_textos_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [corrigir_texto(str(col)).strip() for col in df.columns]
    for col in df.select_dtypes(include='object').columns:
        df[col] = df[col].map(corrigir_texto)
    return df


_ABREVIACOES = {
    r'\bDR\b': 'DOUTOR',
    r'\bDRA\b': 'DOUTORA',
    r'\bPROF\b': 'PROFESSOR',
    r'\bPROFA\b': 'PROFESSORA',
    r'\bPRES\b': 'PRESIDENTE',
    r'\bDEP\b': 'DEPUTADO',
    r'\bENG\b': 'ENGENHEIRO',
    r'\bENGO\b': 'ENGENHEIRO',
    r'\bPE\b': 'PADRE',
    r'\bSTA\b': 'SANTA',
    r'\bSTO\b': 'SANTO',
    r'\bS\b': 'SAO',
    r'\bCEL\b': 'CORONEL',
    r'\bCAP\b': 'CAPITAO',
    r'\bGEN\b': 'GENERAL',
    r'\bEMB\b': 'EMBAIXADOR',
}

_ALIASES_LOGRADOURO = {
    'GUACURUS': 'GUAICURUS',
    'AVENIDA DOS BANDEIRANTES': 'BANDEIRANTES',
}


def normalizar_cep(valor) -> str:
    if not isinstance(valor, str):
        return ''
    return re.sub(r'\D', '', valor)


def parse_data(serie):
    return pd.to_datetime(serie, dayfirst=True, errors='coerce', format='mixed')


_PREFIXOS = re.compile(
    r'^(RUA|R\.?|AVENIDA|AV\.?|ALAMEDA|AL\.?|TRAVESSA|TV\.?|'
    r'ESTRADA|EST\.?|RODOVIA|ROD\.?|PRAÇA|PC\.?|LARGO|LGO\.?|'
    r'VIELA|VL\.?|VIADUTO|VD\.?)\s+', re.IGNORECASE
)

def normalizar_rua(nome: str) -> str:
    if not isinstance(nome, str):
        return ''
    nome = corrigir_texto(nome)
    nome = re.split(r'\s+-\s+|,\s+|/\s*|\s+\(', nome, maxsplit=1)[0]
    nome = nome.upper().strip()
    nome = unicodedata.normalize('NFKD', nome).encode('ascii', 'ignore').decode('ascii')
    nome = re.sub(r'[^\w\s]', ' ', nome)   # remove pontuação
    nome = _PREFIXOS.sub('', nome)          # remove prefixo de logradouro
    for padrao, substituto in _ABREVIACOES.items():
        nome = re.sub(padrao, substituto, nome)
    nome = re.sub(r'\s+', ' ', nome).strip()
    nome = _ALIASES_LOGRADOURO.get(nome, nome)
    return nome


def eh_toda_extensao(valor) -> bool:
    valor_norm = normalizar_rua(valor)
    return any(termo in valor_norm for termo in (
        'TODA EXTENSAO',
        'TODA A EXTENSAO',
        'EM TODA EXTENSAO',
    ))


def classificar_situacao(status_recape, metodo_match: str) -> str:
    """Classifica o resultado operacional sem alterar as regras de match.

    ``REVISAO`` é reservado para decisões humanas na camada de produto. O ETL
    mantém a classificação histórica: match por nome/CEP/coordenada não muda o
    estado de execução do recape associado.
    """
    if metodo_match == 'SEM_COBERTURA':
        return SITUACAO_SEM_COBERTURA
    status = str(status_recape or '').strip().upper()
    if status == 'CONCLUIDO':
        return SITUACAO_CONCLUIDO
    if status == 'PLANEJADO':
        return SITUACAO_PLANEJADO
    return SITUACAO_EM_ANDAMENTO


def calcular_cobertura(cruzamento: pd.DataFrame) -> dict:
    """Retorna contagens de cobertura, inclusive para DataFrames vazios."""
    total = int(len(cruzamento))
    if total == 0 or 'metodo_match' not in cruzamento.columns:
        return {'total': total, 'com_cobertura': 0, 'sem_cobertura': total, 'cobertura_pct': 0.0}
    com_cobertura = int(cruzamento['metodo_match'].fillna('SEM_COBERTURA').ne('SEM_COBERTURA').sum())
    return {
        'total': total,
        'com_cobertura': com_cobertura,
        'sem_cobertura': total - com_cobertura,
        'cobertura_pct': com_cobertura / total * 100,
    }


def salvar_pipeline_run(payload: dict, path: str = PIPELINE_RUN_PATH) -> None:
    """Persiste o estado da execução atual sem inventar histórico de runs."""
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    temporary_path = f'{path}.tmp'
    with open(temporary_path, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    os.replace(temporary_path, path)


def _atomic_write_dataframe(frame: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = f'{path}.tmp'
    frame.to_csv(temporary_path, index=False, encoding='utf-8-sig')
    os.replace(temporary_path, path)


# ─────────────────────────────────────────
# DISTÂNCIA GEOGRÁFICA (haversine, km)
# ─────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    try:
        phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
        dphi       = math.radians(float(lat2) - float(lat1))
        dlambda    = math.radians(float(lon2) - float(lon1))
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    except Exception:
        return float('inf')


def coordenada_valida(lat, lon) -> bool:
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False
    return -24.1 <= lat <= -23.3 and -47.0 <= lon <= -46.3


def montar_indice_espacial(df_recape: pd.DataFrame, cell_size=0.005):
    indice = {}
    coords = []
    for idx, row in df_recape.iterrows():
        lat = row.get('latitude')
        lon = row.get('longitude')
        if not coordenada_valida(lat, lon):
            continue
        lat = float(lat)
        lon = float(lon)
        regional = str(row.get('subprefeitura', '')).strip().upper()
        coords.append((idx, lat, lon, regional))
        cell = (math.floor(lat / cell_size), math.floor(lon / cell_size))
        indice.setdefault(cell, []).append((idx, lat, lon, regional))
    return indice, cell_size, coords


def recape_mais_proximo(lat, lon, indice_espacial, limite_km=0.15, regional=None, exigir_regional=False):
    if not coordenada_valida(lat, lon):
        return None, float('inf')

    indice, cell_size, _ = indice_espacial
    lat = float(lat)
    lon = float(lon)
    regional = str(regional or '').strip().upper()
    cell_lat = math.floor(lat / cell_size)
    cell_lon = math.floor(lon / cell_size)
    alcance = math.ceil((limite_km / 111) / cell_size) + 1
    melhor_idx = None
    melhor_dist = float('inf')

    for dlat in range(-alcance, alcance + 1):
        for dlon in range(-alcance, alcance + 1):
            for idx, lat_r, lon_r, reg_r in indice.get((cell_lat + dlat, cell_lon + dlon), []):
                if exigir_regional and regional and reg_r != regional:
                    continue
                dist = haversine(lat, lon, lat_r, lon_r)
                if dist < melhor_dist:
                    melhor_idx = idx
                    melhor_dist = dist

    if melhor_dist <= limite_km:
        return melhor_idx, melhor_dist
    return None, melhor_dist


# ─────────────────────────────────────────
# LEITURA — RECAPE
# ─────────────────────────────────────────
def load_recape(filename='recape.csv') -> pd.DataFrame:
    candidate = os.path.join(RAW_DIR, filename)
    if not os.path.exists(candidate):
        for ext in ('.xlsx', '.xls', '.csv'):
            cand = os.path.join(RAW_DIR, f'recape{ext}')
            if os.path.exists(cand):
                candidate = cand
                break

    ext = os.path.splitext(candidate)[1].lower()
    if ext in ('.xlsx', '.xls'):
        df = pd.read_excel(candidate, dtype=str)
    else:
        df = pd.read_csv(candidate, sep='\t', encoding='latin-1', dtype=str)

    df = corrigir_textos_df(df)
    df = df.rename(columns={
        'Número do Processo': 'numero_processo', 'Nº de OS': 'numero_os',
        'Tipo de Serviço': 'tipo_servico', 'Número': 'numero',
        'Latitude': 'latitude_raw', 'Longitude': 'longitude_raw',
        'Data Hora Recebimento': 'data_recebimento',
        'Data Hora Atualização': 'data_atualizacao',
        'Priorização': 'priorizacao', 'status': 'status',
        'id': 'id', 'Recurso': 'recurso', 'Status': 'status',
        'Data Término': 'data_termino', 'Via': 'via',
        'De': 'de', 'Até': 'ate',
        'Extensão (m)': 'extensao_m', 'Área (m²)': 'area_m2',
        'Data Criação': 'data_criacao', 'Data Última Atualização': 'data_atualizacao',
        'Data Término': 'data_termino', 'Data TÃ©rmino': 'data_termino', 'Via': 'via',
        'De': 'de', 'Até': 'ate', 'AtÃ©': 'ate',
        'Logradouro Geosampa': 'logradouro_geosampa',
        'Subprefeitura': 'subprefeitura',
        'Extensão (m)': 'extensao_m', 'ExtensÃ£o (m)': 'extensao_m',
        'Área (m²)': 'area_m2', 'Ã\x81rea (mÂ²)': 'area_m2',
        'Revestimento': 'revestimento', 'Ativo?': 'ativo',
        'Data Criação': 'data_criacao', 'Data CriaÃ§Ã£o': 'data_criacao',
        'Data Última Atualização': 'data_atualizacao', 'Data Ãšltima AtualizaÃ§Ã£o': 'data_atualizacao',
        'Ponto Geometria': 'ponto_geometria',
    })

    df['data_criacao']  = parse_data(df.get('data_criacao'))
    df['data_termino']  = parse_data(df.get('data_termino'))
    df['extensao_m']    = pd.to_numeric(df.get('extensao_m'), errors='coerce')
    df['area_m2']       = pd.to_numeric(df.get('area_m2'),    errors='coerce')
    df['status']        = df.get('status', pd.Series(dtype=str)).astype(str).str.strip().str.upper()
    df['subprefeitura'] = df.get('subprefeitura', pd.Series(dtype=str)).astype(str).str.strip().str.upper()

    # Usar logradouro_geosampa como rua principal (mais padronizado)
    df['rua_raw'] = df.get('logradouro_geosampa', df.get('via', '')).astype(str)
    df['rua_norm'] = df['rua_raw'].apply(normalizar_rua)

    # Extrair lat/lon do campo "Ponto Geometria" → "-23.487, -46.392"
    def parse_ponto(val):
        try:
            parts = str(val).split(',')
            return float(parts[0].strip()), float(parts[1].strip())
        except Exception:
            return None, None

    coords = df.get('ponto_geometria', pd.Series(dtype=str)).apply(parse_ponto)
    df['latitude']  = coords.apply(lambda x: x[0])
    df['longitude'] = coords.apply(lambda x: x[1])
    df['fonte'] = 'RECAPE'
    return df


def baixar_segmentos_geosampa(cache_path=GEOSAMPA_SEGMENTOS, page_size=10000) -> str | None:
    if gpd is None or requests is None:
        print("   ⚠️ geopandas/requests não estão instalados; recapes ficarão sem linhas.")
        return None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding='utf-8') as stream:
                cached = json.load(stream)
            if cached.get('type') == 'FeatureCollection' and cached.get('features'):
                return cache_path
        except (OSError, ValueError, AttributeError):
            pass

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    url = 'https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/wfs'
    params_base = {
        'service': 'WFS',
        'version': '2.0.0',
        'request': 'GetFeature',
        'typeNames': 'geoportal:segmento_logradouro',
        'outputFormat': 'application/json',
        'count': page_size,
    }
    features = []
    start = 0
    total = None
    while total is None or start < total:
        params = {**params_base, 'startIndex': start}
        resp = requests.get(url, params=params, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        total = data.get('totalFeatures') or data.get('numberMatched') or len(data.get('features', []))
        page = data.get('features', [])
        if not page:
            break
        features.extend(page)
        start += len(page)
        print(f"   ↳ GeoSampa: {min(start, total):,}/{total:,} segmentos baixados")

    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump({
            'type': 'FeatureCollection',
            'crs': {'type': 'name', 'properties': {'name': 'EPSG:31983'}},
            'features': features,
        }, f)
    return cache_path


def enriquecer_recape_com_geosampa(df_recape: pd.DataFrame) -> pd.DataFrame:
    if gpd is None or RoadGraph is None:
        return df_recape
    return df_recape
    """Implementação anterior mantida apenas como referência histórica.

    try:
        cache_path = baixar_segmentos_geosampa()
        if not cache_path:
            return df_recape
        ruas = gpd.read_file(cache_path)
        if ruas.crs is None:
            ruas = ruas.set_crs('EPSG:31983')
        ruas = ruas.to_crs('EPSG:31983')
    except Exception as exc:
        print(f"   ⚠️ Não foi possível carregar GeoSampa: {exc}")
        return df_recape

    ruas = ruas[ruas.geometry.notna() & ~ruas.geometry.is_empty].copy()
    ruas['rua_norm'] = ruas['nm_logradouro'].astype(str).apply(normalizar_rua)
    linhas_por_rua = ruas.groupby('rua_norm')['geometry'].apply(list).to_dict()
    nomes_ruas = list(linhas_por_rua.keys())
    to_utm = Transformer.from_crs('EPSG:4326', 'EPSG:31983', always_xy=True).transform
    to_ll = Transformer.from_crs('EPSG:31983', 'EPSG:4326', always_xy=True).transform
    geom_cache = {}
    path_cache = {}

    def geom_rua(nome, threshold=92):
        nome = normalizar_rua(nome)
        if not nome:
            return None
        if nome in geom_cache:
            return geom_cache[nome]
        if nome in linhas_por_rua:
            geom_cache[nome] = linhas_por_rua[nome]
            return geom_cache[nome]
        best = process.extractOne(nome, nomes_ruas, scorer=fuzz.token_sort_ratio)
        if best and best[1] >= threshold:
            geom_cache[nome] = linhas_por_rua[best[0]]
            return geom_cache[nome]
        geom_cache[nome] = None
        return None

    paths = []
    status_paths = []
    for _, row in df_recape.iterrows():
        via_nome = row.get('logradouro_geosampa') or row.get('via') or row.get('rua_raw')
        de_nome = row.get('de')
        ate_nome = row.get('ate')
        cache_key = (normalizar_rua(via_nome), normalizar_rua(de_nome), normalizar_rua(ate_nome))
        if cache_key in path_cache:
            path, status = path_cache[cache_key]
            paths.append(path)
            status_paths.append(status)
            continue

        via_linhas = geom_rua(via_nome)
        if via_linhas is None:
            path_cache[cache_key] = (None, 'SEM_RUA_GEOM')
            paths.append(None)
            status_paths.append('SEM_RUA_GEOM')
            continue

        referencia = None
        if pd.notna(row.get('longitude')) and pd.notna(row.get('latitude')):
            referencia = shp_transform(to_utm, Point(float(row['longitude']), float(row['latitude'])))

        via_geom = _unir_linhas_principais(via_linhas, referencia, raio_m=5000, limite_sem_raio=80, fator_minimo=0.5)
        if via_geom is None:
            path_cache[cache_key] = (None, 'SEM_RUA_GEOM')
            paths.append(None)
            status_paths.append('SEM_RUA_GEOM')
            continue

        if eh_toda_extensao(de_nome) or eh_toda_extensao(ate_nome):
            trecho = _linha_mais_representativa(via_geom, referencia)
            if trecho is not None and not trecho.is_empty:
                trecho_ll = shp_transform(to_ll, trecho)
                path = json.dumps([[round(x, 7), round(y, 7)] for x, y in trecho_ll.coords], ensure_ascii=False)
                path_cache[cache_key] = (path, 'TODA_EXTENSAO')
                paths.append(path)
                status_paths.append('TODA_EXTENSAO')
                continue

        de_linhas = geom_rua(de_nome)
        ate_linhas = geom_rua(ate_nome)
        if de_linhas is None or ate_linhas is None:
            path_cache[cache_key] = (None, 'SEM_RUA_GEOM')
            paths.append(None)
            status_paths.append('SEM_RUA_GEOM')
            continue

        de_geom = _unir_linhas_locais(de_linhas, referencia)
        ate_geom = _unir_linhas_locais(ate_linhas, referencia)
        if de_geom is None or ate_geom is None:
            if via_geom is not None:
                trecho = _linha_mais_representativa(via_geom, referencia)
                if trecho is not None and not trecho.is_empty:
                    trecho_ll = shp_transform(to_ll, trecho)
                    path = json.dumps([[round(x, 7), round(y, 7)] for x, y in trecho_ll.coords], ensure_ascii=False)
                    path_cache[cache_key] = (path, 'FALLBACK_VIA')
                    paths.append(path)
                    status_paths.append('FALLBACK_VIA')
                    continue
            path_cache[cache_key] = (None, 'SEM_RUA_GEOM')
            paths.append(None)
            status_paths.append('SEM_RUA_GEOM')
            continue

        p_ini = _ponto_intersecao(via_geom, de_geom, referencia)
        p_fim = _ponto_intersecao(via_geom, ate_geom, referencia)
        trecho = None
        if p_ini is not None and p_fim is not None:
            trecho = _cortar_linha_entre_pontos(via_geom, p_ini, p_fim, referencia)
        if trecho is None or trecho.is_empty:
            trecho = _cortar_linha_por_aproximacao(via_geom, de_geom, ate_geom, referencia)
        if trecho is None or trecho.is_empty:
            trecho = _linha_mais_representativa(via_geom, referencia)
        if trecho is None or trecho.is_empty:
            path_cache[cache_key] = (None, 'SEM_TRECHO')
            paths.append(None)
            status_paths.append('SEM_TRECHO')
            continue
        if via_geom is not None and trecho.length < max(120, via_geom.length * 0.25):
            trecho_amplo = _linha_mais_representativa(via_geom, referencia)
            if trecho_amplo is not None and not trecho_amplo.is_empty and trecho_amplo.length >= trecho.length:
                trecho = trecho_amplo
        status_path = 'OK' if p_ini is not None and p_fim is not None else 'FALLBACK_VIA'

        trecho_ll = shp_transform(to_ll, trecho)
        path = json.dumps([[round(x, 7), round(y, 7)] for x, y in trecho_ll.coords], ensure_ascii=False)
        path_cache[cache_key] = (path, status_path)
        paths.append(path)
        status_paths.append(status_path)

    df_recape = df_recape.copy()
    df_recape['path'] = paths
    df_recape['status_path'] = status_paths
    total_linhas = df_recape['path'].notna().sum()
    print(f"   ✅ {total_linhas:,}/{len(df_recape):,} recapes com linha GeoSampa")
    return df_recape
    """


# ─────────────────────────────────────────
# LEITURA — SGZ CONVIAS
# ─────────────────────────────────────────
def enriquecer_recape_com_geosampa(
    df_recape: pd.DataFrame,
    human_review_mode: str = 'off',
    human_review_path=None,
    output_suffix: str = '',
) -> pd.DataFrame:
    """Roteia todos os recapes sobre um único índice persistente."""
    if human_review_mode not in ('off', 'shadow', 'apply'):
        raise ValueError(f'Modo de revisão humana inválido: {human_review_mode}')
    started = time.perf_counter()
    if gpd is None or RoadGraph is None:
        return df_recape
    try:
        cache_path = baixar_segmentos_geosampa()
        if not cache_path:
            return df_recape
        read_started = time.perf_counter()
        ruas = gpd.read_file(cache_path)
        if ruas.crs is None:
            ruas = ruas.set_crs('EPSG:31983')
        ruas = ruas.to_crs('EPSG:31983')
        ruas = ruas[ruas.geometry.notna() & ~ruas.geometry.is_empty].copy()
        print(f'   Leitura GeoJSON: {time.perf_counter() - read_started:.2f}s ({len(ruas):,} segmentos)')
        graph_path = os.path.join(CACHE_DIR, 'geosampa_road_graph.pkl')
        graph_started = time.perf_counter()
        graph = RoadGraph.load_cached(graph_path, cache_path, normalizer=normalizar_rua)
        if graph is None:
            print('   Construindo grafo topologico GeoSampa...')
            graph = RoadGraph.from_geodataframe(
                ruas, normalizar_rua,
                progress=lambda done, total: print(f'      índice: {done:,}/{total:,}', end='\r')
            )
            os.makedirs(CACHE_DIR, exist_ok=True)
            graph.save(graph_path, cache_path)
        else:
            print('   Grafo GeoSampa carregado do cache.')
        print(f'   Grafo + caches estruturais: {time.perf_counter() - graph_started:.2f}s')
    except Exception as exc:
        print(f'   Nao foi possivel carregar o grafo GeoSampa: {exc}')
        return df_recape

    to_utm = Transformer.from_crs('EPSG:4326', 'EPSG:31983', always_xy=True).transform
    to_ll = Transformer.from_crs('EPSG:31983', 'EPSG:4326', always_xy=True).transform
    result = df_recape.copy()
    overrides = None
    overrides_by_index = {}
    if human_review_mode in ('shadow', 'apply'):
        overrides = load_human_review_overrides(
            graph, normalizar_rua, review_path=human_review_path or DEFAULT_HUMAN_REVIEW_PATH
        )
        for index, row in result.iterrows():
            overrides_by_index[index] = overrides.for_record(row)
        related = sum(item is not None for item in overrides_by_index.values())
        print(f'   Revisoes humanas: {overrides.total_reviews_loaded:,} carregadas; {related:,} relacionadas por registro')
        for column in (
            'human_review_applied', 'human_review_decision', 'human_review_source', 'human_review_key',
            'human_reviewed_at', 'human_reviewed_by', 'human_resolved_street', 'human_resolved_codlog',
            'human_override_valid', 'human_override_validation_reason', 'street_resolution_method_final',
            'street_resolution_source_final',
        ):
            result[column] = False if column in ('human_review_applied', 'human_override_valid') else None
    route_started = time.perf_counter()
    paths, statuses, calculated_lengths, deviations, segment_counts = [], [], [], [], []
    resolution_methods, failure_categories = [], []
    final_methods, final_sources = [], []
    failure_rows = []
    tasks, keys, routed, pending = [], {}, {}, set()
    effective_values = {}
    for index, row in result.iterrows():
        via = next((row.get(column) for column in ('logradouro_geosampa', 'via', 'rua_raw')
                    if isinstance(row.get(column), str) and row.get(column).strip()), '')
        de, ate = row.get('de', ''), row.get('ate', '')
        codlog = row.get('codlog') or row.get('cd_codlog') or ''
        override = overrides_by_index.get(index)
        if override is not None:
            result.at[index, 'human_review_decision'] = override.decision
            result.at[index, 'human_review_source'] = override.source
            result.at[index, 'human_review_key'] = override.review_key
            result.at[index, 'human_reviewed_at'] = override.reviewed_at
            result.at[index, 'human_reviewed_by'] = override.reviewed_by
            result.at[index, 'human_resolved_street'] = override.resolved_street
            result.at[index, 'human_resolved_codlog'] = override.resolved_codlog
            result.at[index, 'human_override_valid'] = override.valid
            result.at[index, 'human_override_validation_reason'] = override.validation_reason
            if human_review_mode == 'apply' and override.valid and override.applicable:
                if override.block_fuzzy:
                    keys[index] = ('HUMAN_UNRESOLVED', str(index))
                    result.at[index, 'human_review_applied'] = True
                    routed[index] = (None, 'HUMAN_UNRESOLVED', {
                        'method_via': 'HUMAN_UNRESOLVED', 'human_review_decision': override.decision,
                        'human_review_source': override.source, 'path_found': False,
                    })
                    effective_values[index] = (via, de, ate, codlog, override)
                    overrides.mark_applied()
                    continue
                via = override.resolved_street or via
                codlog = override.resolved_codlog or ''
                result.at[index, 'human_review_applied'] = True
                effective_values[index] = (via, de, ate, codlog, override)
                overrides.mark_applied()
            elif human_review_mode == 'shadow' and override.valid and override.applicable:
                effective_values[index] = (override.resolved_street or via, de, ate, override.resolved_codlog or '', override)
        effective_values.setdefault(index, (via, de, ate, codlog, override))
        expected = pd.to_numeric(row.get('extensao_m'), errors='coerce')
        expected_value = float(expected) if pd.notna(expected) else None
        key = (normalizar_rua(via), normalizar_rua(de), normalizar_rua(ate), str(codlog).strip(), expected_value)
        keys[index] = key
        if key in graph.route_cache:
            routed[index] = graph.route_cache[key]
            continue
        if key in pending:
            continue
        reference = None
        if pd.notna(row.get('longitude')) and pd.notna(row.get('latitude')):
            reference = shp_transform(to_utm, Point(float(row['longitude']), float(row['latitude'])))
        tasks.append((index, (via, de, ate, reference, expected_value, codlog)))
        pending.add(key)

    if tasks:
        workers = min(max(os.cpu_count() or 2, 2), len(tasks), 8)
        print(f'   Calculando rotas: {len(tasks):,} únicas, {workers} processos...')
        try:
            with ProcessPoolExecutor(max_workers=workers, initializer=_init_route_worker, initargs=(graph,)) as pool:
                futures = [pool.submit(_route_worker, task) for task in tasks]
                for done, future in enumerate(as_completed(futures), 1):
                    index, routed_result, cache_delta = future.result()
                    routed[index] = routed_result
                    graph.intersection_cache.update(cache_delta['intersections'])
                    graph.resolution_cache.update(cache_delta['resolutions'])
                    graph.path_cache.update(cache_delta['paths'])
                    if done == len(futures) or done % 100 == 0:
                        print(f'      {done:,}/{len(futures):,} ({done / len(futures) * 100:.1f}%)', end='\r')
        except Exception as exc:
            print(f'\n   Processamento multiprocesso indisponível ({exc}); usando processo principal.')
            for done, (index, values) in enumerate(tasks, 1):
                routed[index] = graph.route(values[0], values[1], values[2], reference=values[3], expected_length=values[4], codlog=values[5])
                if done == len(tasks) or done % 100 == 0:
                    print(f'      {done:,}/{len(tasks):,}', end='\r')

    route_by_key = {keys[index]: routed[index] for index, _ in tasks if index in routed}
    for index in result.index:
        if index not in routed and keys.get(index) in route_by_key:
            routed[index] = route_by_key[keys[index]]

    shadow_rows = []
    shadow_geometry_possible = 0
    if human_review_mode == 'shadow' and overrides is not None:
        for index, override in overrides_by_index.items():
            if override is None:
                continue
            current_route = routed.get(index)
            current_geometry = current_route[0] if current_route else None
            override_geometry = None
            override_status = 'NAO_APLICAVEL'
            if override.valid and override.applicable:
                if override.block_fuzzy:
                    override_status = 'HUMAN_UNRESOLVED'
                else:
                    via_h, de_h, ate_h, codlog_h, _ = effective_values[index]
                    expected_h = pd.to_numeric(result.loc[index].get('extensao_m'), errors='coerce')
                    expected_h = float(expected_h) if pd.notna(expected_h) else None
                    reference_h = None
                    if pd.notna(result.loc[index].get('longitude')) and pd.notna(result.loc[index].get('latitude')):
                        reference_h = shp_transform(to_utm, Point(float(result.loc[index]['longitude']), float(result.loc[index]['latitude'])))
                    override_route = graph.route(via_h, de_h, ate_h, reference=reference_h, expected_length=expected_h, codlog=codlog_h)
                    override_geometry = override_route[0]
                    override_status = override_route[1]
            shadow_rows.append({
                'ID': result.loc[index].get('id'), 'review_key': override.review_key,
                'resolucao_atual': result.loc[index].get('rua_raw'),
                'resolucao_humana': override.resolved_street, 'codlog_atual': result.loc[index].get('codlog') or result.loc[index].get('cd_codlog'),
                'codlog_humano': override.resolved_codlog, 'decisao': override.decision,
                'override_valido': override.valid, 'motivo': override.validation_reason,
                'mudaria_resultado': bool(override.valid and override.applicable and (
                    override.resolved_street != normalizar_rua(result.loc[index].get('rua_raw', '')) or
                    str(override.resolved_codlog or '') != str(result.loc[index].get('codlog') or result.loc[index].get('cd_codlog') or '')
                )),
                'geometria_atual_disponivel': current_geometry is not None,
                'geometria_com_override_seria_possivel': override_geometry is not None,
                'status_geometria_override': override_status,
            })
        _atomic_write_dataframe(pd.DataFrame(shadow_rows), HUMAN_OVERRIDE_SHADOW_PATH)
        shadow_geometry_possible = sum(bool(row['geometria_com_override_seria_possivel']) for row in shadow_rows)
    for index, row in result.iterrows():
        if index not in routed:
            routed[index] = route_by_key[keys[index]]
        geometry, status, metadata = routed[index]
        path = None
        length = None
        deviation = None
        if geometry is not None:
            length = float(geometry.length)
            expected_value = keys[index][-1]
            if expected_value is not None and expected_value > 0:
                deviation = abs(length - expected_value) / expected_value * 100
            geometry_ll = shp_transform(to_ll, geometry)
            path = json.dumps([[round(x, 7), round(y, 7)] for x, y in geometry_ll.coords], ensure_ascii=False)
        count = metadata.get('segment_count') if isinstance(metadata, dict) else None
        technical_method = metadata.get('method_via') if isinstance(metadata, dict) else None
        resolution_methods.append(technical_method)
        override = overrides_by_index.get(index)
        if human_review_mode == 'apply' and override is not None and override.valid and override.applicable:
            final_method = 'HUMAN_UNRESOLVED' if override.block_fuzzy else (
                'HUMAN_CHOSEN_CANDIDATE' if override.decision == 'ESCOLHER_OUTRO_CANDIDATO' else 'HUMAN_REVIEW'
            )
            final_source = 'HUMAN_REVIEW'
        else:
            final_method = technical_method if technical_method in ('CODLOG', 'ALIAS', 'EXATO', 'FUZZY') else 'SEM_RESOLUCAO'
            final_source = technical_method or 'ROAD_GRAPH'
        final_methods.append(final_method)
        final_sources.append(final_source)
        category = None
        if geometry is None:
            reason, detail = _failure_diagnosis(status, metadata, row)
            category = reason
            failure_rows.append({
                'id': row.get('id'),
                'recurso': row.get('recurso'),
                'status': row.get('status'),
                'via': row.get('via'),
                'de': row.get('de'),
                'até': row.get('ate'),
                'logradouro_geosampa': row.get('logradouro_geosampa'),
                'codlog': row.get('codlog') or row.get('cd_codlog'),
                'rua_encontrada_no_geosampa': 'sim' if metadata.get('rua_via_resolvida') else 'não',
                'quantidade_segmentos_encontrados': int(metadata.get('segment_count_via', 0) or 0),
                'quantidade_intersecoes_de': int(metadata.get('intersection_count_de', 0) or 0),
                'quantidade_intersecoes_ate': int(metadata.get('intersection_count_ate', 0) or 0),
                'componente_conectado_encontrado': 'sim' if metadata.get('component_connected') else 'não',
                'caminho_encontrado': 'sim' if metadata.get('path_found') else 'não',
                'motivo_final_da_falha': reason,
                'mensagem_detalhada': detail,
            })
        failure_categories.append(category)
        paths.append(path)
        statuses.append(status)
        calculated_lengths.append(length)
        deviations.append(deviation)
        segment_counts.append(count)
    result['path'] = paths
    result['status_path'] = statuses
    result['comprimento_path_m'] = calculated_lengths
    result['desvio_extensao_pct'] = deviations
    result['segment_count_path'] = segment_counts
    result['resolucao_via'] = resolution_methods
    result['categoria_falha'] = failure_categories
    if human_review_mode in ('shadow', 'apply'):
        result['street_resolution_method_final'] = final_methods
        result['street_resolution_source_final'] = final_sources
    for index, route_result in routed.items():
        graph.route_cache[keys[index]] = route_result
    if human_review_mode != 'shadow':
        graph.save(graph_path, cache_path)
    status_counts = result['status_path'].value_counts(dropna=False).to_dict()
    failure_reason_names = [
        'SEM_RUA', 'SEM_INTERSECAO_DE', 'SEM_INTERSECAO_ATE', 'SEM_CAMINHO',
        'CODLOG_INEXISTENTE', 'GEOMETRIA_INVALIDA', 'FUZZY_NAO_RESOLVEU', 'OUTROS',
    ]
    failure_counts = result['categoria_falha'].value_counts(dropna=True).to_dict()
    report = {
        'total_recapes': int(len(result)),
        'com_geometria': int(result['path'].notna().sum()),
        'cobertura_pct': float(result['path'].notna().mean() * 100) if len(result) else 0.0,
        'status': {str(k): int(v) for k, v in status_counts.items()},
        'falhas_por_motivo': {name: int(failure_counts.get(name, 0)) for name in failure_reason_names},
        'falhas_por_categoria': {str(k): int(v) for k, v in failure_counts.items()},
        'fuzzy': int((result['resolucao_via'] == 'FUZZY').sum()),
        'falhas_detalhadas': int(len(failure_rows)),
    }
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    failure_columns = [
        'id', 'recurso', 'status', 'via', 'de', 'até', 'logradouro_geosampa', 'codlog',
        'rua_encontrada_no_geosampa', 'quantidade_segmentos_encontrados',
        'quantidade_intersecoes_de', 'quantidade_intersecoes_ate',
        'componente_conectado_encontrado', 'caminho_encontrado',
        'motivo_final_da_falha', 'mensagem_detalhada',
    ]
    pd.DataFrame(failure_rows, columns=failure_columns).to_csv(
        os.path.join(PROCESSED_DIR, f'recapes_sem_cobertura{output_suffix}.csv'),
        index=False, encoding='utf-8-sig'
    )
    with open(os.path.join(PROCESSED_DIR, f'geosampa_coverage_report{output_suffix}.json'), 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    if overrides is not None:
        human_applied = result['human_review_applied'].fillna(False).astype(bool)
        valid_applicable = result['human_override_valid'].fillna(False).astype(bool) & result['human_review_decision'].isin((
            'APROVAR_RECOMENDACAO', 'ESCOLHER_OUTRO_CANDIDATO', 'MARCAR_COMO_NAO_RESOLVIDO'
        ))
        resolved_by_human = human_applied & result['path'].notna()
        overrides.write_report(
            HUMAN_OVERRIDE_REPORT_PATH,
            HUMAN_OVERRIDE_ERRORS_PATH,
            recapes_resolved_by_human_review=int(resolved_by_human.sum()),
            recapes_resolved_by_fallback=int(result['path'].notna().sum()) - int(resolved_by_human.sum()),
            recapes_unresolved_after_all_methods=int(result['path'].isna().sum()),
            overrides_would_apply=int(valid_applicable.sum()),
            recapes_would_be_resolved_by_human_review=int(shadow_geometry_possible),
            human_review_mode=human_review_mode,
        )
    print(f'\n   Rotas: {time.perf_counter() - route_started:.2f}s')
    print(f'   Grafo: {result["path"].notna().sum():,}/{len(result):,} recapes com linha GeoSampa ({result["path"].notna().mean() * 100:.1f}%)')
    print(f'   Falhas/status: {status_counts}')
    print(f'   Relatório de cobertura: {report["falhas_por_categoria"]} | fuzzy={report["fuzzy"]}')
    print(f'   Recapes sem cobertura: {len(failure_rows):,} | CSV: data/processed/recapes_sem_cobertura.csv')
    print(f'   Tempo total GeoSampa: {time.perf_counter() - started:.2f}s')
    return result


def load_sgz_convias(filename='sgz_convias.csv') -> pd.DataFrame:
    path = os.path.join(RAW_DIR, filename)
    df = pd.read_csv(path, sep='|', encoding='utf-8', dtype=str)
    df = corrigir_textos_df(df)

    df = df.rename(columns={
        'Nº Processo': 'numero_processo', 'Nº de OS': 'numero_os',
        'Tipo de Serviço': 'tipo_servico', 'CEP': 'cep',
        'Rua': 'rua_raw', 'Número': 'numero',
        'X': 'coord_x_raw', 'Y': 'coord_y_raw',
        'Observação': 'observacao', 'VISTORIADOR': 'vistoriador',
        'Prefeitura Regional': 'prefeitura_regional',
        'Data Recebimento': 'data_recebimento',
        'Executora': 'executora', 'Permissionaria': 'permissionaria',
        'Priorização': 'priorizacao', 'Status': 'status',
    })

    df = df.rename(columns={
        'Número do Processo': 'numero_processo',
        'Nº de OS': 'numero_os',
        'Tipo de Serviço': 'tipo_servico',
        'Número': 'numero',
        'Latitude': 'latitude_raw',
        'Longitude': 'longitude_raw',
        'Data Hora Recebimento': 'data_recebimento',
        'Data Hora Atualização': 'data_atualizacao',
        'Priorização': 'priorizacao',
        'status': 'status',
    })

    def parse_coord(val, inteiros=2):
        try:
            s = str(val).strip()
            if '.' in s or ',' in s:
                return float(s.replace(',', '.'))
            s_neg = s.startswith('-')
            s_clean = s.replace('-', '')
            coord = float(s_clean) / (10 ** (len(s_clean) - inteiros))
            return -coord if s_neg else coord
        except Exception:
            return None

    df['latitude']  = df.get('latitude_raw', pd.Series(dtype=str)).apply(lambda x: parse_coord(x, 2))
    df['longitude'] = df.get('longitude_raw', pd.Series(dtype=str)).apply(lambda x: parse_coord(x, 2))
    df['data_recebimento']   = pd.to_datetime(df.get('data_recebimento'), dayfirst=True, errors='coerce')
    df['status']             = df.get('status', pd.Series(dtype=str)).astype(str).str.strip()
    df['prefeitura_regional']= df.get('prefeitura_regional', pd.Series(dtype=str)).astype(str).str.strip().str.upper()
    df['rua_raw']            = df.get('rua_raw', pd.Series(dtype=str)).astype(str)
    df['rua_norm']           = df['rua_raw'].apply(normalizar_rua)
    df['cep']                = df.get('cep', pd.Series(dtype=str)).astype(str).apply(normalizar_cep)
    df['fonte'] = 'SGZ_CONVIAS'
    return df


# ─────────────────────────────────────────
# LEITURA — SGZ 156
# ─────────────────────────────────────────
def load_sgz_156(filename='sgz_156.csv') -> pd.DataFrame:
    path = os.path.join(RAW_DIR, filename)
    df = pd.read_csv(path, sep='|', encoding='utf-8', dtype=str)
    df = corrigir_textos_df(df)

    df = df.rename(columns={
        'NumeroOS': 'numero_os', 'TipoServico': 'tipo_servico',
        'NumeroOrigem': 'numero_origem', 'Justificativa': 'justificativa',
        'CEP': 'cep', 'Endereco': 'rua_raw', 'Numero': 'numero',
        'Latitude': 'latitude', 'Longitude': 'longitude',
        'PrefeituraRegional': 'prefeitura_regional',
        'DataHoraRecebimento': 'data_recebimento',
        'UnidadeNegocio': 'unidade_negocio', 'Polo': 'polo',
        'status': 'status',
    })

    df['latitude']  = df.get('latitude',  pd.Series(dtype=str)).astype(str).str.replace(',', '.').apply(pd.to_numeric, errors='coerce')
    df['longitude'] = df.get('longitude', pd.Series(dtype=str)).astype(str).str.replace(',', '.').apply(pd.to_numeric, errors='coerce')
    df['data_recebimento']    = pd.to_datetime(
        df.get('data_recebimento', pd.Series(dtype=str)).astype(str).str.strip(),
        format='%d/%m/%Y %H: %M: %S', errors='coerce'
    )
    df['status']              = df.get('status', pd.Series(dtype=str)).astype(str).str.strip()
    df['prefeitura_regional'] = df.get('prefeitura_regional', pd.Series(dtype=str)).astype(str).str.strip().str.upper()
    df['rua_raw']             = df.get('rua_raw', pd.Series(dtype=str)).astype(str)
    df['rua_norm']            = df['rua_raw'].apply(normalizar_rua)
    df['cep']                 = df.get('cep', pd.Series(dtype=str)).astype(str).apply(normalizar_cep)
    df['fonte'] = 'SGZ_156'
    return df


# ─────────────────────────────────────────
# CRUZAMENTO NOTIFICAÇÕES × RECAPE
# Estratégia em cascata:
#   1. Nome de rua normalizado (fuzzy ≥ 85)
#   2. Reforço: mesmo CEP ou distância ≤ 0.3 km
# ─────────────────────────────────────────
def cruzar(df_notif: pd.DataFrame, df_recape: pd.DataFrame,
           fuzzy_threshold: int = 85, dist_threshold_km: float = 0.3,
           coord_only_threshold_km: float = 0,
           coord_regional_threshold_km: float = 0,
           coord_regional_long_threshold_km: float = 0) -> pd.DataFrame:

    crossing_started = time.perf_counter()
    recape_norms = df_recape['rua_norm'].fillna('').tolist()
    indice_espacial = montar_indice_espacial(df_recape)
    indices_por_rua = {}
    for idx, rua in enumerate(recape_norms):
        indices_por_rua.setdefault(rua, []).append(idx)
    melhores_por_rua = {}

    resultado = []
    total_notificacoes = len(df_notif)
    for notif_pos, (_, notif) in enumerate(df_notif.iterrows(), 1):
        rua_n  = notif.get('rua_norm', '')
        cep_n  = notif.get('cep', '')
        lat_n  = notif.get('latitude')
        lon_n  = notif.get('longitude')
        regional_n = notif.get('prefeitura_regional')

        match_recape = None
        metodo       = 'SEM_COBERTURA'
        score        = 0
        dist_recape  = None

        # ── PASSO 1: fuzzy match no nome da rua ──────────────────────────
        if rua_n:
            if rua_n not in melhores_por_rua:
                melhores_por_rua[rua_n] = process.extractOne(rua_n, recape_norms, scorer=fuzz.token_sort_ratio)
            best = melhores_por_rua[rua_n]
            if best and best[1] >= fuzzy_threshold:
                idx_candidatos = indices_por_rua.get(best[0], [])

                # ── PASSO 2: desempate por CEP ou coordenada ─────────────
                for idx in idx_candidatos:
                    rec = df_recape.iloc[idx]

                    # CEP bate?
                    if cep_n and str(rec.get('cep', '')) == cep_n:
                        match_recape = rec
                        metodo = 'NOME+CEP'
                        score  = best[1]
                        break

                    # Coordenada próxima?
                    lat_r, lon_r = rec.get('latitude'), rec.get('longitude')
                    if all(pd.notna(v) for v in [lat_n, lon_n, lat_r, lon_r]):
                        dist = haversine(lat_n, lon_n, lat_r, lon_r)
                        if dist <= dist_threshold_km:
                            match_recape = rec
                            metodo = 'NOME+COORD'
                            score  = best[1]
                            dist_recape = dist
                            break

                # se passou no fuzzy mas sem desempate — aceita só pelo nome
                if match_recape is None and best[1] >= 90:
                    match_recape = df_recape.iloc[idx_candidatos[0]]
                    metodo = 'NOME'
                    score  = best[1]

        if match_recape is None and coord_only_threshold_km > 0:
            idx_proximo, dist = recape_mais_proximo(
                lat_n, lon_n, indice_espacial, limite_km=coord_only_threshold_km
            )
            if idx_proximo is not None:
                match_recape = df_recape.loc[idx_proximo]
                metodo = 'COORD_PROXIMA'
                dist_recape = dist

        if match_recape is None and coord_regional_threshold_km > 0:
            idx_proximo, dist = recape_mais_proximo(
                lat_n,
                lon_n,
                indice_espacial,
                limite_km=coord_regional_threshold_km,
                regional=regional_n,
                exigir_regional=True,
            )
            if idx_proximo is not None:
                match_recape = df_recape.loc[idx_proximo]
                metodo = 'COORD_REGIONAL'
                dist_recape = dist

        if match_recape is None and coord_regional_long_threshold_km > 0:
            idx_proximo, dist = recape_mais_proximo(
                lat_n,
                lon_n,
                indice_espacial,
                limite_km=coord_regional_long_threshold_km,
                regional=regional_n,
                exigir_regional=True,
            )
            if idx_proximo is not None:
                match_recape = df_recape.loc[idx_proximo]
                metodo = 'COORD_REGIONAL_LONGA'
                dist_recape = dist

        linha = {
            # campos da notificação
            'numero_os'          : notif.get('numero_os'),
            'fonte_notif'        : notif.get('fonte'),
            'tipo_servico'       : notif.get('tipo_servico'),
            'rua_notif'          : notif.get('rua_raw'),
            'numero'             : notif.get('numero'),
            'cep'                : cep_n,
            'prefeitura_regional': notif.get('prefeitura_regional'),
            'data_recebimento'   : notif.get('data_recebimento'),
            'status_notif'       : notif.get('status'),
            'latitude'           : lat_n,
            'longitude'          : lon_n,

            # resultado do cruzamento
            'metodo_match'       : metodo,
            'score_fuzzy'        : score,
            'dist_recape_km'      : dist_recape,

            # campos do recape encontrado
            'id_recape'          : match_recape.get('id')          if match_recape is not None else None,
            'recurso_recape'     : match_recape.get('recurso')     if match_recape is not None else None,
            'status_recape'      : match_recape.get('status')      if match_recape is not None else None,
            'rua_recape'         : match_recape.get('rua_raw')     if match_recape is not None else None,
            'subprefeitura'      : match_recape.get('subprefeitura') if match_recape is not None else None,
            'data_termino_recape': match_recape.get('data_termino') if match_recape is not None else None,
            'extensao_m'         : match_recape.get('extensao_m')  if match_recape is not None else None,
            'area_m2'            : match_recape.get('area_m2')     if match_recape is not None else None,
        }

        # ── CLASSIFICAÇÃO OPERACIONAL ─────────────────────────────────────
        linha['situacao'] = classificar_situacao(linha['status_recape'], metodo)

        resultado.append(linha)
        if notif_pos == total_notificacoes or notif_pos % 250 == 0:
            print(f'   Cruzamento: {notif_pos:,}/{total_notificacoes:,} ({notif_pos / max(total_notificacoes, 1) * 100:.1f}%)', end='\r')

    print(f'\n   Tempo cruzamento: {time.perf_counter() - crossing_started:.2f}s')
    return pd.DataFrame(resultado)


# ─────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────
def _backup_apply_outputs() -> list[str]:
    """Preserva saídas oficiais antes da primeira execução com overrides."""
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    paths = [
        os.path.join(PROCESSED_DIR, 'recape_clean.csv'),
        os.path.join(PROCESSED_DIR, 'notificacoes.csv'),
        os.path.join(PROCESSED_DIR, 'cruzamento.csv'),
        os.path.join(PROCESSED_DIR, 'recapes_sem_cobertura.csv'),
        os.path.join(PROCESSED_DIR, 'geosampa_coverage_report.json'),
        PIPELINE_RUN_PATH,
    ]
    backups = []
    for path in paths:
        if os.path.exists(path):
            backup = f'{path}.human_review_backup_{timestamp}'
            shutil.copy2(path, backup)
            backups.append(backup)
    return backups


def run(human_review_mode: str = 'off', human_review_path=None):
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    pipeline_started = time.perf_counter()
    stage_seconds = {}
    run_timestamp = datetime.now(timezone.utc).isoformat()

    output_suffix = '_human_review_shadow' if human_review_mode == 'shadow' else ''
    if human_review_mode == 'apply':
        backups = _backup_apply_outputs()
        print(f'   Backups de segurança: {len(backups)} arquivos')
    print("📂 Carregando recapeamentos...")
    stage_started = time.perf_counter()
    recape = load_recape()
    stage_seconds['leitura_recapes'] = round(time.perf_counter() - stage_started, 3)
    print("🧭 Calculando trechos dos recapes via GeoSampa...")
    stage_started = time.perf_counter()
    recape = enriquecer_recape_com_geosampa(
        recape, human_review_mode=human_review_mode, human_review_path=human_review_path,
        output_suffix=output_suffix,
    )
    stage_seconds['roteamento_geosampa'] = round(time.perf_counter() - stage_started, 3)
    stage_started = time.perf_counter()
    recape.to_csv(os.path.join(PROCESSED_DIR, f'recape_clean{output_suffix}.csv'), index=False)
    stage_seconds['persistencia_recapes'] = round(time.perf_counter() - stage_started, 3)
    print(f"   ✅ {len(recape)} registros | status: {recape['status'].value_counts().to_dict()}")

    print("📂 Carregando SGZ Convias...")
    stage_started = time.perf_counter()
    convias = load_sgz_convias()
    stage_seconds['leitura_convias'] = round(time.perf_counter() - stage_started, 3)
    print(f"   ✅ {len(convias)} notificações")

    print("📂 Carregando SGZ 156...")
    stage_started = time.perf_counter()
    sgz_156 = load_sgz_156()
    stage_seconds['leitura_sgz_156'] = round(time.perf_counter() - stage_started, 3)
    print(f"   ✅ {len(sgz_156)} OSs")

    print("🔗 Unificando notificações...")
    stage_started = time.perf_counter()
    colunas = ['numero_os','tipo_servico','cep','rua_raw','rua_norm','numero',
               'latitude','longitude','prefeitura_regional','data_recebimento','status','fonte']
    frames_notificacoes = []
    for origem in (convias, sgz_156):
        frame = origem.reindex(columns=colunas).dropna(axis=1, how='all')
        frames_notificacoes.append(frame)
    notificacoes = pd.concat(frames_notificacoes, ignore_index=True)
    notificacoes.to_csv(os.path.join(PROCESSED_DIR, f'notificacoes{output_suffix}.csv'), index=False)
    stage_seconds['normalizacao_e_unificacao'] = round(time.perf_counter() - stage_started, 3)
    print(f"   ✅ {len(notificacoes)} notificações unificadas")

    print("🔍 Cruzando notificações × recapeamentos...")
    stage_started = time.perf_counter()
    cruzamento = cruzar(notificacoes, recape)
    cruzamento.to_csv(os.path.join(PROCESSED_DIR, f'cruzamento{output_suffix}.csv'), index=False)
    stage_seconds['matching_e_persistencia'] = round(time.perf_counter() - stage_started, 3)

    cobertura = calcular_cobertura(cruzamento)
    cache_path = os.path.join(CACHE_DIR, 'geosampa_road_graph.pkl')
    cache_version = getattr(RoadGraph, 'CACHE_VERSION', None) if RoadGraph is not None else None
    run_payload = {
        'timestamp': run_timestamp,
        'status': 'SUCCESS',
        'duration_seconds': round(time.perf_counter() - pipeline_started, 3),
        'stage_seconds': stage_seconds,
        'files_processed': ['recape', 'sgz_convias', 'sgz_156'],
        'counts': {
            'notificacoes': int(len(notificacoes)),
            'recapes': int(len(recape)),
            'geometrias_geradas': int(recape['path'].notna().sum()) if 'path' in recape.columns else 0,
            'falhas_geometria': int(recape['categoria_falha'].notna().sum()) if 'categoria_falha' in recape.columns else 0,
            'fuzzy_rotas': int(recape['resolucao_via'].eq('FUZZY').sum()) if 'resolucao_via' in recape.columns else 0,
            'matches_com_cobertura': cobertura['com_cobertura'],
        },
        'coverage_pct': cobertura['cobertura_pct'],
        'cache': {'road_graph_available': os.path.exists(cache_path), 'version': cache_version},
        'workers': min(max(os.cpu_count() or 2, 2), max(len(recape), 1), 8),
        'errors': [],
    }
    pipeline_run_path = PIPELINE_RUN_PATH if human_review_mode != 'shadow' else os.path.join(PROCESSED_DIR, 'pipeline_run_human_review_shadow.json')
    salvar_pipeline_run(run_payload, pipeline_run_path)
    print(f"   ✅ {cobertura['com_cobertura']}/{cobertura['total']} notificações com cobertura de recape ({cobertura['cobertura_pct']:.1f}%)")
    print(f"\n✅ Pipeline concluído. Dados em data/processed/")
    return recape, notificacoes, cruzamento


def run_street_resolution_audit(argv=None):
    """Executa somente a camada diagnóstica de logradouros.

    O modo normal continua chamando ``run()``. Esta função não passa pelo
    enriquecimento/roteamento e não grava ``recape_clean.csv``,
    ``recapes_processados.csv`` ou qualquer cache do RoadGraph.
    """
    try:
        from street_resolver import (
            DEFAULT_ALIAS_PATH,
            DEFAULT_CACHE_PATH,
            DEFAULT_GEOSAMPA_PATH,
            DEFAULT_GRAPH_CACHE,
            run_audit,
            load_existing_road_graph,
        )
    except ImportError:
        from .street_resolver import (
            DEFAULT_ALIAS_PATH,
            DEFAULT_CACHE_PATH,
            DEFAULT_GEOSAMPA_PATH,
            DEFAULT_GRAPH_CACHE,
            run_audit,
            load_existing_road_graph,
        )

    started = time.perf_counter()
    print('Auditoria diagnóstica de resolução de logradouros...')
    parser = argparse.ArgumentParser(description='Auditoria diagnóstica de logradouros')
    parser.add_argument('--audit-streets', action='store_true')
    parser.add_argument('--sample', type=int, default=None)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--reset-cache', action='store_true')
    parser.add_argument('--audit-streets-reset', action='store_true')
    parser.add_argument('--skip-route-context', action='store_true')
    parser.add_argument('--street-only', action='store_true')
    parser.add_argument('--checkpoint-every', type=int, default=None)
    parser.add_argument('--output-dir', default=str(PROCESSED_DIR))
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    recape = load_recape()
    limit = args.sample if args.sample is not None else args.limit
    if limit is not None:
        recape = recape.head(max(limit, 0)).copy()
    print(f'   Recapes carregados: {len(recape):,}')
    graph_started = time.perf_counter()
    graph = load_existing_road_graph(
        graph_cache_path=DEFAULT_GRAPH_CACHE,
        source_path=DEFAULT_GEOSAMPA_PATH,
        normalizer=normalizar_rua,
    )
    print(f'   Grafo somente-leitura carregado: {time.perf_counter() - graph_started:.2f}s')
    report = run_audit(
        recape,
        graph,
        output_dir=args.output_dir,
        aliases_path=DEFAULT_ALIAS_PATH,
        cache_path=DEFAULT_CACHE_PATH,
        source_path=DEFAULT_GEOSAMPA_PATH,
        street_only=args.street_only,
        skip_route_context=args.skip_route_context,
        resume=True,
        reset_checkpoint=args.audit_streets_reset,
        reset_cache=args.reset_cache or args.audit_streets_reset,
        checkpoint_every=args.checkpoint_every,
        load_graph_seconds=time.perf_counter() - graph_started,
    )
    print(
        f'   Resultado: HIGH={report["recommended_high"]:,}, '
        f'MEDIUM={report["recommended_medium"]:,}, '
        f'LOW={report["recommended_low"]:,}, '
        f'UNRESOLVED={report["unresolved"]:,}, '
        f'divergências={report["divergences"]:,}'
    )
    print(f'   Tempo total da auditoria: {time.perf_counter() - started:.2f}s')
    return report


if __name__ == '__main__':
    if '--audit-streets' in sys.argv or os.environ.get('STREET_RESOLUTION_AUDIT') == '1':
        run_street_resolution_audit()
    elif '--audit-route-geometries' in sys.argv or '--route-geometry-shadow' in sys.argv:
        # A auditoria de geometrias e deliberadamente isolada do pipeline oficial.
        # Remover apenas o sinalizador de despacho para que o modulo possa tratar
        # os seus proprios argumentos (sample, resume, only-failure etc.).
        try:
            from route_geometry_audit import main as run_route_geometry_audit
        except ImportError:
            from .route_geometry_audit import main as run_route_geometry_audit
        audit_args = [argument for argument in sys.argv[1:] if argument not in {'--audit-route-geometries', '--route-geometry-shadow'}]
        run_route_geometry_audit(audit_args)
    elif '--human-review-shadow' in sys.argv:
        run(human_review_mode='shadow')
    elif '--apply-human-reviews' in sys.argv:
        run(human_review_mode='apply')
    else:
        run()
