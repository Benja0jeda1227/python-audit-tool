"""Starter de investigación empresarial multilingüe.

Busca URLs candidatas con Tavily; NO considera resultados verificados.
Ejecuta: python research.py --demo  o  python research.py
"""
from __future__ import annotations
    
import argparse
import csv
import os
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

SEARCH_QUERIES = {
    "France": [
        "fabricants de produits laitiers France site officiel entreprise",
        "fabricant fromage beurre entreprise française site officiel",
    ],
    "United States": [
        "US dairy food manufacturers official company website",
        "United States cheese butter producer manufacturer official website",
    ],
    "Chile": [
        "fabricantes productos lácteos Chile sitio oficial empresa",
        "empresas productoras queso mantequilla Chile sitio web oficial",
    ],
}
LANGUAGE = {"France": "French", "United States": "English", "Chile": "Spanish"}

FIELDS = [
    "Candidate ID",
    "Country",
    "Source Language",
    "Search Query",
    "Search Result URL",
    "Result Title",
    "Company Name (manual)",
    "Official Website (manual)",
    "Product Category (manual)",
    "Evidence URL (manual)",
    "Public Contact Page (manual)",
    "Verification Status",
    "Reviewed On",
    "Research Notes",
]


def canonical_url(value: str) -> str:
    """Normaliza URL solamente para deduplicación; no verifica el sitio."""
    try:
        p = urlsplit(value.strip())
        host = (p.hostname or "").lower().removeprefix("www.")
        if p.scheme not in ("http", "https") or not host:
            return ""
        path = p.path.rstrip("/") or "/"
        # Eliminar tracking de consulta y fragmentos. No altera URLs de salida.
        return urlunsplit(("https", host, path, "", ""))
    except ValueError:
        return ""


def demo_search(query: str, country: str, limit: int) -> list[dict[str, str]]:
    # Ejemplos ficticios: exclusivamente para comprobar que el programa funciona.
    key = country.lower().replace(" ", "-")
    return [
        {"url": f"https://example.com/demo-{key}-{i}", "title": f"DEMO {country} candidate {i}"}
        for i in range(1, min(limit, 3) + 1)
    ]


def collect(client, *, demo: bool, queries_per_country: int, results_per_query: int):
    records = []
    seen = set()
    errors = []
    for country, queries in SEARCH_QUERIES.items():
        for query in queries[:queries_per_country]:
            print(f"Buscando {country}: {query}")
            try:
                if demo:
                    items = demo_search(query, country, results_per_query)
                else:
                    response = client.search(
                        query=query,
                        max_results=results_per_query,
                        search_depth="basic",
                        include_raw_content=False,
                        include_images=False,
                    )
                    items = response.get("results", [])
            except Exception as exc:
                msg = f"{country}: {type(exc).__name__}: {exc}"
                errors.append(msg)
                print(f"  ERROR: {msg}")
                continue

            added = 0
            for item in items:
                original_url = str(item.get("url", "")).strip()
                key = canonical_url(original_url)
                if not key or key in seen:
                    continue
                seen.add(key)
                added += 1
                records.append({
                    "Candidate ID": f"C-{len(records)+1:03d}",
                    "Country": country,
                    "Source Language": LANGUAGE[country],
                    "Search Query": query,
                    "Search Result URL": original_url,
                    "Result Title": str(item.get("title", "")),
                    "Company Name (manual)": "",
                    "Official Website (manual)": "",
                    "Product Category (manual)": "",
                    "Evidence URL (manual)": "",
                    "Public Contact Page (manual)": "",
                    "Verification Status": "DEMO - NOT REAL" if demo else "Needs review",
                    "Reviewed On": "",
                    "Research Notes": "Fictitious test example; never publish" if demo else "",
                })
            print(f"  {len(items)} resultados; {added} URLs nuevas.")
            if not demo:
                time.sleep(1)
    return records, errors


def save(records, errors, outdir: Path, demo: bool):
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / ("candidates_DEMO.csv" if demo else "candidates.csv")
    with target.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)

    by_country = Counter(row["Country"] for row in records)
    report = [
        "RESUMEN DE DESCUBRIMIENTO - NO ES UNA LISTA VERIFICADA",
        f"Fecha: {date.today().isoformat()}",
        f"Modo: {'DEMO / DATOS FICTICIOS' if demo else 'API REAL'}",
        f"URLs candidatas únicas: {len(records)}",
    ]
    report += [f"{country}: {by_country[country]}" for country in SEARCH_QUERIES]
    report += [f"Errores: {len(errors)}"]
    report.extend(errors)
    report += [
        "",
        "IMPORTANTE: estas URLs pueden apuntar a directorios, noticias u otras páginas.",
        "Investiga manualmente nombre, actividad, sitio oficial y fuentes antes de marcar Verified.",
        "No afirmes cobertura completa del sector ni obtención de 30 empresas verificadas.",
    ]
    summary = outdir / ("summary_DEMO.txt" if demo else "summary.txt")
    summary.write_text("\n".join(report) + "\n", encoding="utf-8")
    return target, summary


def main():
    parser = argparse.ArgumentParser(description="Descubrir candidatos para investigación multilingüe")
    parser.add_argument("--demo", action="store_true", help="Usa ejemplos inventados y no consume créditos")
    parser.add_argument("--queries-per-country", type=int, choices=(1, 2), default=1)
    parser.add_argument("--results-per-query", type=int, choices=range(1, 11), metavar="1..10", default=8)
    args = parser.parse_args()

    client = None
    if not args.demo:
        load_dotenv()
        key = os.getenv("TAVILY_API_KEY", "").strip()
        if not key:
            raise SystemExit("Falta TAVILY_API_KEY. Crea .env desde .env.example, o usa --demo primero.")
        from tavily import TavilyClient
        client = TavilyClient(api_key=key)

    records, errors = collect(
        client,
        demo=args.demo,
        queries_per_country=args.queries_per_country,
        results_per_query=args.results_per_query,
    )
    target, summary = save(records, errors, Path("output"), args.demo)
    print(f"\nArchivo generado: {target}")
    print(f"Resumen: {summary}")
    print(f"Candidatos (sin verificar): {len(records)}")
    if not records:
        print("No se obtuvieron candidatos. Revisa errores en el resumen y prueba una consulta alternativa.")


if __name__ == "__main__":
    main()
