#!/usr/bin/env python3
"""Website Audit v2: auditable evidence, multilingual role hints and QA rules.

Import compatible v1.1 fetcher to avoid a breaking change. Requires ONLY Python
standard library and website_audit.py v1.1 in the same folder.

NOT an autonomous verifier. Uses conservative HTML pattern matching to surface
quotes and follow-ups. Human conclusions in the input CSV are kept unchanged.
"""
from __future__ import annotations

import argparse
import csv
import ipaddress
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import website_audit as base  # v1.1: robots-aware fetcher, CSV parser, URL cleaning

VERSION = "2.0.0"
MAX_SNIPPETS_PER_RULE_PER_PAGE = 3
MAX_SNIPPETS_PER_PAGE = 30
# Captures short sentences with punctuation as context; long paragraphs are split.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[\wÀ-ÿ\"'¿¡])")
MFG_RE = re.compile(r"\b(?:manufactur(?:e|es|ed|ing|er|ers)|processing|production|produc(?:e|es|ed|ing|tion)|factory|factories|plants?|facilit(?:y|ies)|fabric(?:a|an|amos|aci[oó]n|aci[oó]nes|ante(?:s)?|ar)|elabora(?:mos|n|ci[oó]n)|proces(?:a|amos|amiento)|usines?|fabriqu(?:e|ons|ent)|transform(?:ation|ons|e|er)|produi(?:t|sons|sent|re))\b", re.I)
DAIRY_RE = re.compile(r"\b(?:dairy|milk|cheese|butter|yogur(?:t|h)?|cream|lait|laiti[eè]r(?:e|es)|fromage|beurre|cr[eè]me|leche|queso|mantequilla|l[aá]cte(?:o|a|os|as))\b", re.I)
# Product hints: deliberately not proof of manufacturing.
PRODUCTS = {
    "Cheese": r"\b(?:cheese|fromages?|quesos?)\b",
    "Butter": r"\b(?:butter|beurre|mantequilla)\b",
    "Milk": r"\b(?:milk|lait|leche)\b",
    "Yogurt": r"\b(?:yogh?urt|yaourts?|yogur(?:t|es)?)\b",
    "Cream": r"\b(?:cream|cr[eè]me|crema)\b",
    "Milk Powder": r"\b(?:milk powder|powdered milk|leche en polvo|lait en poudre|poudre de lait)\b",
    "Dairy Ingredients": r"\b(?:dairy ingredients|milk proteins?|whey|lactose|ingr[eé]dients? laitiers?|ingredientes? l[aá]cteos?)\b",
}
# Terms claiming that the company itself manufactures/operates plants.
SELF_MFG = [
    ("EN_SELF_MANUFACTURE", re.compile(r"\b(?:we|our company|our group|our dairy|our facilities|our plants?|our factor(?:y|ies)|we own|we operate)\b.{0,110}\b(?:manufactur\w*|process\w*|produc\w*|factor\w*|facilit\w*|plant\w*)", re.I)),
    ("EN_OWNED_PLANTS", re.compile(r"\b(?:our|owned|operat(?:e|es|ing|ed))\b.{0,65}\b(?:manufacturing|processing|production|dairy)\s+(?:plants?|facilit(?:y|ies)|factor(?:y|ies))\b", re.I)),
    ("ES_SELF_MANUFACTURE", re.compile(r"\b(?:fabricamos|elaboramos|producimos|procesamos|nuestras? plantas?|nuestras? f[aá]bricas|contamos con plantas?|operamos plantas?)\b.{0,95}\b(?:leche|l[aá]cteos?|queso|mantequilla|planta|producci[oó]n|fabricaci[oó]n|procesamiento)\b", re.I)),
    ("FR_SELF_MANUFACTURE", re.compile(r"\b(?:nous fabriquons|nous produisons|nous transformons|nos usines?|nos sites? de production|notre laiterie|nous exploitons)\b.{0,110}\b(?:lait|laiti[eè]r\w*|fromage|beurre|cr[eè]me|fabric\w*|produ\w*|transform\w*|usine)\b", re.I)),
]
# Strong direct role evidence without explicitly first-person language.
DIRECT_MFG = [
    ("PLANT_MANUFACTURING", re.compile(r"\b(?:dairy|milk|lait|laiti[eè]re|l[aá]cteos?)\s+(?:manufactur\w*|process\w*|production)\s+(?:plants?|facilit(?:y|ies)|sites?)\b", re.I)),
    ("FACILITY_ACTIVITY", re.compile(r"\b(?:manufacturing|processing|production|dairy)\s+(?:facilit(?:y|ies)|plants?|sites?|factories)\b", re.I)),
    ("ES_PLANTA", re.compile(r"\b(?:planta(?:s)? (?:de producci[oó]n|de procesamiento|productiva(?:s)?|l[aá]cteas?)|f[aá]bricas? (?:de|l[aá]cteas?))\b", re.I)),
    ("FR_USINE", re.compile(r"\b(?:usines? (?:de production|laiti[eè]res?)|sites? (?:industriels?|de production)|transformation du lait|industrie laiti[eè]re)\b", re.I)),
]
SELF_TRADE = [
    ("EN_TRADER", re.compile(r"\b(?:we are|our company is|our business is|we operate as|we specialize in|we act as)\b.{0,75}\b(?:trader|trading|broker|distribut\w*|wholesal\w*|sourc\w*)\b", re.I)),
    ("EN_TRADING_BUSINESS", re.compile(r"\b(?:global|international|leading)\s+(?:dairy\s+)?(?:commodity\s+)?(?:trader|trading company|broker|distributor)\b", re.I)),
    ("ES_TRADER", re.compile(r"\b(?:somos|empresa|nos dedicamos a|especializados en)\b.{0,75}\b(?:comercializaci[oó]n|comercializador\w*|distribuci[oó]n|distribuidor\w*|intermediaci[oó]n)\b", re.I)),
    ("FR_TRADER", re.compile(r"\b(?:nous sommes|notre soci[eé]t[eé]|nous proposons)\b.{0,75}\b(?:n[eé]goce|n[eé]gociant\w*|distribution|distributeur\w*|courtage)\b", re.I)),
]
# A manufacturer term attached to partners/customers does not establish own manufacturing.
THIRD_PARTY = [
    ("PARTNER_MANUFACTURING", re.compile(r"\b(?:suppliers?|partners?|customers?|clients?|third.part(?:y|ies)|proveedores?|socios?|terceros?|fournisseurs?|partenaires?|fabricantes externos?)\b.{0,100}\b(?:manufactur\w*|produc\w*|fabric\w*|elabor\w*|transform\w*|usines?|planta\w*)\b", re.I)),
    ("CONNECT_MANUFACTURERS", re.compile(r"\b(?:we connect|connecting|source from|we source|sourcing from|connectamos|conectamos|nous relions|fournisseurs de)\b.{0,100}\b(?:manufactur\w*|produc\w*|fabric\w*|supplier\w*|producer\w*)\b", re.I)),
]
# Country labels are contextual HINTS only, never confirmation of geographic eligibility.
COUNTRY_ALIASES = {
    "Chile": ["Chile", "Chilean", "chilena", "chileno", "chiliennes?"],
    "France": ["France", "French", "fran[cç]ais(?:e|es|s)?", "francesa?", "franc[eé]s"],
    "United States": ["United States", "U\\.S\\.A\\.?", "U\\.S\\.?", "USA", "American", "estadounidense(?:s)?", "[eé]tats[- ]unis"],
}


@dataclass(frozen=True)
class Evidence:
    candidate_id: str
    company: str
    source_role: str
    url: str
    kind: str
    rule: str
    excerpt: str
    strength: str

    def as_dict(self) -> dict[str, str]:
        return {
            "Candidate ID": self.candidate_id,
            "Company": self.company,
            "Source Role": self.source_role,
            "Source URL": self.url,
            "Evidence Type": self.kind,
            "Rule ID": self.rule,
            "Strength": self.strength,
            "Exact Text Excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class Alert:
    candidate_id: str
    company: str
    code: str
    priority: str
    reason: str
    source_url: str = ""
    next_step: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "Candidate ID": self.candidate_id,
            "Company": self.company,
            "Alert Code": self.code,
            "Priority": self.priority,
            "Reason": self.reason,
            "Source URL": self.source_url,
            "Recommended Action": self.next_step,
        }


def sentences(text: str) -> list[str]:
    """Short reproducible excerpts. HTML in v1.1 is flattened, so this is heuristic."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    pieces: list[str] = []
    for block in SENTENCE_SPLIT.split(cleaned):
        block = block.strip()
        if len(block) <= 360:
            if block:
                pieces.append(block)
        else:
            # 100-character overlap prevents loss at a long-block boundary.
            step = 260
            pieces.extend(block[start:start + 360].strip() for start in range(0, len(block), step))
    return pieces


def has_one(patterns, text: str):
    for rule, pattern in patterns:
        if pattern.search(text):
            return rule
    return ""


def context_country(text: str, country: str) -> bool:
    aliases = COUNTRY_ALIASES.get(country, [])
    if not aliases or not MFG_RE.search(text):
        return False
    if has_one(THIRD_PARTY, text):
        return False
    return any(re.search(r"\b(?:" + alias + r")\b", text, re.I) for alias in aliases)


def extract_evidence(cid: str, company: str, role: str, url: str, text: str, target_country: str) -> list[Evidence]:
    output: list[Evidence] = []
    num_per_rule = Counter()
    for sentence in sentences(text):
        if not sentence or len(output) >= MAX_SNIPPETS_PER_PAGE:
            break
        excerpt = sentence[:360]
        # Third-party context overrides surface manufacturing vocabulary.
        third = has_one(THIRD_PARTY, sentence)
        self_mfg = has_one(SELF_MFG, sentence)
        direct_mfg = has_one(DIRECT_MFG, sentence)
        self_trade = has_one(SELF_TRADE, sentence)
        kinds = []
        if third:
            kinds.append(("THIRD_PARTY_MANUFACTURING", third, "Strong context"))
        elif self_mfg:
            kinds.append(("OWN_MANUFACTURING_CLAIM", self_mfg, "Company claim"))
        elif direct_mfg:
            kinds.append(("MANUFACTURING_SITE_MENTION", direct_mfg, "Context needed"))
        if self_trade:
            kinds.append(("SELF_TRADER_CLAIM", self_trade, "Company claim"))
        if context_country(sentence, target_country) and (self_mfg or direct_mfg):
            kinds.append(("TARGET_COUNTRY_MANUFACTURING_MENTION", "COUNTRY_NEAR_PRODUCTION", "Context needed"))
        if not kinds and MFG_RE.search(sentence) and DAIRY_RE.search(sentence):
            kinds.append(("AMBIGUOUS_MANUFACTURING_TERM", "GENERIC_DAIRY_MANUFACTURING", "Weak"))
        for category, expr in PRODUCTS.items():
            if re.search(expr, sentence, re.I):
                kinds.append(("PRODUCT_MENTION", category, "Weak"))
        for kind, rule, strength in kinds:
            key = (kind, rule)
            if num_per_rule[key] >= MAX_SNIPPETS_PER_RULE_PER_PAGE:
                continue
            num_per_rule[key] += 1
            output.append(Evidence(cid, company, role, url, kind, rule, excerpt, strength))
    return output


def select_sources(row: dict[str,str], max_pages: int) -> list[tuple[str,str]]:
    slots = [
        ("Homepage", row.get("Official Website", "")),
        ("Manufacturing Evidence", row.get("Manufacturing Evidence URL", "")),
        ("Products Evidence", row.get("Products Evidence URL", "")),
        ("Public Contact", row.get("Public Contact Page", "")),
    ]
    selected, seen = [], set()
    for label, original in slots:
        url = allowed_public_url(original)
        if not url or url in seen:
            continue
        seen.add(url)
        selected.append((label, url))
        if len(selected) >= max_pages:
            break
    return selected


def allowed_public_url(given: str) -> str:
    """Reject obvious local/private URL targets; only audit trusted public URLs.

    This is NOT complete DNS-rebinding / redirect protection. Do not feed the
    script adversarial, untrusted URL lists without a hardened network sandbox.
    """
    url = base.clean_url(given)
    if not url:
        return ""
    host = (urlparse(url).hostname or "").casefold().strip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    return url if ip.is_global else ""


def first_party_domain(official_url: str, source_url: str) -> bool:
    """Narrow first-party match: same host or a subdomain of the official host.

    Other official group-owned domains may be valid but need human attribution.
    """
    basehost = (urlparse(official_url).hostname or "").casefold().removeprefix("www.")
    sourcehost = (urlparse(source_url).hostname or "").casefold().removeprefix("www.")
    return bool(basehost and sourcehost and (sourcehost == basehost or sourcehost.endswith("." + basehost)))


def run_audit(rows: list[dict[str,str]], fetcher, max_pages: int=4):
    group_keys = Counter(r.get("Group Key", "").casefold().strip() for r in rows if r.get("Group Key", "").strip())
    results: list[dict[str,str]] = []
    pages: list[dict[str,str]] = []
    evidence: list[Evidence] = []
    alerts: list[Alert] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    for i, row in enumerate(rows, start=1):
        cid = row.get("Candidate ID", "").strip()
        company = row.get("Company / Group", "").strip()
        country = row.get("Target Country", "").strip()
        manual = row.get("Project Eligibility", "").strip()
        confirmed = row.get("Manufacturing Confirmed", "").strip()
        qa = row.get("Independent QA", "").strip()
        group = row.get("Group Key", "").strip()
        print(f"[{i}/{len(rows)}] {cid}: {company}", flush=True)
        selected = select_sources(row, max_pages)
        current: list[Evidence] = []
        page_rows = []
        for role, url in selected:
            fetched = fetcher.fetch(url)
            official_match = first_party_domain(row.get("Official Website", ""), url)
            if fetched.status == "ok":
                examples = extract_evidence(cid, company, role, url, fetched.text, country)
                if not official_match:
                    # A government registry or other external source speaking in
                    # the first person refers to itself, not necessarily to the
                    # company under investigation. Preserve evidence, not claim.
                    examples = [Evidence(e.candidate_id, e.company, e.source_role, e.url,
                                         "EXTERNAL_SOURCE_ROLE_MENTION" if e.kind == "OWN_MANUFACTURING_CLAIM" else e.kind,
                                         e.rule, e.excerpt, "Requires attribution" if e.kind == "OWN_MANUFACTURING_CLAIM" else e.strength)
                                for e in examples]
                current.extend(examples)
            else:
                examples = []
            page_row = {
                "Candidate ID": cid,
                "Company": company,
                "Source Role": role,
                "URL": url,
                "Fetch Status": fetched.status,
                "HTTP Code": fetched.http_code,
                "Page Title": fetched.title,
                "Same Domain As Official Website": "Yes" if official_match else "No - verify attribution",
                "Matched Excerpts": str(len(examples)),
                "Fetch Detail": fetched.detail,
            }
            pages.append(page_row)
            page_rows.append(page_row)
            if fetched.status != "ok":
                reason = ("Automated fetch did not return readable HTML: " + fetched.status + ". ")
                if role == "Manufacturing Evidence":
                    reason += "Manufacturing source needs browser review or another suitable source."
                else:
                    reason += "This does not invalidate a page checked manually."
                alerts.append(Alert(cid, company, "ACCESS_LIMITED", "Low" if "manual" in qa.casefold() else "Medium", reason, url,
                                    "Check manually only if the source has not already been reviewed; retain human verification."))
        evidence.extend(current)
        kinds = Counter(e.kind for e in current)
        self_mfg = kinds["OWN_MANUFACTURING_CLAIM"] > 0
        site_mention = kinds["MANUFACTURING_SITE_MENTION"] > 0
        self_trader = kinds["SELF_TRADER_CLAIM"] > 0
        third_party = kinds["THIRD_PARTY_MANUFACTURING"] > 0
        geography = kinds["TARGET_COUNTRY_MANUFACTURING_MENTION"] > 0
        available = sum(p["Fetch Status"] == "ok" for p in page_rows)
        attempted = len(page_rows)
        positives = self_mfg or site_mention
        if self_mfg and self_trader:
            role_hint = "Mixed direct manufacturing and trading claims"
        elif self_mfg:
            role_hint = "Direct manufacturing claim found"
        elif site_mention and not third_party:
            role_hint = "Manufacturing site mentioned; owner not established"
        elif self_trader and not positives:
            role_hint = "Trading/distribution self-description"
        elif third_party and not positives:
            role_hint = "Manufacturing mentioned for third parties"
        else:
            role_hint = "Not enough contextual evidence"
        if not attempted:
            alerts.append(Alert(cid, company, "NO_VALID_SOURCES", "High", "No valid public URL was provided for automatic review.", "", "Add a valid corporate or manufacturing evidence URL."))
        if attempted and not available:
            alerts.append(Alert(cid, company, "NO_READABLE_HTML", "Medium", "No selected page returned readable HTML for contextual analysis.", "", "Retain manual verification, or provide an accessible alternative source."))
        if attempted and available == attempted and "access limited" in qa.casefold():
            alerts.append(Alert(cid, company, "QA_ANNOTATION_MAY_BE_STALE", "Low", "Previous QA mentions limited automated access, but every selected HTML page was readable in this run.", "", "Update the automated-access note if desired; preserve the original human verification."))
        if available and confirmed.casefold() == "yes" and not positives:
            alerts.append(Alert(cid, company, "MFG_EVIDENCE_GAP", "Medium", "Manually confirmed manufacturing, but no clear claim of own manufacturing or manufacturing-site mention was extracted.", "", "Look for a company statement naming owned or operated manufacturing plants."))
        if available and confirmed.casefold() == "no" and self_mfg:
            alerts.append(Alert(cid, company, "ROLE_CONFLICT", "High", "Manual classification says not a manufacturer, but a first-party manufacturing phrase appeared (check its context).", "", "Inspect the quoted excerpt for supplier/customer references and correct the role if necessary."))
        if available and confirmed.casefold() == "yes" and self_trader and not positives:
            alerts.append(Alert(cid, company, "ROLE_CONFLICT", "Medium", "Manual classification says manufacturer, while only explicit trading/distribution role statements were found.", "", "Look for owned manufacturing-site evidence, or reconsider the classification."))
        if available and manual == "Eligible" and not geography:
            alerts.append(Alert(cid, company, "GEOGRAPHY_NOT_ESTABLISHED", "Low", f"No explicit production + '{country}' statement was extracted from the inspected HTML. This is not evidence that the company operates elsewhere.", "", "Find direct evidence of production in the target country; a website language or HQ alone does not prove manufacturing location."))
        if manual == "Eligible" and confirmed.casefold() == "no":
            alerts.append(Alert(cid, company, "MANUAL_DATA_INCONSISTENCY", "High", "The researcher marked the company as eligible but manufacturing as No.", "", "Resolve the inconsistent manual fields without automatic overwriting."))
        if group and group_keys[group.casefold()] > 1:
            alerts.append(Alert(cid, company, "POSSIBLE_DUPLICATE", "Medium", f"Corporate group key '{group}' occurs {group_keys[group.casefold()]} times across candidates.", "", "Compare the entities with the same group key and count one company group only if the brief requires it."))
        if not row.get("Public Contact Page", "").strip() and manual == "Eligible":
            alerts.append(Alert(cid, company, "CONTACT_NOT_SUPPLIED", "Low", "No public contact URL was supplied for an eligible company.", "", "Identify an official public contact page, if available; never invent personal contact details."))
        products = sorted({e.rule for e in current if e.kind == "PRODUCT_MENTION"})
        own_urls = list(dict.fromkeys(e.url for e in current if e.kind == "OWN_MANUFACTURING_CLAIM"))
        trading_urls = list(dict.fromkeys(e.url for e in current if e.kind == "SELF_TRADER_CLAIM"))
        subset_alerts = [a for a in alerts if a.candidate_id == cid]
        candidate_priority = "High" if any(a.priority == "High" for a in subset_alerts) else ("Medium" if any(a.priority == "Medium" for a in subset_alerts) else ("Low" if subset_alerts else "None"))
        results.append({
            "Candidate ID": cid,
            "Company": company,
            "Target Country": country,
            "Manual Eligibility (unchanged)": manual,
            "Manual Manufacturing (unchanged)": confirmed,
            "Independent QA (unchanged)": qa,
            "Group Key": group,
            "Machine Role Hint": role_hint,
            "Manufacturing Evidence Basis": "Direct own-activity claim" if self_mfg else ("Site mention without ownership proof" if site_mention else "No direct site claim extracted"),
            "Manufacturing Claimed On": "; ".join(own_urls),
            "Trading Claimed On": "; ".join(trading_urls),
            "Target Country + Production Text Found": "Yes - text hint only" if geography else "Not found in fetched text",
            "Products Mentioned (hints only)": "; ".join(products),
            "HTML Pages Read": str(available),
            "Pages Attempted": str(attempted),
            "Unreadable Pages": str(attempted-available),
            "Alert Count": str(len(subset_alerts)),
            "Alert Codes": "; ".join(dict.fromkeys(a.code for a in subset_alerts)),
            "Highest Follow-up Priority": candidate_priority,
            "Audited At (UTC)": now,
        })
    return results, pages, evidence, alerts


def csv_escape_formula(value):
    """Avoid CSV spreadsheet-formula injection if external pages contain special text."""
    s = str(value if value is not None else "")
    if s.lstrip() and s.lstrip()[0] in "=+-@\t\r":
        return "'" + s
    return s


def write_csv(path: Path, headers: list[str], rows: list[dict[str,str]]):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_escape_formula(v) for k, v in row.items()})


def write_outputs(output: Path, results: list[dict[str,str]], pages: list[dict[str,str]], evidence: list[Evidence], alerts: list[Alert], demo: bool):
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output/"audit_results.csv", list(results[0].keys()) if results else ["Candidate ID", "Company"], results)
    write_csv(output/"page_checks.csv", ["Candidate ID", "Company", "Source Role", "URL", "Fetch Status", "HTTP Code", "Page Title", "Same Domain As Official Website", "Matched Excerpts", "Fetch Detail"], pages)
    write_csv(output/"evidence_snippets.csv", ["Candidate ID", "Company", "Source Role", "Source URL", "Evidence Type", "Rule ID", "Strength", "Exact Text Excerpt"], [e.as_dict() for e in evidence])
    write_csv(output/"alerts_detailed.csv", ["Candidate ID", "Company", "Alert Code", "Priority", "Reason", "Source URL", "Recommended Action"], [a.as_dict() for a in alerts])
    grouped = defaultdict(list)
    for a in alerts:
        grouped[a.candidate_id].append(a)
    candidates = []
    for r in results:
        cid = r["Candidate ID"]
        parts = grouped[cid]
        if not parts:
            continue
        candidates.append({
            "Candidate ID": cid,
            "Company": r["Company"],
            "Manual Eligibility": r["Manual Eligibility (unchanged)"],
            "Priority": r["Highest Follow-up Priority"],
            "Alert Codes": "; ".join(dict.fromkeys(a.code for a in parts)),
            "Reasons": " | ".join(dict.fromkeys(a.reason for a in parts)),
            "Suggested Next Steps": " | ".join(dict.fromkeys(a.next_step for a in parts)),
        })
    pr = {"High": 0, "Medium": 1, "Low": 2}
    candidates.sort(key=lambda x:(pr.get(x["Priority"], 3), x["Company"].casefold()))
    write_csv(output/"manual_followups.csv", ["Candidate ID", "Company", "Manual Eligibility", "Priority", "Alert Codes", "Reasons", "Suggested Next Steps"], candidates)
    role_count = Counter(r["Machine Role Hint"] for r in results)
    codes = Counter(a.code for a in alerts)
    country = Counter(r["Target Country"] for r in results)
    lines = [
        "# WEBSITE AUDIT V2 — Automated quality-control report", "",
        f"Run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Mode: {'OFFLINE fictional demo' if demo else 'Public HTML fetch (robots-aware)'}", "",
        f"Candidates processed: **{len(results)}**",
        f"Candidates with at least one alert: **{len(candidates)}**",
        f"Detailed alerts: **{len(alerts)}**",
        f"Pages attempted: **{len(pages)}**",
        f"Readable HTML pages: **{sum(p['Fetch Status']=='ok' for p in pages)}**", "",
        "## Candidates by target country", "",
    ]
    lines.extend(f"- {k}: {v}" for k,v in sorted(country.items()))
    lines += ["", "## Role hints extracted (NOT company verdicts)", ""]
    lines.extend(f"- {k}: {v}" for k,v in sorted(role_count.items()))
    lines += ["", "## Alert codes", ""]
    lines.extend(f"- {k}: {v}" for k,v in sorted(codes.items()))
    lines += ["", "## Decision policy", "",
              "- Evidence is source-specific and includes an excerpt, the matching rule, and the source URL.",
              "- First-person statements are treated as company claims ONLY on the declared official domain or subdomains; other domains need attribution.",
              "- First-person company statements and direct factory references are weighed differently from mentions about suppliers and partners.",
              "- Geographic production evidence is only a textual HINT; it never independently verifies local manufacturing.",
              "- A timeout, HTTP error, or robots-related failure does not cancel an existing human verification.",
              "- Manual Eligibility and Manufacturing Confirmed are NEVER rewritten by this script.",
              "- A candidate without alerts is NOT automatically certified as a manufacturer.",
              "- This is rule-based evidence triage; it does not understand ownership, legal entities, or every language construction reliably.",
              "", "## Files", "",
              "- `audit_results.csv`: all candidates and automatic hints, including candidates without alerts.",
              "- `page_checks.csv`: each requested URL, access status, and number of extracted excerpts.",
              "- `evidence_snippets.csv`: explainable excerpts with rule ID and original URL.",
              "- `manual_followups.csv`: ONE ROW per candidate with at least one alert, sorted by priority.",
              "- `alerts_detailed.csv`: ONE ROW per alert (a candidate may have multiple alerts).", ""]
    (output/"audit_summary.md").write_text("\n".join(lines), encoding="utf-8")
    return len(candidates)


class OfflineDemoFetcher:
    HTML = {
        "https://demo.invalid/andes": "We operate dairy manufacturing plants in Chile. Our factory produces cheese and butter for local markets.",
        "https://demo.invalid/trader": "We are a global dairy commodities trader. We connect manufacturers with customers and our suppliers manufacture milk powder.",
        "https://demo.invalid/foodservice": "Professional foodservice cheese and butter products by Andes Demo Group. We distribute brands to restaurants.",
        "https://demo.invalid/foreign": "We operate dairy manufacturing plants in Canada. Our factories produce butter and cream.",
        "https://demo.invalid/partial": "We produce milk in Chile at our dairy processing plant.",
        "https://demo.invalid/blocked": None,
    }
    def fetch(self, url: str):
        text = self.HTML.get(url)
        if text is None:
            return base.FetchResult(url, "robots_unavailable", detail="OFFLINE DEMO restricted-source fixture")
        return base.FetchResult(url, "ok", "200", "Demo source", text)


def demo_rows():
    fields = {
        "Target Country": "Chile", "Manufacturing Confirmed": "Yes", "Project Eligibility": "Eligible",
        "Independent QA": "Supported", "Group Key": "", "Public Contact Page": "",
        "Products Evidence URL": "", "Manufacturing Evidence URL": "",
    }
    def build(cid, name, url, **other):
        return {**fields,"Candidate ID": cid, "Company / Group": name, "Official Website": url, **other}
    return [
        build("DEMO-001", "Andes Demo Dairy", "https://demo.invalid/andes", **{"Group Key":"Andes Group"}),
        build("DEMO-002", "Cordial Trade Demo", "https://demo.invalid/trader", **{"Manufacturing Confirmed":"No","Project Eligibility":"Excluded - not manufacturer","Group Key":"Cordial"}),
        build("DEMO-003", "Andes Foodservice", "https://demo.invalid/foodservice", **{"Manufacturing Confirmed":"Not separate","Project Eligibility":"Excluded - duplicate","Group Key":"Andes Group"}),
        build("DEMO-004", "Northern Demo Dairy", "https://demo.invalid/foreign", **{"Group Key":"Northern","Target Country":"Chile", "Project Eligibility":"Excluded - geography"}),
        build("DEMO-005", "Dairy Demo Partial", "https://demo.invalid/partial", **{"Public Contact Page":"https://demo.invalid/blocked", "Group Key":"Partial", "Independent QA":"Manual verification; automated access limited"}),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auditable, contextual multilingual manufacturer evidence triage. Does not make verification decisions.")
    parser.add_argument("--input", type=Path, default=Path("input/candidate_review.csv"), help="CSV exported from '03 - Candidate Review'")
    parser.add_argument("--out", type=Path, default=None, help="Output folder, defaults to output/website_audit_v2")
    parser.add_argument("--max-pages", type=int, choices=(1,2,3,4), default=4, help="Maximum different URLs per company; 4 = homepage + manufacturing + products + contact")
    parser.add_argument("--timeout", type=int, default=12, help="Per-request timeout, seconds (3-40)")
    parser.add_argument("--delay", type=float, default=1.2, help="Minimum delay between requests to same domain (>=1s)")
    parser.add_argument("--demo", action="store_true", help="Run entirely OFFLINE fictional test without network")
    args=parser.parse_args(argv)
    if not (3 <= args.timeout <= 40) or args.delay < 1:
        parser.error("--timeout must be 3-40 seconds; --delay must be at least 1 second")
    if args.demo:
        rows = demo_rows()
        fetcher = OfflineDemoFetcher()
    else:
        if not args.input.is_file():
            parser.error(f"Missing input CSV: {args.input}; export the '03 - Candidate Review' tab as CSV")
        try:
            rows = base.read_rows(args.input)
        except (UnicodeError,OSError,ValueError) as ex:
            parser.error(str(ex))
        fetcher = base.PublicFetcher(timeout=args.timeout, delay=args.delay)
    output=args.out or Path("output/website_audit_v2_DEMO" if args.demo else "output/website_audit_v2")
    results,pages,evidence,alerts=run_audit(rows,fetcher,args.max_pages)
    flagged=write_outputs(output,results,pages,evidence,alerts,args.demo)
    print(f"Done: {len(results)} candidates; {flagged} candidates flagged ({len(alerts)} alerts); {len(evidence)} evidence excerpts.")
    print(f"Results folder: {output.resolve()}")
    print("No Google Sheets changes. No Tavily credits used. No automatic verification claims.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
