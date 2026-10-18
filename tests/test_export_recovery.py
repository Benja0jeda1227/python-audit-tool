"""Multi-artifact discovery/audit export failure and restart scenarios."""
from unittest.mock import patch

import integration_store as storage
import research
import candidate_integration as ci
import website_audit_v2_1 as audit
from test_v22_regressions import OfflineCase, source


class ExportRecoveryTests(OfflineCase):
    def test_bundle_before_commit_failure_preserves_entire_previous_export(self):
        bundle = storage.ExportBundle(self.out, 'fixture')
        self.out.mkdir()
        original = {'first.csv': b'old first', 'second.json': b'old second'}
        with storage.writer_lock(self.out): bundle.commit(original)
        real = storage.atomic_bytes
        def fail(path, data):
            if path.name == 'CURRENT': raise OSError('simulated storage failure')
            return real(path, data)
        with storage.writer_lock(self.out), patch.object(storage, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(OSError): bundle.commit({'first.csv': b'new first', 'second.json': b'new second'})
        self.assertEqual({k: (self.out/k).read_bytes() for k in original}, original)

    def test_partial_bundle_recovers_and_retains_previous_bytes(self):
        self.out.mkdir()
        bundle = storage.ExportBundle(self.out, 'fixture')
        with storage.writer_lock(self.out): bundle.commit({'first.csv': b'old', 'second.json': b'old'})
        real = storage.atomic_bytes
        def fail(path, data):
            if path == self.out/'second.json': raise PermissionError('simulated Excel lock')
            return real(path, data)
        with storage.writer_lock(self.out), patch.object(storage, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(PermissionError): bundle.commit({'first.csv': b'new first', 'second.json': b'new second'})
        with storage.writer_lock(self.out): bundle.recover()
        self.assertEqual((self.out/'first.csv').read_bytes(), b'new first')
        self.assertEqual((self.out/'second.json').read_bytes(), b'new second')
        self.assertTrue(any(p.read_bytes() == b'old' for p in bundle.root.glob('*/before-first.csv')))

    def test_discovery_csv_and_summary_recover_together(self):
        target, summary = research.save([source()], [], self.root, False)
        previous_summary = summary.read_bytes()
        real = storage.atomic_bytes
        def fail(path, data):
            if path == summary: raise PermissionError('simulated summary lock')
            return real(path, data)
        with patch.object(storage, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(PermissionError): research.save([source(), source('https://second.example/')], [], self.root, False)
        self.assertEqual(summary.read_bytes(), previous_summary)
        with storage.writer_lock(self.root): storage.ExportBundle(self.root, 'discovery').recover()
        self.assertIn('Observaciones candidatas: 2', summary.read_text(encoding='utf-8'))

    def test_integration_recovers_discovery_before_consuming_it(self):
        target, _ = research.save([source()], [], self.root, False)
        real = storage.atomic_bytes
        def fail(path, data):
            if path == target: raise PermissionError('simulated discovery lock')
            return real(path, data)
        with patch.object(storage, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(PermissionError): research.save([source('https://newer.example/')], [], self.root, False)
        integrated, _ = ci.integrate(target, self.r, self.out)
        rows = ci.read_csv(integrated, ci.REVIEW_REQUIRED)[1]
        self.assertTrue(any(r['Original Search URL'] == 'https://newer.example/' for r in rows))

    def test_audit_export_recovers_complete_seven_file_set(self):
        results, pages, evidence, alerts = audit.audit(audit.demo_rows(), audit.DemoFetcher())
        audit.write_outputs(self.out, results, pages, evidence, alerts, 'demo')
        results[0]['Company'] = 'Updated fixture name'
        real = storage.atomic_bytes
        def fail(path, data):
            if path == self.out/'page_checks.csv': raise PermissionError('simulated report lock')
            return real(path, data)
        with patch.object(storage, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(PermissionError): audit.write_outputs(self.out, results, pages, evidence, alerts, 'demo')
        with storage.writer_lock(self.out): storage.ExportBundle(self.out, 'audit').recover()
        self.assertIn('Updated fixture name', (self.out/'audit_results.csv').read_text(encoding='utf-8-sig'))
        self.assertEqual(len([p for p in self.out.iterdir() if p.suffix in {'.csv', '.md'}]), 7)

    def test_bundle_refuses_to_overwrite_edits_made_after_crash(self):
        self.out.mkdir()
        bundle = storage.ExportBundle(self.out, 'fixture')
        with storage.writer_lock(self.out): bundle.commit({'one.csv': b'old', 'two.csv': b'old'})
        real = storage.atomic_bytes
        def fail(path, data):
            if path == self.out/'two.csv': raise PermissionError('simulated lock')
            return real(path, data)
        with storage.writer_lock(self.out), patch.object(storage, 'atomic_bytes', side_effect=fail):
            with self.assertRaises(PermissionError): bundle.commit({'one.csv': b'new', 'two.csv': b'new'})
        (self.out/'two.csv').write_bytes(b'human edit')
        with self.assertRaisesRegex(ValueError, 'external edits'): bundle.recover()
        self.assertEqual((self.out/'two.csv').read_bytes(), b'human edit')

    def test_bundle_checksum_validation_and_storage_flush_failure(self):
        self.out.mkdir()
        bundle = storage.ExportBundle(self.out, 'fixture')
        with storage.writer_lock(self.out): bundle.commit({'one.csv': b'old'})
        generation = (bundle.root/'CURRENT').read_text()
        (bundle.root/generation/'one.csv').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'): bundle.recover()
        path = self.root/'keep.txt'; path.write_bytes(b'keep')
        with patch.object(storage.os, 'fsync', side_effect=OSError('simulated disk full')):
            with self.assertRaises(OSError): storage.atomic_bytes(path, b'new')
        self.assertEqual(path.read_bytes(), b'keep')
