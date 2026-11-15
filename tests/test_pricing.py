"""Local protocol tests, not provider quotations, bills or paid model calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from solve2ship.contracts import canonical_json, sha256
from solve2ship.gateway import Ledger, MICRO, BudgetError
from solve2ship.pricing import PricingCatalog, PricingError, canonical_endpoint, validate_document
from solve2ship.store import Store


def instant(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()


def document(**changes):
    return {**{
        'schema_version': 1, 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
        'documented_model_version': 'test-only-model-version', 'currency': 'CNY',
        'input_micro_per_token': 2, 'output_micro_per_token': 8, 'context_ceiling': 1048576,
        'max_output_tokens': 8192, 'source': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
        'source_sha256': 'a' * 64, 'reviewed_at': '2026-11-13T10:00:00Z',
        'valid_from': '2026-11-13T10:00:00Z', 'expires_at': '2026-11-15T10:00:00Z',
    }, **changes}


class PricingCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = PricingCatalog(self.root / 'catalog')
        self.clock = patch('solve2ship.pricing.time.time', return_value=instant('2026-11-13T12:00:00Z')).start()
        self.addCleanup(patch.stopall)
        self.file = self.write('review.json', document())

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value) if not isinstance(value, str) else value)
        path.chmod(0o600)
        return path

    def activate(self, value=None, name='review.json'):
        path = self.write(name, value or document())
        preview = self.catalog.preview(path)
        return self.catalog.import_reviewed(path, preview['version_id'])

    def test_preview_and_missing_status_are_read_only_and_explicit_hash_is_required(self):
        preview = self.catalog.preview(self.file)
        self.assertEqual(preview['id'], sha256(document()))
        self.assertEqual(preview['verified_date'], '2026-11-13')
        self.assertFalse(self.catalog.directory.exists())
        self.assertEqual(self.catalog.status(), {'ready': False, 'reason': 'price_snapshot_requires_review',
                                               'active_version_id': None, 'snapshot': None})
        with self.assertRaisesRegex(PricingError, 'pricing_review_hash_mismatch'):
            self.catalog.import_reviewed(self.file, 'b' * 64)
        self.assertFalse(self.catalog.directory.exists())
        self.write('review.json', document(output_micro_per_token=9))
        with self.assertRaisesRegex(PricingError, 'pricing_review_hash_mismatch'):
            self.catalog.import_reviewed(self.file, preview['id'])
        self.assertFalse(self.catalog.directory.exists())

    def test_explicit_valid_window_crosses_utc_midnight_and_expires_exactly(self):
        snapshot = self.activate()
        for stamp in ['2026-11-13T23:59:59Z', '2026-11-14T00:00:00Z', '2026-11-15T09:59:59Z']:
            with self.subTest(stamp=stamp):
                self.assertEqual(self.catalog.resolve(document()['base_url'], 'deepseek-flash', now=instant(stamp)), snapshot)
        self.clock.return_value = instant('2026-11-15T10:00:00Z')
        before = {p.relative_to(self.catalog.directory): p.read_bytes()
                  for p in self.catalog.directory.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(PricingError, 'pricing_expired'):
            self.catalog.resolve(document()['base_url'], 'deepseek-flash')
        status = self.catalog.status()
        self.assertFalse(status['ready'])
        self.assertEqual(status['reason'], 'pricing_expired')
        self.assertEqual(status['snapshot'], snapshot)
        self.assertEqual(before, {p.relative_to(self.catalog.directory): p.read_bytes()
                                  for p in self.catalog.directory.rglob('*') if p.is_file()})

    def test_future_validity_is_explicit_and_future_review_is_rejected(self):
        snapshot = self.activate(document(valid_from='2026-11-14T00:00:00Z'))
        status = self.catalog.status()
        self.assertEqual(status['reason'], 'pricing_not_yet_valid')
        self.assertEqual(status['active_version_id'], snapshot['version_id'])
        with self.assertRaisesRegex(PricingError, 'pricing_not_yet_valid'):
            self.catalog.resolve(document()['base_url'], 'deepseek-flash')
        self.clock.return_value = instant('2026-11-14T00:00:00Z')
        self.assertTrue(self.catalog.status()['ready'])
        self.assertEqual(self.catalog.resolve(document()['base_url'], 'deepseek-flash'), snapshot)
        with self.assertRaisesRegex(PricingError, 'pricing_review_in_future'):
            validate_document(document(reviewed_at='2026-11-15T10:00:00Z', valid_from='2026-11-15T10:00:00Z',
                                       expires_at='2026-11-16T10:00:00Z'))

    def test_versions_remain_resolvable_and_are_never_overwritten(self):
        first = self.activate()
        original_file = self.catalog.directory / 'versions' / (first['version_id'] + '.json')
        original_bytes = original_file.read_bytes()
        second = self.activate(document(input_micro_per_token=3), 'new-review.json')
        self.assertNotEqual(first['version_id'], second['version_id'])
        self.assertEqual(original_file.read_bytes(), original_bytes)
        self.assertEqual(self.catalog.resolve(document()['base_url'], 'deepseek-flash'), second)
        self.assertEqual(self.catalog.resolve(document()['base_url'], 'deepseek-flash', first['id']), first)
        self.assertEqual(json.loads(original_file.read_text()), document())
        self.assertEqual(original_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.catalog.directory / 'active.json').stat().st_mode & 0o777, 0o600)
        for folder in [self.catalog.directory, self.catalog.directory / 'versions']:
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)

    def test_endpoint_and_model_bindings_cannot_borrow_other_prices(self):
        self.activate()
        self.assertEqual(canonical_endpoint('https://API.DEEPSEEK.COM:443/'), 'https://api.deepseek.com')
        self.assertEqual(canonical_endpoint('https://api.deepseek.com:443/v1/'), 'https://api.deepseek.com/v1')
        for endpoint in ['https://api.deepseek.com/v1', 'https://evil.example',
                         'http://api.deepseek.com', 'https://api.deepseek.com:8443',
                         'https://api.deepseek.com/?key=not-a-key', 'https://user@api.deepseek.com',
                         'https://api.deepseek.com/#fragment', 'https://api.deepseek.com/other',
                         ' https://api.deepseek.com', 'https://api.deepseek.com\n']:
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(PricingError, 'pricing_endpoint_mismatch'):
                self.catalog.resolve(endpoint, 'deepseek-flash')
        with self.assertRaisesRegex(PricingError, 'pricing_model_mismatch'):
            self.catalog.resolve(document()['base_url'], 'another-model')
        with self.assertRaisesRegex(PricingError, 'pricing_binding_required'):
            self.catalog.resolve(None, 'deepseek-flash')
        self.assertFalse(self.catalog.status(document()['base_url'], 'another-model')['ready'])

    def test_invalid_schema_rates_source_and_validity_are_refused(self):
        variants = [document(unreviewed=True), document(schema_version=True), document(currency='USD'),
                    document(model='deepseek-pro'), document(input_micro_per_token=True),
                    document(input_micro_per_token=0), document(input_micro_per_token=1.1),
                    document(output_micro_per_token=1000001), document(context_ceiling=2097153),
                    document(max_output_tokens=8193), document(source_sha256='a' * 63),
                    document(source='https://other.example/prices'), document(source='http://api-docs.deepseek.com/pricing'),
                    document(source='https://api-docs.deepseek.com/pricing?api_key=anything'),
                    document(source='https://api-docs.deepseek.com/pricing#fragment'),
                    document(source='https://name:pass@api-docs.deepseek.com/pricing'),
                    document(documented_model_version='unbounded version or instructions'),
                    document(reviewed_at='2026-11-13T10:00:00+00:00'), document(reviewed_at='2026-11-31T10:00:00Z'),
                    document(valid_from='2026-11-13T09:59:59Z'), document(expires_at='2026-11-13T10:00:00Z'),
                    document(expires_at='2026-11-20T10:00:01Z')]
        for index, value in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(PricingError):
                validate_document(value)
        value = document(expires_at='2026-11-20T10:00:00Z')
        self.assertEqual(validate_document(value), value)

    def test_canonical_review_digest_is_stable_across_json_formatting(self):
        first = self.catalog.preview(self.file)
        self.file.write_text(canonical_json(document()) + '\n')
        self.assertEqual(self.catalog.preview(self.file), first)
        self.assertEqual(self.catalog.import_reviewed(self.file, first['id']), first)

    def test_source_files_refuse_symlinks_fifos_directories_loose_or_foreign_ownership(self):
        link = self.root / 'source-link'
        link.symlink_to(self.file)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo, 0o600)
        for path in [link, fifo, self.root]:
            with self.subTest(path=path.name), self.assertRaises(PricingError):
                self.catalog.preview(path)
        self.file.chmod(0o644)
        with self.assertRaisesRegex(PricingError, 'pricing_unsafe_file'):
            self.catalog.preview(self.file)
        self.file.chmod(0o600)
        with patch('solve2ship.pricing.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(PricingError, 'pricing_unsafe_file'):
                self.catalog.preview(self.file)

    def test_catalog_directories_refuse_links_and_shared_permissions(self):
        outside = self.root / 'outside'
        outside.mkdir(mode=0o700)
        self.catalog.directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(PricingError, 'pricing_unsafe_directory'):
            self.activate()
        self.assertFalse(list(outside.iterdir()))
        self.catalog.directory.unlink()
        self.catalog.directory.mkdir(mode=0o700)
        (self.catalog.directory / 'versions').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(PricingError, 'pricing_unsafe_directory'):
            self.activate()
        (self.catalog.directory / 'versions').unlink()
        self.catalog.directory.chmod(0o755)
        with self.assertRaisesRegex(PricingError, 'pricing_unsafe_directory'):
            self.activate()

    def test_corrupt_versions_and_pointers_never_become_ready_or_silently_repaired(self):
        snapshot = self.activate()
        version = self.catalog.directory / 'versions' / (snapshot['id'] + '.json')
        version.write_text(canonical_json(document(input_micro_per_token=1)))
        status = self.catalog.status()
        self.assertEqual(status, {'ready': False, 'reason': 'pricing_version_hash_mismatch',
                                  'active_version_id': None, 'snapshot': None})
        with self.assertRaisesRegex(PricingError, 'pricing_version_hash_mismatch'):
            self.activate()
        self.assertEqual(json.loads(version.read_text())['input_micro_per_token'], 1)
        version.write_text(canonical_json(document()))
        pointer = self.catalog.directory / 'active.json'
        pointer.write_text('{"version_id":"../../outside"}')
        self.assertEqual(self.catalog.status()['reason'], 'pricing_invalid_active_version')
        with self.assertRaisesRegex(PricingError, 'pricing_invalid_active_version'):
            self.activate()

    def test_active_and_version_symlinks_are_never_followed(self):
        snapshot = self.activate()
        pointer = self.catalog.directory / 'active.json'
        outside = self.write('outside-pointer', {'version_id': snapshot['id']})
        pointer.unlink()
        pointer.symlink_to(outside)
        with self.assertRaises(PricingError):
            self.activate()
        self.assertEqual(json.loads(outside.read_text()), {'version_id': snapshot['id']})
        pointer.unlink()
        pointer.write_text(canonical_json({'version_id': snapshot['id']}))
        pointer.chmod(0o600)
        version = self.catalog.directory / 'versions' / (snapshot['id'] + '.json')
        version.unlink()
        version.symlink_to(self.file)
        with self.assertRaises(PricingError):
            self.catalog.resolve(document()['base_url'], 'deepseek-flash')
        self.assertFalse(self.catalog.status()['ready'])

    def test_bounded_strict_json_refuses_duplicate_keys_constants_and_large_files(self):
        for text in ['{"model":"a","model":"b"}', '{"x":NaN}', '[' * 1100,
                     ' ' * 8193, 'null', '[1,2,3]']:
            self.file.write_text(text)
            with self.subTest(length=len(text)), self.assertRaises(PricingError):
                self.catalog.preview(self.file)
        for value in [float('nan'), float('inf'), True, '2026-11-13']:
            with self.subTest(value=value), self.assertRaisesRegex(PricingError, 'pricing_invalid_clock'):
                validate_document(document(), now=value)

    def test_concurrent_same_review_imports_are_idempotent_and_fully_written(self):
        digest = self.catalog.preview(self.file)['id']
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.catalog.import_reviewed(self.file, digest), range(16)))
        self.assertEqual({row['id'] for row in results}, {digest})
        self.assertEqual([p.name for p in (self.catalog.directory / 'versions').iterdir()], [digest + '.json'])
        self.assertEqual(self.catalog.resolve(document()['base_url'], 'deepseek-flash')['id'], digest)
        self.assertFalse(list(self.catalog.directory.glob('.review-*')))

    def test_concurrent_distinct_reviews_retain_every_version_and_complete_active_snapshot(self):
        first = self.activate()
        reviewed = [(self.write('review-' + str(index) + '.json', document(output_micro_per_token=10 + index)))
                    for index in range(6)]
        pairs = [(path, self.catalog.preview(path)['id']) for path in reviewed]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda pair: self.catalog.import_reviewed(*pair), pairs))
        expected = {first['id']} | {row['id'] for row in results}
        self.assertEqual({p.stem for p in (self.catalog.directory / 'versions').iterdir()}, expected)
        self.assertIn(self.catalog.resolve(document()['base_url'], 'deepseek-flash')['id'], expected)
        for version in expected:
            self.assertEqual(self.catalog.resolve(document()['base_url'], 'deepseek-flash', version)['id'], version)

    def test_explicit_expired_import_is_audit_history_and_does_not_renew(self):
        self.clock.return_value = instant('2026-11-18T12:00:00Z')
        snapshot = self.activate()
        self.assertEqual(snapshot['expires_at'], document()['expires_at'])
        self.assertEqual(self.catalog.status()['reason'], 'pricing_expired')
        with self.assertRaisesRegex(PricingError, 'pricing_expired'):
            self.catalog.resolve(document()['base_url'], 'deepseek-flash')


class PricingLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.ledger = Ledger(self.store)
        self.ledger.configure('work', 100 * MICRO)
        self.ledger.configure('scope', 10 * MICRO)
        self.clock = patch('solve2ship.pricing.time.time', return_value=instant('2026-11-13T12:00:00Z')).start()
        self.addCleanup(patch.stopall)
        self.catalog = PricingCatalog(self.root / 'pricing')

    def activate(self, **changes):
        path = self.root / ('review-' + str(uuid.uuid4()) + '.json')
        path.write_text(json.dumps(document(**changes)))
        path.chmod(0o600)
        preview = self.catalog.preview(path)
        return self.catalog.import_reviewed(path, preview['version_id'])

    def test_new_reservations_bind_active_version_and_old_calls_settle_from_their_snapshot(self):
        first = self.activate()
        call = self.ledger.reserve('scope', 'reviewed', 'deepseek-flash', 128,
                                   base_url='https://api.deepseek.com')
        saved = self.ledger.status()['calls'][0]['pricing']
        self.assertEqual(saved['version_id'], first['version_id'])
        second = self.activate(input_micro_per_token=3)
        self.assertNotEqual(first['version_id'], second['version_id'])
        self.ledger.settle(call, {'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
        settled = self.ledger.status()['calls'][0]
        self.assertEqual(settled['pricing'], saved)
        self.assertAlmostEqual(settled['charged_micro'], 280)

    def test_gateway_version_binding_refuses_mid_operation_price_switch(self):
        first = self.activate()
        from solve2ship.gateway import ModelGateway
        profile = type('Profile', (), {'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com'})()
        gateway = ModelGateway(profile, self.ledger, 'scope')
        self.assertEqual(gateway.pricing()['version_id'], first['version_id'])
        self.activate(input_micro_per_token=3)
        with self.assertRaisesRegex(BudgetError, 'pricing_version_changed'):
            gateway.pricing()


if __name__ == '__main__':
    unittest.main()
