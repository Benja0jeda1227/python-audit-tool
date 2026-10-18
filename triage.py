r"""Clasificación preliminar de resultados de búsqueda, SIN consultas a Internet ni API.

No verifica que una empresa fabrique, opere en un país o sea una entidad única.
Usa review_notes.csv como anotaciones manuales enlazadas por URL, no por Candidate ID.

Uso (desde la raíz del starter):
    .\.venv\Scripts\python.exe triage.py
    .\.venv\Scripts\python.exe triage.py --input output/candidates.csv
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from url_policy import normalize_url, public_url

LABELS = {"Manufacturer candidate", "Needs further review", "Discovery source"}
NEW_FIELDS = ["Auto Hint", "Initial Category", "Reason (preliminary)", "Next Verification Step"]


def canonical_url(value: str) -> str:
    """Normalización para comparar enlaces de la misma página (no identifica empresas)."""
    return normalize_url(value)


def automatic_hint(url: str, title: str) -> str:
    """Pista basada en texto/URL; nunca equivale a validación factual."""
    if not public_url(url): return "Invalid or unsafe URL — inspect input"
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    content = (title + " " + url).lower()
    if any(host.endswith(domain) for domain in
           ("wikipedia.org", "facebook.com", "instagram.com", "youtube.com")):
        return "Reference/social site (not proof of a company)"
    if host.endswith((".gov", ".gouv.fr", ".gob.cl")):
        return "Government/register source (inspect entries)"
    directory_terms = (
        "directory", "supplier-search", "ranking", "classement", "top 100",
        "magazine", "list_of", "lista de", "categoría:", "category:",
        "entreprises/", "proveedor de", "fournisseurs certifiés",
    )
    if any(term in content for term in directory_terms):
        return "Directory/list/editorial page (inspect entries)"
    return "Company or sector website? Inspect manually"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(path: Path, records: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Preparar cola de revisión de empresas y fuentes")
    parser.add_argument("--input", type=Path, default=Path("output/candidates.csv"))
    parser.add_argument("--notes", type=Path, default=Path("review_notes.csv"))
    parser.add_argument("--outdir", type=Path, default=Path("output"))
    args = parser.parse_args()
    if not args.input.exists():
        parser.error(f"No se encuentra {args.input}; ejecuta research.py o usa --input")
    if not args.notes.exists():
        parser.error(f"No se encuentra {args.notes}; copia review_notes.csv a esta carpeta")

    source = read_rows(args.input)
    annotations = read_rows(args.notes)
    annotation_map = {canonical_url(r["Search Result URL"]): r for r in annotations if canonical_url(r["Search Result URL"])}

    combined = []
    for original in source:
        result = dict(original)
        result["Auto Hint"] = automatic_hint(result.get("Search Result URL", ""), result.get("Result Title", ""))
        match = annotation_map.get(canonical_url(result.get("Search Result URL", "")))
        if match:
            for field in NEW_FIELDS:
                if field == "Auto Hint":
                    continue
                result[field] = match.get(field, "")
        else:
            result.update({
                "Initial Category": "Needs further review",
                "Reason (preliminary)": "URL nueva: no ha sido evaluada todavía",
                "Next Verification Step": "Inspeccionar sitio y definir tipo de resultado",
            })
        result["Verification Status"] = original.get("Verification Status") or "Needs review"
        combined.append(result)

    first_columns = list(source[0].keys()) if source else ["Search Result URL"]
    columns = first_columns + [name for name in NEW_FIELDS if name not in first_columns]
    args.outdir.mkdir(parents=True, exist_ok=True)
    save_csv(args.outdir / "candidate_triage.csv", combined, columns)

    sources = [r for r in combined if r["Initial Category"] == "Discovery source"]
    candidates = [r for r in combined if r["Initial Category"] != "Discovery source"]
    rank = {"Manufacturer candidate": 0, "Needs further review": 1}
    candidates.sort(key=lambda r: (rank.get(r["Initial Category"], 2), r.get("Country", ""), r.get("Candidate ID", "")))
    save_csv(args.outdir / "manufacturer_review_queue.csv", candidates, columns)
    save_csv(args.outdir / "discovery_sources.csv", sources, columns)

    counts = Counter(r["Initial Category"] for r in combined)
    summary = [
        "CLASIFICACIÓN PRELIMINAR. NINGUNA EMPRESA HA SIDO VERIFICADA POR ESTE SCRIPT.",
        f"Fecha de clasificación: {date.today().isoformat()}",
        f"Resultados totales: {len(combined)}",
    ]
    summary.extend(f"{label}: {counts[label]}" for label in ("Manufacturer candidate", "Needs further review", "Discovery source"))
    summary += [
        "",
        "Los resultados de esta clasificación son anotaciones preliminares.",
        "'Manufacturer candidate' NO implica verificación de fabricación o país.",
        "Revisa fuentes oficiales, evita duplicados por grupo y documenta evidencia antes de marcar Verified.",
        "Si vuelves a ejecutar research.py, tus anotaciones en review_notes.csv permanecen separadas.",
    ]
    (args.outdir / "triage_summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(f"Procesados: {len(combined)} enlaces")
    for key in ("Manufacturer candidate", "Needs further review", "Discovery source"):
        print(f"  {key}: {counts[key]}")
    print("Archivos generados en", args.outdir)
    print("  candidate_triage.csv")
    print("  manufacturer_review_queue.csv")
    print("  discovery_sources.csv")
    print("  triage_summary.txt")
    print("IMPORTANTE: ninguna empresa fue verificada automáticamente.")


if __name__ == "__main__":
    main()
