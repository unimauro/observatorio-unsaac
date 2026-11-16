#!/usr/bin/env python3
"""Reconstruye data/proveedores-unsaac.json desde los CSV.gz normalizados de CONOSCE
(generados por licitaperu-alertas/seace-scraper/conosce_sync.py), periodo 2023-2026.
Conserva 'dueno'/'fuente_dueno' ya investigados (por RUC) del JSON anterior."""
import csv, gzip, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, '..', 'data', 'proveedores-unsaac.json')
D = os.environ.get('CONOSCE_OUT', '/Users/unimauro/Documents/Repos/licitaperu-alertas/seace-scraper/data/conosce/out')
RUC_ENT = '20172474501'; CODIGO = '001909'
YEARS = (2023, 2024, 2025, 2026)
CORTE = sys.argv[1] if len(sys.argv) > 1 else '2026-11'

prev = json.load(open(OUT, encoding='utf-8'))
duenos = {p['ruc']: (p.get('dueno'), p.get('fuente_dueno')) for p in prev['proveedores'] if p.get('dueno')}

def tp(ruc, tipo_seace):
    if (tipo_seace or '').lower().startswith('consorcio'):
        return 'empresa'
    return 'natural' if str(ruc)[:2] in ('10', '15', '17') else 'empresa'

rows = []
for y in YEARS:
    with gzip.open(f'{D}/adjudicaciones_{y}.csv.gz', 'rt', encoding='utf-8') as f:
        rows += [r for r in csv.DictReader(f) if r['entidad_ruc'] == RUC_ENT and r['codigo_entidad'] == CODIGO]

agg = {}
for r in rows:
    ruc = r['ruc_proveedor']
    a = agg.setdefault(ruc, {'nombre': r['proveedor'], 'monto': 0.0, 'convs': set(), 'tipos': {}, 'objeto': {},
                             'anios': {}, 'tipo_seace': r['tipo_proveedor']})
    m = float(r['monto_adjudicado_item'] or 0)
    a['monto'] += m
    a['convs'].add(r['codigo_convocatoria'])
    a['tipos'][r['tipo_proceso']] = a['tipos'].get(r['tipo_proceso'], 0) + 1
    a['objeto'][r['objeto']] = a['objeto'].get(r['objeto'], 0) + 1
    a['anios'][r['anio']] = round(a['anios'].get(r['anio'], 0) + m, 2)

provs = []
for ruc, a in agg.items():
    d, f = duenos.get(ruc, (None, None))
    provs.append({'nombre': a['nombre'], 'ruc': ruc, 'monto': round(a['monto'], 2), 'n': len(a['convs']),
                  'tipos': a['tipos'], 'objeto': a['objeto'], 'por_anio': a['anios'],
                  'tipo_persona': tp(ruc, a['tipo_seace']), 'tipo_proveedor_seace': a['tipo_seace'],
                  'dueno': d, 'fuente_dueno': f})
provs.sort(key=lambda x: -x['monto'])
emp = [p for p in provs if p['tipo_persona'] == 'empresa']
nat = [p for p in provs if p['tipo_persona'] == 'natural']
por_anio = {}
for r in rows:
    por_anio[r['anio']] = round(por_anio.get(r['anio'], 0) + float(r['monto_adjudicado_item'] or 0), 2)

meta = dict(prev['_meta'])
meta.update({'periodo': '2023-2026', 'extraido': CORTE,
  'nota': meta['nota'].replace('CONOSCE_ADJUDICACIONES{2023,2024,2025}_0.xlsx',
          'CONOSCE_ADJUDICACIONES{2023,2024,2025,2026}_0.xlsx (versiones regeneradas por OECE al 2/3-nov-2026; 2026 parcial: buenas pro otorgadas hasta esa fecha)')
          .replace("'monto' = suma de monto_adjudicado_item_soles", "'monto' = suma de monto_adjudicado_item")})
out = {'_meta': meta,
       'totales': {'monto_total': round(sum(p['monto'] for p in provs), 2), 'n_proveedores': len(provs),
                   'n_procesos': len({r['codigo_convocatoria'] for r in rows}), 'n_empresas': len(emp),
                   'n_personas_naturales': len(nat), 'monto_empresas': round(sum(p['monto'] for p in emp), 2),
                   'monto_personas_naturales': round(sum(p['monto'] for p in nat), 2), 'por_anio': por_anio},
       'top_personas': sorted([{'nombre': p['nombre'], 'ruc': p['ruc'], 'monto': p['monto'], 'n': p['n']} for p in nat], key=lambda x: -x['monto']),
       'proveedores': provs}
json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(json.dumps(out['totales'], ensure_ascii=False))
print('--- top 30 (sin dueño marcado con *) ---')
for p in provs[:30]:
    print(f"{'*' if not p['dueno'] else ' '} {p['monto']:>14,.2f} {p['ruc']:12} {p['tipo_persona']:8} {p['por_anio']} {p['nombre']}")
