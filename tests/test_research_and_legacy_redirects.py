"""Offline regressions for discovery exit status and legacy redirect robots checks."""
import csv
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import Message
from urllib.request import HTTPSHandler
from urllib.response import addinfourl
from unittest.mock import patch

import candidate_integration as integration
import safe_http
import website_audit as audit
import website_audit_v2 as audit_v2


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://origin.example/start'
TARGET = 'https://target.example/restricted'
ALLOW = b'User-agent: *\nAllow: /\n'
DENY = b'User-agent: *\nDisallow: /restricted\n'
HTML = b'<html><body>Fictional destination content.</body></html>'


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


class OfflineRegressionCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.getaddrinfo', 'dotenv.load_dotenv'):
            guard = patch(target, side_effect=AssertionError('External access forbidden'))
            guard.start()
            self.addCleanup(guard.stop)


class ResearchExitTests(OfflineRegressionCase):
    def setUp(self):
        super().setUp()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.manual = self.root / 'input/candidate_review.csv'
        self.out = self.root / 'output/integration_v2_2_DEMO'

    def run_research(self):
        output = io.StringIO()
        with patch('sys.argv', ['research.py', '--demo', '--results-per-query', '1']), redirect_stdout(output):
            try:
                runpy.run_path(str(ROOT / 'research.py'), run_name='__main__')
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        return code, output.getvalue()

    def test_real_manual_conflict_warns_exits_two_and_preserves_alternatives(self):
        row = dict.fromkeys(integration.REVIEW_FIELDS, '')
        row.update({'Candidate ID': 'DEMO-REVIEW-001', 'Target Country': 'France',
                    'Original Search URL': 'https://example.com/demo-france-1',
                    'Project Eligibility': 'Needs review', 'Research Notes': 'Original manual note'})
        write_csv(self.manual, [row], integration.REVIEW_FIELDS)
        self.assertEqual(self.run_research()[0], 0)
        target = self.out / 'candidate_review.csv'
        rows = read_csv(target)
        original_ids = [item['Candidate ID'] for item in rows]
        next(item for item in rows if item['Candidate ID'] == row['Candidate ID'])['Research Notes'] = 'Integrated alternative'
        write_csv(target, rows, list(rows[0]))
        row['Research Notes'] = 'Manual alternative'
        write_csv(self.manual, [row], integration.REVIEW_FIELDS)

        # Repeat the conflicting run to verify the alternatives survive subsequent exports.
        for _ in range(2):
            code, output = self.run_research()
            report = json.loads((self.out / 'integration_report.json').read_text(encoding='utf-8-sig'))
            conflict = next(item for item in report['conflicts'] if item['field'] == 'Research Notes')
            self.assertEqual(conflict['base'], 'Original manual note')
            self.assertEqual(conflict['manual'], 'Manual alternative')
            self.assertEqual(conflict['integrated'], 'Integrated alternative')
            self.assertEqual(read_csv(self.manual)[0]['Research Notes'], 'Manual alternative')
            exported = read_csv(target)
            self.assertEqual([item['Candidate ID'] for item in exported], original_ids)
            self.assertEqual(next(item for item in exported if item['Candidate ID'] == row['Candidate ID'])['Research Notes'],
                             'Original manual note')
            self.assertEqual(len(read_csv(self.root / 'output/candidates_DEMO.csv')), 3)
            self.assertTrue((self.root / 'output/summary_DEMO.txt').is_file())
            self.assertEqual(code, 2)
            self.assertIn('conflictos manuales pendientes', output.lower())
            self.assertIn('integration_report.json', output)

    def test_successful_integration_exits_zero_without_conflict_warning(self):
        for _ in range(2):
            code, output = self.run_research()
            self.assertEqual(code, 0)
            self.assertNotIn('conflictos manuales pendientes', output.lower())
            report = json.loads((self.out / 'integration_report.json').read_text(encoding='utf-8-sig'))
            self.assertEqual(report['conflicts'], [])
            self.assertEqual(len(read_csv(self.out / 'candidate_review.csv')), 3)


class FixtureHTTPSHandler(HTTPSHandler):
    """Replace the wire only; real urllib redirect and safe_http policy logic still run."""
    def __init__(self, fixtures, requested):
        super().__init__()
        self.fixtures = fixtures
        self.requested = requested

    def https_open(self, request):
        url = request.full_url
        self.requested.append(url)
        if len(self.requested) > 15:
            raise AssertionError('Unexpected recursion while fetching robots.txt')
        code, values, body = self.fixtures[url]
        headers = Message()
        for name, value in values.items():
            headers[name] = value
        response = addinfourl(io.BytesIO(body), headers, url, code)
        response.msg = 'Found' if code == 302 else 'OK'
        return response


class LegacyRedirectTests(OfflineRegressionCase):
    def setUp(self):
        super().setUp()
        self.requested = []
        self.fixtures = {
            'https://origin.example/robots.txt': (200, {'Content-Type': 'text/plain'}, ALLOW),
            ORIGIN: (302, {'Location': TARGET}, b''),
            'https://target.example/robots.txt': (200, {'Content-Type': 'text/plain'}, ALLOW),
            TARGET: (200, {'Content-Type': 'text/html; charset=utf-8'}, HTML),
        }
        transport = patch.object(safe_http, 'PublicHTTPSHandler',
                                 side_effect=lambda **kwargs: FixtureHTTPSHandler(self.fixtures, self.requested))
        transport.start()
        self.addCleanup(transport.stop)
        pause = patch.object(audit.PublicFetcher, 'pause')
        pause.start()
        self.addCleanup(pause.stop)
        self.fetcher = audit.PublicFetcher(timeout=3, delay=1)

    def test_redirect_disallowed_by_target_robots_never_requests_content(self):
        self.fixtures['https://target.example/robots.txt'] = (200, {'Content-Type': 'text/plain'}, DENY)
        result = self.fetcher.fetch(ORIGIN)
        self.assertEqual(result.status, 'network_error')
        self.assertEqual(self.requested, ['https://origin.example/robots.txt', ORIGIN,
                                          'https://target.example/robots.txt'])
        self.assertNotIn(TARGET, self.requested)
        self.assertIs(self.fetcher.fetch(ORIGIN), result)
        self.assertEqual(len(self.requested), 3)

    def test_redirect_allowed_by_target_robots_reads_content(self):
        result = self.fetcher.fetch(ORIGIN)
        self.assertEqual(result.status, 'ok')
        self.assertIn('Fictional destination content.', result.text)
        self.assertEqual(self.requested, ['https://origin.example/robots.txt', ORIGIN,
                                          'https://target.example/robots.txt', TARGET])

    def test_every_redirect_hop_checks_robots_before_content(self):
        middle = 'https://middle.example/next'
        self.fixtures[ORIGIN] = (302, {'Location': middle}, b'')
        self.fixtures['https://middle.example/robots.txt'] = (200, {'Content-Type': 'text/plain'}, ALLOW)
        self.fixtures[middle] = (302, {'Location': TARGET}, b'')
        self.fixtures['https://target.example/robots.txt'] = (200, {'Content-Type': 'text/plain'}, DENY)
        self.assertEqual(self.fetcher.fetch(ORIGIN).status, 'network_error')
        self.assertEqual(self.requested, ['https://origin.example/robots.txt', ORIGIN,
                                          'https://middle.example/robots.txt', middle,
                                          'https://target.example/robots.txt'])

    def test_redirected_robots_file_does_not_recurse_into_robots_policy(self):
        rules = 'https://target.example/robots-rules.txt'
        self.fixtures['https://target.example/robots.txt'] = (302, {'Location': rules}, b'')
        self.fixtures[rules] = (200, {'Content-Type': 'text/plain'}, ALLOW)
        with patch.object(self.fetcher, 'robots_permission', wraps=self.fetcher.robots_permission) as permission:
            result = self.fetcher.fetch(ORIGIN)
        self.assertEqual(result.status, 'ok')
        self.assertEqual([call.args[0] for call in permission.call_args_list], [ORIGIN, TARGET])
        self.assertEqual(self.requested, ['https://origin.example/robots.txt', ORIGIN,
                                          'https://target.example/robots.txt', rules, TARGET])

    def test_unsafe_redirects_still_block_before_destination_requests(self):
        for target in ('https://127.0.0.1/', 'https://10.0.0.1/',
                       'http://target.example/', 'file:///tmp/fictional'):
            with self.subTest(target=target):
                self.requested.clear()
                self.fixtures[ORIGIN] = (302, {'Location': target}, b'')
                fetcher = audit.PublicFetcher(timeout=3, delay=1)
                self.assertNotEqual(fetcher.fetch(ORIGIN).status, 'ok')
                self.assertEqual(self.requested, ['https://origin.example/robots.txt', ORIGIN])

    def test_unsafe_initial_urls_make_no_requests(self):
        for target in ('https://127.0.0.1/', 'https://10.0.0.1/', 'file:///tmp/fictional', 'https://['):
            with self.subTest(target=target):
                self.assertEqual(self.fetcher.fetch(target).status, 'missing_or_invalid_url')
                self.assertEqual(self.requested, [])

    def test_v2_cli_uses_legacy_fetcher_with_destination_robots_checks(self):
        self.fixtures['https://target.example/robots.txt'] = (200, {'Content-Type': 'text/plain'}, DENY)
        row = dict.fromkeys(integration.REVIEW_FIELDS, '')
        row.update({'Candidate ID': 'TEST-REDIRECT', 'Company / Group': 'Fictional fixture',
                    'Target Country': 'Chile', 'Official Website': ORIGIN, 'Project Eligibility': 'Needs review'})
        source = self.root / 'review.csv'
        out = self.root / 'audit'
        write_csv(source, [row], integration.REVIEW_FIELDS)
        with redirect_stdout(io.StringIO()):
            code = audit_v2.main(['--input', str(source), '--out', str(out), '--delay', '1'])
        self.assertEqual(code, 0)
        self.assertEqual(self.requested, ['https://origin.example/robots.txt', ORIGIN,
                                          'https://target.example/robots.txt'])
        self.assertNotIn(TARGET, self.requested)
        self.assertEqual(len(read_csv(out / 'page_checks.csv')), 1)


if __name__ == '__main__':
    unittest.main()
