"""Permanent reproductions of the v2.2 technical audit. Never use real data."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import candidate_integration as ci
import integration_store as storage
import pipeline_v2_2 as pipeline
import research
import website_audit as base
import website_audit_v2_1 as audit
from url_policy import URL_FIELDS, normalize_url, public_url, redact


def source(url='https://new.example/', **values):
    row = dict.fromkeys(research.FIELDS, '')
    row.update({'Candidate ID': 'C-001', 'Country': 'Chile', 'Search Result URL': url,
                'Verification Status': 'Needs review'})
    return {**row, **values}


def review(cid='R1', **values):
    row = dict.fromkeys(ci.REVIEW_FIELDS, '')
    row.update({'Candidate ID': cid, 'Company / Group': 'Manual Dairy', 'Target Country': 'Chile',
                'Official Website': 'https://manual.example/', 'Original Search URL': 'https://manual.example/',
                'Project Eligibility': 'Eligible', 'Research Notes': 'original manual note'})
    return {**row, **values}


def write_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(k for row in rows for k in row)) or ci.REVIEW_FIELDS
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.getaddrinfo', 'research.load_dotenv'):
            p = patch(target, side_effect=AssertionError('External access forbidden'))
            p.start(); self.addCleanup(p.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.d, self.r, self.out = self.root/'discovery.csv', self.root/'review.csv', self.root/'out'
        write_csv(self.d, [], research.FIELDS)
        write_csv(self.r, [review()])

    def integrate(self, sources=None, manual=None):
        if sources is not None: write_csv(self.d, sources, research.FIELDS)
        if manual is not None: write_csv(self.r, manual, ci.REVIEW_FIELDS)
        self.target, report = ci.integrate(self.d, self.r, self.out)
        return report

    def rows(self):
        return ci.read_csv(self.target, ci.REVIEW_REQUIRED)[1]

    def edit_export(self, **values):
        fields, rows = ci.read_csv(self.target, ci.REVIEW_REQUIRED)
        rows[0].update(values)
        write_csv(self.target, rows, fields)


class IdentityAndManualTests(OfflineCase):
    def test_alternative_url_learned_and_reused_without_official(self):
        s = source('https://alternative.example/', **{'Official Website (manual)': 'https://manual.example/'})
        self.assertEqual(self.integrate([s])['counts']['existing'], 1)
        s['Official Website (manual)'] = ''
        self.assertEqual(self.integrate([s])['counts']['new'], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_id_survives_manual_name_and_url_changes(self):
        self.integrate([source()], manual=[])
        cid = self.rows()[0]['Candidate ID']
        self.edit_export(**{'Company / Group': 'New Manual Name', 'Original Search URL': 'https://changed.example/'})
        self.integrate([source()])
        self.assertEqual(self.rows()[0]['Candidate ID'], cid)
        self.assertEqual(len(self.rows()), 1)

    def test_renumbered_manual_entry_is_alias_not_duplicate(self):
        self.integrate([source()], manual=[])
        cid = self.rows()[0]['Candidate ID']
        manual = review('MANUAL-NEW', **{'Original Search URL': 'https://new.example/', 'Company / Group': '', 'Official Website': ''})
        self.integrate(manual=[manual])
        self.assertEqual([r['Candidate ID'] for r in self.rows()], [cid])
        manual['Company / Group'] = 'Later name correction'
        self.integrate(manual=[manual])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['Company / Group'], 'Later name correction')

    def test_different_names_shared_url_is_ambiguous(self):
        rows, events = ci.merge_candidates([source('https://manual.example/', **{'Company Name (manual)': 'Other Company'})], [review()])
        self.assertEqual(events[0]['action'], 'ambiguous')
        self.assertEqual(rows, [review()])

    def test_manual_identity_conflict_is_quarantined_until_explicit_resolution(self):
        self.integrate()
        different = review('OTHER', **{'Company / Group': 'Different legal entity'})
        report = self.integrate(manual=[different])
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(any(c['field'] == '__identity__' for c in report['conflicts']))
        self.assertTrue(self.integrate(manual=[])['conflicts'])
        write_csv(self.r, [different])
        _, report = ci.integrate(self.d, self.r, self.out, identity_map={'OTHER': 'R1'})
        self.assertFalse(report['conflicts'])
        self.assertEqual(self.rows()[0]['Candidate ID'], 'R1')
        self.assertEqual(self.rows()[0]['Company / Group'], 'Different legal entity')

    def test_explicit_resolution_cannot_merge_two_established_ids(self):
        self.integrate([source()])
        other = self.rows()[1]['Candidate ID']
        with self.assertRaises(ValueError): ci.integrate(self.d, self.r, self.out, identity_map={other: 'R1'})

    def test_existing_duplicate_urls_are_reported_without_deletion(self):
        self.integrate(manual=[review('A'), review('B')])
        report = self.integrate()
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(any(set(w['candidate_ids']) == {'A', 'B'} for w in report['identity_warnings']))

    def test_shared_group_or_domain_does_not_merge(self):
        for s in [source('https://manual.example/other'), source(**{'Group Key': 'Dairy Group'})]:
            _, events = ci.merge_candidates([s], [review(**{'Group Key': 'Dairy Group'})])
            self.assertEqual(events[0]['action'], 'ambiguous')

    def test_extended_discovery_group_hints_are_preserved_without_asserting_facts(self):
        rows, events = ci.merge_candidates([source(**{'Group Key': 'Shared'}),
                                            source('https://other.example/', **{'Group Key': 'shared'})], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(events[1]['action'], 'ambiguous')
        self.assertEqual(rows[0]['Group Key'], '')

    def test_same_url_different_country_is_ambiguous_not_exception(self):
        rows, events = ci.merge_candidates([source(), source(Country='France')], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(events[1]['action'], 'ambiguous')

    def test_export_only_correction_is_preserved(self):
        self.integrate()
        self.edit_export(**{'Research Notes': 'correction in working copy'})
        self.integrate()
        self.assertEqual(self.rows()[0]['Research Notes'], 'correction in working copy')
        self.integrate()
        self.assertEqual(self.rows()[0]['Research Notes'], 'correction in working copy')

    def test_master_only_correction_is_preserved(self):
        self.integrate()
        self.integrate(manual=[review(**{'Research Notes': 'master correction'})])
        self.assertEqual(self.rows()[0]['Research Notes'], 'master correction')

    def test_concurrent_field_conflicts_persist_and_resolve(self):
        self.integrate()
        self.edit_export(**{'Research Notes': 'working correction'})
        report = self.integrate(manual=[review(**{'Research Notes': 'master correction'})])
        self.assertEqual(self.rows()[0]['Research Notes'], 'original manual note')
        conflict = next(c for c in report['conflicts'] if c['field'] == 'Research Notes')
        self.assertEqual((conflict['manual'], conflict['integrated']), ('master correction', 'working correction'))
        report = self.integrate()
        conflict = next(c for c in report['conflicts'] if c['field'] == 'Research Notes')
        self.assertEqual(conflict['integrated'], 'working correction')
        self.edit_export(**{'Research Notes': 'master correction'})
        self.assertFalse(self.integrate()['conflicts'])
        self.assertEqual(self.rows()[0]['Research Notes'], 'master correction')

    def test_independent_manual_edits_merge_by_field(self):
        self.integrate()
        self.edit_export(**{'Research Notes': 'working correction'})
        report = self.integrate(manual=[review(**{'Reviewed On': '2026-10-17'})])
        self.assertFalse(report['conflicts'])
        self.assertEqual(self.rows()[0]['Research Notes'], 'working correction')
        self.assertEqual(self.rows()[0]['Reviewed On'], '2026-10-17')

    def test_manual_conflict_can_resolve_back_to_original_value(self):
        self.integrate()
        self.edit_export(**{'Research Notes': 'working'})
        self.integrate(manual=[review(**{'Research Notes': 'master'})])
        report = self.integrate(manual=[review()])
        self.assertFalse(report['conflicts'])
        self.assertEqual(self.rows()[0]['Research Notes'], 'original manual note')

    def test_deleted_rows_are_not_silently_removed(self):
        self.integrate([source()])
        before = self.rows()
        self.integrate(sources=[], manual=[])
        self.assertEqual(self.rows(), before)

    def test_repetition_preserves_bytes_and_manual_exclusions(self):
        self.integrate([source()], [review(**{'Project Eligibility': 'Excluded - duplicate'})])
        before = self.target.read_bytes()
        for _ in range(5): self.integrate()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.rows()[0]['Project Eligibility'], 'Excluded - duplicate')

    def test_ids_with_whitespace_or_duplicates_are_rejected(self):
        for manual in [[review('R1'), review(' R1 ')], [review(), review()], [review('=1+1')]]:
            with self.assertRaises(ValueError): self.integrate(manual=manual)

    def test_unknown_manual_columns_and_multiline_text_survive(self):
        row = review(**{'Custom evidence': 'extra', 'Research Notes': ' notes, accents \u00e1\nnext '})
        write_csv(self.r, [row])
        self.integrate()
        self.assertEqual(self.rows()[0], row)

    def test_removing_a_column_does_not_clear_historical_data(self):
        row = review(**{'Custom evidence': 'historical reference'})
        write_csv(self.r, [row])
        self.integrate()
        write_csv(self.r, [review()])
        fields, rows = ci.read_csv(self.target, ci.REVIEW_REQUIRED)
        fields.remove('Custom evidence')
        rows[0].pop('Custom evidence')
        write_csv(self.target, rows, fields)
        self.integrate()
        self.assertEqual(self.rows()[0]['Custom evidence'], 'historical reference')

    def test_working_copy_renumbering_preserves_canonical_id(self):
        self.integrate()
        self.edit_export(**{'Candidate ID': 'RENAMED-ID'})
        self.integrate()
        self.assertEqual([r['Candidate ID'] for r in self.rows()], ['R1'])

    def test_incoming_manual_suggestions_do_not_override_qa(self):
        report = self.integrate([source('https://manual.example/', **{'Research Notes': 'incoming note', 'Verification Status': 'Verified'})])
        self.assertEqual(self.rows()[0]['Research Notes'], 'original manual note')
        self.assertIn('Research Notes', report['events'][0]['suggestions'])


class ValidationAndPrivacyTests(OfflineCase):
    def test_all_url_columns_are_quarantined_without_crashing_audit(self):
        for field in URL_FIELDS - {'Search Result URL', 'Original Search URL'}:
            s = source(**{field: 'https://['})
            rows, events = ci.merge_candidates([s], [])
            self.assertTrue(events[0]['url_issues'])
            self.assertIsInstance(audit.sources_for(rows[0], 4), list)

    def test_invalid_manual_url_retained_in_state_not_audit_projection(self):
        self.integrate(manual=[review(**{'Public Contact Page': 'https://['})])
        self.assertEqual(self.rows()[0]['Public Contact Page'], '')
        state = storage.Store(self.out).load()
        self.assertEqual(state['rows'][0]['Public Contact Page'], 'https://[')
        self.integrate()
        self.assertEqual(storage.Store(self.out).load()['rows'][0]['Public Contact Page'], 'https://[')

    def test_rejected_url_and_encoded_query_credentials_are_redacted(self):
        for url in ['https://fixture:SYNTHETIC_PASSWORD_ONLY@public.example/',
                    'https://public.example/?token=SYNTHETIC_PASSWORD_ONLY',
                    'https://public.example/?%74oken=SYNTHETIC_PASSWORD_ONLY',
                    'https://public.example/#token=SYNTHETIC_PASSWORD_ONLY',
                    'https://public.example/?sig=SYNTHETIC_PASSWORD_ONLY']:
            report = self.integrate([source(url)])
            self.assertEqual(report['counts']['rejected'], 1)
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'SYNTHETIC_PASSWORD_ONLY', path.read_bytes(), str(path))

    def test_known_credentials_in_free_text_are_redacted(self):
        values = ['Authorization: Bearer SYNTHETIC_PASSWORD_ONLY', 'password=SYNTHETIC_PASSWORD_ONLY',
                  'API key: SYNTHETIC_PASSWORD_ONLY',
                  '-----BEGIN PRIVATE KEY-----\nSYNTHETIC_PASSWORD_ONLY\n-----END PRIVATE KEY-----']
        for value in values:
            self.assertNotIn('SYNTHETIC_PASSWORD_ONLY', redact(value))

    def test_formula_projection_is_reversible_without_prefix_growth(self):
        for value in ['=1+1', '+SUM(1)', '-1', '@SUM(1)', '\t=1', "'=literal"]:
            with self.subTest(value=value):
                self.integrate(manual=[review(**{'Company / Group': value})])
                cell = self.rows()[0]['Company / Group']
                self.assertFalse(cell.startswith(('=', '+', '-', '@', '\t')))
                state = storage.Store(self.out).load()
                self.assertEqual(state['rows'][0]['Company / Group'], value)
                before = self.target.read_bytes()
                self.integrate()
                self.assertEqual(self.target.read_bytes(), before)

    def test_demo_manual_and_previous_state_cannot_enter_live(self):
        with self.assertRaises(ValueError): self.integrate(manual=[review('DEMO-N-fixture')])
        write_csv(self.r, [])
        ci.integrate(self.d, self.r, self.out, demo=True)
        with self.assertRaises(ValueError): ci.integrate(self.d, self.r, self.out)

    def test_url_normalization_and_rejections(self):
        self.assertEqual(normalize_url('https://WWW.a.example:443/?utm_source=x#foo'), 'https://a.example/')
        self.assertNotEqual(normalize_url('https://a.example/?id=1'), normalize_url('https://a.example/?id=2'))
        self.assertNotEqual(normalize_url('https://a.example/?id=%FF'), normalize_url('https://a.example/?id=%FE'))
        for url in ['https://a.example:0', 'https://a.example:65536', 'http://127.1', 'http://2130706433',
                    'http://localhost.', 'http://service.local', 'https://[::1]', 'http://224.0.0.1/', 'file:///tmp/a',
                    'https://host.example/space here', 'https://a.example/%GG', 'https://user:pass@a.example']:
            self.assertEqual(public_url(url), '', url)
        for value in ['https://\u00e9xample.test/\u00e1?x=\u00e9', 'https://[2606:4700:4700::1111]:8443/a']:
            self.assertTrue(normalize_url(value))
            self.assertEqual(normalize_url(normalize_url(value)), normalize_url(value))

    def test_malformed_csv_and_headers_are_rejected(self):
        for text in ['Candidate ID,Country\n', 'Candidate ID,Country,Search Result URL,Verification Status,Country\n',
                     'Candidate ID,Country,Search Result URL,Verification Status\nx,Chile\n',
                     'Candidate ID,Country,Search Result URL,Verification Status\nx,Chile,https://a.example/,"Needs review']:
            self.d.write_text(text)
            with self.assertRaises(ValueError): ci.read_csv(self.d, ci.DISCOVERY_REQUIRED)

    def test_spreadsheet_formula_header_is_rejected(self):
        write_csv(self.r, [review(**{'=1+1': 'unsafe header'})])
        with self.assertRaises(ValueError): self.integrate()

    def test_wrong_report_type_is_controlled_error(self):
        self.out.mkdir()
        (self.out/'integration_report.json').write_text('[]')
        with self.assertRaises(ValueError): self.integrate()

    def test_outputs_cannot_alias_inputs(self):
        manual = self.root/'candidate_review.csv'
        write_csv(manual, [review()])
        before = manual.read_bytes()
        with self.assertRaises(ValueError): ci.integrate(self.d, manual, self.root)
        self.assertEqual(manual.read_bytes(), before)

    def test_credential_paths_are_rejected_without_reading(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('No credential reads')):
            with self.assertRaises(ValueError): ci.integrate(self.root/'.env', self.r, self.out)


class RecoveryTests(OfflineCase):
    def test_atomic_failure_keeps_old_and_removes_temporary(self):
        path = self.root/'original.txt'; path.write_bytes(b'old')
        with patch.object(storage.os, 'replace', side_effect=PermissionError('simulated lock')):
            with self.assertRaises(PermissionError): storage.atomic_bytes(path, b'new')
        self.assertEqual(path.read_bytes(), b'old')
        self.assertFalse(list(self.root.glob('.integration-*.tmp')))

    def test_failure_before_commit_does_not_change_exports(self):
        self.integrate()
        before = (self.target.read_bytes(), (self.out/'integration_report.json').read_bytes(), (self.out/'.integration/CURRENT').read_bytes())
        real = storage.atomic_bytes
        def fail(path, data):
            if path.name == 'CURRENT': raise OSError('simulated disk full')
            return real(path, data)
        with patch.object(storage, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(OSError): self.integrate([source()])
        self.assertEqual(before, (self.target.read_bytes(), (self.out/'integration_report.json').read_bytes(), (self.out/'.integration/CURRENT').read_bytes()))
        self.assertEqual(self.integrate()['counts']['new'], 1)

    def test_bad_staged_bytes_are_detected_before_confirmation(self):
        self.integrate()
        before = (self.target.read_bytes(), (self.out/'.integration/CURRENT').read_bytes())
        real = storage.atomic_bytes
        def corrupt(path, data):
            return real(path, b'corrupt' if path.name == 'state.json' else data)
        with patch.object(storage, 'atomic_bytes', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'Staging checksum'): self.integrate([source()])
        self.assertEqual(before, (self.target.read_bytes(), (self.out/'.integration/CURRENT').read_bytes()))

    def test_failure_after_commit_rolls_forward_both_exports(self):
        for failed_file in ['candidate_review.csv', 'integration_report.json']:
            with self.subTest(file=failed_file):
                self.integrate(sources=[])
                real = storage.atomic_bytes
                def fail(path, data):
                    if path == self.out/failed_file: raise PermissionError('simulated Excel lock')
                    return real(path, data)
                s = source('https://' + failed_file.replace('.', '-').replace('_', '-') + '.example/')
                self.assertTrue(public_url(s['Search Result URL']))
                with patch.object(storage, 'atomic_bytes', side_effect=fail):
                    with self.assertRaises(PermissionError): self.integrate([s])
                recovered = storage.Store(self.out).load()
                self.assertTrue(any(r['Original Search URL'] == s['Search Result URL'] for r in recovered['rows']))
                self.assertEqual(self.integrate()['counts']['new'], 0)

    def test_checksum_corruption_stops_without_overwriting_working_copy(self):
        self.integrate()
        before = self.target.read_bytes()
        generation = (self.out/'.integration/CURRENT').read_text()
        (self.out/'.integration'/generation/'state.json').write_text('{}')
        with self.assertRaises(ValueError): self.integrate()
        self.assertEqual(self.target.read_bytes(), before)

    def test_missing_current_does_not_silently_remigrate(self):
        self.integrate()
        before = self.target.read_bytes()
        (self.out/'.integration/CURRENT').unlink()
        with self.assertRaisesRegex(ValueError, 'CURRENT is missing'): self.integrate()
        self.assertEqual(self.target.read_bytes(), before)

    def test_missing_projection_is_restored_from_committed_generation(self):
        self.integrate()
        before = self.target.read_bytes()
        self.target.unlink()
        storage.Store(self.out).load()
        self.assertEqual(self.target.read_bytes(), before)

    def test_writer_lock_and_stale_temp_cleanup(self):
        self.integrate()
        orphan = self.out/'.integration-abandoned.tmp'; orphan.write_bytes(b'partial')
        with storage.writer_lock(self.out):
            with self.assertRaises(ValueError): self.integrate()
        self.integrate()
        self.assertFalse(orphan.exists())

    def test_recovery_does_not_discard_edits_after_interrupted_export(self):
        self.integrate()
        real = storage.atomic_bytes
        def fail(path, data):
            if path == self.target: raise PermissionError('simulated lock')
            return real(path, data)
        with patch.object(storage, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(PermissionError): self.integrate([source()])
        self.edit_export(**{'Research Notes': 'edit after crash'})
        before = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, 'edits detected'): self.integrate()
        self.assertEqual(self.target.read_bytes(), before)

    def test_edit_during_processing_is_not_overwritten(self):
        self.integrate()
        real = ci.merge_candidates
        def edit_mid_run(*args, **kwargs):
            self.edit_export(**{'Research Notes': 'concurrent human edit'})
            return real(*args, **kwargs)
        with patch.object(ci, 'merge_candidates', side_effect=edit_mid_run):
            with self.assertRaisesRegex(ValueError, 'changed during integration'): self.integrate()
        self.assertEqual(self.rows()[0]['Research Notes'], 'concurrent human edit')

    def test_legacy_migration_keeps_ids_and_aliases(self):
        self.out.mkdir()
        write_csv(self.out/'candidate_review.csv', [review('C-019')])
        write_csv(self.r, [review('C-019')])
        event = {'action': 'existing', 'candidate_id': 'C-019', 'source': source('https://alias.example/')}
        (self.out/'integration_report.json').write_text(json.dumps({'demo': False, 'provenance': [event]}))
        report = self.integrate([source('https://alias.example/')])
        self.assertEqual(report['counts']['existing'], 1)
        self.assertEqual(self.rows()[0]['Candidate ID'], 'C-019')
        self.assertTrue((self.out/'.integration/CURRENT').exists())


class PipelineRegressionTests(OfflineCase):
    def test_research_keeps_distinct_query_pages_without_api(self):
        class FakeClient:
            def search(self, **kwargs):
                return {'results': [{'url': 'https://directory.example/?id=1'}, {'url': 'https://directory.example/?id=2'}]}
        with patch.object(research, 'SEARCH_QUERIES', {'Chile': ['fixture']}), patch.object(research.time, 'sleep'):
            rows, _ = research.collect(FakeClient(), demo=False, queries_per_country=1, results_per_query=2)
        self.assertEqual(len(rows), 2)

    def test_research_preserves_query_and_country_observations(self):
        class FakeClient:
            def search(self, **kwargs): return {'results': [{'url': 'https://shared.example/'}]}
        with patch.object(research, 'SEARCH_QUERIES', {'Chile': ['query one', 'query two'], 'France': ['query three']}), patch.object(research.time, 'sleep'):
            rows, _ = research.collect(FakeClient(), demo=False, queries_per_country=2, results_per_query=2)
        self.assertEqual(len(rows), 3)
        merged, events = ci.merge_candidates(rows, [])
        self.assertEqual(len(merged), 1)
        self.assertEqual([e['action'] for e in events], ['new', 'existing', 'ambiguous'])

    def test_invalid_search_responses_are_errors_without_network(self):
        class FakeClient:
            def __init__(self, response): self.response = response
            def search(self, **kwargs): return self.response
        with patch.object(research, 'SEARCH_QUERIES', {'Chile': ['fixture']}), patch.object(research.time, 'sleep'):
            for response in [None, {'results': None}, {'results': ['bad item']}]:
                rows, errors = research.collect(FakeClient(response), demo=False, queries_per_country=1, results_per_query=2)
                self.assertFalse(rows); self.assertTrue(errors)

    def test_research_demo_works_without_manual_input(self):
        before = Path.cwd()
        try:
            os.chdir(self.root)
            with patch('sys.argv', ['research.py', '--demo']): research.main()
            self.assertTrue((self.root/'output/integration_v2_2_DEMO/candidate_review.csv').exists())
        finally:
            os.chdir(before)

    def test_research_export_keeps_previous_content_backup(self):
        row = source()
        target, _ = research.save([row], [], self.root, False)
        previous = target.read_bytes()
        research.save([source('https://second.example/')], [], self.root, False)
        self.assertIn(previous, [p.read_bytes() for p in (self.root/'.exports/discovery').glob('*/before-candidates.csv')])

    def test_full_pipeline_audit_uses_v21_with_fixture_fetcher(self):
        self.integrate([source()])
        class FixtureFetcher:
            def __init__(self, **kwargs): pass
            def fetch(self, url):
                return base.FetchResult(url, 'ok', '200', 'Fixture', 'We operate dairy manufacturing plants in Chile. Our factories produce cheese and butter.')
        with patch.object(audit, 'CleanPublicFetcher', FixtureFetcher):
            code = pipeline.main(['--input', str(self.d), '--review', str(self.r), '--out', str(self.out), '--audit'])
        self.assertEqual(code, 0)
        for name in ['audit_results.csv', 'page_checks.csv', 'evidence_snippets.csv', 'alerts_detailed.csv',
                     'manual_followups.csv', 'quality_comparison.csv', 'audit_summary.md']:
            self.assertTrue((self.out/'audit'/name).exists())

    def test_end_to_end_mock_discovery_manual_review_and_audit(self):
        class FakeClient:
            def __init__(self): self.calls = 0
            def search(self, **kwargs):
                self.calls += 1
                return {'results': [{'url': f'https://fixture-{self.calls}.example/', 'title': 'Search observation'}]}
        fake = FakeClient()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch('sys.argv', ['research.py']), patch.object(research, 'load_dotenv'), \
                 patch.object(research.os, 'getenv', return_value='SYNTHETIC_KEY_ONLY'), \
                 patch.dict('sys.modules', {'tavily': SimpleNamespace(TavilyClient=lambda **kw: fake)}), \
                 patch.object(research.time, 'sleep'):
                research.main()
            out = self.root/'output/integration_v2_2'
            target = out/'candidate_review.csv'
            fields, rows = ci.read_csv(target, ci.REVIEW_REQUIRED)
            self.assertEqual(len(rows), 3)
            rows[0].update({'Company / Group': 'Manually Reviewed Fixture', 'Official Website': rows[0]['Original Search URL'],
                            'Manufacturing Confirmed': 'Yes', 'Project Eligibility': 'Eligible'})
            write_csv(target, rows, fields)
            class FakeFetcher:
                def __init__(self, **kwargs): pass
                def fetch(self, url):
                    return base.FetchResult(url, 'ok', '200', 'Fixture', 'We operate dairy manufacturing plants in France. Our factories produce cheese and butter.')
            with patch.object(audit, 'CleanPublicFetcher', FakeFetcher):
                self.assertEqual(pipeline.main(['--audit']), 0)
            audit_rows = ci.read_csv(out/'audit/audit_results.csv', {'Candidate ID'})[1]
            self.assertEqual(len(audit_rows), 3)
            self.assertGreater(int(audit_rows[0]['Own Manufacturing Claims']), 0)
            self.assertEqual(audit_rows[0]['Manual Eligibility (unchanged)'], 'Eligible')
            self.assertEqual(fake.calls, 3)
        finally:
            os.chdir(previous)

    def test_manual_conflict_blocks_audit(self):
        self.integrate()
        self.edit_export(**{'Research Notes': 'working'})
        write_csv(self.r, [review(**{'Research Notes': 'master'})])
        with patch.object(audit, 'main') as run:
            code = pipeline.main(['--input', str(self.d), '--review', str(self.r), '--out', str(self.out), '--audit'])
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_audit_reads_confirmed_snapshot_despite_later_root_edits(self):
        real = ci.integrate
        def concurrent_edit(*args, **kwargs):
            target, report = real(*args, **kwargs)
            write_csv(target, [review('CONCURRENT')])
            return target, report
        with patch.object(pipeline, 'integrate', side_effect=concurrent_edit), patch.object(audit, 'main', return_value=0) as run:
            self.assertEqual(pipeline.main(['--input', str(self.d), '--review', str(self.r), '--out', str(self.out), '--audit']), 0)
        snapshot = Path(run.call_args.args[0][1])
        self.assertIn('.integration', snapshot.parts)
        self.assertEqual(ci.read_csv(snapshot, ci.REVIEW_REQUIRED)[1][0]['Candidate ID'], 'R1')

    def test_demo_audit_combination_rejected(self):
        with self.assertRaises(SystemExit) as caught: pipeline.main(['--demo', '--audit'])
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
