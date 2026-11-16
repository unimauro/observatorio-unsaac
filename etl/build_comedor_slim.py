#!/usr/bin/env python3
"""Extrae de data/ordenes-unsaac.json (8.9 MB) solo lo del comedor -> data/comedor-ordenes-unsaac.json (liviano para la web)."""
import json, os
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
o = json.load(open(f'{R}/ordenes-unsaac.json', encoding='utf-8'))
keep = ('fecha', 'orden', 'tipo', 'descripcion', 'monto', 'proveedor', 'ruc', 'comedor_cat', 'estado')
ords = sorted([{k: x.get(k) for k in keep} for x in o['ordenes'] if x['comedor'] and x['estado'] != 'Anulada'], key=lambda x: x['fecha'], reverse=True)
out = {'_meta': {k: o['_meta'][k] for k in o['_meta'] if k in ('fuente', 'fuente_url', 'periodo_solicitado', 'fecha_min', 'fecha_max', 'nota', 'caveats', 'generado')},
       'comedor': o['comedor'], 'totales': {k: o['totales'][k] for k in ('n_vigentes', 'monto_vigente_pen', 'n_menor_8uit', 'monto_menor_8uit_pen', 'por_anio')},
       'top_proveedores': o['top_proveedores'][:25], 'ordenes': ords}
json.dump(out, open(f'{R}/comedor-ordenes-unsaac.json', 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print(len(ords), os.path.getsize(f'{R}/comedor-ordenes-unsaac.json') // 1024, 'KB')
