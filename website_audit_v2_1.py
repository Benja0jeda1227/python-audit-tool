#!/usr/bin/env python3
"""Multilingual Website Audit v2.1: evidence-first QA for dairy-company research.

Pure Python standard library. Reuses v1.1 robots-aware fetcher's request/robots
policy while extracting focused HTML text. NO automated final-verdict changes.

Output is a conservative triage report, not a legal / business verification.
"""
from __future__ import annotations

import argparse
import csv
import http.client
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request
from safe_http import urlopen
from url_policy import normalize_url
from url_policy import redact, safe_cell as safe_csv_cell
from integration_store import ExportBundle, writer_lock

import website_audit as base

VERSION = "2.1.0"
MAX_PAGE_CHARS = 35_000
MAX_PAGE_BYTES = 650_000
MAX_SNIPPETS_BY_KIND = {
    "OWN_MANUFACTURING_CLAIM": 3,
    "MANUFACTURING_SITE_MENTION": 3,
    "THIRD_PARTY_MANUFACTURING": 3,
    "SELF_TRADER_CLAIM": 3,
    "TARGET_COUNTRY_PRODUCTION_HINT": 2,
    "FOREIGN_COUNTRY_PRODUCTION_HINT": 2,
    "PRODUCT_CATEGORY_MENTION": 6,
}

# These verbs MUST represent manufacturing actions, not the noun "products".
MFG_ACTION = r"(?:manufactur(?:e|es|ed|ing)|produc(?:e|es|ed|ing)|process(?:es|ed|ing)?|fabric(?:amos|amos|a|an|an|ado|aci[oó]n)|elabor(?:amos|an|an|ado)|produ(?:cimos|ce|cen|cci[oó]n)|fabriqu(?:ons|e|ent)|produis(?:ons|ent)|transform(?:ons|e|ent))"
COUNTRY = {
    "Chile": r"\b(?:Chile|Chilean|chileno|chilena|chilenas|chilenos)\b",
    "France": r"\b(?:France|French|fran[cç]ais(?:e|es|s)?|francesa|franc[eé]s)\b",
    "United States": r"\b(?:United States|U\.?S\.?A\.?|U\.?S\.?|American|estadounidense(?:s)?|[eé]tats[- ]unis)\b",
}
FOREIGN_COUNTRIES = re.compile(r"\b(?:Canada|Canadian|Australia|Australian|Germany|German|Netherlands|Dutch|United Kingdom|UK|Brazil|Brasil|Argentina|New Zealand|Nouvelle-Z[eé]lande|Allemagne|canadienne?|australien(?:ne)?)\b", re.I)

# Require FIRST-PERSON SUBJECT + actual action or an owned facility; no general
# "our company ... products" shortcut that makes 'products' act as 'produce'.
SELF_MFG = [
    ("EN_WE_MAKE", re.compile(r"\b(?:we|our company|our group)\s+(?:(?:also|directly|currently)\s+)?(?:manufacture|manufactures|manufactured|produce|produces|produced|process|processes|operate|operates|own|owns|run|runs)\b.{0,105}\b(?:milk|dairy|cheese|butter|yogurt|manufactur\w*|process\w*|factor(?:y|ies)|plants?|facilit(?:y|ies)|production)\b", re.I)),
    ("EN_OWN_FACILITY", re.compile(r"\b(?:our|company[- ]owned|we own|we operate)\s+(?:dairy\s+)?(?:manufacturing\s+)?(?:plants?|factories|facilities|production sites?)\b|\b(?:our|the company's)\s+(?:dairy\s+)?(?:plants?|factories)\b.{0,90}\b(?:produc\w*|manufactur\w*|process\w*)\b", re.I)),
    ("ES_FABRICAMOS", re.compile(r"\b(?:fabricamos|elaboramos|producimos|procesamos|nuestra(?:s)?\s+(?:planta(?:s)?|f[aá]brica(?:s)?))\b.{0,95}\b(?:l[aá]cteos?|leche|queso|mantequilla|produc\w*|fabric\w*|planta\w*)\b", re.I)),
    ("FR_NOUS_PRODUISONS", re.compile(r"\b(?:nous fabriquons|nous produisons|nous transformons|nos usines?|notre laiterie|nous exploitons|nos sites? de production)\b.{0,100}\b(?:lait|laiti[eè]r\w*|fromage|beurre|cr[eè]me|fabriqu\w*|produ\w*|transform\w*|usines?|sites?)\b", re.I)),
]
DIRECT_SITE = [
    ("EN_DAIRY_PLANT", re.compile(r"\b(?:dairy|milk|cheese)\s+(?:manufacturing|processing|production)\s+(?:plants?|facilit(?:y|ies)|sites?)\b|\b(?:manufacturing|processing|production)\s+(?:plants?|facilit(?:y|ies)|sites?)\b", re.I)),
    ("ES_PLANTA", re.compile(r"\b(?:plantas?\s+(?:de\s+)?(?:producci[oó]n|procesamiento|l[aá]cteas?|industrial(?:es)?)|f[aá]bricas?\s+(?:de\s+)?(?:quesos?|l[aá]cteos?|leche))\b", re.I)),
    ("FR_USINE", re.compile(r"\b(?:usines?\s+(?:de\s+production|laiti[eè]res?)|sites?\s+(?:de production|industriels?)|transformation du lait)\b", re.I)),
]
SELF_TRADER = [
    ("EN_SELF_TRADER", re.compile(r"\b(?:we are|our company is|we operate as|we specialize in|we are a leading player in|we are involved in)\b.{0,105}\b(?:trader|trading|broker|distribut\w*|wholesal\w*|commodit(?:y|ies) trading)\b", re.I)),
    ("EN_TRADING_DESCRIPTION", re.compile(r"\b(?:global|international|leading)\s+(?:dairy\s+)?(?:commodit(?:y|ies)\s+)?(?:trader|trading company|broker|distributor)\b", re.I)),
    ("ES_TRADER", re.compile(r"\b(?:somos|nuestra empresa|nos dedicamos a)\b.{0,90}\b(?:comercializaci[oó]n|comercializador\w*|distribuci[oó]n|distribuidor\w*|intermediaci[oó]n)\b", re.I)),
    ("FR_TRADER", re.compile(r"\b(?:nous sommes|notre soci[eé]t[eé]|nous faisons)\b.{0,90}\b(?:n[eé]goce|n[eé]gociant\w*|distributeur\w*|distribution|courtage)\b", re.I)),
]
THIRD_PARTY = [
    ("SUPPLIERS_MANUFACTURE", re.compile(r"\b(?:our\s+)?(?:suppliers?|partners?|customers?|clients?|third[- ]part(?:y|ies)|proveedores?|socios?|fournisseurs?|partenaires?)\b.{0,75}\b(?:manufactur\w*|produc(?:e|es|ing|ed)|fabric\w*|elabor\w*|fabriqu\w*|usines?|plants?)\b", re.I)),
    ("CONNECT_MANUFACTURERS", re.compile(r"\b(?:we connect|connecting|we source|sourcing from|connectamos|nous relions)\b.{0,100}\b(?:manufactur\w*|produc(?:e|ers?|ing)|fabricantes?|fabricants?|producteurs?)\b", re.I)),
]
PRODUCTS = {
    "Milk Powder": re.compile(r"\b(?:milk powder|powdered milk|leche en polvo|lait en poudre|poudre de lait)\b", re.I),
    "Cheese": re.compile(r"\b(?:cheese|fromages?|quesos?)\b", re.I),
    "Butter": re.compile(r"\b(?:butter|beurre|mantequilla)\b", re.I),
    "Yogurt": re.compile(r"\b(?:yogh?urt|yaourts?|yogur(?:t|es)?)\b", re.I),
    "Cream": re.compile(r"\b(?:cream|cr[eè]me|crema)\b", re.I),
    "Milk": re.compile(r"\b(?:milk|lait|leche)\b", re.I),
    "Dairy Ingredients": re.compile(r"\b(?:dairy ingredients?|whey|lactose|ingr[eé]dients? laitiers?|ingredientes? l[aá]cteos?)\b", re.I),
}
INGREDIENT_CONTEXT = re.compile(r"\b(?:made from|made with|contains|based on|elaborad[oa] a base de|a base de|contiene|avec du lait|[aà] base de|compos[eé] de|ingredients?\s*[:：]|ingredientes?\s*[:：])", re.I)
CATALOG_CONTEXT = re.compile(r"\b(?:our products|product(?:s)?(?: portfolio| range| catalog| catalogue)?|portfolio|categories|our brands|nos produits|gamme|notre catalogue|nuestros productos|cat[aá]logo|linea de productos|l[ií]nea de productos|our factory produces|we produce|fabricamos|producimos)\b", re.I)
# Standalone product patterns can be misleading in compound labels.
COMPOUND_CREAM = re.compile(r"\b(?:cream cheese|queso crema|fromage [aà] la cr[eè]me)\b", re.I)
COMPOUND_MILK = PRODUCTS["Milk Powder"]


@dataclass(frozen=True)
class Evidence:
    cid: str
    company: str
    role: str
    url: str
    kind: str
    rule: str
    strength: str
    excerpt: str

    def row(self) -> dict[str,str]:
        return {"Candidate ID": self.cid, "Company": self.company, "Source Role": self.role,
                "Source URL": self.url, "Evidence Type": self.kind, "Rule ID": self.rule,
                "Strength": self.strength, "Exact Text Excerpt": self.excerpt}


@dataclass(frozen=True)
class Alert:
    cid: str
    company: str
    code: str
    priority: str
    reason: str
    url: str = ""
    action: str = ""

    def row(self) -> dict[str,str]:
        return {"Candidate ID": self.cid, "Company": self.company, "Alert Code": self.code,
                "Priority": self.priority, "Reason": self.reason, "Source URL": self.url,
                "Recommended Action": self.action}


# Unlike v1.1's flattened text, keep paragraph/list/heading boundaries and
# prefer the main/article element when present. Fallback: body excluding UI.
class ContentParser(HTMLParser):
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "div", "section", "article", "main", "blockquote", "br", "tr", "td"}
    NOISE_TAG = {"script", "style", "noscript", "svg", "iframe", "template", "nav", "footer", "header", "aside", "form", "button", "select"}
    NOISE_HINT = re.compile(r"(?:\bnav(?:igation)?\b|menu|footer|cookie|breadcrumb|popup|overlay|newsletter|social[-_ ]?links|mobile[-_ ]?menu|sidebar|legal[-_ ]?links|search[-_ ]?form)", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str,bool,bool]] = []
        self.main_found = False
        self.body: list[str] = []
        self.main: list[str] = []
        self.title: list[str] = []
        self.in_title = False

    def handle_starttag(self,tag,attrs):
        props = dict(attrs)
        noise = tag in self.NOISE_TAG or bool(self.NOISE_HINT.search(" ".join(str(props.get(k) or "") for k in ("id","class","role"))))
        inside_main = tag in {"main", "article"} or any(x[2] for x in self.stack)
        if tag in {"main", "article"}: self.main_found = True
        self.stack.append((tag,noise,inside_main))
        if tag == "title": self.in_title = True
        if tag in self.BLOCK:
            self.body.append("\n")
            if inside_main: self.main.append("\n")
        if tag in {"br", "hr", "img", "input", "meta", "link", "source"}:
            self.stack.pop()

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if self.stack and self.stack[-1][0] == tag: self.stack.pop()

    def handle_endtag(self,tag):
        if tag == "title": self.in_title = False
        if tag in self.BLOCK:
            self.body.append("\n")
            if any(x[2] for x in self.stack): self.main.append("\n")
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:]
                break

    def handle_data(self,data):
        if self.in_title: self.title.append(data)
        if any(x[1] for x in self.stack) or not data.strip() or self.in_title: return
        self.body.append(data+" ")
        if any(x[2] for x in self.stack): self.main.append(data+" ")


def clean_blocks(text:str)->list[str]:
    seen=set(); blocks=[]
    for line in text.splitlines():
        line=re.sub(r"\s+"," ",line).strip()
        if not (18 <= len(line) <= 850): continue
        key=re.sub(r"\W+"," ",line.casefold()).strip()
        if key in seen: continue
        seen.add(key); blocks.append(line)
        if sum(len(x) for x in blocks)>MAX_PAGE_CHARS: break
    return blocks


def html_to_content(html:str)->tuple[str,str]:
    p=ContentParser();p.feed(html)
    main=clean_blocks("".join(p.main))
    body=clean_blocks("".join(p.body))
    # Some sites place all real content outside <main>. Prefer <main> only if
    # it contains useful substantive text; otherwise use cleaned body.
    selected=main if sum(map(len,main))>=140 else body
    title=re.sub(r"\s+"," "," ".join(p.title)).strip()[:130]
    return "\n".join(selected)[:MAX_PAGE_CHARS],title


class CleanPublicFetcher(base.PublicFetcher):
    """Reuses robots.txt decision and rate limiting from the v1.1 fetcher.

    Does not bypass robots/403 or browser security controls. This script is
    for preselected public URLs, not adversarial/untrusted network targets.
    """
    def fetch(self, given_url):
        url=public_url(given_url)
        if not url: return base.FetchResult(given_url,"unsupported_url",detail="Not a public HTTP(S) URL")
        if url in self.cache: return self.cache[url]
        p=urlparse(url)
        allowed,status=self.robots_permission(url)
        if not allowed:
            result=base.FetchResult(url,"robots_disallow" if status=="robots_checked" else "robots_unavailable",detail=status)
            self.cache[url]=result;return result
        self.pause(p.netloc)
        req=Request(url,headers={"User-Agent":base.AGENT,"Accept":"text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"})
        try:
            with urlopen(req,timeout=self.timeout,context=self.context,
                         redirect_policy=lambda url: self.robots_permission(url)[0]) as resp:
                code=str(resp.getcode())
                kind=resp.headers.get("Content-Type","").lower()
                final=public_url(resp.geturl())
                if not final:
                    result=base.FetchResult(url,"unsafe_redirect",code)
                elif not kind.startswith(("text/html","application/xhtml+xml")):
                    result=base.FetchResult(url,"non_html",code,detail=f"Content-Type: {kind[:85]}")
                else:
                    data=resp.read(MAX_PAGE_BYTES+1)
                    if len(data)>MAX_PAGE_BYTES:
                        result=base.FetchResult(url,"too_large",code)
                    else:
                        charset=resp.headers.get_content_charset() or "utf-8"
                        content,title=html_to_content(data.decode(charset,errors="replace"))
                        result=base.FetchResult(final,"ok" if content else "empty_page",code,title,content)
        except HTTPError as ex:
            result=base.FetchResult(url,f"http_{ex.code}",str(ex.code),detail="HTTP error; manual browser may still work")
        except (URLError,OSError,TimeoutError) as ex:
            result=base.FetchResult(url,"network_error",detail=type(ex).__name__)
        except LookupError:
            result=base.FetchResult(url,"encoding_error")
        except (http.client.HTTPException, ValueError):
            result=base.FetchResult(url,"invalid_response")
        self.cache[url]=result;return result


def public_url(raw:str)->str:
    return base.clean_url(raw)


def first_party(official:str,source:str)->bool:
    if not public_url(official) or not public_url(source):return False
    a=(urlparse(normalize_url(official)).hostname or "")
    b=(urlparse(normalize_url(source)).hostname or "")
    return bool(a and b and (a==b or b.endswith("."+a)))


def sources_for(row:dict,max_pages:int)->list[tuple[str,str]]:
    slots=[("Homepage","Official Website"),("Manufacturing Evidence","Manufacturing Evidence URL"),
           ("Products Evidence","Products Evidence URL"),("Public Contact","Public Contact Page")]
    found=[];seen=set()
    for label,key in slots:
        url=public_url(row.get(key,""))
        key=normalize_url(url)
        if key and key not in seen:
            found.append((label,url));seen.add(key)
        if len(found)>=max_pages:break
    return found


def first_match(patterns:list[tuple[str,re.Pattern]],txt:str)->str:
    for name,pattern in patterns:
        if pattern.search(txt):return name
    return ""


def blocks_as_sentences(text:str)->list[str]:
    result=[]
    for block in text.splitlines():
        for chunk in re.split(r"(?<=[.!?;])\s+(?=[A-ZÀ-Ÿa-zà-ÿ¿¡\"'])",block):
            chunk=chunk.strip()
            if len(chunk)<=400:
                if chunk:result.append(chunk)
            else:
                # Long text is divided on commas if possible to avoid mixing
                # unrelated subjects (e.g. "we trade" with "clients produce").
                pieces=re.split(r"(?<=,)\s+",chunk)
                if len(pieces)>1 and max(map(len,pieces))<=450:
                    result.extend(p.strip() for p in pieces if len(p.strip())>=10)
                else:
                    for i in range(0,len(chunk),340):
                        result.append(chunk[i:i+380])
    return result


def product_hints(sentence:str,source_role:str)->list[str]:
    if source_role not in {"Homepage","Products Evidence","Manufacturing Evidence"}:return []
    if source_role!="Products Evidence" and not CATALOG_CONTEXT.search(sentence):return []
    # In a product statement, do not infer sale of an ingredient.
    # Example: "queso crema elaborado a base de leche" => Cheese, not Milk/Cream.
    ingredient=INGREDIENT_CONTEXT.search(sentence)
    if ingredient:
        sentence=sentence[:ingredient.start()].strip()
        if not sentence:return []
    hints=[]
    for name,rx in PRODUCTS.items():
        if not rx.search(sentence):continue
        if name=="Cream" and COMPOUND_CREAM.search(sentence):
            rest=COMPOUND_CREAM.sub("",sentence)
            if not PRODUCTS["Cream"].search(rest):continue
        if name=="Milk" and COMPOUND_MILK.search(sentence):
            rest=COMPOUND_MILK.sub("",sentence)
            if not PRODUCTS["Milk"].search(rest):continue
        hints.append(name)
    return hints


def extract_evidence(cid:str, company:str, role:str, url:str, text:str, country:str, is_first_party:bool)->list[Evidence]:
    output=[];seen=set();count=Counter();country_re=COUNTRY.get(country,"")
    for sentence in blocks_as_sentences(text):
        if len(sentence)<12:continue
        third=first_match(THIRD_PARTY,sentence)
        trade=first_match(SELF_TRADER,sentence)
        own=first_match(SELF_MFG,sentence)
        site=first_match(DIRECT_SITE,sentence)
        # Third-party attribution and an explicit trading self-description
        # override accidental first-person production interpretation.
        if third:own=""
        if trade and not (re.search(r"\b(?:we also|we operate|our factories|our plants|nous fabriquons|fabricamos)\b",sentence,re.I)):
            own=""
        if not is_first_party:own=""  # Gov/other-domain page needs entity attribution.
        kinds=[]
        if third:kinds.append(("THIRD_PARTY_MANUFACTURING",third,"Third-party context"))
        if trade:kinds.append(("SELF_TRADER_CLAIM",trade,"Company role claim" if is_first_party else "External reference"))
        if own:kinds.append(("OWN_MANUFACTURING_CLAIM",own,"First-party company claim"))
        if site:kinds.append(("MANUFACTURING_SITE_MENTION",site,"Plant mention, ownership unproven"))
        if country_re and re.search(country_re,sentence,re.I) and (own or site) and not third:
            kinds.append(("TARGET_COUNTRY_PRODUCTION_HINT","COUNTRY_NEAR_PRODUCTION","Proximity hint only"))
        elif (own or site) and FOREIGN_COUNTRIES.search(sentence) and not third:
            kinds.append(("FOREIGN_COUNTRY_PRODUCTION_HINT","FOREIGN_COUNTRY_NEAR_PRODUCTION","Proximity hint only"))
        for category in product_hints(sentence,role):
            kinds.append(("PRODUCT_CATEGORY_MENTION",category,"Product listing hint only"))
        for kind,rule,strength in kinds:
            # Deduplicate by candidate, type and representative category before
            # emitting many repeated menu-like snippets.
            key=(kind,rule)
            max_for=1 if kind=="PRODUCT_CATEGORY_MENTION" else MAX_SNIPPETS_BY_KIND.get(kind,2)
            if count[key]>=max_for:continue
            norm=re.sub(r"\W+"," ",sentence.casefold())[:300]
            if (kind,rule,norm) in seen:continue
            seen.add((kind,rule,norm));count[key]+=1
            output.append(Evidence(cid,company,role,url,kind,rule,strength,sentence[:360]))
    return output


def normalized_status(row:dict)->str:
    return (row.get("Project Eligibility") or "").strip()


def audit(rows:list[dict],fetcher,max_pages:int=4):
    groups=Counter(x.get("Group Key","").strip().casefold() for x in rows if x.get("Group Key","").strip())
    results=[];pages=[];all_evidence=[];alerts=[]
    stamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    for index,row in enumerate(rows,1):
        cid=row.get("Candidate ID","").strip();company=row.get("Company / Group","").strip()
        country=row.get("Target Country","").strip();eligibility=normalized_status(row)
        confirmed=row.get("Manufacturing Confirmed","").casefold().strip()
        official=row.get("Official Website","")
        print(f"[{index}/{len(rows)}] {cid}: {company}",flush=True)
        selected=sources_for(row,max_pages)
        current=[];page_rows=[]
        for role,url in selected:
            info=fetcher.fetch(url)
            first=first_party(official,info.url)
            extracted=extract_evidence(cid,company,role,info.url,info.text,country,first) if info.status=="ok" else []
            current.extend(extracted)
            entry={"Candidate ID":cid,"Company":company,"Source Role":role,"URL":url,"Final URL":info.url,
                   "Fetch Status":info.status,"HTTP Code":info.http_code,"Page Title":info.title,
                   "First Party Domain": "Yes" if first else "No; verify attribution",
                   "Evidence Count":str(len(extracted)),"Fetch Detail":info.detail}
            pages.append(entry);page_rows.append(entry)
            if info.status!="ok":
                alerts.append(Alert(cid,company,"ACCESS_LIMITED","Low" if "manual" in row.get("Independent QA","").casefold() else "Medium",
                                    f"Automated HTML retrieval was unavailable: {info.status}; this does not invalidate manual research.",url,
                                    "Review in a browser only if not already checked; keep the human verification."))
        # Keep a single exemplar of each product across pages (not duplicate
        # product terms from page header/footer). Preserve page-level evidence
        # in preference order (homepage, manufacturing, products, contact).
        products_seen=set();filtered=[]
        for e in current:
            if e.kind=="PRODUCT_CATEGORY_MENTION":
                if e.rule in products_seen:continue
                products_seen.add(e.rule)
            filtered.append(e)
        current=filtered
        all_evidence.extend(current)
        count=Counter(e.kind for e in current)
        own=count["OWN_MANUFACTURING_CLAIM"]>0
        site=count["MANUFACTURING_SITE_MENTION"]>0
        trade=count["SELF_TRADER_CLAIM"]>0
        third=count["THIRD_PARTY_MANUFACTURING"]>0
        target=count["TARGET_COUNTRY_PRODUCTION_HINT"]>0
        foreign=count["FOREIGN_COUNTRY_PRODUCTION_HINT"]>0
        attempted=len(page_rows);readable=sum(x["Fetch Status"]=="ok" for x in page_rows)
        if own and trade:hint="Mixed own-manufacturing and trading claims — manual context needed"
        elif own:hint="Own manufacturing claim extracted (first-party text)"
        elif trade:hint="Self-described trader / distributor"
        elif third and not site:hint="Manufacturing attributed to third parties"
        elif site:hint="Factory/plant mentioned — ownership not established"
        else:hint="Insufficient manufacturing-role evidence"
        priority_url=next((p["URL"] for p in page_rows if p["Source Role"]=="Manufacturing Evidence"),"")
        if attempted==0:
            alerts.append(Alert(cid,company,"NO_VALID_SOURCES","High","No valid public URL provided.","","Provide an official website or manufacturing evidence URL."))
        elif not readable:
            alerts.append(Alert(cid,company,"NO_READABLE_HTML","Medium","No selected source produced readable HTML; keep prior manual verification.","","Use manually checked or alternate official evidence."))
        if readable and confirmed=="yes" and not (own or site):
            alerts.append(Alert(cid,company,"MFG_EVIDENCE_GAP","Medium","Manufacturing is manually confirmed, but this scan did not extract a clearly attributable own-production claim or plant mention.",priority_url,"Prefer an owned-plant or government registration source. Lack of a phrase does not disprove manufacturing."))
        if readable and confirmed=="no" and own:
            source=next((e.url for e in current if e.kind=="OWN_MANUFACTURING_CLAIM"),"")
            alerts.append(Alert(cid,company,"ROLE_CONFLICT","High","Manual classification indicates no manufacturing, but a first-party own-manufacturing claim was extracted. Inspect the exact quotation.",source,"Check subject and referent of the claim before changing manual classification."))
        if readable and confirmed=="yes" and trade and not (own or site):
            alerts.append(Alert(cid,company,"ROLE_CONFLICT","Medium","Manual manufacturing classification contrasts with first-party trader description and no own-production evidence found.","","Seek direct factory evidence; trade and manufacture can coexist."))
        if eligibility=="Eligible" and not target:
            alerts.append(Alert(cid,company,"GEOGRAPHY_NOT_ESTABLISHED","Low",f"No explicit first-party factory/production statement near '{country}' in fetched text; not proof of manufacturing elsewhere.","","Check factory location independently; do not infer it from website language or HQ."))
        if eligibility=="Excluded - geography":
            if target:
                alerts.append(Alert(cid,company,"GEOGRAPHY_EXCLUSION_CONFLICT","High",f"A manufacturing mention appears near target country '{country}' despite manual geographic exclusion. Context needs review.","","Check whether the statement concerns the company's own manufacturing plants."))
            else:
                alerts.append(Alert(cid,company,"GEOGRAPHY_EXCLUSION_EVIDENCE_GAP","Low",f"No factory statement for '{country}' was found. Absence of text alone cannot prove the geography-based exclusion.","","Check documented manufacturing footprint and keep exclusion as a human research decision."))
        if eligibility=="Eligible" and confirmed=="no":
            alerts.append(Alert(cid,company,"MANUAL_DATA_INCONSISTENCY","High","Eligible is inconsistent with Manufacturing Confirmed = No.","","Resolve the two manually entered fields."))
        if eligibility=="Excluded - not manufacturer" and trade and not own:
            # This is positive corroboration, not a warning.
            pass
        group=row.get("Group Key","").strip()
        if group and groups[group.casefold()]>1:
            alerts.append(Alert(cid,company,"POSSIBLE_DUPLICATE","Medium",f"Group key '{group}' occurs in {groups[group.casefold()]} records.","","Review group relationship and count only independent manufacturers."))
        if eligibility=="Eligible" and not row.get("Public Contact Page","").strip():
            alerts.append(Alert(cid,company,"CONTACT_NOT_SUPPLIED","Low","No public contact URL was supplied.","","Look for a public company contact page; never invent personal details."))
        if attempted and attempted==readable and "access limited" in row.get("Independent QA","").casefold():
            alerts.append(Alert(cid,company,"QA_ANNOTATION_MAY_BE_STALE","Low","Previously noted automated-access limitation did not recur in this execution.","","Keep the original manual verification; optionally update the technical access note."))
        group_alerts=[a for a in alerts if a.cid==cid]
        level="High" if any(a.priority=="High" for a in group_alerts) else "Medium" if any(a.priority=="Medium" for a in group_alerts) else "Low" if group_alerts else "None"
        role_comparison="Agreement: manufacturer" if confirmed=="yes" and own else "Agreement: trader" if confirmed=="no" and trade and not own else "Possible conflict" if (confirmed=="no" and own) or (confirmed=="yes" and trade and not (own or site)) else "Insufficient machine evidence"
        result={
          "Candidate ID":cid,"Company":company,"Target Country":country,
          "Manual Eligibility (unchanged)":eligibility,"Manual Manufacturing (unchanged)":row.get("Manufacturing Confirmed",""),
          "Independent QA (unchanged)":row.get("Independent QA",""),"Group Key":group,
          "Machine Role Hint":hint,"Machine Role Comparison":role_comparison,
          "Own Manufacturing Claims":str(count["OWN_MANUFACTURING_CLAIM"]),
          "Factory Mentions (owner unknown)":str(count["MANUFACTURING_SITE_MENTION"]),
          "Third-party Manufacturing Mentions":str(count["THIRD_PARTY_MANUFACTURING"]),
          "Trading Self-descriptions":str(count["SELF_TRADER_CLAIM"]),
          "Target-country Factory Text Hint": "Yes - hint only" if target else "Not extracted",
          "Other-country Factory Text Hint":"Yes - hint only" if foreign else "Not extracted",
          "Products Mentioned (hints only)":"; ".join(sorted(products_seen)),
          "HTML Pages Read":str(readable),"Pages Attempted":str(attempted),"Unreadable Pages":str(attempted-readable),
          "Alert Count":str(len(group_alerts)),"Alert Codes":"; ".join(dict.fromkeys(a.code for a in group_alerts)),
          "Highest Follow-up Priority":level,"Audited At (UTC)":stamp}
        results.append(result)
    return results,pages,all_evidence,alerts


def safe_cell(value)->str:
    return safe_csv_cell(redact(str(value if value is not None else "")))


def write_csv(path:Path,headers:list[str],rows:list[dict]):
    with path.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=headers,extrasaction="ignore");w.writeheader()
        for r in rows:w.writerow({k:safe_cell(r.get(k,"")) for k in headers})


def _write_outputs(folder:Path, results:list[dict], pages:list[dict], evidences:list[Evidence], alerts:list[Alert], mode="live"):
    folder.mkdir(parents=True,exist_ok=True)
    write_csv(folder/"audit_results.csv",list(results[0]) if results else ["Candidate ID","Company"],results)
    write_csv(folder/"page_checks.csv",["Candidate ID","Company","Source Role","URL","Final URL","Fetch Status","HTTP Code","Page Title","First Party Domain","Evidence Count","Fetch Detail"],pages)
    write_csv(folder/"evidence_snippets.csv",["Candidate ID","Company","Source Role","Source URL","Evidence Type","Rule ID","Strength","Exact Text Excerpt"],[e.row() for e in evidences])
    write_csv(folder/"alerts_detailed.csv",["Candidate ID","Company","Alert Code","Priority","Reason","Source URL","Recommended Action"],[a.row() for a in alerts])
    byid=defaultdict(list)
    for a in alerts:byid[a.cid].append(a)
    followups=[]
    comparison=[]
    for r in results:
        cid=r["Candidate ID"];items=byid[cid]
        if items:
            followups.append({"Candidate ID":cid,"Company":r["Company"],
              "Manual Eligibility":r["Manual Eligibility (unchanged)"],
              "Priority":r["Highest Follow-up Priority"],
              "Alert Codes":"; ".join(dict.fromkeys(a.code for a in items)),
              "Reasons":" | ".join(dict.fromkeys(a.reason for a in items)),
              "Suggested Next Steps":" | ".join(dict.fromkeys(a.action for a in items))})
        comparison.append({"Candidate ID":cid,"Company":r["Company"],
           "Manual Manufacturing":r["Manual Manufacturing (unchanged)"],
           "Manual Eligibility":r["Manual Eligibility (unchanged)"],
           "Machine Role Hint":r["Machine Role Hint"],
           "Role Agreement (not a verdict)":r["Machine Role Comparison"],
           "Target-country factory text":r["Target-country Factory Text Hint"],
           "Eligibility Automatically Confirmed":"No — manual-only decision",
           "Alert Codes":r["Alert Codes"]})
    order={"High":0,"Medium":1,"Low":2}
    followups.sort(key=lambda x:(order.get(x["Priority"],3),x["Company"].casefold()))
    write_csv(folder/"manual_followups.csv",["Candidate ID","Company","Manual Eligibility","Priority","Alert Codes","Reasons","Suggested Next Steps"],followups)
    write_csv(folder/"quality_comparison.csv",["Candidate ID","Company","Manual Manufacturing","Manual Eligibility","Machine Role Hint","Role Agreement (not a verdict)","Target-country factory text","Eligibility Automatically Confirmed","Alert Codes"],comparison)
    count=Counter(a.code for a in alerts)
    lines=["# Website Audit v2.1 — Evidence quality report","",f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
       f"Run mode: { {'demo':'OFFLINE fictional demo','replay':'OFFLINE replay of previously saved v2 snippets','live':'Public website fetch (robots-aware)'}.get(mode, mode) }", "",
       f"Candidates: {len(results)}",f"Follow-up candidates: {len(followups)}",f"Detailed alerts: {len(alerts)}",
       f"Pages attempted: {len(pages)}",f"Pages with readable content: {sum(p['Fetch Status']=='ok' for p in pages)}",
       f"Evidence snippets after deduplication: {len(evidences)}","","## Alerts by code","",
       *(f"- {k}: {n}" for k,n in sorted(count.items())),"","## Decision limitations","",
       "- No automatic manufacturer eligibility decisions; all manual classifications are preserved.",
       "- A first-party explicit claim is still a claim, not independent proof of ownership.",
       "- Matching a country near production words is only a location hint; it does not prove local operations.",
       "- Excluding a company for geography cannot be *proven* by absence of a keyword.",
       "- Websites can present trading and manufacturing together, or refer to suppliers; examine exact quotations.",
       "- Non-readable automated sources do not invalidate browser-verified public URLs.",
       "- HTML cleaner prefers main/article and removes common navigation; page builders may require human review.",
       "- Product categories are hints and are conservatively filtered to avoid ingredients/compound products.",
       "- This script does not bypass site anti-bot protections or robots.txt.",
       "","## Output files","", "- audit_results.csv: complete candidate-level results",
       "- page_checks.csv: URL/fetch status", "- evidence_snippets.csv: short evidence excerpts",
       "- alerts_detailed.csv: one row per alert","- manual_followups.csv: one row per flagged candidate",
       "- quality_comparison.csv: machine/manual side-by-side, not accuracy statistics", ""]
    (folder/"audit_summary.md").write_text(redact("\n".join(lines)),encoding="utf-8")
    return len(followups)


def write_outputs(folder:Path, results:list[dict], pages:list[dict], evidences:list[Evidence], alerts:list[Alert], mode="live"):
    with writer_lock(folder):
        bundle=ExportBundle(folder,'audit')
        bundle.recover()
        with tempfile.TemporaryDirectory(prefix='audit-export-') as staging:
            staged=Path(staging)
            flagged=_write_outputs(staged,results,pages,evidences,alerts,mode)
            bundle.commit({p.name:p.read_bytes() for p in staged.iterdir() if p.is_file()})
    return flagged


class DemoFetcher:
    FIXTURES={
      "https://demo.invalid/manufacturer":"<html><head><title>Andes Fictional Dairy</title></head><body><nav>Milk Cream Butter Cheese</nav><main><h1>Andes Fictional Dairy</h1><p>We operate dairy manufacturing plants in Chile. Our factories produce cheese and butter.</p></main></body></html>",
      "https://demo.invalid/trader":"<html><main><p>We are a leading player in commodity trading in dairy products.</p><p>Our suppliers manufacture milk powder.</p></main></html>",
      "https://demo.invalid/foodservice":"<html><main><p>We distribute professional dairy products for restaurants.</p></main></html>",
      "https://demo.invalid/foreign":"<html><main><p>We operate dairy manufacturing plants in Canada. Our factories produce butter.</p></main></html>",
      "https://demo.invalid/blocked":None,
    }
    def fetch(self,url):
        raw=self.FIXTURES.get(url)
        if raw is None:return base.FetchResult(url,"robots_unavailable",detail="Fictional restricted demo URL")
        txt,title=html_to_content(raw)
        return base.FetchResult(url,"ok","200",title,txt)


def demo_rows():
    common={"Target Country":"Chile","Public Contact Page":"","Manufacturing Evidence URL":"","Products Evidence URL":"","Independent QA":"Supported"}
    def row(cid,name,url,eligibility,confirmed,group):
        return {**common,"Candidate ID":cid,"Company / Group":name,"Official Website":url,
                "Project Eligibility":eligibility,"Manufacturing Confirmed":confirmed,"Group Key":group}
    return [row("TEST-001","Andes Fictional Dairy","https://demo.invalid/manufacturer","Eligible","Yes","Andes"),
            row("TEST-002","Demo Dairy Trader","https://demo.invalid/trader","Excluded - not manufacturer","No","Trader"),
            row("TEST-003","Andes Professional Foodservice","https://demo.invalid/foodservice","Excluded - duplicate","Not separate","Andes"),
            row("TEST-004","Foreign Dairy Demo","https://demo.invalid/foreign","Excluded - geography","Yes","Foreign"),
            row("TEST-005","Restricted Demo","https://demo.invalid/blocked","Eligible","Yes","Restricted")]


def main(argv=None):
    ap=argparse.ArgumentParser(description="Multilingual manufacturer-research evidence triage v2.1. All manual labels preserved.")
    ap.add_argument("--input",type=Path,default=Path("input/candidate_review.csv"),help="CSV from Google Sheets tab 03 - Candidate Review")
    ap.add_argument("--out",type=Path,default=None,help="Output directory; defaults to output/website_audit_v2_1")
    ap.add_argument("--max-pages",type=int,choices=(1,2,3,4),default=4)
    ap.add_argument("--timeout",type=int,default=12)
    ap.add_argument("--delay",type=float,default=1.2)
    ap.add_argument("--demo",action="store_true",help="OFFLINE fictional test — no internet")
    args=ap.parse_args(argv)
    if not 3<=args.timeout<=40 or args.delay<1:ap.error("--timeout must be 3-40; --delay must be at least 1s")
    if args.demo:
        rows=demo_rows();fetcher=DemoFetcher()
    else:
        if not args.input.is_file():ap.error(f"Missing CSV: {args.input}")
        try:rows=base.read_rows(args.input)
        except (UnicodeError,OSError,ValueError) as exc:ap.error(str(exc))
        fetcher=CleanPublicFetcher(timeout=args.timeout,delay=args.delay)
    output=args.out or Path("output/website_audit_v2_1_DEMO" if args.demo else "output/website_audit_v2_1")
    results,pages,evidence,alerts=audit(rows,fetcher,args.max_pages)
    flagged=write_outputs(output,results,pages,evidence,alerts,mode="demo" if args.demo else "live")
    print(f"Done: {len(results)} candidates; {flagged} flagged; {len(alerts)} alerts; {len(evidence)} evidence snippets.")
    print(f"Results: {output.resolve()}")
    print("No Google Sheets changes. No Tavily credits consumed. Manual labels unchanged.")
    return 0


if __name__=="__main__":sys.exit(main())
