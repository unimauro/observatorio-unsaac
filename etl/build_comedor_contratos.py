#!/usr/bin/env python3
"""
Comedor universitario UNSAAC: procesos de selección y contratos 2018-2026.

Reconstruye, desde datos abiertos del OECE (ex OSCE), todo lo que la Universidad
Nacional de San Antonio Abad del Cusco (RUC 20172474501, código de entidad
SEACE/CONOSCE 001909) convocó, adjudicó, declaró desierto y contrató para su
comedor universitario (víveres, carnes, verduras, frutas, pan, panetones, servicio
de alimentación en filiales, combustible de calderas, menaje, infraestructura).

Fuentes (solo lectura):
  1. CONOSCE (OECE) - CSV.gz normalizados por el scraper de licitaperu-alertas
     (--conosce-dir): convocatorias, adjudicaciones, desiertos, contratos,
     postores, pac (2018-2026), sancionados, inhabilitados.
  2. Contrataciones Abiertas OCDS (OECE) - bulk mensual CSV
     https://contratacionesabiertas.oece.gob.pe/api/v1/file/seace_v3/csv/AAAA/MM/
     Con --fetch-ocds AAAA_MM AAAA_MM se descargan los ZIP y se guardan SOLO las
     filas de la UNSAAC (buyer PE-CONSUCODE-1909) en --ocds-dir. Sirve para
     cubrir convocatorias posteriores al corte de CONOSCE y para fechas/plazos de
     contratos que CONOSCE no trae.

Clasificación: reglas por palabras clave sobre descripción del proceso + ítem
(ver KEYWORDS / reglas en classify()). Revisado a mano: los falsos positivos
(jardín zoológico, granja, "alimentación eléctrica", análisis de alimentos NIR,
consultorías de PIP sin comedor) quedan excluidos y listados aparte.

Uso:
  python3 etl/build_comedor_contratos.py \
      --conosce-dir ../licitaperu-alertas/seace-scraper/data/conosce/out \
      --ocds-dir ~/.cache/observatorio-unsaac/ocds-unsaac \
      [--fetch-ocds 2021_07 2026_11]

Solo stdlib.
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
import zipfile

csv.field_size_limit(10**9)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_CONOSCE = os.path.normpath(os.path.join(
    REPO, '..', 'licitaperu-alertas', 'seace-scraper', 'data', 'conosce', 'out'))
DEFAULT_OCDS = os.path.expanduser('~/.cache/observatorio-unsaac/ocds-unsaac')
DEFAULT_OUT = os.path.join(REPO, 'data', 'comedor-contratos-unsaac.json')

ENTIDAD_RUC = '20172474501'
ENTIDAD_COD = '1909'           # sin ceros a la izquierda (CONOSCE usa 001909)
OCDS_BUYER = 'PE-CONSUCODE-1909'
OCDS_URL = 'https://contratacionesabiertas.oece.gob.pe/api/v1/file/seace_v3/csv/{y}/{m:02d}/'
YEARS = range(2018, 2027)

# ---------------------------------------------------------------- clasificación

def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.replace('¿', ' ').upper()
    return re.sub(r'\s+', ' ', s)

# Palabras clave que disparan la revisión (cualquier coincidencia)
KEYWORDS = [
    'ALIMENT', 'VIVER', 'ABARROT', 'CARNE', 'POLLO', 'PESCAD', 'LECHE', 'LACTE',
    'QUESO', 'YOGUR', 'VERDUR', 'HORTALIZ', 'TUBERCUL', 'FRUTA', 'PAN', 'PANETON',
    'CEREAL', 'MENESTR', 'QUINUA', 'PASTAS', 'FIDEO', 'ATUN', 'ACEITE', 'ARROZ',
    'AZUCAR', 'RACION', 'REFRIGERIO', 'CENA', 'DESAYUNO', 'MENU', 'CONCESION',
    'CATERING', 'GAS', 'GLP', 'COMBUSTIBLE', 'PETROLEO', 'MENAJE', 'VAJILLA',
    'TAZA', 'TAZON', 'GAMELA', 'MARMITA', 'COCINA', 'COMEDOR', 'PROTEIC', 'PAVO',
]
KW_RE = re.compile(r'\b(' + '|'.join([
    r'ALIMENT\w*', r'VIVERES?', r'ABARROT\w*', r'CARNES?', r'POLLO', r'PESCADO',
    r'LECHE', r'LACTE\w*', r'QUESO', r'YOGURT?', r'VERDURAS?', r'HORTALIZAS?',
    r'TUBERCULOS?', r'FRUTAS?', r'PAN', r'PANETON\w*', r'CEREALES', r'MENESTRAS?',
    r'QUINUA', r'PASTAS', r'FIDEOS?', r'ATUN', r'ACEITE', r'ARROZ', r'AZUCAR',
    r'RACION\w*', r'REFRIGERIOS?', r'CENA', r'DESAYUNO', r'MENU', r'CONCESION',
    r'CATERING', r'GAS', r'GLP', r'COMBUSTIBLES?', r'PETROLEO', r'MENAJE',
    r'VAJILLA', r'TAZAS?', r'TAZON\w*', r'GAMELAS?', r'MARMITAS?', r'COCINA',
    r'COMEDOR\w*', r'PROTEIC\w*', r'PAVO']) + r')\b')

CATEGORIAS = {
    'viveres_comedor': 'Víveres/alimentos para el comedor universitario (sede Cusco)',
    'servicio_alimentacion_filiales': 'Servicio de alimentación (raciones preparadas) para estudiantes de filiales',
    'navidad_estudiantes': 'Panetón / desayuno o cena navideña de estudiantes (comedor)',
    'combustible_comedor': 'Combustible para calderas/cocina del comedor (compartido con vehículos y planta piloto)',
    'equipamiento_menaje_comedor': 'Menaje y equipamiento del comedor',
    'infraestructura_comedor': 'Obras, expedientes, supervisión y mantenimiento de comedores',
}
RELACIONADAS = {
    'alimentos_personal': 'Alimentos/vales/tarjetas de consumo para personal docente y administrativo (NO es el comedor estudiantil)',
    'refrigerios_cepru_admision': 'Refrigerios para exámenes del CEPRU/admisión (no es el comedor)',
}

def classify(desc, item, objeto):
    """Devuelve (categoria, confianza, motivo) o (None, None, motivo_exclusion)."""
    t = norm(desc + ' || ' + item)
    d = norm(desc)
    if not KW_RE.search(t):
        return None, None, None
    # --- exclusiones (falsos positivos revisados a mano)
    if re.search(r'ZOOLOG|ANIMALES|GANADO|VACUNO|PORCINO|CERDOS|CUYES|GRANJA|BALANCEADO|'
                 r'POLLO DE CARNE Y GALLINA|MAIZ AMARILLO|ESTABLO|NUTRICION ANIMAL', t):
        return None, None, 'excluido: alimentación animal (zoológico/granja/zootecnia)'
    if re.search(r'ALIMENTACION (ELECTRICA|ININTERRUMPIDA)|PROTECCION ELECTRICA|ALIMENTADOR', t):
        return None, None, 'excluido: "alimentación" eléctrica'
    if re.search(r'ANALISIS DE ALIMENTOS|EQUIPOS? PARA ANALISIS', t):
        return None, None, 'excluido: equipo de laboratorio'
    if re.search(r'ODONTOL|CHOCOLATE(?!.*COMEDOR)|CACAO', d) and 'COMEDOR' not in d:
        return None, None, 'excluido: otro rubro'
    # --- relacionadas (se listan, no suman al comedor)
    if re.search(r'DOCENTE Y ADMINISTRATIVO|CUPONES|VALES DE CONSUMO|TARJETAS (FISICAS|DE CONSUMO)', d):
        return 'rel:alimentos_personal', 'alta', 'destinatario: personal docente/administrativo'
    if re.search(r'CEPRU|PRE ?UNIVERSITARIO|ADMISION', d):
        return 'rel:refrigerios_cepru_admision', 'alta', 'destinatario: exámenes CEPRU/admisión'
    if re.search(r'SERVICIO DE ATENCION DE ALIMENTACION', d) and re.search(r'FILIAL|ALUMNOS|ESTUDIANTES', d):
        return 'servicio_alimentacion_filiales', 'alta', 'servicio de alimentación (raciones/concesión) para estudiantes de filiales'
    # --- comedor explícito
    if re.search(r'COMEDOR|COMEDOR UNIVERISTARIO', d):
        if objeto in ('Obra', 'Consultoría de Obra') or re.search(
                r'EXPEDIENTE|SUPERVISION|CONSTRUCCION|ASCENSOR|PISO|MANTENIMIENTO|OBRA', d):
            return 'infraestructura_comedor', 'alta', 'menciona COMEDOR + obra/mantenimiento'
        if re.search(r'COMBUSTIBLE|PETROLEO|DIESEL|\bGAS\b|GLP', d):
            return 'combustible_comedor', 'alta', 'menciona COMEDOR + combustible'
        if re.search(r'TAZA|TAZON|GAMELA|TOMATODO|MARMITA|EQUIPOS|MENAJE|VAJILLA', d):
            return 'equipamiento_menaje_comedor', 'alta', 'menciona COMEDOR + menaje/equipos'
        if re.search(r'PANETON|NAVIDE', d):
            return 'navidad_estudiantes', 'alta', 'menciona COMEDOR + panetón/navidad'
        return 'viveres_comedor', 'alta', 'menciona COMEDOR + alimentos'
    if re.search(r'BRINDAR SERVICIOS DE ALIMENTACION EN UNIVERSIDADES', d):
        return 'viveres_comedor', 'alta', 'meta presupuestal 0045 (alimentación en universidades)'
    if re.search(r'ALIMENTACION PARA (LOS )?ALUMNOS', d):
        return 'servicio_alimentacion_filiales', 'alta', 'servicio de alimentación para alumnos de filiales'
    if re.search(r'NAVIDE\w* (DE|PARA) (LOS )?ESTUDIANTES|CENA NAVIDENA DE LOS ESTUDIANTES', d):
        return 'navidad_estudiantes', 'alta', 'evento navideño de estudiantes'
    if re.search(r'MODULOS PARA ATENCION DE ALIMENTOS', d):
        return 'infraestructura_comedor', 'media', 'módulos de atención de alimentos (no dice "comedor")'
    if re.search(r'^(CONTRATACION|ADQUISICION) DE ALIMENTOS PARA LA (UNSAAC|UNIVERSIDAD)', d):
        return 'viveres_comedor', 'media', 'alimentos genéricos para la UNSAAC (ítems típicos del comedor; no dice "comedor")'
    if re.search(r'^ADQUISICION DE QUESO ANDINO$', d):
        return 'viveres_comedor', 'baja', 'queso sin destino declarado'
    return None, None, 'excluido: coincide palabra clave pero no es comedor (revisado)'

# ---------------------------------------------------------------- utilidades

def fnum(x):
    try:
        return round(float(x), 2) if x not in (None, '') else None
    except ValueError:
        return None

def is_unsaac(row):
    cod = (row.get('codigo_entidad') or '').lstrip('0')
    return cod == ENTIDAD_COD or row.get('entidad_ruc') == ENTIDAD_RUC or row.get('ruc_entidad') == ENTIDAD_RUC

def read_gz(path):
    with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
        yield from csv.DictReader(f)

# --- CONOSCE fresco (XLSX directo del portal), lector stdlib
CONOSCE_XLSX_URL = 'https://conosce.osce.gob.pe/buscador/assets/67ae6c4a/reportes/{kind}/{y}/{KIND}{y}_0.xlsx'
XLSX_MAP = {
    'CODIGOENTIDAD': 'codigo_entidad', 'ENTIDAD_RUC': 'entidad_ruc', 'ENTIDAD': 'entidad',
    'CODIGOCONVOCATORIA': 'codigo_convocatoria', 'OBJETOCONTRACTUAL': 'objeto',
    'TIPOPROCESOSELECCION': 'tipo_proceso', 'PROCESO': 'proceso', 'DESCRIPCION_PROCESO': 'descripcion',
    'N_ITEM': 'n_item', 'NUM_ITEM': 'n_item', 'DESCRIPCION_ITEM': 'descripcion_item',
    'ESTADO_ITEM': 'estado_item', 'ESTADOITEM': 'estado_item', 'CANTIDAD_ADJUDICADO_ITEM': 'cantidad',
    'MONTO_REFERENCIAL_ITEM_SOLES': 'monto_referencial_item', 'MONTO_REFERENCIAL_ITEM': 'monto_referencial_item',
    'MONTO_ADJUDICADO_ITEM_SOLES': 'monto_adjudicado_item', 'UNIDAD_MEDIDA': 'unidad',
    'RUC PROVEEDOR': 'ruc_proveedor', 'PROVEEDOR': 'proveedor', 'TIPO_PROVEEDOR': 'tipo_proveedor',
    'FECHA_CONVOCATORIA': 'fecha_convocatoria', 'FECHA_BUENAPRO': 'fecha_buenapro',
    'FECHA_CONSENTIMIENTO_BP': 'fecha_consentimiento', 'MONTOREFERENCIAL': 'monto_referencial',
    'SISTEMA_CONTRATACION': 'sistema', 'FECHAINTEGRACIONBASES': 'fecha_integracion',
    'FECHAPRESENTACIONPROPUESTA': 'fecha_presentacion',
    'NUM_CONTRATO': 'num_contrato', 'CODIGO_CONTRATO': 'codigo_contrato', 'MONTO_CONTRATADO_TOTAL': 'monto_total',
    'MONTO_CONTRATADO_ITEM': 'monto_item', 'MONTO_ADICIONAL': 'monto_adicional', 'MONTO_REDUCCION': 'monto_reduccion',
    'MONTO_COMPLEMENTARIO': 'monto_complementario', 'RUC_CONTRATISTA': 'ruc_contratista', 'URLCONTRATO': 'url_contrato',
    'FECHA_SUSCRIPCION_CONTRATO': 'fecha_suscripcion', 'FECHA_VIGENCIA_INICIAL': 'vigencia_inicio',
    'FECHA_VIGENCIA_FINAL': 'vigencia_fin', 'FECHA_VIGENCIA_FIN_ACTUALIZADA': 'vigencia_fin_actualizada',
}

def _iso(v):
    m = re.match(r'^(\d{2})/(\d{2})/(\d{4})', v or '')
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else v

def read_xlsx(path):
    import xml.etree.ElementTree as ET
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    z = zipfile.ZipFile(path)
    ss = []
    for _, el in ET.iterparse(z.open('xl/sharedStrings.xml')):
        if el.tag == ns + 'si':
            ss.append(''.join(t.text or '' for t in el.iter(ns + 't')))
            el.clear()

    def col(ref):
        n = 0
        for ch in re.match(r'[A-Z]+', ref).group(0):
            n = n * 26 + ord(ch) - 64
        return n - 1
    hdr = None
    for _, el in ET.iterparse(z.open('xl/worksheets/sheet1.xml')):
        if el.tag != ns + 'row':
            continue
        cells = {}
        for c in el.findall(ns + 'c'):
            v, t = c.find(ns + 'v'), c.get('t')
            if t == 'inlineStr':
                val = ''.join(x.text or '' for x in c.iter(ns + 't'))
            elif v is None:
                val = ''
            elif t == 's':
                val = ss[int(v.text)]
            else:
                val = v.text
            cells[col(c.get('r'))] = val
        el.clear()
        if not cells:
            continue
        row = [cells.get(i, '') for i in range(max(cells) + 1)]
        if hdr is None:
            hdr = [XLSX_MAP.get(h, h.lower()) for h in row]
            continue
        d = {h: (row[i] if i < len(row) else '') for i, h in enumerate(hdr)}
        if (d.get('codigo_entidad') or '').lstrip('0') != ENTIDAD_COD and d.get('entidad_ruc') != ENTIDAD_RUC:
            continue
        for k in list(d):
            if k.startswith('fecha') or k.startswith('vigencia'):
                d[k] = _iso(d[k])
        yield d

FRESH = {}  # (kind, year) -> path xlsx

def lbl(kind, y):
    return f'CONOSCE {kind}_{y}' + (' (XLSX portal, fresco)' if (kind, y) in FRESH else ' (CSV.gz caché)')

def src(C, kind, y):
    if (kind, y) in FRESH:
        return read_xlsx(FRESH[(kind, y)])
    return read_gz(f'{C}/{kind}_{y}.csv.gz')

def fetch_conosce(year, outdir):
    os.makedirs(outdir, exist_ok=True)
    for kind in ('convocatorias', 'adjudicaciones', 'desiertos', 'contratos'):
        url = CONOSCE_XLSX_URL.format(kind=kind, KIND=kind.upper(), y=year)
        dst = os.path.join(outdir, f'{kind}_{year}.xlsx')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (datos-abiertos)'})
            data = urllib.request.urlopen(req, timeout=600).read()
            with open(dst, 'wb') as fh:
                fh.write(data)
            print('  CONOSCE', kind, year, len(data), 'bytes', file=sys.stderr)
        except Exception as e:
            print('  CONOSCE', kind, year, 'error', e, file=sys.stderr)

def family(nomen):
    """AS-SM-70-2018-UNSAAC-2 -> AS-SM-70-2018-UNSAAC (sin nº de convocatoria)."""
    return re.sub(r'-\d+$', '', nomen or '')

def conv_number(nomen):
    m = re.search(r'-(\d+)$', nomen or '')
    return int(m.group(1)) if m else None

def d10(x):
    return (x or '')[:10] or None

# ---------------------------------------------------------------- OCDS

def fetch_ocds(start, end, outdir):
    os.makedirs(outdir, exist_ok=True)
    months = [(y, m) for y in YEARS for m in range(1, 13) if start <= f'{y}_{m:02d}' <= end]
    for y, m in months:
        tag = f'{y}_{m:02d}'
        done = os.path.join(outdir, f'{tag}.done')
        if os.path.exists(done) and not tag.startswith(str(dt.date.today().year)):
            continue
        url = OCDS_URL.format(y=y, m=m)
        data = None
        for _ in range(3):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (datos-abiertos)'})
                data = urllib.request.urlopen(req, timeout=300).read()
                break
            except Exception as e:  # 403/timeout: se sigue sin ese mes
                print('  OCDS', tag, 'error', e, file=sys.stderr)
                time.sleep(5)
        if not data:
            continue
        z = zipfile.ZipFile(io.BytesIO(data))
        ocids = set()
        with z.open('records.csv') as f:
            for r in csv.reader(io.TextIOWrapper(f, encoding='utf-8', errors='replace')):
                if len(r) > 7 and r[7] == OCDS_BUYER:
                    ocids.add(r[0])
        for name in z.namelist():
            with z.open(name) as f:
                rd = csv.reader(io.TextIOWrapper(f, encoding='utf-8', errors='replace'))
                hdr = next(rd, None)
                if not hdr:
                    continue
                with open(os.path.join(outdir, f'{tag}__{name}'), 'w', newline='') as o:
                    w = csv.writer(o)
                    w.writerow(hdr)
                    for r in rd:
                        if r and r[0] in ocids:
                            w.writerow(r)
        with open(done, 'w') as fh:
            fh.write(str(len(ocids)))
        print('  OCDS', tag, len(ocids), 'procesos UNSAAC', file=sys.stderr)

def load_ocds(ocds_dir):
    """Devuelve dict codigo_convocatoria -> info OCDS (última versión compilada)."""
    out = {}
    if not ocds_dir or not os.path.isdir(ocds_dir):
        return out, []
    months = sorted({os.path.basename(p).split('__')[0] for p in glob.glob(os.path.join(ocds_dir, '*__records.csv'))})

    def rows(tag, name):
        p = os.path.join(ocds_dir, f'{tag}__{name}')
        if os.path.exists(p):
            with open(p, newline='', encoding='utf-8', errors='replace') as f:
                yield from csv.DictReader(f)

    for tag in months:
        for r in rows(tag, 'records.csv'):
            cid = r['compiledRelease/tender/id']
            prev = out.get(cid)
            date = r.get('compiledRelease/date', '')
            if prev and prev['_date'] >= date:
                continue
            out[cid] = {
                '_date': date, '_tag': tag, 'ocid': r['ocid'],
                'nomenclatura': r.get('compiledRelease/tender/title'),
                'descripcion': r.get('compiledRelease/tender/description'),
                'fecha_publicacion': d10(r.get('compiledRelease/tender/datePublished')),
                'metodo': r.get('compiledRelease/tender/procurementMethodDetails'),
                'categoria': r.get('compiledRelease/tender/mainProcurementCategory'),
                'valor': fnum(r.get('compiledRelease/tender/value/amount')),
                'plazo_ofertas': [d10(r.get('compiledRelease/tender/tenderPeriod/startDate')),
                                  d10(r.get('compiledRelease/tender/tenderPeriod/endDate'))],
                'n_postores': r.get('compiledRelease/tender/numberOfTenderers'),
                'awards': {}, 'contracts': {}, 'items': {}, 'tenderers': {}, 'documentos': [],
            }
        by_ocid = {v['ocid']: v for v in out.values() if v['_tag'] == tag}

        def keyed(name, prefix):
            for r in rows(tag, name):
                v = by_ocid.get(r['ocid'])
                if v is None:
                    continue
                yield v, {k.replace(prefix, ''): val for k, val in r.items()}

        for v, r in keyed('com_ten_items.csv', 'compiledRelease/tender/items/0/'):
            v['items'][r.get('position') or r.get('id')] = {
                'descripcion': r.get('description'), 'estado': r.get('statusDetails') or r.get('status'),
                'monto': fnum(r.get('totalValue/amount'))}
        for v, r in keyed('com_awards.csv', 'compiledRelease/awards/0/'):
            v['awards'][r['id']] = {'monto': fnum(r.get('value/amount')), 'fecha': d10(r.get('date')),
                                    'estado': r.get('status'), 'proveedores': []}
        for v, r in keyed('com_awa_suppliers.csv', 'compiledRelease/awards/0/'):
            a = v['awards'].setdefault(r['id'], {'proveedores': []})
            a['proveedores'].append({'id': r.get('suppliers/0/id'), 'nombre': r.get('suppliers/0/name')})
        for v, r in keyed('com_contracts.csv', 'compiledRelease/contracts/0/'):
            v['contracts'][r['id']] = {
                'award_id': r.get('awardID'), 'numero': r.get('title'),
                'inicio': d10(r.get('period/startDate')), 'fin': d10(r.get('period/endDate')),
                'dias': r.get('period/durationInDays'), 'monto': fnum(r.get('value/amount')),
                'firmado': d10(r.get('dateSigned')), 'estado': r.get('status')}
        for v, r in keyed('com_ten_documents.csv', 'compiledRelease/tender/documents/0/'):
            doc = {'tipo': r.get('title') or r.get('documentType'), 'fecha': d10(r.get('datePublished')), 'url': r.get('url')}
            if doc['url'] and doc not in v['documentos']:
                v['documentos'].append(doc)
        for v, r in keyed('com_ten_tenderers.csv', 'compiledRelease/tender/'):
            v['tenderers'][r.get('tenderers/0/id')] = r.get('tenderers/0/name')
    return out, months

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--conosce-dir', default=DEFAULT_CONOSCE)
    ap.add_argument('--ocds-dir', default=DEFAULT_OCDS)
    ap.add_argument('--fetch-ocds', nargs=2, metavar=('DESDE', 'HASTA'),
                    help='descargar bulk OCDS AAAA_MM..AAAA_MM al --ocds-dir antes de construir')
    ap.add_argument('--conosce-fresh-dir', default=os.path.expanduser('~/.cache/observatorio-unsaac/conosce-xlsx'),
                    help='XLSX descargados del portal CONOSCE ({tipo}_{anio}.xlsx); reemplazan al CSV.gz de ese año')
    ap.add_argument('--fetch-conosce', type=int, metavar='ANIO', help='descargar XLSX CONOSCE del año al --conosce-fresh-dir')
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--fecha-corte', default=dt.date.today().isoformat())
    a = ap.parse_args()
    C = a.conosce_dir

    if a.fetch_conosce:
        fetch_conosce(a.fetch_conosce, a.conosce_fresh_dir)
    for pth in glob.glob(os.path.join(a.conosce_fresh_dir, '*_*.xlsx')):
        m = re.match(r'(\w+?)_(\d{4})\.xlsx$', os.path.basename(pth))
        if m:
            FRESH[(m.group(1), int(m.group(2)))] = pth
            print('  usando CONOSCE fresco', pth, file=sys.stderr)
    if a.fetch_ocds:
        fetch_ocds(a.fetch_ocds[0], a.fetch_ocds[1], a.ocds_dir)

    # ---- 1. ítems de procesos (adjudicados/contratados + desiertos)
    items = {}      # (cod_conv, n_item, desc_item_norm) -> dict
    procs = {}      # cod_conv -> cabecera
    excluded = collections.Counter()
    excluded_examples = collections.defaultdict(set)

    def add_proc(r):
        cid = r['codigo_convocatoria']
        p = procs.setdefault(cid, {
            'codigo_convocatoria': cid, 'nomenclatura': r.get('proceso'),
            'descripcion': r.get('descripcion'), 'objeto': r.get('objeto'),
            'tipo_proceso': r.get('tipo_proceso'), 'fecha_convocatoria': r.get('fecha_convocatoria'),
            'monto_referencial_proceso': None, 'sistema': r.get('sistema'),
        })
        if r.get('monto_referencial') and p['monto_referencial_proceso'] is None:
            p['monto_referencial_proceso'] = fnum(r['monto_referencial'])
        if r.get('fecha_integracion'):
            p['fecha_integracion'] = r['fecha_integracion']
        if r.get('fecha_presentacion'):
            p['fecha_presentacion_ofertas'] = r['fecha_presentacion']
        return p

    for y in YEARS:
        # convocatorias: monto referencial de proceso, fechas de integración/presentación
        for r in src(C, 'convocatorias', y):
            if is_unsaac(r):
                add_proc(r)
        for r in src(C, 'adjudicaciones', y):
            if not is_unsaac(r):
                continue
            add_proc(r)
            k = (r['codigo_convocatoria'], r['n_item'], norm(r['descripcion_item'])[:120])
            items[k] = {
                'n_item': r['n_item'], 'descripcion_item': r['descripcion_item'],
                'estado': r['estado_item'], 'cantidad': fnum(r.get('cantidad')), 'unidad': r.get('unidad'),
                'monto_referencial': fnum(r['monto_referencial_item']),
                'monto_adjudicado': fnum(r['monto_adjudicado_item']),
                'ruc_proveedor': r['ruc_proveedor'] or None, 'proveedor': r['proveedor'] or None,
                'tipo_proveedor': r.get('tipo_proveedor') or None,
                'fecha_buenapro': r['fecha_buenapro'] or None,
                'fecha_consentimiento': r['fecha_consentimiento'] or None,
                '_fuente': lbl('adjudicaciones', y),
            }
        for r in src(C, 'desiertos', y):
            if not is_unsaac(r):
                continue
            add_proc(r)
            k = (r['codigo_convocatoria'], r['n_item'], norm(r['descripcion_item'])[:120])
            if k in items and items[k]['estado'] != 'Desierto':
                continue
            items[k] = {
                'n_item': r['n_item'], 'descripcion_item': r['descripcion_item'],
                'estado': 'Desierto', 'cantidad': None, 'unidad': r.get('unidad'),
                'monto_referencial': fnum(r['monto_referencial_item']), 'monto_adjudicado': None,
                'ruc_proveedor': None, 'proveedor': None, 'tipo_proveedor': None,
                'fecha_buenapro': None, 'fecha_consentimiento': None,
                '_fuente': lbl('desiertos', y),
            }

    # ---- 2. clasificar por proceso (descripción) + ítem
    sel = {}  # cid -> proc con items
    related = {}
    for (cid, n, _), it in items.items():
        p = procs[cid]
        cat, conf, why = classify(p['descripcion'], it['descripcion_item'], p['objeto'])
        if cat is None:
            if why:
                excluded[why] += 1
                excluded_examples[why].add(f"{p['nomenclatura']} | {p['descripcion'][:110]}")
            continue
        target = related if cat.startswith('rel:') else sel
        q = target.setdefault(cid, dict(p, categoria=cat.replace('rel:', ''), confianza=conf,
                                        motivo_clasificacion=why, items=[]))
        q['items'].append(it)

    # ---- 3. contratos CONOSCE, postores, PAC
    wanted = set(sel) | set(related)
    contratos = collections.defaultdict(list)
    for y in YEARS:
        for r in src(C, 'contratos', y):
            if r['codigo_convocatoria'] in wanted and (r['codigo_entidad'] or '').lstrip('0') == ENTIDAD_COD:
                c = {
                    'n_item': r['n_item'], 'numero': r['num_contrato'], 'codigo_contrato': r['codigo_contrato'],
                    'ruc_contratista': r['ruc_contratista'], 'monto_item': fnum(r['monto_item']),
                    'monto_total': fnum(r['monto_total']), 'adicional': fnum(r['monto_adicional']),
                    'reduccion': fnum(r['monto_reduccion']), 'complementario': fnum(r['monto_complementario']),
                    'fecha_suscripcion': r['fecha_suscripcion'] or None,
                    'vigencia_inicio': r['vigencia_inicio'] or None, 'vigencia_fin': r['vigencia_fin'] or None,
                    'vigencia_fin_actualizada': r['vigencia_fin_actualizada'] or None,
                    'url_contrato': r['url_contrato'] or None, 'fuente': lbl('contratos', y),
                }
                if c not in contratos[r['codigo_convocatoria']]:
                    contratos[r['codigo_convocatoria']].append(c)
    postores = collections.defaultdict(dict)
    for y in YEARS:
        for r in read_gz(f'{C}/postores_{y}.csv.gz'):
            if r['codigo_convocatoria'] in wanted:
                postores[(r['codigo_convocatoria'], r['n_item'])][r['ruc_postor']] = r['postor']

    # ---- 4. OCDS
    ocds, ocds_months = load_ocds(a.ocds_dir)
    # convocatorias OCDS de la UNSAAC no presentes en CONOSCE (posteriores al corte o en curso)
    ocds_new = []
    for cid, o in ocds.items():
        if cid in procs:
            continue
        cat, conf, why = classify(o['descripcion'] or '', '', 'Obra' if o['categoria'] == 'works' else '')
        if cat is None:
            continue
        ocds_new.append({
            'codigo_convocatoria': cid, 'nomenclatura': o['nomenclatura'], 'descripcion': o['descripcion'],
            'categoria': cat.replace('rel:', ''), 'relacionado_no_comedor': cat.startswith('rel:'),
            'confianza': conf, 'fecha_publicacion': o['fecha_publicacion'], 'metodo': o['metodo'],
            'valor_estimado': o['valor'], 'plazo_ofertas': o['plazo_ofertas'],
            'items': list(o['items'].values()),
            'estado_items': sorted({(i.get('estado') or '') for i in o['items'].values()}),
            'n_postores': o['n_postores'], 'postores': [{'id': k, 'nombre': v} for k, v in o['tenderers'].items()] or None,
            'documentos': o['documentos'] or None,
            'adjudicaciones': list(o['awards'].values()), 'contratos': list(o['contracts'].values()),
            'fuente': f"OCDS {o['_tag']} ({o['ocid']})",
        })

    # ---- 5. sancionados / inhabilitados
    sanc = collections.defaultdict(list)
    for r in read_gz(f'{C}/sancionados.csv.gz'):
        sanc[r['ruc']].append({k: r[k] for k in ('nombre', 'fecha_inicio', 'fecha_fin', 'resolucion', 'motivo')})
    inhab = collections.defaultdict(list)
    for r in read_gz(f'{C}/inhabilitados.csv.gz'):
        if r.get('ruc_dni'):
            inhab[r['ruc_dni']].append({k: r[k] for k in ('nombre', 'organo', 'resolucion', 'fecha_inicio', 'fecha_fin')})
    corte_sanc = next(read_gz(f'{C}/sancionados.csv.gz'))['fecha_corte']

    # ---- 6. armar procesos finales
    def finish(q):
        cid = q['codigo_convocatoria']
        o = ocds.get(cid)
        for it in q['items']:
            ps = postores.get((cid, it['n_item']))
            it['postores'] = [{'ruc': k, 'nombre': v} for k, v in sorted(ps.items())] if ps else None
            it['n_postores'] = len(ps) if ps else None
            it['contratos'] = [c for c in contratos.get(cid, []) if c['n_item'] == it['n_item']
                               and (not it['ruc_proveedor'] or c['ruc_contratista'] == it['ruc_proveedor'])] or None
            if o and it['ruc_proveedor']:
                oc = [dict(c, fuente=f"OCDS {o['_tag']}") for c in o['contracts'].values()
                      if (c.get('award_id') or '').endswith('-' + it['ruc_proveedor'])]
                it['contratos_ocds'] = oc or None
            ruc = it['ruc_proveedor']
            alerts = []
            if ruc and ruc in sanc:
                ref = it['fecha_buenapro'] or q['fecha_convocatoria'] or ''
                regs = [dict(s_, vigente_en_fecha_buenapro=bool(ref and s_['fecha_inicio'] <= ref and (not s_['fecha_fin'] or ref <= s_['fecha_fin'])),
                             nota_nombre=None if norm(s_['nombre'])[:12] == norm(it['proveedor'] or '')[:12] else 'nombre distinto en el registro de sancionados (mismo RUC)')
                        for s_ in sanc[ruc]]
                alerts.append({'tipo': 'sancionado_tce', 'registros': regs})
            if ruc and (ruc in inhab or (len(ruc) == 11 and ruc[:2] == '10' and ruc[2:10] in inhab)):
                alerts.append({'tipo': 'inhabilitado_judicial', 'registros': inhab.get(ruc) or inhab.get(ruc[2:10])})
            it['alertas_proveedor'] = alerts or None
            it.pop('_fuente_tmp', None)
        q['items'].sort(key=lambda i: (int(i['n_item']) if i['n_item'].isdigit() else 0))
        est = [i['estado'] for i in q['items']]
        if all(e == 'Desierto' for e in est):
            q['estado'] = 'desierto'
        elif 'Desierto' in est:
            q['estado'] = 'parcialmente desierto'
        elif all(e == 'Contratado' for e in est):
            q['estado'] = 'contratado'
        elif any(e == 'Contratado' for e in est):
            q['estado'] = 'contratado (parcial)'
        else:
            q['estado'] = 'adjudicado (buena pro, sin contrato registrado)'
        q['anio'] = int((q['fecha_convocatoria'] or '0000')[:4])
        q['familia'] = family(q['nomenclatura'])
        q['n_convocatoria'] = conv_number(q['nomenclatura'])
        q['monto_referencial_items'] = round(sum(i['monto_referencial'] or 0 for i in q['items']), 2)
        q['monto_adjudicado'] = round(sum(i['monto_adjudicado'] or 0 for i in q['items']
                                          if i['estado'] != 'Desierto'), 2)
        q['monto_desierto'] = round(sum(i['monto_referencial'] or 0 for i in q['items']
                                        if i['estado'] == 'Desierto'), 2)
        q['proveedores'] = sorted({f"{i['proveedor']} ({i['ruc_proveedor']})" for i in q['items'] if i['ruc_proveedor']})
        # fin de vigencia más tardío conocido (CONOSCE actualizada > CONOSCE > OCDS)
        fins = []
        for i in q['items']:
            for c in i.get('contratos') or []:
                f = c.get('vigencia_fin_actualizada') or c.get('vigencia_fin')
                if f:
                    fins.append((f, 'CONOSCE contratos'))
            for c in i.get('contratos_ocds') or []:
                if c.get('fin'):
                    fins.append((c['fin'], 'OCDS contracts'))
        q['vigencia_fin_max'] = max(fins)[0] if fins else None
        q['vigencia_fin_fuente'] = max(fins)[1] if fins else None
        if o:
            q['ocds'] = {'ocid': o['ocid'], 'mes_archivo': o['_tag'], 'fecha_publicacion': o['fecha_publicacion'],
                         'plazo_ofertas': o['plazo_ofertas'], 'n_postores': o['n_postores'],
                         'documentos': o['documentos'] or None}
        q['fuente'] = sorted({i.pop('_fuente') for i in q['items']}) + (
            [f"OCDS {o['_tag']}"] if o else [])
        q['url_ficha_seace'] = None  # CONOSCE no trae URL de ficha; buscar por nomenclatura en SEACE
        return q

    procesos = sorted((finish(q) for q in sel.values()),
                      key=lambda q: (q['fecha_convocatoria'] or '', q['nomenclatura']))
    relacionados = sorted((finish(q) for q in related.values()),
                          key=lambda q: (q['fecha_convocatoria'] or '', q['nomenclatura']))

    # ---- 7. resumen por año
    resumen = {}
    for q in procesos:
        r = resumen.setdefault(q['anio'], collections.Counter())
        r['procesos'] += 1
        r['items'] += len(q['items'])
        r['items_desiertos'] += sum(1 for i in q['items'] if i['estado'] == 'Desierto')
        r['items_contratados'] += sum(1 for i in q['items'] if i['estado'] == 'Contratado')
        r['items_adjudicados_sin_contrato'] += sum(1 for i in q['items'] if i['estado'] in ('Adjudicado', 'Consentido'))
        r['monto_referencial'] += q['monto_referencial_items']
        r['monto_adjudicado'] += q['monto_adjudicado']
        r['monto_desierto'] += q['monto_desierto']
        if q['confianza'] == 'baja':
            r['procesos_confianza_baja'] += 1
        g = 'alimentacion' if q['categoria'] in ('viveres_comedor', 'navidad_estudiantes', 'servicio_alimentacion_filiales') else q['categoria']
        r[f'adjudicado__{g}'] += q['monto_adjudicado']
        r[f'desierto__{g}'] += q['monto_desierto']
    resumen_anual = [dict(anio=y, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in sorted(c.items())})
                     for y, c in sorted(resumen.items())]
    por_categoria = collections.defaultdict(lambda: collections.Counter())
    for q in procesos:
        c = por_categoria[q['categoria']]
        c['procesos'] += 1
        c['monto_adjudicado'] += q['monto_adjudicado']
        c['monto_desierto'] += q['monto_desierto']
    por_categoria = {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in por_categoria.items()}

    # ---- 8. proveedores
    prov = {}
    for q in procesos:
        for i in q['items']:
            if not i['ruc_proveedor'] or i['estado'] == 'Desierto':
                continue
            p = prov.setdefault(i['ruc_proveedor'], {'ruc': i['ruc_proveedor'], 'nombre': i['proveedor'],
                                                    'tipo': i['tipo_proveedor'], 'items': 0, 'monto': 0.0,
                                                    'anios': set(), 'procesos': set(), 'rubros': set(),
                                                    'alertas': i['alertas_proveedor']})
            p['items'] += 1
            p['monto'] += i['monto_adjudicado'] or 0
            p['anios'].add(q['anio'])
            p['procesos'].add(q['nomenclatura'])
            p['rubros'].add(norm(i['descripcion_item'])[:40])
    tot_adj = sum(p['monto'] for p in prov.values()) or 1
    proveedores = sorted(({**p, 'monto': round(p['monto'], 2), 'participacion_pct': round(100 * p['monto'] / tot_adj, 1),
                           'anios': sorted(p['anios']), 'procesos': sorted(p['procesos']),
                           'rubros': sorted(p['rubros'])[:12]} for p in prov.values()),
                         key=lambda p: -p['monto'])

    # ---- 9. alertas
    alertas = []
    fam = collections.defaultdict(list)
    for q in procesos:
        fam[q['familia']].append(q)
    for f, qs in sorted(fam.items()):
        des = [q for q in qs if q['estado'] in ('desierto', 'parcialmente desierto')]
        if len(qs) >= 2 and des:
            alertas.append({
                'tipo': 'reconvocatorias_por_desierto', 'familia': f,
                'detalle': f"{len(qs)} convocatorias del mismo procedimiento; {len(des)} con ítems desiertos",
                'convocatorias': [{'nomenclatura': q['nomenclatura'], 'fecha': q['fecha_convocatoria'],
                                   'estado': q['estado'], 'monto_desierto': q['monto_desierto']} for q in qs]})
    def toks(x):
        return {w for w in re.findall(r'[A-Z]{4,}', norm(x)) if w not in ('PARA', 'UNIVERSIDAD', 'NACIONAL', 'SAN', 'ANTONIO', 'ABAD', 'CUSCO', 'UNSAAC', 'CONTRATACION', 'ADQUISICION', 'SUMINISTRO')}
    for q in procesos:
        if q['estado'] == 'desierto' and len(fam[q['familia']]) == 1:
            tq = toks(q['descripcion'])
            cands = []
            for r in procesos:
                if r is q or r['categoria'] != q['categoria'] or (r['fecha_convocatoria'] or '') <= (q['fecha_convocatoria'] or ''):
                    continue
                tr = toks(r['descripcion'])
                jac = len(tq & tr) / (len(tq | tr) or 1)
                gap = (dt.date.fromisoformat(r['fecha_convocatoria']) - dt.date.fromisoformat(q['fecha_convocatoria'])).days
                if jac >= 0.5 and gap <= 365:
                    cands.append({'nomenclatura': r['nomenclatura'], 'fecha': r['fecha_convocatoria'], 'estado': r['estado'],
                                  'similitud': round(jac, 2), 'dias_despues': gap})
            alertas.append({'tipo': 'desierto_sin_reconvocatoria_misma_nomenclatura', 'familia': q['familia'],
                            'detalle': f"{q['nomenclatura']} desierto (S/ {q['monto_desierto']:,.2f}); sin otra convocatoria con la misma nomenclatura base"
                                       + ('; posible reconvocatoria con otra nomenclatura' if cands else '; no se halló proceso similar en los 12 meses siguientes'),
                            'posibles_reconvocatorias': cands or None,
                            'descripcion': q['descripcion'][:160]})
        for i in q['items']:
            if i['monto_adjudicado'] and i['monto_referencial'] and i['monto_adjudicado'] > i['monto_referencial'] * 1.01:
                alertas.append({'tipo': 'adjudicado_supera_referencial', 'nomenclatura': q['nomenclatura'],
                                'item': i['descripcion_item'][:80], 'monto_referencial': i['monto_referencial'],
                                'monto_adjudicado': i['monto_adjudicado'],
                                'exceso_pct': round(100 * (i['monto_adjudicado'] / i['monto_referencial'] - 1), 1),
                                'nota': 'Puede deberse a actualización del valor estimado/referencial no reflejada en el dataset; verificar en el expediente SEACE.'})
            if i['alertas_proveedor']:
                alertas.append({'tipo': 'proveedor_con_registro_sancion_o_inhabilitacion', 'nomenclatura': q['nomenclatura'],
                                'proveedor': i['proveedor'], 'ruc': i['ruc_proveedor'], 'registros': i['alertas_proveedor']})
    ALIM = ('viveres_comedor', 'navidad_estudiantes', 'servicio_alimentacion_filiales')
    pa = collections.defaultdict(lambda: [0.0, set(), None])
    for q in procesos:
        if q['categoria'] not in ALIM:
            continue
        for i in q['items']:
            if i['ruc_proveedor'] and i['estado'] != 'Desierto':
                pa[i['ruc_proveedor']][0] += i['monto_adjudicado'] or 0
                pa[i['ruc_proveedor']][1].add(q['anio'])
                pa[i['ruc_proveedor']][2] = i['proveedor']
    tot_alim = sum(v[0] for v in pa.values()) or 1
    top = sorted(pa.items(), key=lambda kv: -kv[1][0])[:6]
    alertas.append({'tipo': 'concentracion_proveedores_alimentos',
                    'detalle': f"Víveres + servicio de alimentación + panetones 2018-2026 (S/ {tot_alim:,.2f} adjudicados): top 6 proveedores",
                    'top': [{'ruc': k, 'nombre': v[2], 'monto': round(v[0], 2), 'participacion_pct': round(100 * v[0] / tot_alim, 1),
                             'anios': sorted(v[1])} for k, v in top]})

    for o in ocds_new:
        if o['relacionado_no_comedor']:
            continue
        est = o['estado_items']
        if est and all(e == 'NULO' for e in est):
            alertas.append({'tipo': 'proceso_nulo', 'nomenclatura': o['nomenclatura'], 'fecha': o['fecha_publicacion'],
                            'valor_estimado': o['valor_estimado'], 'n_postores': o['n_postores'],
                            'detalle': 'Todos los ítems figuran NULO en OCDS (no aparece en CONOSCE adjudicaciones/desiertos).',
                            'descripcion': (o['descripcion'] or '')[:160], 'fuente': o['fuente']})
        elif any(e in ('CONVOCADO', 'EN EVALUACION', 'EN EVALUACIÓN') for e in est):
            alertas.append({'tipo': 'proceso_en_curso', 'nomenclatura': o['nomenclatura'], 'fecha': o['fecha_publicacion'],
                            'estado_items': est, 'descripcion': (o['descripcion'] or '')[:200], 'fuente': o['fuente']})

    # cobertura dic-2026
    corte = a.fecha_corte
    cob = []
    for q in procesos:
        if q['anio'] < 2025 or q['categoria'] not in ('viveres_comedor', 'servicio_alimentacion_filiales', 'navidad_estudiantes', 'combustible_comedor'):
            continue
        for i in q['items']:
            if i['estado'] == 'Desierto':
                continue
            fins = [(c.get('vigencia_fin_actualizada') or c.get('vigencia_fin'), 'CONOSCE') for c in (i.get('contratos') or [])]
            fins += [(c.get('fin'), 'OCDS') for c in (i.get('contratos_ocds') or []) if c.get('fin')]
            fins = [f for f in fins if f[0]]
            cob.append({'nomenclatura': q['nomenclatura'], 'categoria': q['categoria'], 'item': i['descripcion_item'][:70],
                        'estado': i['estado'], 'proveedor': i['proveedor'], 'monto_adjudicado': i['monto_adjudicado'],
                        'fecha_buenapro': i['fecha_buenapro'],
                        'vigencia_fin': max(fins)[0] if fins else None,
                        'fuente_vigencia': max(fins)[1] if fins else None})
    def contratos_de(cat_set, desde, hasta):
        out_ = []
        for q in procesos:
            if q['categoria'] not in cat_set:
                continue
            for i in q['items']:
                for c in (i.get('contratos_ocds') or []):
                    if c.get('inicio') and desde <= c['inicio'] < hasta:
                        out_.append({'nomenclatura': q['nomenclatura'], 'item': i['descripcion_item'][:60], 'proveedor': i['proveedor'],
                                     'contrato': c.get('numero'), 'inicio': c['inicio'], 'fin': c.get('fin')})
                for c in (i.get('contratos') or []):
                    if c.get('vigencia_inicio') and desde <= c['vigencia_inicio'] < hasta:
                        out_.append({'nomenclatura': q['nomenclatura'], 'item': i['descripcion_item'][:60], 'proveedor': i['proveedor'],
                                     'contrato': c.get('numero'), 'inicio': c['vigencia_inicio'],
                                     'fin': c.get('vigencia_fin_actualizada') or c.get('vigencia_fin')})
        best = {}
        for c in out_:  # mismo contrato/ítem en CONOSCE y OCDS: se queda el fin más tardío (vigencia actualizada)
            k = (c['contrato'], c['item'])
            if k not in best or (c['fin'] or '') > (best[k]['fin'] or ''):
                best[k] = c
        return sorted(best.values(), key=lambda x: (x['fin'] or '', x['item']))
    viv = ('viveres_comedor',)
    ciclo_2025 = contratos_de(viv, '2025-01-01', '2026-07-01')
    ciclo_2026 = contratos_de(viv, '2026-07-01', '2027-12-31')
    brecha = {
        'nota': 'Contratos de víveres del comedor (sede Cusco) según fecha de inicio de vigencia. La vigencia es el plazo contractual; '
                'en suministros las entregas pueden agotarse antes. Comparar fin del ciclo 2025-26 con inicio del ciclo 2026-27.',
        'ciclo_2025_2026': ciclo_2025,
        'ciclo_2026_2027': ciclo_2026,
        'fin_mas_temprano_ciclo_2025': min((c['fin'] for c in ciclo_2025 if c['fin']), default=None),
        'fin_mas_tardio_ciclo_2025': max((c['fin'] for c in ciclo_2025 if c['fin']), default=None),
        'inicio_mas_temprano_ciclo_2026': min((c['inicio'] for c in ciclo_2026), default=None),
        'inicio_mas_tardio_ciclo_2026': max((c['inicio'] for c in ciclo_2026), default=None),
    }
    cobertura = {
        'brecha_viveres': brecha,
        'pregunta': '¿Hay contratos vigentes o procesos en curso que cubran el abastecimiento del comedor en diciembre 2026?',
        'items_2025_2026': cob,
        'convocatorias_ocds_no_en_conosce': ocds_new,
    }

    meta = {
        'titulo': 'Comedor universitario UNSAAC: procesos de selección y contratos 2018-2026',
        'entidad': {'nombre': 'Universidad Nacional de San Antonio Abad del Cusco', 'ruc': ENTIDAD_RUC,
                    'codigo_entidad_conosce': '001909', 'ocds_buyer_id': OCDS_BUYER},
        'generado': dt.datetime.now().isoformat(timespec='seconds'),
        'fecha_corte': corte,
        'fuentes': [
            {'nombre': 'OECE CONOSCE - datos abiertos (convocatorias, adjudicaciones, desiertos, contratos, postores, PAC)',
             'archivos': 'licitaperu-alertas/seace-scraper/data/conosce/out/*_{2018..2026}.csv.gz',
             'generados': '2026-10-27 (2018-2023) y 2026-11-03 (2024-2026)',
             'nota': 'El portal CONOSCE devolvía 404 al 2026-11-16; se usan las descargas previas.'},
            {'nombre': 'OECE CONOSCE - XLSX descargados directamente del portal (reemplazan al caché para ese año)',
             'url': CONOSCE_XLSX_URL.replace('{kind}', '<tipo>').replace('{KIND}', '<TIPO>').replace('{y}', 'AAAA'),
             'archivos': sorted(f'{k}_{y}' for k, y in FRESH),
             'descargado': sorted({dt.date.fromtimestamp(os.path.getmtime(v)).isoformat() for v in FRESH.values()})},
            {'nombre': 'OECE CONOSCE - sancionados (TCE) e inhabilitados judiciales', 'fecha_corte': corte_sanc},
            {'nombre': 'OECE Contrataciones Abiertas (OCDS) - bulk mensual CSV',
             'url': OCDS_URL.replace('{y}', 'AAAA').replace('{m:02d}', 'MM'),
             'meses_cargados': [ocds_months[0], ocds_months[-1]] if ocds_months else None,
             'n_meses': len(ocds_months),
             'meses': ocds_months,
             'nota': 'Meses no cargados (2018-08 a 2021-06) solo afectan fechas de contratos antiguos. Cada archivo mensual agrupa procesos por mes de convocatoria con su última versión compilada (descargado 2026-11-16, archivos fechados hasta 2026-11-15).'},
        ],
        'metodo': {
            'filtro_entidad': "codigo_entidad == 001909 o RUC 20172474501 (excluye GORE Cusco, municipalidades y otras universidades)",
            'palabras_clave': KEYWORDS,
            'reglas': 'classify(): exclusiones (zoológico/granja/zootecnia, alimentación eléctrica, laboratorio, odontología) -> relacionados (personal docente/administrativo; CEPRU/admisión) -> categorías comedor. Revisado a mano.',
            'categorias': CATEGORIAS, 'relacionados': RELACIONADAS,
            'excluidos_por_regla': {k: {'items': v, 'ejemplos': sorted(excluded_examples[k])[:15]} for k, v in excluded.most_common()},
            'estado_proceso': 'derivado de estado_item: contratado / contratado (parcial) / adjudicado (buena pro, sin contrato registrado) / parcialmente desierto / desierto',
            'familia': 'nomenclatura sin el sufijo de nº de convocatoria (-1, -2, -3) para detectar reconvocatorias',
        },
        'caveats': [
            'Solo procedimientos de selección registrados en SEACE (LP, AS, SIE, CP, contratación directa, etc.). Las órdenes de compra/servicio ≤ 8 UIT (compras menores) NO están aquí.',
            'CONOSCE no publica procesos "nulos" ni cancelados como estado propio: solo adjudicados/contratados y desiertos. Un proceso convocado que aún no tiene buena pro puede no figurar; por eso se complementa con OCDS.',
            'El dataset de contratos de CONOSCE está incompleto para la UNSAAC (pocos contratos registrados); las fechas de vigencia faltantes se buscan en OCDS. Sin contrato registrado, el fin de vigencia es desconocido, no "vencido".',
            'Los suministros de víveres suelen ejecutarse por entregas parciales hasta agotar el monto; la vigencia del contrato no garantiza entregas efectivas.',
            'Montos en soles, tal como figuran en los datasets (monto referencial/valor estimado y monto adjudicado por ítem). No incluyen adicionales salvo que se indique.',
            'Un monto adjudicado mayor al referencial puede reflejar actualización del valor estimado no capturada por el dataset; verificar en el expediente.',
            'Clasificación por palabras clave: procesos con descripción genérica ("alimentos para la UNSAAC") se marcan confianza media; "queso andino" 2018 sin destino declarado, confianza baja.',
            'OCDS vincula contratos al proceso por RUC del proveedor; un mismo contrato puede cubrir varios ítems (su monto es el del contrato, no del ítem). Ej.: el contrato 087-2025-UA-DIGA-UNSAAC (S/ 163,980, Cruz Suárez) figura en el registro OCDS de LP-SM-3-2025 pero su monto coincide con AS-SM-17-2025 (abarrotes); probablemente corresponde a este último.',
            'Los procesos con estado NULO solo se ven en OCDS (CONOSCE no los publica).',
            'Ausencia en datos públicos no prueba inexistencia: puede haber contratos complementarios, ampliaciones o compras menores no reflejadas.',
        ],
    }

    out = {
        '_meta': meta,
        'resumen_anual': resumen_anual,
        'resumen_por_categoria': por_categoria,
        'proveedores': proveedores,
        'alertas': alertas,
        'cobertura_dic_2026': cobertura,
        'procesos': procesos,
        'relacionados_no_comedor': relacionados,
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=lambda x: sorted(x) if isinstance(x, set) else str(x))
    print(f'OK {a.out}: {len(procesos)} procesos comedor, {len(relacionados)} relacionados, '
          f'{len(proveedores)} proveedores, {len(ocds)} procesos OCDS, {len(ocds_new)} OCDS nuevos', file=sys.stderr)

if __name__ == '__main__':
    main()
