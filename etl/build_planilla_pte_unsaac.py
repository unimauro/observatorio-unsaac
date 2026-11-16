#!/usr/bin/env python3
"""Planilla NOMINAL de la UNSAAC (docentes, administrativos, funcionarios; nunca estudiantes).

Genera data/planilla-unsaac.json en el schema v2 de observatorio-uni (data/planilla-uni.json),
conservando el bloque AIRHSP agregado que ya tenga el JSON.

Fuentes, en orden de preferencia:

1. Portal de Transparencia Estándar (PTE), rubro Personal, id_entidad=10443.
   Se usa la exportación CSV del propio PTE (una sola petición por mes y régimen, sin paginar):
     https://www.transparencia.gob.pe/personal/pte_transparencia_personal_genera.aspx
       ?id_entidad=10443&in_anno_consulta=AAAA&ch_mes_consulta=MM&ch_tipo_regimen=R
       &vc_dni_funcionario=&vc_nombre_funcionario=&ch_tipo_descarga=2
   Si no hay datos, el portal responde 200 con cuerpo vacío. Al 16-nov-2026 la UNSAAC NO tiene
   ningún registro en el PTE (2013-2026, todos los meses, regímenes CAS/276/728): el HTML dice
   "No se han encontrado coincidencias". Si algún día publica, este script lo usa automáticamente.

2. Respaldo: Presupuesto Analítico de Personal (PAP) 2026 NOMINAL, Formato A.2, publicado por la
   UNSAAC en gob.pe (aprobado con R-500-2026-UNSAAC):
     https://www.gob.pe/institucion/unsaac/informes-publicaciones/8014341-presupuesto-analitico-de-personal-pap-2026-unsaac
   Es un PDF de 420 páginas: una tabla muy ancha partida en 10 bloques de columnas de 42 páginas
   cada uno (bloque 1 = plaza/unidad/régimen, 2 = condición/cargo, 3 = apellidos y nombres + DNI,
   ..., 8 = ingreso imponible / carga social / ingreso no imponible, 9 = incentivo único / TOTAL
   MENSUAL / costos ocasionales, 10 = costo total anual). Las filas de un mismo índice de página
   están a la misma altura (y) en los 10 bloques. Se extrae con `pdftotext -raw -bbox` (poppler).
   OJO: el PAP es lo PRESUPUESTADO por plaza para 2026 (fuente Recursos Ordinarios), no la planilla
   pagada en un mes concreto. El JSON lo declara en _meta.tipo_fuente.

El DNI que trae el PAP se lee solo para alinear filas y NUNCA se escribe en el JSON.
Las plazas vacantes (sin ocupante) se cuentan en _meta pero no se listan como personas.

Validaciones (si alguna falla, NO se sobreescribe el JSON y la salida es != 0):
  * cada fila: TOTAL MENSUAL == ingreso imponible + carga social + ingreso no imponible + incentivo único (±0.05)
  * cada fila: COSTO ANUAL == 12 × TOTAL MENSUAL + TOTAL OCASIONAL (±1.00)
  * suma de columnas == fila de totales impresa en la última página del PDF (cuando es legible)
  * cada fila ocupada tiene nombre con coma ("APELLIDOS, NOMBRES") y montos numéricos

Uso:
    etl/venv/bin/python etl/build_planilla_pte_unsaac.py            # PTE año en curso; si vacío -> PAP
    python3 etl/build_planilla_pte_unsaac.py --solo-pap
    python3 etl/build_planilla_pte_unsaac.py --no-net                 # solo caché (etl/cache_pte/)
    python3 etl/build_planilla_pte_unsaac.py --id-entidad 10048 --out /tmp/x.json   # probar la ruta PTE con otra entidad

Requiere: Python 3.9+ (stdlib) y `pdftotext` (poppler) solo para la ruta PAP.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "etl" / "cache_pte"
OUT = ROOT / "data" / "planilla-unsaac.json"

ID_ENTIDAD = 10443
ENTIDAD = "Universidad Nacional de San Antonio Abad del Cusco (UNSAAC) - pliego 511 / id_entidad PTE 10443"
PTE_CSV = "https://www.transparencia.gob.pe/personal/pte_transparencia_personal_genera.aspx"
PTE_HTML = "https://www.transparencia.gob.pe/personal/pte_transparencia_personal.aspx"
PAP_PAGE = ("https://www.gob.pe/institucion/unsaac/informes-publicaciones/"
            "8014341-presupuesto-analitico-de-personal-pap-2026-unsaac")
PAP_PDF = "https://cdn.www.gob.pe/uploads/document/file/9804859/8014341-reporte_formato_a2_89.pdf"
PAP_ANIO = 2026
PAP_NORMA = "R-500-2026-UNSAAC"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 90
RETRIES = 4

REGIMENES = {1: "CAS", 2: "Régimen 276", 3: "Régimen 728/OTROS", 4: "PAC", 5: "FAG",
             6: "PNUD", 7: "Altos Funcionarios", 9: "Ley Servir"}  # 8 = pensionistas: excluido
REG_LABEL_PTE = {
    "CAS": "CAS (D.Leg. 1057)", "Régimen 276": "D.Leg. 276 (nombrados)",
    "Régimen 728/OTROS": "Régimen 728 / otros (donde el PTE lista a los docentes)",
    "PAC": "PAC", "FAG": "FAG", "PNUD": "PNUD", "Altos Funcionarios": "Altos funcionarios",
    "Ley Servir": "Ley Servir (30057)",
}
MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "setiembre", "octubre", "noviembre", "diciembre"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch_bytes(url: str) -> bytes:
    last = None
    for i in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*",
                                                      "Accept-Language": "es-PE,es;q=0.9"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last = e
            wait = 2 ** i + 0.5
            log(f"  ! reintento {i + 1}/{RETRIES} en {wait:.0f}s: {url[-60:]} ({e})")
            time.sleep(wait)
    raise RuntimeError(f"fallo definitivo: {url} ({last})")


def money(s: str) -> float:
    s = (s or "").replace("S/.", "").replace("S/", "").replace(",", "").strip()
    if s in ("", "-", "--"):
        return 0.0
    if not re.fullmatch(r"-?\d{1,10}(\.\d{1,4})?", s):
        raise ValueError(f"monto no numérico: {s!r}")
    return round(float(s), 2)


def categoria_por_cargo(cargo: str) -> str:
    c = (cargo or "").upper()
    if re.search(r"DOCENTE|PROFESOR|JEFE DE PR[AÁ]CTICA|CATEDR|INVESTIGADOR", c):
        return "docente"
    if re.search(r"\bRECTOR|VICE\s?RECTOR|VICERRECTOR|DECAN|DIRECTOR|\bJEFE\b|JEFA\b|SECRETARI[AO] GENERAL|"
                 r"GERENTE|SUBDIRECTOR|SUB DIRECTOR", c):
        return "funcionario"
    return "administrativo"


def stats(xs: list[float]) -> dict:
    xs = [v for v in xs if v > 0]
    return {"n": len(xs), "prom": round(sum(xs) / len(xs), 2) if xs else 0,
            "mediana": round(statistics.median(xs), 2) if xs else 0,
            "min": min(xs) if xs else 0, "max": max(xs) if xs else 0}


# =============================================================== 1) PTE (exportación CSV)
def pte_csv_url(id_ent: int, anio: int, mes: int, reg: int) -> str:
    return (f"{PTE_CSV}?id_entidad={id_ent}&in_anno_consulta={anio}&ch_mes_consulta={mes:02d}"
            f"&ch_tipo_regimen={reg}&vc_dni_funcionario=&vc_nombre_funcionario=&ch_tipo_descarga=2")


def pte_html_url(id_ent: int, anio: int, mes: int, reg: int) -> str:
    return (f"{PTE_HTML}?id_entidad={id_ent}&in_anno_consulta={anio}&ch_mes_consulta={mes:02d}"
            f"&vc_nombre_funcionario=&vc_dni_funcionario=&ch_tipo_regimen={reg}&id_tema=32&Ver=&pag=1")


def pte_month_regime(id_ent: int, anio: int, mes: int, reg: int, no_net: bool) -> list[dict]:
    f = CACHE / f"pte_{id_ent}_{anio}_{mes:02d}_r{reg}.csv"
    if f.exists():
        raw = f.read_bytes()
    elif no_net:
        return []
    else:
        raw = fetch_bytes(pte_csv_url(id_ent, anio, mes, reg))
        CACHE.mkdir(parents=True, exist_ok=True)
        f.write_bytes(raw)
    txt = raw.decode("utf-8-sig", errors="replace").strip()
    if not txt or not txt.startswith("PK_ID_PERSONAL"):
        return []
    rows = []
    for x in csv.DictReader(io.StringIO(txt)):
        pat, mat, nom = (x.get("VC_PERSONAL_PATERNO") or "").strip(), (x.get("VC_PERSONAL_MATERNO") or "").strip(), \
            (x.get("VC_PERSONAL_NOMBRES") or "").strip()
        nombre = f"{' '.join(t for t in (pat, mat) if t)}, {nom}".strip(", ")
        if not re.search(r"[A-Za-zÁÉÍÓÚÑ]", nombre):
            continue
        r = {
            "nombre": nombre[:120], "cargo": (x.get("VC_PERSONAL_CARGO") or "").strip()[:160],
            "dependencia": (x.get("VC_PERSONAL_DEPENDENCIA") or "").strip()[:160],
            "regimen": REG_LABEL_PTE[REGIMENES[reg]],
            "remun": money(x.get("MO_PERSONAL_REMUNERACIONES")), "honorarios": money(x.get("MO_PERSONAL_HONORARIOS")),
            "incentivo": money(x.get("MO_PERSONAL_INCENTIVO")), "aguinaldo": money(x.get("MO_PERSONAL_GRATIFICACION")),
            "otros": money(x.get("MO_PERSONAL_OTROS_BENEFICIOS")), "total": money(x.get("MO_PERSONAL_TOTAL")),
        }
        obs = (x.get("VC_PERSONAL_OBSERVACIONES") or "").strip()
        if obs:
            r["descripcion"] = obs[:400]
        rows.append(r)
    return rows


def run_pte(id_ent: int, anio: int, meses: list[int], no_net: bool) -> tuple[dict, list[dict]] | None:
    """Devuelve (meta_parcial, personas) del mes más reciente con datos, o None si el PTE no tiene nada."""
    result: dict[tuple[int, int], list[dict]] = {}
    for m in meses:
        for reg in REGIMENES:
            rows = pte_month_regime(id_ent, anio, m, reg, no_net)
            if rows:
                result[(m, reg)] = rows
                log(f"  PTE {anio}-{m:02d} r{reg} {REGIMENES[reg]:<20} {len(rows):5d} filas")
    if not result:
        return None
    meses_con = sorted({m for m, _ in result})
    mes_ult = meses_con[-1]
    personas = [p for (m, _), rows in sorted(result.items()) if m == mes_ult for p in rows]
    for p in personas:
        p["categoria"] = categoria_por_cargo(p["cargo"])
    serie = [{"anio": anio, "mes": m, "regimen": REG_LABEL_PTE[REGIMENES[r]], "n": len(rows),
              "masa_total": round(sum(x["total"] for x in rows), 2)} for (m, r), rows in sorted(result.items())]
    discrep = sum(1 for (m, _), rows in result.items() for x in rows
                  if abs(x["remun"] + x["honorarios"] + x["incentivo"] + x["aguinaldo"] + x["otros"] - x["total"]) > 1.0)
    meta = {
        "tipo_fuente": "PTE (planilla mensual publicada)",
        "fuente": "Portal de Transparencia Estándar (PTE) · rubro Personal · transparencia.gob.pe (exportación CSV) + AIRHSP (agregado)",
        "url_fuente": pte_html_url(id_ent, anio, mes_ult, 1),
        "anio": anio, "mes": mes_ult, "mes_nombre": MESES[mes_ult],
        "meses_consultados": meses, "meses_con_datos": meses_con,
        "meses_sin_datos": [m for m in meses if m not in meses_con],
        "regimenes_consultados": list(REGIMENES.values()),
        "regimenes_con_datos": [REGIMENES[r] for r in sorted({r for _, r in result})],
        "discrepancias_suma": discrep,
        "nota_periodo": f"Mes más reciente con datos en el PTE: {MESES[mes_ult]} {anio}.",
    }
    return meta, personas, serie


# =============================================================== 2) PAP 2026 nominal (PDF)
WORD_RE = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>')
NUM_RE = re.compile(r"-?[\d,]+\.\d\d")
COL_STARTS = (52.68, 117.14, 181.58, 246.02, 310.49, 374.95, 439.39, 503.83)  # inicio de celdas de texto (paso 64.44 pt)


def pdf_pages(pdf: Path) -> list[list[tuple[float, float, float, str]]]:
    """Palabras por página en orden del flujo de contenido: (xMin, yMin, xMax, texto)."""
    if not shutil.which("pdftotext"):
        raise RuntimeError("falta `pdftotext` (poppler): brew install poppler")
    out = subprocess.run(["pdftotext", "-raw", "-bbox", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    pages = []
    for chunk in re.split(r"<page\b", out)[1:]:
        pages.append([(float(a), float(b), float(c), html.unescape(w)) for a, b, _, c, w in
                      ((m.group(1), m.group(2), None, m.group(3), m.group(5)) for m in WORD_RE.finditer(chunk))])
    return pages


def cells_of(words: list[tuple[float, float, float, str]]) -> list[dict]:
    """Agrupa palabras consecutivas (mismo renglón) en celdas. Las celdas del PAP son de una línea y su
    texto desborda sobre la columna vecina, así que la separación se hace por el orden del flujo:
    palabra nueva = celda nueva si retrocede en x, si el hueco no es un espacio (~2.5 pt) o si arranca
    en el inicio de una columna de texto."""
    cells: list[dict] = []
    for x0, y0, x1, w in words:
        prev = cells[-1] if cells else None
        gap = x0 - prev["x1"] if prev else None
        at_col = any(abs(x0 - c) < 0.3 for c in COL_STARTS)
        if prev is None or gap < 1.5 or gap > 3.2 or at_col:
            cells.append({"x0": x0, "x1": x1, "t": w})
        else:
            prev["x1"] = x1
            prev["t"] += " " + w
    return cells


def near(v: float, target: float, tol: float = 1.2) -> bool:
    return abs(v - target) <= tol


DOC_RE = re.compile(r"(D\.N\.I\.|C\.E\.|PASAPORTE|P\.T\.P\.|C\.I\.|OTROS?|DNI)")
CLASIF_RE = re.compile(r"(EMPLEADO DE CONFIANZA|SERVIDOR CIVIL DE CARRERA|FUNCIONARIO P[UÚ]BLICO|DIRECTIVO SUPERIOR|SERVIDOR DE ACTIVIDADES COMPLEMENTARIAS)")
CONDICIONES = ("Contratado a plazo fijo", "Nombrado", "Designado", "Reincorporado", "Destacado", "Encargado")


def text_fields(words: list, starts: tuple[float, ...], checks: dict | None = None) -> dict[float, str]:
    """Arma los campos de texto de una fila. Un campo solo se abre con una celda que empieza en SU columna
    esperada (y, si hay `checks`, que pase la validación de esa columna); cualquier otra celda de texto es
    continuación del campo en curso (texto que desbordó y fue cortado en el inicio de otra columna).
    Los tokens numéricos se ignoran (se leen aparte por su borde derecho)."""
    checks = checks or {}
    out = {s: "" for s in starts}
    cur = None
    for c in cells_of(words):
        t = c["t"].strip()
        if re.fullmatch(r"[\d,.\-]+", t):
            cur = None
            continue
        s = next((s for s in starts if abs(c["x0"] - s) < 0.5), None)
        if s is not None and not out[s] and (s not in checks or checks[s](t)):
            cur = s
            out[s] = t
        elif cur is not None:
            out[cur] += " " + t
    return out


def parse_pap(pdf: Path) -> tuple[list[dict], dict]:
    pages = pdf_pages(pdf)
    n = len(pages)

    def has_hdr(pg, *keys):
        txt = " ".join(w[3] for w in pg if w[1] < 355).upper()
        return all(k in txt for k in keys)
    # Bloque 1 empieza en p1 ("NRO PAP"); el bloque 2 en la primera página con cabecera "CONDICION ... GRUPO".
    if not has_hdr(pages[0], "NRO", "PAP"):
        raise RuntimeError("no se reconoce la cabecera del PAP en la página 1")
    per = next((i for i in range(1, n) if has_hdr(pages[i], "CONDICION", "GRUPO")), 0)
    if per <= 0 or n % per != 0:
        raise RuntimeError(f"estructura inesperada de bloques: {n} páginas, bloque 2 en p{per + 1}")
    nblk = n // per
    blk = lambda b, k: pages[b * per + k]  # noqa: E731
    # Identificar bloques por su cabecera
    need = {"plaza": ("NRO", "PAP", "REGIMEN"), "cargo": ("CONDICION", "GRUPO", "CARGO"), "nombre": ("APELLIDOS", "NOMBRES"),
            "costo": ("INGRESO", "IMPONIBLE", "CARGA", "SOCIAL"), "total": ("TOTAL", "MENSUAL", "OCASIONAL"), "anual": ("ANUAL",)}
    bidx: dict[str, int] = {}
    for key, kws in need.items():
        hits = [b for b in range(nblk) if has_hdr(blk(b, 0), *kws)]
        if not hits:
            raise RuntimeError(f"no se encontró el bloque '{key}' (cabecera {kws})")
        bidx[key] = hits[-1] if key in ("costo", "anual") else hits[0]
    log(f"  PAP: {n} páginas = {nblk} bloques × {per}; bloques {bidx}")

    rows: list[dict] = []
    totals_printed: dict[str, float] = {}
    bad: list[str] = []
    for k in range(per):
        # anclas de fila: N.º PAP (entero alineado a la derecha en x≈113.3) del bloque 1
        anchors = [(y0, int(w)) for x0, y0, x1, w in blk(bidx["plaza"], k) if re.fullmatch(r"\d+", w) and near(x1, 113.26) and y0 > (250 if k == 0 else 40)]
        if not anchors:
            continue
        ys = [a[0] for a in anchors]

        def rowwords(b):
            byrow: dict[int, list] = defaultdict(list)
            extra: list = []
            for wd in blk(b, k):
                if wd[1] < ys[0] - 5:
                    continue
                j = min(range(len(ys)), key=lambda i: abs(ys[i] - wd[1]))
                if abs(ys[j] - wd[1]) <= 4:
                    byrow[j].append(wd)
                elif wd[1] > ys[-1] + 4:
                    extra.append(wd)
            return byrow, extra
        R = {key: rowwords(b) for key, b in bidx.items()}
        # fila de totales impresa al final (última página): montos con y > última ancla
        if k == per - 1:
            for key, cols in (("costo", {"iim": 371.0, "cso": 435.5, "ini": 499.9}),
                              ("total", {"inu": 113.2, "total_mensual": 177.6, "total_ocasional": 499.9})):
                for x0, y0, x1, w in R[key][1]:
                    for name, xm in cols.items():
                        if near(x1, xm) and NUM_RE.fullmatch(w):
                            totals_printed[name] = money(w)
        for j, (y, nro) in enumerate(anchors):
            W = {key: R[key][0].get(j, []) for key in R}
            f_pl = text_fields(W["plaza"], (117.14, 181.58, 374.95, 439.39))
            f_cg = text_fields(W["cargo"], (52.68, 117.14, 181.58, 246.02, 310.49, 374.95, 439.39),
                               {246.02: lambda t: bool(CLASIF_RE.fullmatch(t)), 310.49: lambda t: len(t) <= 6 and t.upper() == t})
            cargo, cargo_cod = f_cg[181.58], f_cg[310.49]
            m_cl = CLASIF_RE.search(cargo)  # clasificación pegada al cargo
            if m_cl and m_cl.end() == len(cargo):
                cargo, f_cg[246.02] = cargo[:m_cl.start()].rstrip(), f_cg[246.02] or m_cl.group(0)
            m_cod = re.search(r"(F-\d|[SAPDJ][A-Z]{1,3}\d?)$", cargo)  # código pegado: "JEFE DE UNIDAD DE EMPLEOF-4"
            if not cargo_cod and m_cod and m_cod.group(1).startswith("F-"):
                cargo, cargo_cod = cargo[:m_cod.start()].rstrip(), m_cod.group(1)
            f_nm = text_fields(W["nombre"], (52.68, 117.14), {117.14: lambda t: bool(DOC_RE.fullmatch(t))})
            airhsp = [w for x0, y0, x1, w in W["plaza"] if near(x1, 306.6) and re.fullmatch(r"\d+", w)]
            jornada = [w for x0, y0, x1, w in W["cargo"] if abs(x0 - 374.95) < 0.5 and re.fullmatch(r"\d+", w)]
            nombre = f_nm[52.68]
            m_doc = re.search(r"\s*(D\.N\.I\.|C\.E\.)$", nombre)  # tipo de doc pegado al nombre por pdftotext
            if m_doc and not f_nm[117.14]:
                nombre, f_nm[117.14] = nombre[:m_doc.start()].rstrip(), m_doc.group(1)
            condicion, grupo = f_cg[52.68], f_cg[117.14]
            if not grupo:  # "ReincorporadoProfesionales": pdftotext pegó dos celdas
                for cnd in CONDICIONES:
                    if condicion.startswith(cnd) and len(condicion) > len(cnd):
                        condicion, grupo = cnd, condicion[len(cnd):].strip()
                        break

            def num_at(key, xm):
                vals = [w for x0, y0, x1, w in R[key][0].get(j, []) if near(x1, xm) and NUM_RE.fullmatch(w)]
                if len(vals) != 1:
                    raise ValueError(f"PAP {nro}: {len(vals)} valores en {key}@{xm}")
                return money(vals[0])
            try:
                r = {
                    "nro_pap": nro,
                    "unidad": f_pl[117.14], "subtipo": f_pl[181.58], "airhsp": airhsp[0] if airhsp else "",
                    "estado": f_pl[374.95], "regimen": f_pl[439.39],
                    "condicion": condicion, "grupo": grupo, "cargo": cargo, "cargo_cod": cargo_cod, "clasificacion": f_cg[246.02],
                    "jornada": jornada[0] if jornada else "", "modalidad": f_cg[439.39],
                    "nombre": re.sub(r"\s+", " ", nombre).strip(), "tipo_doc": f_nm[117.14],
                    "iim": num_at("costo", 371.0), "cso": num_at("costo", 435.5), "ini": num_at("costo", 499.9),
                    "inu": num_at("total", 113.2), "total_mensual": num_at("total", 177.6),
                    "escolaridad": num_at("total", 242.1), "aguinaldo_anual": num_at("total", 306.6),
                    "otros_ocasionales": num_at("total", 371.0), "cts": num_at("total", 435.5),
                    "total_ocasional": num_at("total", 499.9), "costo_anual": num_at("anual", 113.2),
                }
            except ValueError as e:
                bad.append(str(e))
                continue
            # validaciones aritméticas por fila
            if abs(r["iim"] + r["cso"] + r["ini"] + r["inu"] - r["total_mensual"]) > 0.05:
                bad.append(f"PAP {nro}: IIM+CSO+INI+INU={r['iim'] + r['cso'] + r['ini'] + r['inu']:.2f} ≠ total mensual {r['total_mensual']:.2f}")
            if abs(12 * r["total_mensual"] + r["total_ocasional"] - r["costo_anual"]) > 1.0:
                bad.append(f"PAP {nro}: 12×mensual+ocasional={12 * r['total_mensual'] + r['total_ocasional']:.2f} ≠ anual {r['costo_anual']:.2f}")
            if r["estado"] != "V" and "NO REGISTRADO" not in r["nombre"].upper() and r["nombre"]:
                if not r["tipo_doc"] or not re.fullmatch(r"[^,]+, \S.*", r["nombre"]):
                    bad.append(f"PAP {nro}: nombre/doc mal leído ({r['nombre']!r}, {r['tipo_doc']!r})")
            rows.append(r)
    sums = {"iim": sum(r["iim"] for r in rows), "cso": sum(r["cso"] for r in rows), "ini": sum(r["ini"] for r in rows),
            "inu": sum(r["inu"] for r in rows), "total_mensual": sum(r["total_mensual"] for r in rows),
            "total_ocasional": sum(r["total_ocasional"] for r in rows)}
    chk = {}
    for name, v in totals_printed.items():
        chk[name] = {"pdf": v, "suma_filas": round(sums[name], 2), "ok": abs(v - sums[name]) < 1.0}
        if not chk[name]["ok"]:
            bad.append(f"total impreso {name}={v:,.2f} ≠ suma de filas {sums[name]:,.2f}")
    nros = [r["nro_pap"] for r in rows]
    if nros != list(range(1, len(nros) + 1)):
        bad.append(f"numeración PAP no correlativa (primeros saltos: {[x for i, x in enumerate(nros, 1) if x != i][:5]})")
    return rows, {"bloques": nblk, "paginas": n, "totales_pdf": chk, "errores": bad}


REG_PAP = {
    "Carreras Especiales": "Carreras Especiales (docentes universitarios, Ley 30220)",
    "D. Leg. Nº 276": "D.Leg. 276 (administrativos nombrados/contratados)",
    "D. Leg. Nº 728": "D.Leg. 728",
    "D. Leg. 1057 CAS": "CAS (D.Leg. 1057)",
    "Ley Nº 30057 SERVIR": "Ley Servir (30057)",
}


def is_vacante(r: dict) -> bool:
    n = r["nombre"].upper()
    return (not n) or "NO REGISTRADO" in n or "," not in n


def run_pap(no_net: bool) -> tuple[dict, list[dict], dict]:
    pdf = CACHE / "pap_2026_formato_a2_unsaac.pdf"
    if not pdf.exists():
        if no_net:
            raise RuntimeError(f"sin red y sin caché {pdf}")
        raw = fetch_bytes(PAP_PDF)
        if not raw.startswith(b"%PDF"):
            raise RuntimeError("la URL del PAP no devolvió un PDF")
        CACHE.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(raw)
    rows, diag = parse_pap(pdf)
    if diag["errores"]:
        raise RuntimeError(f"{len(diag['errores'])} error(es) de validación en el PAP: " + " | ".join(diag["errores"][:12]))
    ocup = [r for r in rows if not is_vacante(r)]
    vac = [r for r in rows if is_vacante(r)]
    estados = Counter(r["estado"] for r in rows)
    personas = []
    for r in ocup:
        reg = REG_PAP.get(r["regimen"], r["regimen"] or "sin régimen")
        grupo = r["grupo"].lower()
        if "docente" in grupo or r["regimen"] == "Carreras Especiales":
            cat = "funcionario" if categoria_por_cargo(r["cargo"]) == "funcionario" and "docente" not in r["cargo"].lower() \
                and not re.search(r"PROFESOR|JEFE DE PR", r["cargo"].upper()) else "docente"
        elif "funcionario" in grupo or "directivo" in grupo:
            cat = "funcionario"
        else:
            cat = categoria_por_cargo(r["cargo"])
        remun = round(r["iim"] + r["ini"], 2)
        total = round(remun + r["inu"], 2)
        personas.append({
            "nombre": r["nombre"][:120], "cargo": r["cargo"][:160], "dependencia": r["unidad"][:160],
            "regimen": reg, "categoria": cat,
            "remun": remun, "honorarios": 0.0, "incentivo": r["inu"], "aguinaldo": 0.0, "otros": 0.0, "total": total,
            "condicion": r["condicion"], "jornada": r["jornada"], "estado_plaza": r["estado"],
            "costo_mensual": r["total_mensual"], "costo_anual": r["costo_anual"],
        })
    meta = {
        "tipo_fuente": "PAP 2026 nominal (monto PRESUPUESTADO por plaza ocupada; no es la planilla pagada de un mes)",
        "fuente": (f"Presupuesto Analítico de Personal (PAP) {PAP_ANIO} · Formato A.2 nominal · UNSAAC en gob.pe "
                   f"({PAP_NORMA}) · fuente de financiamiento 00 Recursos Ordinarios + AIRHSP (agregado)"),
        "url_fuente": PAP_PAGE, "url_pdf": PAP_PDF,
        "anio": PAP_ANIO, "mes": None, "mes_nombre": f"PAP {PAP_ANIO} (mensual presupuestado)",
        "meses_consultados": [], "meses_con_datos": [], "meses_sin_datos": [],
        "regimenes_consultados": sorted({r["regimen"] for r in rows}),
        "regimenes_con_datos": sorted({r["regimen"] for r in ocup}),
        "discrepancias_suma": 0,
        "pap": {
            "plazas_total": len(rows), "plazas_ocupadas_listadas": len(ocup), "plazas_sin_ocupante": len(vac),
            "estado_registro": dict(estados.most_common()),
            "masa_mensual_por_estado": {e: round(sum(round(r["iim"] + r["ini"] + r["inu"], 2) for r in ocup if r["estado"] == e), 2)
                                        for e in sorted({r["estado"] for r in ocup})},
            "nota_estados": ("Códigos 'ESTADO REGISTRO' del PAP/AIRHSP: O = ocupada, V = vacante (no se lista), T y R = otros estados "
                             "con ocupante (en 38 de las personas con dos plazas se combina O + T, p. ej. autoridades o directivos "
                             "con su plaza docente/de origen: el PAP presupuesta ambas, por eso total > personas_distintas)."),
            "costo_mensual_total_todas_las_plazas": round(sum(r["total_mensual"] for r in rows), 2),
            "costo_mensual_essalud_9pct": round(sum(r["cso"] for r in rows), 2),
            "costo_anual_total_todas_las_plazas": round(sum(r["costo_anual"] for r in rows), 2),
            "costo_anual_plazas_ocupadas": round(sum(r["costo_anual"] for r in ocup), 2),
            "validacion": {"filas_validadas": len(rows), "totales_pdf": diag["totales_pdf"],
                           "reglas": "por fila: total mensual = ingreso imponible + carga social + ingreso no imponible + incentivo único; "
                                     "costo anual = 12 × total mensual + total ocasional; sumas = fila de totales del PDF"},
            "definicion_total": ("total = ingreso imponible (IIM) + ingreso no imponible (INI) + incentivo único (INU) mensual "
                                 "presupuestado del trabajador; NO incluye el aporte del empleador a EsSalud (9 %) ni los "
                                 "conceptos ocasionales anuales (escolaridad, aguinaldos, CTS), que van en costo_anual."),
        },
        "nota_periodo": f"PAP {PAP_ANIO} aprobado con {PAP_NORMA} (publicado en gob.pe). No hay serie mensual.",
    }
    return meta, personas, {"vacantes": len(vac)}


# =============================================================== main
def main() -> int:
    ap = argparse.ArgumentParser()
    hoy = datetime.now(timezone.utc)
    ap.add_argument("--anio", type=int, default=hoy.year)
    ap.add_argument("--meses", default=None, help="'a-b' o '7,8,9' (por defecto 1..mes actual)")
    ap.add_argument("--id-entidad", type=int, default=ID_ENTIDAD)
    ap.add_argument("--solo-pap", action="store_true", help="no consultar el PTE")
    ap.add_argument("--no-net", action="store_true")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    if a.meses is None:
        meses = list(range(1, (hoy.month if a.anio == hoy.year else 12) + 1))
    elif "-" in a.meses:
        lo, hi = a.meses.split("-")
        meses = list(range(int(lo), int(hi) + 1))
    else:
        meses = [int(x) for x in a.meses.split(",") if x]

    out_path = Path(a.out)
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    airhsp = (prev.get("resumen") or {}).get("por_regimen_airhsp") or []
    airhsp_total = (prev.get("_meta") or {}).get("total_planilla_airhsp")
    prev_total = (prev.get("_meta") or {}).get("total") or 0
    prev_schema = (prev.get("_meta") or {}).get("schema")

    fuentes_probadas = []
    serie: list[dict] = []
    got = None
    if not a.solo_pap:
        log(f"PTE id_entidad={a.id_entidad} {a.anio} meses {meses} (exportación CSV)…")
        try:
            got = run_pte(a.id_entidad, a.anio, meses, a.no_net)
        except Exception as e:  # noqa: BLE001
            log(f"ERROR PTE: {e}")
            return 2
        fuentes_probadas.append({"fuente": f"PTE rubro Personal id_entidad={a.id_entidad} {a.anio} (CSV, regímenes {list(REGIMENES)})",
                                 "resultado": "con datos" if got else "sin registros (respuesta vacía)"})
    if got:
        meta_src, personas, serie = got
        if a.id_entidad != ID_ENTIDAD:
            meta_src["entidad_probada"] = a.id_entidad
    else:
        if a.id_entidad != ID_ENTIDAD:
            log("PTE sin datos para la entidad de prueba; no se usa el PAP de la UNSAAC.")
            return 1
        log("PTE sin datos para la UNSAAC → PAP 2026 nominal (gob.pe)…")
        try:
            meta_src, personas, _ = run_pap(a.no_net)
        except Exception as e:  # noqa: BLE001
            log(f"ERROR PAP: {e}\nNo se sobreescribe {out_path}.")
            return 3
        fuentes_probadas.append({"fuente": "PAP 2026 nominal Formato A.2 (gob.pe, R-500-2026-UNSAAC)", "resultado": "con datos (usado)"})
    if not personas:
        log(f"Sin personas: no se sobreescribe {out_path}.")
        return 1
    if prev_schema == 2 and prev_total and len(personas) < 0.5 * prev_total:
        log(f"ERROR: {len(personas)} personas, menos de la mitad del JSON anterior ({prev_total}). No se sobreescribe.")
        return 4

    personas.sort(key=lambda p: (-p["total"], p["nombre"], p["regimen"], p["cargo"]))
    grupos: dict[str, list[dict]] = defaultdict(list)
    for p in personas:
        grupos[p["regimen"]].append(p)
    por_reg = []
    for rg, grp in sorted(grupos.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        s = stats([p["total"] for p in grp])
        por_reg.append({"nombre": rg, "n": len(grp), "total_mes": round(sum(p["total"] for p in grp), 2),
                        "prom": s["prom"], "mediana": s["mediana"], "max": s["max"]})
    cargos = Counter(p["cargo"] for p in personas)
    top_cargos = []
    for cg, n in sorted(cargos.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
        tot = [p["total"] for p in personas if p["cargo"] == cg]
        top_cargos.append({"cargo": cg, "n": n, "prom": round(sum(tot) / n, 2), "total_mes": round(sum(tot), 2)})
    deps: dict[str, list[float]] = defaultdict(list)
    for p in personas:
        deps[p["dependencia"]].append(p["total"])
    por_dep = sorted(({"dependencia": d, "n": len(v), "total_mes": round(sum(v), 2), "prom": round(sum(v) / len(v), 2)}
                      for d, v in deps.items()), key=lambda x: (-x["total_mes"], x["dependencia"]))[:25]
    por_cat = [{"nombre": k, "n": v} for k, v in Counter(p["categoria"] for p in personas).most_common()]
    remun = stats([p["total"] for p in personas])
    remun["n_cero"] = sum(1 for p in personas if p["total"] <= 0)
    remun["nota"] = (f"Estadísticos sobre el TOTAL mensual (S/) por registro ({meta_src['tipo_fuente']}), "
                     f"excluyendo {remun['n_cero']} registros con total 0.")
    masa = round(sum(p["total"] for p in personas), 2)
    top_ing = [{"nombre": p["nombre"], "cargo": p["cargo"], "regimen": p["regimen"], "total": p["total"]} for p in personas[:25]]

    fuentes_probadas += [
        {"fuente": "peru-transparente (funcionarios*.csv, shards, histórico)", "resultado": "sin registros de id_entidad 10443"},
        {"fuente": "transparencia.unsaac.edu.pe (Información de personal)", "resultado": "solo CAP/PAP/consolidados 2016 y planillas parciales 2019 (desactualizado)"},
        {"fuente": "remuneraciones.unsaac.edu.pe", "resultado": "portal de boletas con login del trabajador (no es información pública); no se usó"},
    ]
    meta = {
        "schema": 2,
        "tipo_fuente": meta_src["tipo_fuente"],
        "fuente": meta_src["fuente"],
        "url_fuente": meta_src["url_fuente"],
        "entidad": ENTIDAD,
        "anio": meta_src["anio"], "mes": meta_src["mes"], "mes_nombre": meta_src["mes_nombre"],
        "total": len(personas),
        "personas_distintas": len({p["nombre"] for p in personas}),
        "nota_total": "total = registros (una persona puede ocupar más de una plaza); personas_distintas = nombres únicos",
        "masa_mensual_total": masa,
        "total_planilla_airhsp": airhsp_total,
        "meses_consultados": meta_src["meses_consultados"], "meses_con_datos": meta_src["meses_con_datos"],
        "meses_sin_datos": meta_src["meses_sin_datos"],
        "regimenes_consultados": meta_src["regimenes_consultados"], "regimenes_con_datos": meta_src["regimenes_con_datos"],
        "regimenes_sin_datos": [r for r in meta_src["regimenes_consultados"] if r not in meta_src["regimenes_con_datos"]],
        "discrepancias_suma": meta_src["discrepancias_suma"],
        "fuentes_probadas": fuentes_probadas,
        "nota": ("Solo docentes, funcionarios y personal administrativo: nunca estudiantes. " + meta_src["nota_periodo"] + " "
                 + ("La UNSAAC (id_entidad 10443) no tiene registros en el rubro Personal del Portal de Transparencia Estándar "
                    "(consultado 2013-2026), por eso la lista nominal sale del PAP nominal que la propia universidad publicó en gob.pe: "
                    "es lo presupuestado por plaza ocupada (Recursos Ordinarios), no lo efectivamente pagado en un mes; no incluye a quienes "
                    "se pagan con otras fuentes (p. ej. recursos directamente recaudados) ni pagos variables. "
                    if "PAP" in meta_src["tipo_fuente"] else "")
                 + f"El 'sueldo_promedio' AIRHSP es remuneración base por plaza; no es comparable con el total nominal "
                   f"(el agregado AIRHSP registra {airhsp_total} plazas). No se publican DNI. No se inventaron personas ni sueldos."),
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for k in ("url_pdf", "pap", "entidad_probada"):
        if k in meta_src:
            meta[k] = meta_src[k]
    out = {
        "_meta": meta,
        "resumen": {"por_regimen_nominal": por_reg, "por_regimen_airhsp": airhsp, "por_categoria": por_cat,
                    "top_cargos": top_cargos, "por_dependencia": por_dep, "remun": remun},
        "serie_mensual": serie,
        "top_ingresos": top_ing,
        "personas": personas,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    log(f"\n== {meta['tipo_fuente']}: {len(personas)} registros ({meta['personas_distintas']} personas), masa mensual S/ {masa:,.2f}")
    for r in por_reg:
        log(f"   {r['nombre']:<58} n={r['n']:5d}  masa={r['total_mes']:>14,.2f}  prom={r['prom']:>9,.2f}  med={r['mediana']:>9,.2f}  max={r['max']:>9,.2f}")
    log(f"escrito {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
