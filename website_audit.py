#!/usr/bin/env python3
"""Step 3 (v1.1): conservative, robots-aware website signals for manually reviewed manufacturers.

No Tavily credits, credentials, browser automation, or automatic verification.
All derived fields are *hints*, never ground truth.
"""
from __future__ import annotations

import argparse
import csv
import http.client
import re
import ssl
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.request import Request
from safe_http import urlopen
from url_policy import public_url as validated_public_url
from urllib.robotparser import RobotFileParser

AGENT = "MultilingualResearchAudit/1.0 (public business research; respects robots.txt)"
MAX_HTML_BYTES = 650_000
MAX_TEXT_CHARS = 40_000

# Keyword families are *signals*. They do not prove a company owns or operates a plant.
MANUFACTURING = {
    "manufacturing plant": r"\bmanufacturing (?:plants?|facilit(?:y|ies))\b",
    "dairy processing": r"\bdairy process(?:ing|or)\b",
    "manufacture": r"\bmanufactur(?:es?|ing|er|ers)\b",
    "production facility": r"\bproduction facilit(?:y|ies)\b",
    "factory": r"\bfactor(?:y|ies)\b",
    "fabrication": r"\bfabric(?:ation|ant|ants|amos|aci[oó]n|aci[oó]nes|ar|amos)\b",
    "usine": r"\busines?\b",
    "laiterie": r"\blaiter(?:ie|ies)\b",
    "transformation du lait": r"\btransformation (?:du lait|laiti[eè]re)\b",
    "planta productiva": r"\bplanta(?:s)? (?:productiva(?:s)?|de (?:producci[oó]n|procesamiento|l[aá]cteos))\b",
    "elaboramos": r"\belabor(?:amos|aci[oó]n|amos nuestros productos)\b",
    "fabricamos": r"\bfabricamos\b",
    "industrial site": r"\bindustrial (?:site|plant)\b",
}
TRADING = {
    "trader": r"\btrader(?:s)?\b",
    "trading": r"\btrading\b",
    "broker": r"\bbroker(?:s|age)?\b",
    "distributor": r"\bdistribut(?:or|ion|ors|eur|eurs|rice|rices|ion)\b",
    "comercializador": r"\bcomercializad(?:or|ora|ores|oras)\b",
    "compraventa": r"\bcompraventa\b",
    "négociant": r"\bn[eé]gociant(?:s)?\b",
}
PRODUCT_CATEGORIES = {
    "Cheese": r"\b(?:cheese|fromages?|quesos?)\b",
    "Butter": r"\b(?:butter|beurre|mantequilla)\b",
    "Milk Powder": r"\b(?:milk powder|powdered milk|leche en polvo|lait en poudre|poudre de lait)\b",
    "Yogurt": r"\b(?:yogh?urt|yaourt|yogur(?:t|es)?)\b",
    "Cream": r"\b(?:cream|cr[eè]me|crema)\b",
    "Milk": r"\b(?:milk|leche|lait)\b",
    "Dairy Ingredients": r"\b(?:dairy ingredients|ingr[eé]dients laitiers|ingredientes l[aá]cteos|whey|lactose|lactos[eé]rum)\b",
}
DAIRY = {
    "dairy": r"\bdairy\b",
    "milk": r"\bmilk\b",
    "cheese": r"\bcheese\b",
    "butter": r"\bbutter\b",
    "yogurt": r"\byogh?urt\b",
    "cream": r"\bcream\b",
    "lacteos": r"\bl[aá]cteos?\b",
    "leche": r"\bleche\b",
    "queso": r"\bquesos?\b",
    "mantequilla": r"\bmantequilla\b",
    "yogur": r"\byogur(?:t|es)?\b",
    "lait": r"\blait\b",
    "fromage": r"\bfromages?\b",
    "beurre": r"\bbeurre\b",
    "yaourt": r"\byaourts?\b",
    "crème": r"\bcr[eè]me\b",
    "lactose": r"\blactose\b",
}


class TextExtractor(HTMLParser):
    """Extract visible-ish HTML text; not JS rendered; no scripts/styles."""
    IGNORE = {"script", "style", "noscript", "svg", "iframe", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.bits: list[str] = []
        self.title_bits: list[str] = []
        self.in_title = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORE:
            self.depth += 1
        if tag == "title":
            self.in_title += 1

    def handle_endtag(self, tag):
        if tag in self.IGNORE:
            self.depth = max(0, self.depth - 1)
        if tag == "title":
            self.in_title = max(0, self.in_title - 1)

    def handle_data(self, data):
        if not self.depth:
            self.bits.append(data)
            if self.in_title:
                self.title_bits.append(data)


def extract_text(html: str) -> tuple[str, str]:
    p = TextExtractor()
    p.feed(html)
    txt = re.sub(r"\s+", " ", " ".join(p.bits)).strip()[:MAX_TEXT_CHARS]
    title = re.sub(r"\s+", " ", " ".join(p.title_bits)).strip()[:130]
    return txt, title


def matches(text: str, dictionary: dict[str, str]) -> list[str]:
    return [name for name, expr in dictionary.items() if re.search(expr, text, flags=re.I)]


def clean_url(url: str) -> str:
    url = validated_public_url(url or "")
    if not url: return ""
    return urldefrag(url)[0]


@dataclass
class FetchResult:
    url: str
    status: str
    http_code: str = ""
    title: str = ""
    text: str = ""
    detail: str = ""


class PublicFetcher:
    def __init__(self, timeout: int, delay: float):
        self.timeout = timeout
        self.delay = delay
        self.robots: dict[str, tuple[RobotFileParser | None, str]] = {}
        self.cache: dict[str, FetchResult] = {}
        self.last_request: dict[str, float] = {}
        self.context = ssl.create_default_context()

    def pause(self, host: str):
        elapsed = time.monotonic() - self.last_request.get(host, -100_000)
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_request[host] = time.monotonic()

    def robots_permission(self, url: str) -> tuple[bool, str]:
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in self.robots:
            robots_url = root + "/robots.txt"
            self.pause(p.netloc)
            try:
                request = Request(robots_url, headers={"User-Agent": AGENT, "Accept": "text/plain"})
                # Applying the content robots policy here would recurse into robots.txt retrieval.
                with urlopen(request, timeout=self.timeout, context=self.context) as response:
                    chunk = response.read(150_001)
                    if len(chunk) > 150_000:
                        self.robots[root] = (None, "robots_too_large")
                        return False, "robots_too_large"
                    content = chunk.decode("utf-8", errors="replace")
                    if content.lstrip().startswith("<"):
                        self.robots[root] = (None, "robots_invalid_response")
                        return False, "robots_invalid_response"
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(content.splitlines())
                self.robots[root] = (parser, "robots_checked")
            except HTTPError as exc:
                # A missing robots.txt imposes no robots.txt restriction. Other errors are unknown/deny.
                self.robots[root] = (None, "no_robots_file") if exc.code in (404, 410) else (None, f"robots_http_{exc.code}")
            except (URLError, OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
                self.robots[root] = (None, "robots_fetch_error")
        parser, status = self.robots[root]
        if status == "no_robots_file":
            return True, status
        if not parser:
            return False, status
        return parser.can_fetch(AGENT, url), status

    def fetch(self, given_url: str) -> FetchResult:
        url = clean_url(given_url)
        if not url:
            return FetchResult(given_url, "missing_or_invalid_url")
        if url in self.cache:
            return self.cache[url]
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            result = FetchResult(url, "unsupported_url", detail="Only public website URLs may be audited")
            self.cache[url] = result
            return result
        allowed, robots_status = self.robots_permission(url)
        if not allowed:
            result = FetchResult(url, "robots_disallow" if robots_status == "robots_checked" else "robots_unavailable", detail=robots_status)
            self.cache[url] = result
            return result
        self.pause(parsed.netloc)
        req = Request(url, headers={"User-Agent": AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"})
        try:
            with urlopen(req, timeout=self.timeout, context=self.context,
                         redirect_policy=lambda target: self.robots_permission(target)[0]) as resp:
                code = resp.getcode()
                kind = resp.headers.get("Content-Type", "").lower()
                final_url = resp.geturl()
                if not kind.startswith(("text/html", "application/xhtml+xml")):
                    result = FetchResult(url, "non_html", str(code), detail=f"Content-Type: {kind[:90]}")
                elif urlparse(final_url).scheme not in ("https", "http"):
                    result = FetchResult(url, "unsupported_redirect", str(code))
                else:
                    chunk = resp.read(MAX_HTML_BYTES + 1)
                    if len(chunk) > MAX_HTML_BYTES:
                        result = FetchResult(url, "too_large", str(code), detail="HTML exceeds safe preview limit")
                    else:
                        charset = resp.headers.get_content_charset() or "utf-8"
                        html = chunk.decode(charset, errors="replace")
                        text, title = extract_text(html)
                        result = FetchResult(url, "ok" if text else "empty_page", str(code), title, text)
        except HTTPError as exc:
            result = FetchResult(url, f"http_{exc.code}", str(exc.code), detail="HTTP error; not proof that site is down for human visitors")
        except (URLError, OSError, TimeoutError) as exc:
            result = FetchResult(url, "network_error", detail=type(exc).__name__)
        except LookupError:
            result = FetchResult(url, "encoding_error")
        except (http.client.HTTPException, ValueError):
            result = FetchResult(url, "invalid_response")
        self.cache[url] = result
        return result


class DemoFetcher:
    """Entirely offline fixture content; fictional companies only."""
    FIXTURES = {
        "https://demo.invalid/andes": "<html><title>Andes Demo Dairy (fictional)</title><body>Chile dairy processing plant. We manufacture cheese and butter at our factory.</body></html>",
        "https://demo.invalid/trader": "<html><title>Southern Demo Trading (fictional)</title><body>We are a global milk ingredients trader and distributor for customers.</body></html>",
        "https://demo.invalid/duplicate": "<html><title>Andes Demo Foodservice (fictional)</title><body>Our professional dairy products include milk, cream and cheese.</body></html>",
    }

    def fetch(self, url: str) -> FetchResult:
        u = clean_url(url)
        html = self.FIXTURES.get(u)
        if html is None:
            return FetchResult(u or url, "fixture_missing")
        text, title = extract_text(html)
        return FetchResult(u, "ok", "200", title, text, "Offline fictional fixture")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"Candidate ID", "Company / Group", "Target Country", "Official Website", "Project Eligibility"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"Unexpected CSV columns. Missing: {sorted(required - set(reader.fieldnames or []))}. "
                             "Export the '03 - Candidate Review' tab as CSV (not the original triage sheet).")
        return [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader if row.get("Candidate ID")]


def demo_rows() -> list[dict[str, str]]:
    return [
        {"Candidate ID": "DEMO-001", "Company / Group": "Andes Demo Dairy", "Target Country": "Chile", "Official Website": "https://demo.invalid/andes", "Manufacturing Evidence URL": "", "Products Evidence URL": "", "Public Contact Page": "", "Project Eligibility": "Eligible", "Manufacturing Confirmed": "Yes", "Group Key": "Andes Demo Dairy", "Independent QA": "Supported"},
        {"Candidate ID": "DEMO-002", "Company / Group": "Southern Demo Trading", "Target Country": "Chile", "Official Website": "https://demo.invalid/trader", "Manufacturing Evidence URL": "", "Products Evidence URL": "", "Public Contact Page": "", "Project Eligibility": "Excluded - not manufacturer", "Manufacturing Confirmed": "No", "Group Key": "Southern Demo Trading", "Independent QA": "Supported"},
        {"Candidate ID": "DEMO-003", "Company / Group": "Andes Demo Foodservice", "Target Country": "Chile", "Official Website": "https://demo.invalid/duplicate", "Manufacturing Evidence URL": "", "Products Evidence URL": "", "Public Contact Page": "", "Project Eligibility": "Excluded - duplicate", "Manufacturing Confirmed": "Not separate", "Group Key": "Andes Demo Dairy", "Independent QA": "Supported"},
    ]


def audit(rows: list[dict[str, str]], fetcher, max_pages: int) -> tuple[list[dict[str,str]], list[dict[str,str]]]:
    group_keys = Counter(r.get("Group Key", "").strip().casefold() for r in rows if r.get("Group Key", "").strip())
    report: list[dict[str, str]] = []
    followups: list[dict[str, str]] = []
    for i, row in enumerate(rows, start=1):
        cid = row.get("Candidate ID", "")
        company = row.get("Company / Group", "")
        print(f"[{i}/{len(rows)}] {cid}: {company}", flush=True)
        slots = [
            ("Homepage", row.get("Official Website", "")),
            ("Manufacturing Evidence", row.get("Manufacturing Evidence URL", "")),
            ("Products Evidence", row.get("Products Evidence URL", "")),
            ("Public Contact", row.get("Public Contact Page", "")),
        ]
        inspected = []
        seen = set()
        for label, original in slots:
            url = clean_url(original)
            if not url or url in seen:
                continue
            seen.add(url)
            if len(inspected) >= max_pages:
                break
            f = fetcher.fetch(url)
            manufacturing = matches(f.text, MANUFACTURING) if f.status == "ok" else []
            trading = matches(f.text, TRADING) if f.status == "ok" else []
            dairy = matches(f.text, DAIRY) if f.status == "ok" else []
            inspected.append({"label": label, "url": url, "fetch": f, "manufacturing": manufacturing,
                              "trading": trading, "dairy": dairy})
        good = [item for item in inspected if item["fetch"].status == "ok"]
        statuses = [item["fetch"].status for item in inspected]
        man_signals = sorted(set(k for item in good for k in item["manufacturing"]))
        trading_signals = sorted(set(k for item in good for k in item["trading"]))
        dairy_signals = sorted(set(k for item in good for k in item["dairy"]))
        product_categories = sorted(set(k for item in good for k in matches(item["fetch"].text, PRODUCT_CATEGORIES)))
        slot_outcomes = {item["label"]: item["fetch"].status for item in inspected}
        if man_signals and dairy_signals and not trading_signals:
            hint = "manufacturing_signals_found"
        elif trading_signals and not man_signals:
            hint = "trading_signals_found"
        elif good:
            hint = "mixed_or_insufficient_signals"
        else:
            hint = "no_text_accessed"
        # A *partial* retrieval is important, even when the other pages contain
        # strong manufacturing keywords. The old implementation only flagged
        # a company if *all* pages failed or its keywords were ambiguous.
        incomplete = [item for item in inspected if item["fetch"].status != "ok"]
        failure_summary = "; ".join(
            f"{item['label']}: {item['fetch'].status}" for item in incomplete
        )
        previous_qa = row.get("Independent QA", "").strip()
        was_access_limited = "access limited" in previous_qa.casefold()
        access_issue_now_cleared = was_access_limited and bool(inspected) and not incomplete
        group = row.get("Group Key", "").strip()
        group_count = group_keys.get(group.casefold(), 0) if group else 0
        note_parts = []
        if not good:
            note_parts.append("No machine-readable HTML retrieved; retain human verification.")
        if any(s in {"robots_disallow", "robots_unavailable"} for s in statuses):
            note_parts.append("Robots restriction/uncertainty: no bypass attempted.")
        if any(s.startswith("http_") or s=="network_error" for s in statuses):
            note_parts.append("Network/HTTP errors do not imply that the page is inaccessible to a human.")
        if incomplete:
            note_parts.append(f"Not all attempted pages yielded readable HTML ({failure_summary}).")
            note_parts.append("A non-HTML result may be a valid PDF, and an HTTP/robots error is not proof that a browser link is broken.")
        if access_issue_now_cleared:
            note_parts.append("Previous access limitation is not reproduced in this run; refresh the QA annotation if appropriate.")
        if group_count > 1:
            note_parts.append("Repeated group key: compare business entities; not automatically a duplicate.")
        if row.get("Manufacturing Confirmed", "").lower() == "no" and man_signals:
            note_parts.append("Manufacturer keywords conflict with manual classification; investigate context.")
        if row.get("Manufacturing Confirmed", "").lower() == "yes" and not man_signals:
            note_parts.append("No manufacturing keyword found in retrieved text; NOT evidence of non-manufacturing.")
        note_parts.append("Automated keywords never establish manufacturing, geography, or entity identity.")
        result = {
            "Candidate ID": cid,
            "Company": company,
            "Target Country": row.get("Target Country", ""),
            "Manual Eligibility": row.get("Project Eligibility", ""),
            "Manual Manufacturing Confirmed": row.get("Manufacturing Confirmed", ""),
            "Group Key": group,
            "Other Rows With Same Group Key": str(max(0, group_count - 1)),
            "Independent QA (manual)": row.get("Independent QA", ""),
            "Automated Role Hint": hint,
            "Retrieved HTML Pages": str(len(good)),
            "Pages Attempted": str(len(inspected)),
            "Pages Without Readable HTML": str(len(incomplete)),
            "Unparsed Page Details": failure_summary,
            "Page Outcomes": " | ".join(f"{p['label']}: {p['fetch'].status}" for p in inspected),
            "Homepage Outcome": slot_outcomes.get("Homepage", "not_requested"),
            "Manufacturing Source Outcome": slot_outcomes.get("Manufacturing Evidence", "not_requested"),
            "Products Source Outcome": slot_outcomes.get("Products Evidence", "not_requested"),
            "Contact Source Outcome": slot_outcomes.get("Public Contact", "not_requested"),
            "Detected Product Categories (hints)": "; ".join(product_categories),
            "Manufacturing Signals": "; ".join(man_signals),
            "Trading Signals": "; ".join(trading_signals),
            "Dairy Signals": "; ".join(dairy_signals),
            "Homepage Title": next((p["fetch"].title for p in inspected if p["label"] == "Homepage"), ""),
            "Automated QA Notes": " ".join(note_parts),
            "Audited At (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        report.append(result)
        reasons = []
        next_steps = []
        if incomplete:
            reasons.append("At least one selected source yielded no readable HTML: " + failure_summary)
            next_steps.append("Open the listed source(s) manually, or identify whether they are PDFs or robots-restricted; keep existing human verification.")
        if access_issue_now_cleared:
            reasons.append("An older independent-QA note says automated access was limited, but every attempted source returned readable HTML in this run.")
            next_steps.append("Review the old access-limit annotation; mark the new successful retrieval without changing the original human verification.")
        if not good:
            reasons.append("No readable HTML was obtained from the selected sources.")
            next_steps.append("Check source access in a browser and preserve manual evidence.")
        if hint == "mixed_or_insufficient_signals":
            reasons.append("Keyword-based role classification is inconclusive (not proof of ineligibility).")
            next_steps.append("Read the manufacturing evidence in context; do not classify solely from keywords.")
        if row.get("Manufacturing Confirmed", "").lower() == "no" and man_signals:
            reasons.append("Manufacturing keywords conflict with the existing human 'No' classification.")
            next_steps.append("Check whether the text refers to partners, customers or the actual company.")
        if group_count > 1:
            reasons.append("Repeated corporate group key; possible duplicate business unit.")
            next_steps.append("Check parent/subsidiary relationship and count the group only once.")
        if reasons:
            followups.append({
                "Candidate ID": cid,
                "Company": company,
                "Manual Eligibility": row.get("Project Eligibility", ""),
                "Reason for Manual Review": "; ".join(reasons),
                "Suggested Next Step": " ".join(dict.fromkeys(next_steps)),
            })
    return report, followups


def write_csv(path: Path, rows: list[dict[str, str]], headers: list[str]):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(folder: Path, report: list[dict[str, str]], followups: list[dict[str, str]], demo: bool):
    folder.mkdir(parents=True, exist_ok=True)
    report_cols = list(report[0]) if report else ["Candidate ID", "Company"]
    write_csv(folder / "audit_results.csv", report, report_cols)
    write_csv(folder / "manual_followups.csv", followups,
              ["Candidate ID", "Company", "Manual Eligibility", "Reason for Manual Review", "Suggested Next Step"])
    eligibility = Counter(x["Manual Eligibility"] for x in report)
    hints = Counter(x["Automated Role Hint"] for x in report)
    md = ["# Website audit — automated quality-control signals", "",
          "**Mode:** Offline fictional demonstration" if demo else "**Mode:** Public website fetch (robots-aware)",
          f"**Records reviewed:** {len(report)}", "",
          "## Manually established eligibility (ground truth for comparison)"]
    md += [f"- {k}: {v}" for k,v in sorted(eligibility.items())]
    md += ["", "## Automated HTML signals (NOT verified facts)"]
    md += [f"- {k}: {v}" for k,v in sorted(hints.items())]
    md += ["", f"**Cases worth review or QA-note refresh:** {len(followups)}",
           f"**Candidates with partial HTML retrieval:** {sum(int(r['Pages Without Readable HTML']) > 0 for r in report)}", "",
           "## Interpretation", "",
           "The script extracts keyword signals from public HTML where permitted. It does **not** independently confirm legal identity, manufacturing sites, country of operation or real-world contact pages. A network/robots failure means only that the automated request failed or was not made.",
           "Human verification, source citations and deduplication decisions remain authoritative.", "",
           "## Outputs", "", "- `audit_results.csv`: per-company signals and page outcomes.",
           "- `manual_followups.csv`: partial retrievals, ambiguous signals, duplicates or QA notes that may need updating.",
           "", "A partially readable website can still be flagged. This is an automation-coverage note, not a reversal of human verification.", ""]
    (folder / "audit_summary.md").write_text("\n".join(md), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a candidate review CSV and audit public HTML signals safely (no API credits).")
    parser.add_argument("--input", type=Path, default=Path("input/candidate_review.csv"), help="CSV exported from '03 - Candidate Review'")
    parser.add_argument("--out", type=Path, default=None, help="Output folder; default output/website_audit or output/website_audit_DEMO")
    parser.add_argument("--max-pages", type=int, choices=(1, 2, 3, 4), default=3, help="Distinct URLs to inspect per company, max 4")
    parser.add_argument("--timeout", type=int, default=10, help="Per-request timeout in seconds (3-40)")
    parser.add_argument("--delay", type=float, default=1.2, help="Minimum delay per domain in seconds (>= 1)")
    parser.add_argument("--demo", action="store_true", help="Offline fictional test, no network access")
    args = parser.parse_args()
    if not 3 <= args.timeout <= 40 or args.delay < 1:
        parser.error("--timeout must be 3-40 and --delay at least 1 second")
    if args.demo:
        rows = demo_rows()
        fetcher = DemoFetcher()
    else:
        if not args.input.exists():
            print(f"ERROR: CSV not found: {args.input}. Export Google Sheets tab '03 - Candidate Review' as CSV.", file=sys.stderr)
            return 2
        try:
            rows = read_rows(args.input)
        except (UnicodeError, OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        fetcher = PublicFetcher(timeout=args.timeout, delay=args.delay)
    output = args.out or Path("output/website_audit_DEMO" if args.demo else "output/website_audit")
    report, followups = audit(rows, fetcher, args.max_pages)
    write_outputs(output, report, followups, args.demo)
    print(f"Done: {len(report)} candidates, {len(followups)} flagged for follow-up.")
    print(f"Results: {output.resolve()}")
    print("No Google Sheets cell was modified. No Tavily credits used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
