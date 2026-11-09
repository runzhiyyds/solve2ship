"""Structural-criteria protocol fixtures; no paid models or real optimization claims."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from importlib import metadata
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from solve2ship.candidates import builtin_registry
from solve2ship.contracts import canonical_json, compile_task, sha256
from solve2ship.criteria import (
    CriteriaUnavailable, EXPECTED_VERSIONS, SchemaAdapter, capability, check_output,
    get_bundle, prepare_bundle, validate_schema_shape,
)
from solve2ship.delivery import DeliveryError, DeliveryRepository
from solve2ship.evaluation import candidate_metrics, partition_indices
from solve2ship.gateway import BudgetError, MICRO, ModelGateway, PRICING_VERIFIED_DATE, ProviderError
from solve2ship.invocation import invocation_call_ids
from solve2ship.search import SearchRepository, execute_search
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository
from test_search import ProviderFixture


ENGINE_AVAILABLE = capability()['available']


def proposal(schema=None):
    return {'output_schema': schema or {'type': 'string', 'enum': ['cancel', 'other']},
            'covered': ['Output is a bounded category string.'],
            'uncovered': ['Correct category semantics need independent evidence.']}


class CriteriaSafetyTests(unittest.TestCase):
    def test_capability_requires_all_reviewed_versions_without_silent_fallback(self):
        with patch('solve2ship.criteria.metadata.version', side_effect=lambda name: EXPECTED_VERSIONS[name]):
            self.assertTrue(capability()['available'])
        with patch('solve2ship.criteria.metadata.version', side_effect=metadata.PackageNotFoundError):
            self.assertEqual(capability()['reason'], 'schema_dependency_unavailable')
            with self.assertRaises(CriteriaUnavailable):
                SchemaAdapter()
        changed = dict(EXPECTED_VERSIONS, jsonschema='999.0.0')
        with patch('solve2ship.criteria.metadata.version', side_effect=lambda name: changed[name]):
            self.assertEqual(capability()['reason'], 'schema_dependency_review_required')
            with self.assertRaises(CriteriaUnavailable):
                SchemaAdapter()

    def test_refs_regex_formats_code_combinators_and_vacuous_schemas_are_rejected(self):
        unsafe = [True, False, {}, {'enum': [1]}, {'type': True}, {'type': ['string', 'string']},
                  {'type': 'string', '$ref': 'https://example.invalid/schema'},
                  {'type': 'string', '$dynamicRef': '#loop'}, {'type': 'string', 'pattern': '(a+)+$'},
                  {'type': 'string', 'format': 'email'}, {'type': 'object', 'patternProperties': {}},
                  {'type': 'string', 'allOf': [{'type': 'string'}]},
                  {'type': 'object', 'code': 'print("never execute")'},
                  {'type': 'object', 'properties': {'child': {'type': 'string', '$ref': 'file:///private'}}},
                  {'type': 'array', 'items': {'type': 'string', 'anyOf': []}},
                  {'type': 'object', 'additionalProperties': {'type': 'string'}}]
        for schema in unsafe:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validate_schema_shape(schema)

    def test_schema_depth_size_properties_enums_and_bounds_are_finite(self):
        deep = {'type': 'integer'}
        for _ in range(8):
            deep = {'type': 'array', 'items': deep}
        invalid = [deep, {'type': 'string', 'description': 'x' * 1001},
                   {'type': 'object', 'properties': {str(i): {'type': 'integer'} for i in range(33)}},
                   {'type': 'string', 'enum': list(range(33))},
                   {'type': 'array', 'maxItems': 262145}, {'type': 'string', 'minLength': True},
                   {'type': 'object', 'properties': {'known': {'type': 'string'}}, 'required': ['unknown']},
                   {'type': 'object', 'properties': {'known': {'type': 'string'}}, 'required': ['known', 'known']}]
        for schema in invalid:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validate_schema_shape(schema)
        # Literal data containing a reserved-looking property name is not a reference.
        validate_schema_shape({'type': 'object', 'properties': {'$ref': {'type': 'string'}}, 'required': ['$ref']})

    def test_unavailable_checks_never_turn_into_success_or_verified_quality(self):
        self.assertEqual(check_output(None, None)['status'], 'unavailable')
        result = check_output({'status': 'invalid_proposal', 'reason': 'invalid_criteria_proposal'}, {})
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('passed', result)
        self.assertFalse(result['semantic_quality_verified'])
        bundle = {'status': 'proposed', 'output_schema': {'type': 'string'}, 'schema_sha256': sha256({'type': 'string'})}
        with patch('solve2ship.criteria.capability', return_value={'available': False, 'reason': 'schema_dependency_unavailable'}):
            result = check_output(bundle, 'cancel')
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('passed', result)


@unittest.skipUnless(ENGINE_AVAILABLE, 'Optional reviewed schema dependencies are not installed')
class SchemaEngineTests(unittest.TestCase):
    def test_real_upstream_adapter_checks_values_and_returns_only_bounded_violation_metadata(self):
        adapter = SchemaAdapter()
        schema = {'type': 'object', 'properties': {'类别': {'type': 'string', 'enum': ['费用', '天数']},
                  '结果': {'type': 'integer', 'minimum': 0}}, 'required': ['类别', '结果'], 'additionalProperties': False}
        with patch('socket.create_connection', side_effect=AssertionError('Schema checks must not use network')):
            passed = adapter.check(schema, {'类别': '费用', '结果': 42})
            failed = adapter.check(schema, {'类别': 'wrong', '结果': 'not an integer', 'extra': 'private-data'})
        self.assertTrue(passed['passed']); self.assertFalse(failed['passed'])
        self.assertEqual(passed['schema_sha256'], sha256(schema))
        self.assertEqual({x['keyword'] for x in failed['errors']}, {'enum', 'type', 'additionalProperties'})
        self.assertFalse(failed['semantic_quality_verified'])
        self.assertNotIn('private-data', canonical_json(failed))
        self.assertEqual(failed['source'], 'model_proposed_structure_unreviewed')

    def test_upstream_schema_invalidity_is_a_controlled_failure(self):
        with self.assertRaises(ValueError):
            SchemaAdapter().compile({'type': 'integer', 'minimum': 'not a number'})
        with self.assertRaises(ValueError):
            SchemaAdapter().compile({'type': 'object', 'properties': {'nested': {'type': 'string', 'format': 'uri'}}})

    def test_type_semantics_and_error_collection_do_not_confuse_booleans_and_integers(self):
        adapter = SchemaAdapter()
        self.assertFalse(adapter.check({'type': 'integer'}, True)['passed'])
        self.assertTrue(adapter.check({'type': 'integer'}, 1)['passed'])
        # JSON Schema's mathematical-integer semantics accept 1.0; exact label comparison remains separate.
        self.assertTrue(adapter.check({'type': 'integer'}, 1.0)['passed'])
        schema = {'type': 'object', 'properties': {str(i): {'type': 'integer'} for i in range(20)}}
        result = adapter.check(schema, {str(i): 'bad' for i in range(20)})
        self.assertEqual(len(result['errors']), 8)
        self.assertTrue(result['errors_truncated'])
        self.assertFalse(result['passed'])

    def test_reserved_looking_literal_property_is_validated_as_data(self):
        schema = {'type': 'object', 'properties': {'$ref': {'type': 'string'}}, 'required': ['$ref']}
        with patch('socket.create_connection', side_effect=AssertionError('No retrieval permitted')):
            self.assertTrue(SchemaAdapter().check(schema, {'$ref': 'https://example.invalid'})['passed'])
        self.assertFalse(SchemaAdapter().check(schema, {})['passed'])


class CriteriaRepositoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.db'
        self.store = Store(self.path); self.repository = SearchRepository(self.store)
        self.repository.ledger.configure('work', 100 * MICRO)
        self.provider = ProviderFixture(); self.addCleanup(self.provider.close)
        date = patch('solve2ship.gateway.time.strftime', return_value=PRICING_VERIFIED_DATE)
        date.start(); self.addCleanup(date.stop)

    def make_run(self, *, labelled=True):
        definition = {'samples': [{'text': 'cancel', 'secret_case_marker': 'case-' + str(i)} for i in range(20)],
                      'output_specification': 'Return cancel or other as the category string.',
                      'labels': [{'sample_index': i, 'expected_output': 'cancel'} for i in range(20)] if labelled else [],
                      'budget': {'max_cost_cny': 10, 'max_candidate_executions': 120}}
        self.contract = compile_task(definition)['contract']
        task = TaskRepository(self.store).save(definition, compile_task(definition)['content_sha256'])
        self.run = self.repository.create(task['task_id'], 1, uuid.uuid4().hex, protocol='m8')
        self.search_id, self.owner = self.repository.claim()
        self.gateway = ModelGateway(self.provider.profile, self.repository.ledger, self.search_id, allow_fixture=True)
        self.original_json = self.gateway.json
        self.proposal_contexts = []
        return self.repository.get(self.search_id)

    def responses(self, value=None):
        def respond(system, user, **kwargs):
            response, call_id = self.original_json(system, user, **kwargs)
            if kwargs['purpose'] == 'criteria_proposer':
                self.proposal_contexts.append(json.loads(user))
                return copy.deepcopy(value if value is not None else proposal()), call_id
            return response, call_id
        return patch.object(self.gateway, 'json', side_effect=respond)

    def test_missing_capability_persists_explicit_unavailable_status_without_paid_proposal(self):
        record = self.make_run()
        with patch('solve2ship.criteria.capability', return_value={'available': False, 'reason': 'schema_dependency_unavailable'}):
            bundle = prepare_bundle(self.repository, record, self.contract, self.gateway)
        self.assertEqual(bundle['status'], 'unavailable')
        self.assertFalse(bundle['human_reviewed']); self.assertFalse(bundle['semantic_quality_verified'])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(get_bundle(SearchRepository(Store(self.path)), self.search_id), bundle)

    def test_zero_budget_refuses_before_provider_submission_and_is_not_reset_on_reentry(self):
        record = self.make_run()
        self.repository.ledger.configure('work', 0)
        with patch('solve2ship.criteria.capability', return_value={'available': True, 'versions': EXPECTED_VERSIONS}):
            with self.assertRaises(BudgetError):
                prepare_bundle(self.repository, record, self.contract, self.gateway)
        self.assertEqual(self.provider.calls, [])
        bundle = get_bundle(self.repository, self.search_id)
        self.assertEqual(bundle['status'], 'unavailable')
        self.assertEqual(bundle['reason'], 'budget_exhausted')
        self.assertIsNone(bundle['call_id'])
        self.repository.ledger.configure('work', 100 * MICRO)
        self.assertEqual(prepare_bundle(self.repository, record, self.contract, self.gateway), bundle)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.repository.ledger.status()['committed_ceiling_cny'], 0)

    def test_interrupted_claim_is_not_replayed_or_reported_as_a_completed_check(self):
        record = self.make_run()
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO criteria_attempts VALUES(?,?)', (self.search_id, 0))
        self.assertEqual(get_bundle(self.repository, self.search_id)['status'], 'preparing')
        self.repository.finish(self.search_id, self.owner, 'fixture_interruption')
        result = prepare_bundle(self.repository, record, self.contract, self.gateway)
        self.assertEqual(result['status'], 'interrupted_requires_review')
        self.assertFalse(result['automatic_replay']); self.assertEqual(self.provider.calls, [])

    def test_schema12_migration_retains_usage_and_old_execution_without_invented_criteria(self):
        self.make_run()
        self.gateway.json('Protocol fixture', 'cancel', purpose='fixture_inference', max_tokens=32)
        before = self.repository.ledger.status()['committed_ceiling_cny']
        with self.store.connection(write=True) as db:
            db.execute('DROP TABLE criteria_bundles'); db.execute('DROP TABLE criteria_attempts')
            db.execute('ALTER TABLE executions DROP COLUMN criteria_json'); db.execute('PRAGMA user_version=12')
        migrated = SearchRepository(Store(self.path))
        self.assertEqual(migrated.ledger.status()['committed_ceiling_cny'], before)
        self.assertIsNone(migrated.get(self.search_id)['criteria_bundle'])
        with migrated.store.connection() as db:
            self.assertIn('criteria_json', {r[1] for r in db.execute('PRAGMA table_info(executions)')})

    @unittest.skipUnless(ENGINE_AVAILABLE, 'Optional reviewed schema dependencies are not installed')
    def test_paid_fixture_proposal_uses_train_inputs_only_and_freezes_across_restart_and_descendants(self):
        record = self.make_run()
        with self.responses():
            bundle = prepare_bundle(self.repository, record, self.contract, self.gateway)
            again = prepare_bundle(self.repository, record, self.contract, self.gateway)
        self.assertEqual(bundle, again); self.assertEqual(len(self.provider.calls), 1)
        splits = partition_indices(self.contract)
        context = self.proposal_contexts[0]
        self.assertEqual(context.keys(), {'goal', 'hard_constraints', 'training_inputs'})
        self.assertEqual(context['training_inputs'], [self.contract['definition']['samples'][i] for i in splits['train'][:10]])
        for i in splits['final_test'] + splits['validation']:
            self.assertNotIn('case-' + str(i) + '"', canonical_json(context))
        self.assertEqual(bundle['training_sample_indices'], splits['train'][:10])
        self.assertFalse(bundle['final_test_used']); self.assertFalse(bundle['human_reviewed'])
        self.repository.finish(self.search_id, self.owner)
        child = self.repository.create(record['task_id'], 1, uuid.uuid4().hex, protocol='m4', source_search_id=self.search_id)
        inherited = prepare_bundle(self.repository, child, self.contract, self.gateway)
        self.assertEqual(inherited, bundle)
        restarted = SearchRepository(Store(self.path))
        self.assertEqual(get_bundle(restarted, child['id']), bundle)
        self.assertEqual(len(restarted.ledger.status()['calls']), 1)
        with self.store.connection(write=True) as db:
            for table in ('criteria_attempts', 'criteria_bundles'):
                with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                    db.execute('DELETE FROM ' + table)

    @unittest.skipUnless(ENGINE_AVAILABLE, 'Optional reviewed schema dependencies are not installed')
    def test_concurrent_prepare_has_one_live_proposal_and_other_reader_observes_preparing(self):
        record = self.make_run(); entered = threading.Event(); release = threading.Event()
        def respond(system, user, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Fixture proposal was not released')
            _, call_id = self.original_json(system, user, **kwargs)
            return proposal(), call_id
        with patch.object(self.gateway, 'json', side_effect=respond), ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(prepare_bundle, self.repository, record, self.contract, self.gateway)
            try:
                self.assertTrue(entered.wait(2))
                other = prepare_bundle(self.repository, record, self.contract, self.gateway)
                self.assertEqual(other['status'], 'preparing')
                self.assertFalse(other['automatic_replay'])
            finally:
                release.set()
            bundle = future.result(timeout=5)
        self.assertEqual(bundle['status'], 'proposed'); self.assertEqual(len(self.provider.calls), 1)

    @unittest.skipUnless(ENGINE_AVAILABLE, 'Optional reviewed schema dependencies are not installed')
    def test_invalid_proposal_keeps_paid_fixture_call_but_never_creates_passed_checks(self):
        record = self.make_run()
        with self.responses({'output_schema': {'type': 'string', 'pattern': '(a+)+$'}, 'covered': [], 'uncovered': []}):
            bundle = prepare_bundle(self.repository, record, self.contract, self.gateway)
        self.assertEqual(bundle['status'], 'invalid_proposal')
        self.assertEqual(bundle['failure_stage'], 'schema')
        event = next(e for e in self.repository.get(record['id'])['events'] if e['type'] == 'evaluation_criteria_proposal')
        self.assertEqual(event['detail']['response']['output_schema']['pattern'], '(a+)+$')
        self.assertFalse(event['detail']['response_omitted'])
        self.assertEqual(event['detail']['response_sha256'], sha256(event['detail']['response']))
        self.assertEqual(len(self.repository.ledger.status()['calls']), 1)
        call = self.repository.ledger.status()['calls'][0]
        self.assertEqual(call['state'], 'settled'); self.assertEqual(bundle['call_id'], call['id'])
        self.assertEqual(check_output(bundle, 'cancel')['status'], 'unavailable')
        self.assertNotIn('passed', check_output(bundle, 'cancel'))

    @unittest.skipUnless(ENGINE_AVAILABLE, 'Optional reviewed schema dependencies are not installed')
    def test_unknown_charge_is_preserved_and_does_not_resubmit_proposal(self):
        record = self.make_run(); self.provider.mode = 'missing_usage'
        with self.assertRaises(ProviderError):
            prepare_bundle(self.repository, record, self.contract, self.gateway)
        before = self.repository.ledger.status()['committed_ceiling_cny']
        self.assertGreater(before, 2)
        self.provider.mode = 'normal'
        bundle = prepare_bundle(self.repository, record, self.contract, self.gateway)
        self.assertEqual(bundle['status'], 'unavailable')
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.repository.ledger.status()['calls'][0]['state'], 'reserved')
        self.assertEqual(SearchRepository(Store(self.path)).ledger.status()['committed_ceiling_cny'], before)

    @unittest.skipUnless(ENGINE_AVAILABLE, 'Optional reviewed schema dependencies are not installed')
    def test_failed_structural_hypothesis_does_not_override_label_quality_or_inference_costs(self):
        self.make_run()
        with self.responses(proposal({'type': 'integer'})):
            execute_search(self.repository, self.search_id, self.owner, self.gateway, builtin_registry())
        result = self.repository.get(self.search_id)
        self.assertEqual(result['state'], 'succeeded')
        self.assertTrue(all(r['label_agreement'] == 1 and r['judge'] is None for r in result['executions']))
        self.assertTrue(all(r['criteria']['status'] == 'checked' and not r['criteria']['passed'] for r in result['executions']))
        self.assertTrue(all(not r['criteria']['semantic_quality_verified'] for r in result['executions']))
        self.assertEqual(result['selection']['evidence']['basis'], 'provided_label_agreement_unverified')
        self.assertFalse(result['selection']['evidence']['structural_criteria_used_for_quality_ranking'])
        winner = next(c for c in result['candidates'] if c['id'] == result['selection']['candidate_id'])
        self.assertEqual(winner['family'], 'rules.v1')
        calls = result['budget']['calls']
        criteria_id = result['criteria_bundle']['call_id']
        self.assertEqual(sum(c['purpose'] == 'criteria_proposer' for c in calls), 1)
        self.assertTrue(all(criteria_id not in invocation_call_ids(r) for r in result['executions']))
        self.assertFalse(set(r['sample_index'] for r in result['executions']) & set(partition_indices(self.contract)['final_test']))
        metrics = candidate_metrics(result['executions'])
        self.assertEqual(metrics['structural_criteria']['passed'], 0)
        self.assertEqual(metrics['label_agreement']['matches'], len(result['executions']))

    @unittest.skipUnless(ENGINE_AVAILABLE, 'Optional reviewed schema dependencies are not installed')
    def test_unlabelled_search_keeps_judge_proxy_and_cannot_use_structure_to_unlock_final(self):
        self.make_run(labelled=False)
        with self.responses():
            execute_search(self.repository, self.search_id, self.owner, self.gateway, builtin_registry())
        result = self.repository.get(self.search_id)
        self.assertTrue(all(r['criteria']['passed'] for r in result['executions']))
        self.assertTrue(all(r['label_agreement'] is None and r['judge']['source'] == 'uncalibrated_model_judge' for r in result['executions']))
        self.assertEqual(result['selection']['evidence']['basis'], 'uncalibrated_model_judge_proxy')
        self.assertTrue(all(d['action'] == 'active' for d in result['decisions']))
        with self.assertRaisesRegex(DeliveryError, 'final_quality_checks_or_human_review_required'):
            DeliveryRepository(self.store).prepare(self.search_id, uuid.uuid4().hex)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM final_quarantine').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)

    @unittest.skipUnless(ENGINE_AVAILABLE, 'Optional reviewed schema dependencies are not installed')
    def test_mutation_context_retains_structural_evidence_and_never_final_feedback(self):
        self.make_run()
        with self.responses(proposal({'type': 'integer'})):
            execute_search(self.repository, self.search_id, self.owner, self.gateway, builtin_registry())
        root = self.repository.get(self.search_id)
        parent = next(c for c in root['candidates'] if c['family'] == 'rules.v1')
        child = self.repository.evolve(self.search_id, parent['id'], uuid.uuid4().hex)
        child_id, owner = self.repository.claim()
        child_gateway = ModelGateway(self.provider.profile, self.repository.ledger, child_id, allow_fixture=True)
        execute_search(self.repository, child_id, owner, child_gateway, builtin_registry())
        request = next(c for c in self.provider.calls if 'Improve this executable artifact' in c['messages'][0]['content'])
        context = json.loads(request['messages'][1]['content'])
        self.assertEqual(context['structural_criteria_hypothesis'], root['criteria_bundle'])
        self.assertTrue(all(r['criteria']['status'] == 'checked' for r in context['feedback']))
        final_indices = set(partition_indices(self.contract)['final_test'])
        self.assertFalse({r['sample_index'] for r in context['feedback']} & final_indices)
        self.assertEqual(get_bundle(self.repository, child['id']), root['criteria_bundle'])
        self.assertEqual(sum(c['purpose'] == 'criteria_proposer' for c in self.repository.ledger.status()['calls']), 1)
