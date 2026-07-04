#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fusiona la serie histórica 2012-2024 (ca_unsaac_raw.json) en
data/presupuesto-unsaac.json SIN tocar 2025/2026 ni detalle_ultimo_anio."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(HERE, "ca_unsaac_raw.json")
TARGET = os.path.join(ROOT, "data", "presupuesto-unsaac.json")
CUR_YEAR = 2026

def main():
    raw = json.load(open(RAW, encoding="utf-8"))
    doc = json.load(open(TARGET, encoding="utf-8"))

    existing = {r["year"] for r in doc["serie"]}
    added = []
    for y, r in raw.items():
        yr = int(y)
        if yr in existing:
            continue  # nunca pisar 2025/2026 ya presentes
        pim = r["pim"]
        ejec = round(100 * r["dev"] / pim, 1) if pim else 0
        rec = {"year": yr, "pia": r["pia"], "pim": pim, "cert": r["cert"],
               "dev": r["dev"], "gir": r["gir"], "ejec_pct": ejec}
        if yr == CUR_YEAR and ejec < 70:
            rec["parcial"] = True
        doc["serie"].append(rec)
        added.append(yr)

    doc["serie"].sort(key=lambda x: x["year"])
    doc["_meta"]["nota"] = ("Serie histórica 2012-2026 vía Consulta Amigable MEF "
                            "(pliego 511, Gobierno Nacional). 2025 cerrado; "
                            "2026 en ejecución (parcial).")

    json.dump(doc, open(TARGET, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print("Agregados:", sorted(added))
    print("Serie final:", [r["year"] for r in doc["serie"]])
    for r in doc["serie"]:
        p = " PARCIAL" if r.get("parcial") else ""
        print(f"  {r['year']}: PIM {r['pim']/1e6:.1f}M  dev {r['dev']/1e6:.1f}M  {r['ejec_pct']}%{p}")

if __name__ == "__main__":
    main()
