"""Self-attested review protocol fixtures, never real human/quality evidence."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from solve2ship.candidates import ArtifactError, RulesAdapter
from solve2ship.contracts import canonical_json, compile_task, sha256
from solve2ship.delivery import DeliveryError, DeliveryRepository
from solve2ship.evaluation import partition_indices
from solve2ship.gateway import BudgetError, MICRO, PRICING_VERIFIED_DATE
from solve2ship.reviews import ReviewError, ReviewRepository
from solve2ship.search import SearchError, SearchRepository
from solve2ship.service_export import export_service
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state.sqlite3')
        self.searches = SearchRepository(self.store)
        self.searches.ledger.configure('work', 100 * MICRO)
        self.deliveries = DeliveryRepository(self.store)
        self.reviews = ReviewRepository(self.store)
        date = patch('solve2ship.gateway.time.strftime', return_value=PRICING_VERIFIED_DATE)
        date.start(); self.addCleanup(date.stop)
        self.run, self.definition = self.source()

    def source(self, *, hard=None, definition=None):
        definition = definition or {'samples': [{'text': 'cancel fixture ' + str(i)} for i in range(20)],
                      'output_specification': 'Return cancel. Explicit protocol fixture, not business truth.',
                      'budget': {'max_cost_cny': 10, 'max_candidate_executions': 60}}
        if hard is not None:
            definition['constraints'] = {'hard': hard, 'soft': []}
        preview = compile_task(definition)
        task = TaskRepository(self.store).save(definition, preview['content_sha256'])
        run = self.searches.create(task['task_id'], 1, uuid.uuid4().hex)
        search_id, owner = self.searches.claim()
        candidate = {'family': 'rules.v1', 'name': 'Protocol fixture only',
                     'rationale': 'Not model optimization or human review', 'artifact': {'rules': [], 'default': 'cancel'}}
        cid = self.searches.add_candidate(search_id, candidate, {'source': 'protocol_fixture'})
        for position, index in enumerate(partition_indices(preview['contract'])['validation']):
            self.searches.reserve_execution(search_id, cid, index)
            output = RulesAdapter().execute(candidate['artifact'], definition['samples'][index], None)[0]
            self.searches.add_execution(search_id, cid, index, output, None, .1, None, None, split='validation',
                judge={'status': 'scored', 'score': .9 if position % 2 == 0 else .2,
                       'source': 'uncalibrated_model_judge', 'reason': 'Explicit fixture, no model call'})
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO selections VALUES(?,?,?)', (search_id, cid, canonical_json({
                'status': 'provisional', 'basis': 'uncalibrated_model_judge_proxy',
                'quality_is_verified_accuracy': False})))
        self.searches.finish(search_id, owner)
        return self.searches.get(run['id']), definition

    @staticmethod
    def review_body(target, *, final=False, verdict='accept', previous=None, comment=''):
        return {'sample_index' if final else 'execution_id': target['sample_index'] if final else target['execution_id'],
                'request_id': uuid.uuid4().hex, 'snapshot_sha256': target['snapshot_sha256'],
                'previous_review_id': previous, 'verdict': verdict, 'reviewer': 'Protocol fixture',
                'comment': comment, 'attested': True}

    def prepare(self):
        return self.deliveries.prepare(self.run['id'], uuid.uuid4().hex, quality_mode='operator_review')

    def rate_final(self, *, verdicts=None):
        packet = self.reviews.packet(self.run['id'], final=True)
        verdicts = verdicts or ['accept'] * len(packet['targets'])
        for target, verdict in zip(packet['targets'], verdicts):
            self.reviews.record(self.run['id'], self.review_body(target, final=True, verdict=verdict), final=True)
        return self.reviews.packet(self.run['id'], final=True)

    @staticmethod
    def completion(packet):
        return {'request_id': uuid.uuid4().hex, 'review_evidence_sha256': packet['review_evidence_sha256'],
                'reviewer': 'Protocol fixture', 'comment': 'Protocol attestation only', 'attested': True}

    def test_validation_packet_never_exposes_final_and_agreement_is_descriptive(self):
        before = self.searches.get(self.run['id'])
        packet = self.reviews.packet(self.run['id'])
        final_indices = set(partition_indices(compile_task(self.definition)['contract'])['final_test'])
        self.assertTrue(packet['targets'])
        self.assertFalse(final_indices & {target['sample_index'] for target in packet['targets']})
        self.assertTrue(all(target['snapshot']['delivery_id'] is None for target in packet['targets']))
        for target in packet['targets'][:2]:
            self.reviews.record(self.run['id'], self.review_body(target))
        target = packet['targets'][2]
        packet = self.reviews.record(self.run['id'], self.review_body(target, verdict='unsure'))
        self.assertEqual(packet['summary']['reviewed'], 3)
        self.assertEqual(packet['summary']['judge_agreement']['count'], 2)
        self.assertEqual(packet['summary']['judge_agreement']['matches'], 1)
        self.assertFalse(packet['summary']['judge_agreement']['calibrated_for_pruning'])
        self.assertFalse(packet['summary']['quality_is_verified_accuracy'])
        self.assertFalse(packet['summary']['human_identity_verified'])
        after = self.searches.get(self.run['id'])
        self.assertEqual(before['selection'], after['selection'])
        self.assertEqual(before['executions'], after['executions'])
        self.assertEqual(before['budget'], after['budget'])

    def test_snapshot_and_request_binding_refuse_conflicting_or_unknown_subjects(self):
        target = self.reviews.packet(self.run['id'])['targets'][0]
        body = self.review_body(target)
        packet = self.reviews.record(self.run['id'], body)
        repeated = self.reviews.record(self.run['id'], body)
        self.assertEqual(len(packet['events']), 1); self.assertEqual(len(repeated['events']), 1)
        self.assertEqual(repeated['events'][0]['review']['source'], 'self_attested_operator')
        self.assertFalse(repeated['events'][0]['review']['human_identity_verified'])
        with self.assertRaisesRegex(ReviewError, 'request_conflict'):
            self.reviews.record(self.run['id'], {**body, 'verdict': 'reject'})
        with self.assertRaisesRegex(ReviewError, 'snapshot_changed'):
            self.reviews.record(self.run['id'], {**body, 'request_id': uuid.uuid4().hex, 'snapshot_sha256': '0' * 64})
        with self.assertRaisesRegex(ReviewError, 'subject_not_available'):
            self.reviews.record(self.run['id'], {**body, 'execution_id': 999999, 'request_id': uuid.uuid4().hex})

    def test_review_amendments_are_append_only_and_require_current_revision_reason(self):
        target = self.reviews.packet(self.run['id'])['targets'][0]
        first = self.reviews.record(self.run['id'], self.review_body(target, verdict='reject'))
        original = first['targets'][0]['latest_review']['id']
        with self.assertRaisesRegex(ReviewError, 'revision_conflict'):
            self.reviews.record(self.run['id'], self.review_body(target))
        with self.assertRaisesRegex(ReviewError, 'revision_reason_required'):
            self.reviews.record(self.run['id'], self.review_body(target, previous=original))
        revised = self.reviews.record(self.run['id'], self.review_body(target, previous=original, comment='Correcting fixture input mistake'))
        self.assertEqual(len(revised['events']), 2)
        self.assertEqual(revised['events'][0]['review']['verdict'], 'reject')
        self.assertEqual(revised['targets'][0]['latest_review']['review']['verdict'], 'accept')
        with self.store.connection(write=True) as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE quality_review_events SET review_json='{}'")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute('DELETE FROM quality_review_events')

    def test_explicit_attestation_is_required_and_no_trusted_identity_is_invented(self):
        target = self.reviews.packet(self.run['id'])['targets'][0]
        for mutation in ({'attested': False}, {'attested': 1}, {'reviewer': ''}, {'reviewer': 'x' * 101},
                         {'comment': 'x' * 2001}, {'request_id': 'not-an-id'}, {'verdict': 'maybe'}):
            with self.subTest(mutation=mutation), self.assertRaises(ReviewError):
                self.reviews.record(self.run['id'], {**self.review_body(target), **mutation})
        self.assertEqual(self.reviews.packet(self.run['id'])['events'], [])

    def test_default_no_label_mode_refuses_but_explicit_mode_freezes_quarantines_and_seals_calls(self):
        with self.assertRaisesRegex(DeliveryError, 'final_quality_checks_or_human_review_required'):
            self.deliveries.prepare(self.run['id'], uuid.uuid4().hex)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM final_quarantine').fetchone()[0], 0)
        delivery = self.prepare()
        self.assertEqual(delivery['state'], 'awaiting_operator_review')
        self.assertFalse(delivery['report']['gate_passed'])
        self.assertIsNone(delivery['report']['provided_label_matches'])
        self.assertFalse(delivery['draft_integrity_verified'])
        self.assertEqual(delivery['frozen']['quality_mode'], 'operator_review')
        self.assertEqual(len(delivery['report']['observations']), 4)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM final_quarantine').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_reports').fetchone()[0], 1)
        with self.assertRaisesRegex(BudgetError, 'delivery_not_active'):
            self.searches.ledger.reserve('delivery:' + delivery['id'], 'forbidden-replay', 'deepseek-flash', 32)
        with self.assertRaisesRegex(SearchError, 'final_test_consumed'):
            self.searches.evolve(self.run['id'], self.run['selection']['candidate_id'], uuid.uuid4().hex)
        with self.assertRaisesRegex(DeliveryError, 'frozen_quality_mode_conflict'):
            self.deliveries.prepare(self.run['id'], uuid.uuid4().hex)
        repeated = self.prepare()
        self.assertEqual(repeated['id'], delivery['id'])
        self.assertEqual(repeated['report']['observations'], delivery['report']['observations'])
        self.assertEqual(self.searches.ledger.status()['calls'], [])

    def test_frozen_final_packet_binds_policy_schema_and_output_without_pre_exposure_access(self):
        with self.assertRaisesRegex(ReviewError, 'frozen_delivery_required'):
            self.reviews.packet(self.run['id'], final=True)
        schema = {'type': 'string'}
        bundle = {'status': 'proposed', 'schema_sha256': sha256(schema), 'output_schema': schema,
                  'source': 'protocol_fixture_unreviewed', 'human_reviewed': False}
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO criteria_attempts VALUES(?,?)', (self.run['id'], 1))
            db.execute('INSERT INTO criteria_bundles VALUES(?,?,?)', (self.run['id'], 1, canonical_json(bundle)))
        delivery = self.prepare(); packet = self.reviews.packet(self.run['id'], final=True)
        self.assertEqual(delivery['frozen']['criteria_bundle'], bundle)
        self.assertTrue(all(t['snapshot']['criteria_schema_sha256'] == sha256(schema) for t in packet['targets']))
        self.assertTrue(all(t['snapshot']['artifact_sha256'] == delivery['frozen']['candidate']['artifact_sha256'] for t in packet['targets']))
        self.assertTrue(all(t['snapshot']['output_sha256'] == sha256('cancel') for t in packet['targets']))
        self.assertTrue(all(t['snapshot']['split'] == 'final_test_once' for t in packet['targets']))
        self.assertFalse(packet['closed']); self.assertEqual(packet['review_state'], 'pending')

    def test_completion_requires_all_final_votes_and_does_not_mutate_original_report(self):
        delivery = self.prepare()
        with self.store.connection() as db:
            original = db.execute('SELECT report_json FROM delivery_reports').fetchone()[0]
        packet = self.reviews.packet(self.run['id'], final=True)
        with self.assertRaisesRegex(ReviewError, 'all_final_outputs_need_review'):
            self.reviews.complete(self.run['id'], self.completion(packet))
        self.reviews.record(self.run['id'], self.review_body(packet['targets'][0], final=True), final=True)
        packet = self.reviews.packet(self.run['id'], final=True)
        for target in packet['targets'][1:]:
            self.reviews.record(self.run['id'], self.review_body(target, final=True, verdict='unsure'), final=True)
        packet = self.reviews.packet(self.run['id'], final=True)
        with self.assertRaisesRegex(ReviewError, 'all_final_outputs_need_review'):
            self.reviews.complete(self.run['id'], self.completion(packet))
        for target in packet['targets'][1:]:
            self.reviews.record(self.run['id'], self.review_body(target, final=True,
                previous=target['latest_review']['id'], comment='Protocol fixture resolved uncertainty'), final=True)
        packet = self.reviews.packet(self.run['id'], final=True)
        completed = self.reviews.complete(self.run['id'], self.completion(packet))
        self.assertTrue(completed['report']['gate_passed'])
        self.assertEqual(completed['report']['accepted_review_count'], 4)
        self.assertEqual(completed['report']['label_source'], 'self_attested_operator_review')
        self.assertFalse(completed['report']['human_identity_verified'])
        self.assertFalse(completed['report']['quality_is_verified_accuracy'])
        self.assertTrue(completed['draft_integrity_verified'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT report_json FROM delivery_reports').fetchone()[0], original)
            self.assertFalse(json.loads(original)['gate_passed'])
        self.assertEqual(self.searches.ledger.status()['calls'], [])

    def test_rejected_completed_final_cannot_be_reopened_or_exported(self):
        delivery = self.prepare(); packet = self.rate_final(verdicts=['accept', 'reject', 'accept', 'accept'])
        completed = self.reviews.complete(self.run['id'], self.completion(packet))
        self.assertEqual(completed['state'], 'final_checks_failed'); self.assertFalse(completed['report']['gate_passed'])
        self.assertFalse(completed['draft_integrity_verified'])
        packet = self.reviews.packet(self.run['id'], final=True)
        self.assertTrue(packet['closed']); self.assertEqual(packet['review_state'], 'rejected')
        with self.assertRaisesRegex(ReviewError, 'quality_decision_closed'):
            self.reviews.record(self.run['id'], self.review_body(packet['targets'][1], final=True,
                previous=packet['targets'][1]['latest_review']['id'], comment='Cannot reopen a completed rejection'), final=True)
        with self.assertRaises(DeliveryError): export_service(self.store, delivery['id'])
        with self.assertRaisesRegex(BudgetError, 'final_gate_required'):
            self.searches.ledger.grant_service(delivery['id'], 3 * MICRO)

    def test_stale_completion_fingerprint_refuses_changed_votes_and_cannot_race_into_acceptance(self):
        self.prepare(); packet = self.rate_final()
        stale = self.completion(packet)
        target = packet['targets'][0]
        self.reviews.record(self.run['id'], self.review_body(target, final=True, verdict='reject',
            previous=target['latest_review']['id'], comment='Explicit fixture correction'), final=True)
        with self.assertRaisesRegex(ReviewError, 'snapshot_changed'):
            self.reviews.complete(self.run['id'], stale)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_quality_decisions').fetchone()[0], 0)

    def test_failed_execution_cannot_be_accepted_and_failed_final_closes_without_replay(self):
        with patch.object(RulesAdapter, 'execute', side_effect=ArtifactError('explicit_fixture_failure')):
            delivery = self.prepare()
        packet = self.reviews.packet(self.run['id'], final=True)
        with self.assertRaisesRegex(ReviewError, 'failed_output_cannot_be_accepted'):
            self.reviews.record(self.run['id'], self.review_body(packet['targets'][0], final=True), final=True)
        for target in packet['targets']:
            self.reviews.record(self.run['id'], self.review_body(target, final=True, verdict='reject'), final=True)
        packet = self.reviews.packet(self.run['id'], final=True)
        result = self.reviews.complete(self.run['id'], self.completion(packet))
        self.assertEqual(result['state'], 'final_checks_failed')
        self.assertEqual(result['report']['accepted_review_count'], 0)
        self.assertEqual(self.prepare()['report']['observations'], delivery['report']['observations'])

    def test_concurrent_submission_and_completion_are_atomic_idempotent_and_restart_safe(self):
        self.prepare(); packet = self.reviews.packet(self.run['id'], final=True)
        for target in packet['targets']:
            body = self.review_body(target, final=True)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: self.reviews.record(self.run['id'], body, final=True), range(4)))
        restored = ReviewRepository(Store(self.store.path))
        packet = restored.packet(self.run['id'], final=True)
        self.assertEqual(len(packet['events']), 4)
        last_review_body = body
        body = self.completion(packet)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: restored.complete(self.run['id'], body), range(3)))
        self.assertTrue(all(r['report']['gate_passed'] for r in results))
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_quality_decisions').fetchone()[0], 1)
        packet = restored.packet(self.run['id'], final=True)
        replay = restored.record(self.run['id'], last_review_body, final=True)
        self.assertTrue(replay['closed']); self.assertEqual(len(replay['events']), 4)
        self.assertEqual(self.searches.get(self.run['id'])['executions'], self.run['executions'])
        with self.assertRaisesRegex(ReviewError, 'quality_decision_closed'):
            restored.record(self.run['id'], self.review_body(packet['targets'][0], final=True, verdict='reject',
                previous=packet['targets'][0]['latest_review']['id'], comment='Closed'), final=True)

    def test_positive_final_review_unlocks_real_source_proof_export_and_delegated_grant(self):
        delivery = self.prepare(); packet = self.rate_final()
        delivery = self.reviews.complete(self.run['id'], self.completion(packet))
        spec = importlib.util.spec_from_file_location('review_verifier', Path(__file__).resolve().parents[1] / 'scripts/verify_service.py')
        verifier = importlib.util.module_from_spec(spec); spec.loader.exec_module(verifier)
        cases = [{'input': t['input'], 'expected_output': t['output']} for t in self.reviews.packet(self.run['id'])['targets']]
        proof = verifier.verify(delivery['draft_path'], cases)
        self.assertEqual(proof['status'], 'passed'); self.assertTrue(proof['independent_process'])
        self.deliveries.record_proof(delivery['id'], proof)
        exported = export_service(self.store, delivery['id'])
        self.assertTrue(exported['archive_integrity_verified'])
        with zipfile.ZipFile(exported['archive_path']) as archive:
            metadata = json.loads(archive.read('service/provenance.json'))
            summary = metadata['final_report_summary']
            self.assertEqual(summary['label_source'], 'self_attested_operator_review')
            self.assertFalse(summary['quality_is_verified_accuracy']); self.assertIsNone(summary['provided_label_matches'])
            review = metadata['quality_review_summary']
            self.assertEqual(review['quality_mode'], 'operator_review')
            self.assertEqual(review['review_state'], 'accepted'); self.assertEqual(review['accepted_review_count'], 4)
            self.assertFalse(review['human_identity_verified'])
            self.assertEqual(review['review_evidence_sha256'], delivery['report']['review_evidence_sha256'])
            self.assertEqual(exported['export_template_version'], 4)
            self.assertNotIn('Protocol attestation only', archive.read('service/provenance.json').decode())
        grant = self.searches.ledger.grant_service(delivery['id'], 3 * MICRO)
        self.assertEqual(grant['amount_micro'], 3 * MICRO)
        self.assertEqual(self.searches.ledger.status()['delegated_ceiling_cny'], 3)

    def test_unsupported_hard_constraints_are_not_bypassed_by_operator_mode(self):
        second, _ = self.source(hard=['Must enforce an unimplemented business constraint'])
        with self.assertRaisesRegex(DeliveryError, 'executable_hard_constraint_checks_required'):
            self.deliveries.prepare(second['id'], uuid.uuid4().hex, quality_mode='operator_review')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM final_quarantine').fetchone()[0], 0)

    def test_schema13_migration_preserves_records_and_adds_append_only_review_tables(self):
        self.searches.ledger.configure('migration-review-protocol', 10 * MICRO)
        call_id = self.searches.ledger.reserve('migration-review-protocol', 'explicit_fixture_unknown_charge', 'deepseek-flash', 32)
        before = self.searches.ledger.status()['committed_ceiling_cny']
        with self.store.connection(write=True) as db:
            db.execute('DROP TABLE delivery_quality_decisions')
            db.execute('DROP TABLE quality_review_events')
            db.execute('PRAGMA user_version=13')
        self.assertGreater(before, 0)
        migrated = Store(self.store.path)
        migrated_status = SearchRepository(migrated).ledger.status()
        self.assertEqual(migrated_status['committed_ceiling_cny'], before)
        self.assertEqual(migrated_status['calls'][0]['id'], call_id)
        self.assertEqual(migrated_status['calls'][0]['state'], 'reserved')
        self.assertEqual(SearchRepository(migrated).get(self.run['id'])['executions'], self.run['executions'])
        reviews = ReviewRepository(migrated)
        packet = reviews.packet(self.run['id'])
        reviews.record(self.run['id'], self.review_body(packet['targets'][0]))
        with migrated.connection(write=True) as db:
            with self.assertRaises(sqlite3.DatabaseError): db.execute('DELETE FROM quality_review_events')
        self.prepare(); packet = self.rate_final()
        self.reviews.complete(self.run['id'], self.completion(packet))
        with migrated.connection(write=True) as db:
            with self.assertRaises(sqlite3.DatabaseError): db.execute('DELETE FROM delivery_quality_decisions')
            with self.assertRaises(sqlite3.DatabaseError): db.execute("UPDATE delivery_quality_decisions SET decision_json='{}'")


    def test_competing_amendments_cannot_silently_overwrite_each_other(self):
        target = self.reviews.packet(self.run['id'])['targets'][0]
        packet = self.reviews.record(self.run['id'], self.review_body(target, verdict='unsure'))
        previous = packet['targets'][0]['latest_review']['id']
        bodies = [self.review_body(target, verdict=verdict, previous=previous, comment='Independent fixture amendment')
                  for verdict in ('accept', 'reject')]
        def submit(body):
            try:
                self.reviews.record(self.run['id'], body)
                return 'recorded'
            except ReviewError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, bodies))
        self.assertCountEqual(outcomes, ['recorded', 'review_revision_conflict'])
        self.assertEqual(len(self.reviews.packet(self.run['id'])['events']), 2)

    def test_loopback_http_review_flow_enforces_attestation_stale_binding_and_closed_gate(self):
        import threading
        from urllib.error import HTTPError
        from urllib.request import Request
        from local_http import urlopen
        from solve2ship.server import create_server
        server = create_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port) + '/api/searches/' + self.run['id']
        def request(path, body=None, *, headers=None):
            req = Request(base + path, data=canonical_json(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json', 'X-STS-Request': '1', **(headers or {})})
            with urlopen(req) as response:
                return response.status, json.load(response)
        def refusal(status, path, body=None, *, headers=None):
            with self.assertRaises(HTTPError) as failure:
                request(path, body, headers=headers)
            self.assertEqual(failure.exception.code, status)
            failure.exception.close()
        try:
            status, validation = request('/quality-reviews')
            self.assertEqual(status, 200); self.assertEqual(validation['subject_kind'], 'validation')
            body = self.review_body(validation['targets'][0])
            refusal(422, '/quality-reviews', {**body, 'attested': False})
            refusal(403, '/quality-reviews', body, headers={'Origin': 'https://untrusted.invalid'})
            self.assertEqual(request('/quality-reviews', body)[0], 201)
            refusal(409, '/quality-reviews', {**body, 'request_id': uuid.uuid4().hex, 'verdict': 'reject'})
            refusal(422, '/delivery/reviews')
            refusal(422, '/delivery', {'request_id': uuid.uuid4().hex})
            _, delivery = request('/delivery', {'request_id': uuid.uuid4().hex, 'quality_mode': 'operator_review'})
            self.assertEqual(delivery['state'], 'awaiting_operator_review')
            _, final = request('/delivery/reviews')
            stale = self.completion(final)
            for target in final['targets']:
                _, latest = request('/delivery/reviews', self.review_body(target, final=True))
            refusal(409, '/delivery/quality-decision', stale)
            _, accepted = request('/delivery/quality-decision', self.completion(latest))
            self.assertEqual(accepted['state'], 'private_draft_ready')
            self.assertFalse(accepted['report']['human_identity_verified'])
            target = latest['targets'][0]
            refusal(409, '/delivery/reviews', self.review_body(target, final=True, verdict='reject',
                previous=target['latest_review']['id'], comment='Cannot amend a closed decision'))
            self.assertEqual(self.searches.ledger.status()['calls'], [])
        finally:
            server.shutdown(); server.server_close(); thread.join()


    def test_conflicting_provided_final_labels_are_not_erased_by_operator_mode(self):
        definition = {'samples': [{'text': 'duplicate fixture ' + str(i // 2)} for i in range(20)],
                      'labels': [{'sample_index': i, 'expected_output': 'cancel' if i % 2 == 0 else 'other'} for i in range(20)],
                      'output_specification': 'Return cancel. Explicit contradictory-label data fixture.',
                      'budget': {'max_cost_cny': 10, 'max_candidate_executions': 60}}
        second, _ = self.source(definition=definition)
        with self.assertRaisesRegex(DeliveryError, 'conflicting_final_labels'):
            self.deliveries.prepare(second['id'], uuid.uuid4().hex, quality_mode='operator_review')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM final_quarantine').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM deliveries').fetchone()[0], 0)

    def test_completion_request_id_cannot_be_reused_for_a_different_delivery(self):
        self.prepare(); first_packet = self.rate_final()
        first_body = self.completion(first_packet)
        self.reviews.complete(self.run['id'], first_body)
        definition = {'samples': [{'text': 'disjoint second review fixture ' + str(i)} for i in range(20)],
                      'output_specification': 'Return cancel. Separate protocol fixture.',
                      'budget': {'max_cost_cny': 10, 'max_candidate_executions': 60}}
        second, _ = self.source(definition=definition)
        delivery = self.deliveries.prepare(second['id'], uuid.uuid4().hex, quality_mode='operator_review')
        packet = self.reviews.packet(second['id'], final=True)
        for target in packet['targets']:
            self.reviews.record(second['id'], self.review_body(target, final=True), final=True)
        packet = self.reviews.packet(second['id'], final=True)
        second_body = {**self.completion(packet), 'request_id': first_body['request_id']}
        with self.assertRaisesRegex(ReviewError, 'conflict'):
            self.reviews.complete(second['id'], second_body)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_quality_decisions WHERE delivery_id=?',
                                        (delivery['id'],)).fetchone()[0], 0)
        self.assertEqual(self.deliveries.get(delivery['id'])['state'], 'awaiting_operator_review')

    def test_concurrent_final_modes_cannot_silently_change_the_requested_quality_gate(self):
        import threading
        definition = {**self.definition,
                      'labels': [{'sample_index': i, 'expected_output': 'cancel'} for i in range(20)]}
        second, _ = self.source(definition=definition)
        original = self.deliveries.for_search
        barrier = threading.Barrier(2)
        def initial_lookup(search_id):
            result = original(search_id)
            barrier.wait(timeout=3)
            return result
        def prepare_mode(mode):
            try:
                value = self.deliveries.prepare(second['id'], uuid.uuid4().hex, quality_mode=mode)
                return ('accepted_request', mode, value['frozen']['quality_mode'])
            except DeliveryError as error:
                return ('refused_request', str(error))
        with patch.object(self.deliveries, 'for_search', side_effect=initial_lookup):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(prepare_mode, ('provided_labels', 'operator_review')))
        successes = [r for r in outcomes if r[0] == 'accepted_request']
        errors = [r for r in outcomes if r[0] == 'refused_request']
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(successes[0][1], successes[0][2])
        self.assertEqual(errors, [('refused_request', 'frozen_quality_mode_conflict')])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM deliveries WHERE search_id=?', (second['id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM final_quarantine').fetchone()[0], 4)

    def test_mutation_feedback_includes_validation_reviews_but_never_final_review_content(self):
        from solve2ship.candidates import builtin_registry
        from solve2ship.evolution import mutate_candidate
        validation = self.reviews.packet(self.run['id'])['targets'][0]
        self.reviews.record(self.run['id'], self.review_body(validation, verdict='reject',
                            comment='validation-review-can-be-feedback'))
        self.prepare()
        final = self.reviews.packet(self.run['id'], final=True)
        self.reviews.record(self.run['id'], self.review_body(final['targets'][0], final=True,
                            comment='final-review-private-never-feedback'), final=True)
        captured = {}
        class ProbeStop(Exception):
            pass
        class GatewayProbe:
            def json(self, system, user, **kwargs):
                captured.update(json.loads(user))
                raise ProbeStop('Stop before any model call or artifact edit')
        source = self.searches.get(self.run['id'])
        parent = source['candidates'][0]
        contract = compile_task(self.definition)['contract']
        with self.assertRaises(ProbeStop):
            mutate_candidate(self.searches, self.run['id'], source, parent, contract,
                             GatewayProbe(), builtin_registry())
        feedback = captured['operator_feedback_hypothesis']
        self.assertEqual([r['execution_id'] for r in feedback], [validation['execution_id']])
        self.assertEqual(feedback[0]['review']['comment'], 'validation-review-can-be-feedback')
        self.assertFalse(feedback[0]['review']['quality_is_verified_accuracy'])
        self.assertNotIn('final-review-private-never-feedback', canonical_json(captured))
        self.assertFalse({t['sample_index'] for t in final['targets']} &
                         {row['sample_index'] for row in captured['feedback']})
        self.assertEqual(self.searches.ledger.status()['calls'], [])
