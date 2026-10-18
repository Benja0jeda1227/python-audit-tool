"""Offline integration contract tests; all sockets are blocked."""
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import candidate_integration as ci
import website_audit as base
import website_audit_v2_1 as audit
import pipeline_v2_2 as pipeline

FIXTURES = Path(__file__).parent / 'fixtures'


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        _, self.sources = ci.read_csv(FIXTURES / 'discovery_v2_2.csv', ci.DISCOVERY_REQUIRED)
        _, self.manual = ci.read_csv(FIXTURES / 'review_v2_2.csv', ci.REVIEW_REQUIRED)

    def test_preserves_manual_and_does_not_invent(self):
        rows, events = ci.merge_candidates(self.sources, self.manual)
        self.assertEqual(rows[0], self.manual[0])
        self.assertEqual(len(rows), 3)
        self.assertEqual([e['action'] for e in events], ['new', 'new', 'existing'])
        self.assertNotEqual(rows[1]['Candidate ID'], 'C-001')
        for key in ['Company / Group', 'Official Website', 'Manufacturing Confirmed', 'Independent QA']:
            self.assertEqual(rows[1][key], '')
        self.assertEqual(rows[1]['Project Eligibility'], 'Needs review')

    def test_url_rules(self):
        n = ci.normalize_url
        self.assertEqual(n('https://WWW.dairy.example:443/?utm_medium=x#foo'), n('https://dairy.example'))
        self.assertNotEqual(n('https://a.example/?id=1'), n('https://a.example/?id=2'))
        self.assertNotEqual(n('https://a.example:8443'), n('https://a.example'))
        self.assertNotEqual(n('http://a.example'), n('https://a.example'))
        for url in ['file:///tmp/a', 'https://a.example:bad', 'http://127.0.0.1', 'https://user:pass@a.example']:
            self.assertEqual(n(url), '')

    def test_ambiguous_domain_and_conflicting_identifiers(self):
        source = dict(self.sources[0], **{'Search Result URL': 'https://manual.example/products'})
        rows, events = ci.merge_candidates([source], self.manual)
        self.assertEqual(len(rows), 1)
        self.assertEqual(events[0]['action'], 'ambiguous')
        other = dict(self.manual[0], **{'Candidate ID': 'OTHER'})
        source['Search Result URL'] = self.manual[0]['Original Search URL']
        _, events = ci.merge_candidates([source], self.manual + [other])
        self.assertEqual(events[0]['action'], 'ambiguous')

    def test_demo_invalid_and_duplicate_ids(self):
        for source in [dict(self.sources[0], **{'Verification Status': 'DEMO - NOT REAL'}),
                       dict(self.sources[0], **{'Search Result URL': 'invalid'})]:
            rows, events = ci.merge_candidates([source], self.manual)
            self.assertEqual(rows, self.manual)
            self.assertEqual(events[0]['action'], 'rejected')
        with self.assertRaises(ValueError):
            ci.merge_candidates([], self.manual * 2)

    def test_resume_and_v21_compatibility_without_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            source = FIXTURES / 'discovery_v2_2.csv'
            review = FIXTURES / 'review_v2_2.csv'
            target, first = ci.integrate(source, review, out)
            original = target.read_bytes()
            _, second = ci.integrate(source, review, out)
            self.assertEqual(first['counts']['new'], 2)
            self.assertEqual(second['counts']['new'], 0)
            self.assertEqual(original, target.read_bytes())
            rows = base.read_rows(target)
            class OfflineFetcher:
                def fetch(self, url):
                    return base.FetchResult(url, 'robots_unavailable')
            results, _, _, alerts = audit.audit(rows, OfflineFetcher())
            self.assertEqual(len(results), 3)
            self.assertTrue(any(a.code == 'NO_VALID_SOURCES' for a in alerts))
            self.assertTrue(json.loads((out / 'integration_report.json').read_text(encoding='utf-8-sig')))

    def test_bad_csv_no_output_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            bad = folder / 'bad.csv'
            bad.write_text('Candidate ID,Country,Search Result URL,Verification Status\nx,Chile\n')
            out = folder / 'out'
            with self.assertRaises(ValueError):
                ci.integrate(bad, FIXTURES / 'review_v2_2.csv', out)
            self.assertFalse(out.exists())

    def test_manual_updates_and_provenance_survive_new_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            review = folder / 'manual.csv'
            review.write_bytes((FIXTURES / 'review_v2_2.csv').read_bytes())
            target, first = ci.integrate(FIXTURES / 'discovery_v2_2.csv', review, folder / 'out')
            review.write_text(review.read_text().replace(' Keep this exact note ', 'Updated by researcher'))
            empty = folder / 'empty.csv'
            empty.write_text('Candidate ID,Country,Search Result URL,Verification Status\n')
            _, second = ci.integrate(empty, review, folder / 'out')
            _, rows = ci.read_csv(target, ci.REVIEW_REQUIRED)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]['Research Notes'], 'Updated by researcher')
            self.assertEqual(second['provenance'], first['provenance'])

    def test_explicit_manual_fields_and_generic_evidence(self):
        source = dict(self.sources[0], **{'Company Name (manual)': 'Known name',
                      'Official Website (manual)': 'https://official.example/',
                      'Evidence URL (manual)': 'https://records.example/document',
                      'Verification Status': 'Verified'})
        rows, events = ci.merge_candidates([source], [])
        self.assertEqual(rows[0]['Company / Group'], 'Known name')
        self.assertEqual(rows[0]['Official Website'], 'https://official.example/')
        self.assertEqual(rows[0]['Project Eligibility'], 'Needs review')
        self.assertEqual(rows[0]['Manufacturing Evidence URL'], '')
        self.assertEqual(events[0]['source']['Evidence URL (manual)'], 'https://records.example/document')

    def test_cli_defaults_to_no_web_audit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(audit, 'main') as run_audit:
            result = pipeline.main(['--input', str(FIXTURES / 'discovery_v2_2.csv'),
                                    '--review', str(FIXTURES / 'review_v2_2.csv'), '--out', tmp])
            self.assertEqual(result, 0)
            run_audit.assert_not_called()

    def test_research_demo_automatically_integrates_without_credentials(self):
        import os
        import research
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'input').mkdir()
            (root / 'input/candidate_review.csv').write_bytes((FIXTURES / 'review_v2_2.csv').read_bytes())
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch('sys.argv', ['research.py', '--demo']), patch.object(research, 'load_dotenv', side_effect=AssertionError('No .env')):
                    research.main()
                self.assertTrue((root / 'output/integration_v2_2_DEMO/candidate_review.csv').exists())
                self.assertFalse((root / 'output/integration_v2_2').exists())
            finally:
                os.chdir(original_cwd)

    def test_demo_output_is_isolated(self):
        source = dict(self.sources[0], **{'Verification Status': 'DEMO - NOT REAL'})
        rows, _ = ci.merge_candidates([source], [], demo=True)
        self.assertTrue(rows[0]['Candidate ID'].startswith('DEMO-N-'))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ci.integrate(FIXTURES / 'discovery_v2_2.csv', FIXTURES / 'review_v2_2.csv', out)
            before = (out / 'candidate_review.csv').read_bytes()
            with self.assertRaises(ValueError):
                ci.integrate(FIXTURES / 'discovery_v2_2.csv', FIXTURES / 'review_v2_2.csv', out, demo=True)
            self.assertEqual(before, (out / 'candidate_review.csv').read_bytes())


if __name__ == '__main__':
    unittest.main()
