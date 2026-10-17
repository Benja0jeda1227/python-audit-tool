r"""Run from project root: .\.venv\Scripts\python.exe -m unittest discover -s tests -v"""
import csv
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import website_audit as base
import website_audit_v2 as v2


class FixtureFetcher:
    def __init__(self, content=None, errors=None):
        self.content = content or {}
        self.errors = errors or {}

    def fetch(self, url):
        if url in self.errors:
            return base.FetchResult(url, self.errors[url])
        return base.FetchResult(url, "ok", "200", "Offline test", self.content.get(url, ""))


def candidate(cid="X1", company="Testing Dairy", url="https://test.example/home", **kwargs):
    data = {
        "Candidate ID":cid,
        "Company / Group":company,
        "Target Country":"Chile",
        "Project Eligibility":"Eligible",
        "Manufacturing Confirmed":"Yes",
        "Group Key":company,
        "Independent QA":"Supported",
        "Official Website":url,
        "Manufacturing Evidence URL":"",
        "Products Evidence URL":"",
        "Public Contact Page":"",
    }
    data.update(kwargs)
    return data


class V2Tests(unittest.TestCase):
    def run_one(self, text, row=None, error=None):
        row = row or candidate()
        fetcher = FixtureFetcher({row["Official Website"]: text}, errors=error)
        results,pages,evidence,alerts = v2.run_audit([row],fetcher,4)
        return results[0],pages,evidence,alerts

    def test_dairy_manufacturer_self_claim(self):
        r,_,ev,alerts = self.run_one("We operate dairy manufacturing plants in Chile. Our factory produces cheese and butter.")
        self.assertEqual(r["Machine Role Hint"],"Direct manufacturing claim found")
        self.assertEqual(r["Target Country + Production Text Found"],"Yes - text hint only")
        self.assertTrue(any(e.kind=="OWN_MANUFACTURING_CLAIM" for e in ev))
        self.assertFalse(any(a.code=="MFG_EVIDENCE_GAP" for a in alerts))

    def test_trader_mentions_supplier_manufacturers_not_self(self):
        row = candidate(**{"Project Eligibility":"Excluded - not manufacturer", "Manufacturing Confirmed":"No"})
        r,_,ev,alerts = self.run_one("We are a global dairy commodities trader. We connect manufacturers with customers and our suppliers manufacture milk powder.",row)
        self.assertEqual(r["Machine Role Hint"],"Trading/distribution self-description")
        self.assertTrue(any(e.kind=="THIRD_PARTY_MANUFACTURING" for e in ev))
        self.assertFalse(any(e.kind=="OWN_MANUFACTURING_CLAIM" for e in ev))
        self.assertFalse(any(a.code=="ROLE_CONFLICT" for a in alerts))

    def test_manually_confirmed_can_have_robots_issue(self):
        row = candidate(**{"Public Contact Page":"https://test.example/contact", "Independent QA":"Manual verification; automated access limited"})
        r,p,ev,alerts = self.run_one("We operate dairy manufacturing plants in Chile.",row,{"https://test.example/contact":"robots_unavailable"})
        self.assertEqual(r["Manual Eligibility (unchanged)"],"Eligible")
        self.assertEqual(r["HTML Pages Read"],"1")
        self.assertIn("ACCESS_LIMITED",[a.code for a in alerts])
        self.assertTrue(any(a.priority=="Low" for a in alerts if a.code=="ACCESS_LIMITED"))

    def test_previous_qa_note_refresh_when_pages_read(self):
        row=candidate(**{"Independent QA":"Manual verification; automated access limited"})
        _,_,_,alerts=self.run_one("We operate dairy manufacturing plants in Chile.",row)
        self.assertIn("QA_ANNOTATION_MAY_BE_STALE",[a.code for a in alerts])

    def test_no_readable_html(self):
        row=candidate()
        r,p,ev,alerts = self.run_one("",row,{row["Official Website"]:"http_403"})
        self.assertEqual(r["HTML Pages Read"],"0")
        self.assertEqual(r["Manual Manufacturing (unchanged)"],"Yes")
        self.assertTrue({"ACCESS_LIMITED","NO_READABLE_HTML"}.issubset({a.code for a in alerts}))

    def test_true_first_party_on_subdomain(self):
        self.assertTrue(v2.first_party_domain("https://example.com","https://corporate.example.com/report"))
        self.assertTrue(v2.first_party_domain("https://www.example.com","https://example.com"))
        self.assertFalse(v2.first_party_domain("https://example.com","https://example.net"))
        self.assertFalse(v2.first_party_domain("https://example.com","https://evil-example.com"))

    def test_external_first_person_does_not_confirm_own_manufacturing(self):
        row=candidate(**{"Manufacturing Evidence URL":"https://records.example/gov"})
        fetcher=FixtureFetcher({"https://test.example/home":"We sell milk and cheese.","https://records.example/gov":"We operate dairy manufacturing plants in Chile."})
        r,p,ev,alerts=v2.run_audit([row],fetcher,4)
        self.assertTrue(any(e.kind=="EXTERNAL_SOURCE_ROLE_MENTION" for e in ev))
        self.assertFalse(any(e.kind=="OWN_MANUFACTURING_CLAIM" for e in ev))
        self.assertTrue(any(a.code=="MFG_EVIDENCE_GAP" for a in alerts))

    def test_manual_eligible_but_no_manufacturing_facts(self):
        r,_,_,alerts=self.run_one("Our delicious cheese and butter products are available in Chile.")
        self.assertIn("MFG_EVIDENCE_GAP",{a.code for a in alerts})
        self.assertIn("GEOGRAPHY_NOT_ESTABLISHED",{a.code for a in alerts})

    def test_website_language_not_evidence_country(self):
        r,_,_,alerts=self.run_one("Notre groupe est francais. Nous fabriquons du lait et du fromage.",candidate(**{"Target Country":"Chile"}))
        self.assertEqual(r["Target Country + Production Text Found"],"Not found in fetched text")
        self.assertTrue(any(a.code=="GEOGRAPHY_NOT_ESTABLISHED" for a in alerts))

    def test_french_first_party_claim(self):
        ev=v2.extract_evidence("F1","Fromages Demo","Homepage","https://demo.fr","Nous fabriquons du fromage et du beurre dans nos usines en France.","France")
        self.assertTrue(any(e.kind=="OWN_MANUFACTURING_CLAIM" for e in ev))
        self.assertTrue(any(e.kind=="TARGET_COUNTRY_MANUFACTURING_MENTION" for e in ev))

    def test_spanish_first_party_claim(self):
        ev=v2.extract_evidence("S1","Quesos Demo","Homepage","https://demo.cl","Fabricamos queso y mantequilla en nuestra planta de producción en Chile.","Chile")
        self.assertTrue(any(e.kind=="OWN_MANUFACTURING_CLAIM" for e in ev))

    def test_manual_no_with_self_claim_flag(self):
        row=candidate(**{"Manufacturing Confirmed":"No","Project Eligibility":"Excluded - not manufacturer"})
        _,_,_,alerts=self.run_one("We manufacture cheese in our dairy plants in Chile.",row)
        self.assertIn("ROLE_CONFLICT",{a.code for a in alerts})

    def test_manual_yes_with_trading_only_flag(self):
        _,_,_,alerts=self.run_one("We are a dairy ingredients trader and distributor.")
        self.assertIn("ROLE_CONFLICT",{a.code for a in alerts})

    def test_duplicate_group_key(self):
        r1=candidate(cid="A",company="Parent",url="https://parent.example",**{"Group Key":"Group A"})
        r2=candidate(cid="B",company="Foodservice",url="https://food.example",**{"Group Key":"group a","Project Eligibility":"Excluded - duplicate","Manufacturing Confirmed":"Not separate"})
        fetcher=FixtureFetcher({"https://parent.example":"We operate dairy manufacturing plants in Chile.","https://food.example":"We distribute milk to clients."})
        reports,_,_,alerts=v2.run_audit([r1,r2],fetcher,1)
        self.assertEqual(sum(a.code=="POSSIBLE_DUPLICATE" for a in alerts),2)
        self.assertEqual(reports[1]["Manual Eligibility (unchanged)"],"Excluded - duplicate")

    def test_full_run_writes_all_five_csv_files_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            r,p,e,a=v2.run_audit(v2.demo_rows(),v2.OfflineDemoFetcher(),4)
            n=v2.write_outputs(Path(tmp),r,p,e,a,True)
            self.assertGreater(n,0)
            for filename in ["audit_results.csv","page_checks.csv","evidence_snippets.csv","alerts_detailed.csv","manual_followups.csv","audit_summary.md"]:
                self.assertTrue((Path(tmp)/filename).is_file())
            with open(Path(tmp)/"audit_results.csv",encoding="utf-8-sig",newline="") as f:
                self.assertEqual(len(list(csv.DictReader(f))),5)
            with open(Path(tmp)/"manual_followups.csv",encoding="utf-8-sig",newline="") as f:
                rows=list(csv.DictReader(f))
                self.assertNotIn("DEMO-002",{r["Candidate ID"] for r in rows})

    def test_csv_formula_injection_protection(self):
        self.assertEqual(v2.csv_escape_formula("=HYPERLINK('evil')"),"'=HYPERLINK('evil')")
        self.assertEqual(v2.csv_escape_formula("+cmd"),"'+cmd")
        self.assertEqual(v2.csv_escape_formula("safe"),"safe")

    def test_private_ip_and_local_addresses_blocked(self):
        self.assertEqual(v2.allowed_public_url("http://192.168.0.1/private"), "")
        self.assertEqual(v2.allowed_public_url("http://127.0.0.1/admin"), "")
        self.assertEqual(v2.allowed_public_url("https://service.internal/"), "")
        self.assertEqual(v2.allowed_public_url("file:///etc/passwd"), "")
        self.assertEqual(v2.allowed_public_url("https://www.example.com/"), "https://www.example.com/")

    def test_candidate_preserves_four_source_order_and_deduplicates_url(self):
        row=candidate(**{"Manufacturing Evidence URL":"https://test.example/home","Products Evidence URL":"https://test.example/products","Public Contact Page":"https://test.example/contact"})
        self.assertEqual(v2.select_sources(row,4),[("Homepage","https://test.example/home"),("Products Evidence","https://test.example/products"),("Public Contact","https://test.example/contact")])

    def test_geographic_mention_not_equal_actual_verification(self):
        r,_,_,alerts=self.run_one("We operate dairy manufacturing plants in Chile.")
        self.assertEqual(r["Target Country + Production Text Found"],"Yes - text hint only")
        self.assertEqual(r["Manual Eligibility (unchanged)"],"Eligible")
        self.assertEqual(r["Independent QA (unchanged)"],"Supported")


if __name__=="__main__":
    unittest.main()
