"""Protocol fixtures are test-only; no live model key/call is used by make check."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import test_foundation as foundation
from solve2ship.candidates import ArtifactError, CandidateRegistry, RulesAdapter, builtin_registry, match_rules
from solve2ship.contracts import compile_task
from solve2ship.gateway import BudgetError, Ledger, MICRO, ModelGateway, ProviderError
from solve2ship.model_config import validate_profile
from solve2ship.search import SearchRepository, execute_search
from solve2ship.server import LoopbackServer
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository


def plan():
    return {'candidates': [
        {'family': 'rules.v1', 'name': 'Rules fixture', 'rationale': 'Test bounded literal rules',
         'artifact': {'rules': [{'path': ['text'], 'op': 'contains_any', 'values': ['cancel'], 'output': 'cancel'}], 'default': 'other'}},
        {'family': 'prompt.v1', 'name': 'Prompt fixture', 'rationale': 'Test protocol transport',
         'artifact': {'system_prompt': 'Return JSON with a result key containing cancel or other.'}},
    ]}


class ProviderFixture:
    def __init__(self):
        self.calls = []
        self.mode = 'normal'
        self.mutations_seen = 0
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.calls.append(request)
                if fixture.mode == 'redirect':
                    self.send_response(302); self.send_header('Location', '/must-not-follow'); self.end_headers(); return
                if fixture.mode == 'unauthorized':
                    self.send_response(401); self.end_headers(); return
                is_planner = 'solution planner' in request['messages'][0]['content']
                output = plan() if is_planner else {'result': 'cancel' if 'cancel' in request['messages'][1]['content'].lower() else 'other'}
                if is_planner and fixture.mode == 'weak_plan':
                    output['candidates'][0]['artifact']['rules'][0]['output'] = 'known-wrong-fixture-value'
                    output['candidates'][0]['artifact']['default'] = 'known-wrong-fixture-value'
                if 'You evaluate a candidate output' in request['messages'][0]['content']:
                    output = {'score': 0.5, 'reason': 'Explicit uncalibrated test fixture'}
                if 'Improve this executable artifact' in request['messages'][0]['content']:
                    fixture.mutations_seen += 1
                    candidate = plan()['candidates'][0]
                    candidate['name'] = 'Mutated rules fixture'
                    candidate['artifact']['rules'][0]['values'].append('withdraw')
                    if fixture.mode == 'regression':
                        candidate['artifact'] = {'rules': [], 'default': 'cancel'}
                    output = {'candidate': candidate, 'mutation_reason': 'Explicit protocol fixture mutation, not live optimization evidence'}
                response = {'id': 'fixture-provider-id', 'model': 'deepseek-flash', 'system_fingerprint': 'fixture-fingerprint',
                            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
                            'usage': {'prompt_tokens': 50, 'completion_tokens': 20}}
                if fixture.mode == 'invalid_mutation_once' and 'Improve this executable artifact' in request['messages'][0]['content'] and fixture.mutations_seen == 1:
                    response['choices'][0]['message']['content'] = 'invalid JSON fixture, not a live result'
                if fixture.mode == 'echo_key':
                    response['choices'][0]['message']['content'] = json.dumps({'result':fixture.profile.api_key})
                if fixture.mode == 'missing_usage':
                    response.pop('usage')
                if fixture.mode == 'truncated':
                    response['choices'][0]['finish_reason'] = 'length'
                if fixture.mode == 'malformed':
                    response['choices'][0]['message']['content'] = 'not JSON'
                body = json.dumps(response).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        self.server = LoopbackServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.profile = validate_profile({'base_url': f'http://127.0.0.1:{self.server.server_port}', 'model': 'deepseek-flash', 'api_key': 'unit-test-placeholder'})

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


class BillingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.db'; self.store = Store(self.path)
        self.ledger = Ledger(self.store)
        self.ledger.configure('work', 100 * MICRO); self.ledger.configure('scope', 10 * MICRO)
        date = patch('solve2ship.gateway.time.strftime', return_value='2026-11-09'); date.start(); self.addCleanup(date.stop)

    def test_concurrent_reservation_cannot_overspend(self):
        self.ledger.configure('work', 3 * MICRO)
        def reserve(_):
            try:
                return self.ledger.reserve('scope', 'test', 'deepseek-flash', 128)
            except BudgetError:
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(reserve, range(8)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertLessEqual(self.ledger.status()['committed_ceiling_cny'], 3)

    def test_unsettled_cost_survives_restart_and_zero_budget_denies(self):
        call = self.ledger.reserve('scope', 'test', 'deepseek-flash', 128)
        self.ledger.uncertain(call, 'connection_lost')
        restarted = Ledger(Store(self.path))
        self.assertGreater(restarted.status()['committed_ceiling_cny'], 2)
        self.assertEqual(restarted.status()['calls'][0]['state'], 'reserved')
        with self.assertRaises(BudgetError):
            restarted.configure('work', 0)

    def test_settlement_is_once_only_and_usage_is_measured(self):
        call = self.ledger.reserve('scope', 'test', 'deepseek-flash', 128)
        response = {'usage': {'prompt_tokens': 100, 'completion_tokens': 10}, 'id': 'fixture-id'}
        self.ledger.settle(call, response); self.ledger.settle(call, response)
        self.assertAlmostEqual(self.ledger.status()['committed_ceiling_cny'], 0.00028)
        self.assertFalse(self.ledger.status()['provider_bill_verified'])
        with self.assertRaises(ProviderError):
            self.ledger.settle(call, {'usage': None})

    def test_unverified_model_and_stale_pricing_fail_closed(self):
        with self.assertRaises(BudgetError):
            self.ledger.reserve('scope', 'test', 'unpriced-model', 128)
        with patch('solve2ship.gateway.time.strftime', return_value='2099-01-01'), self.assertRaises(BudgetError):
            self.ledger.reserve('scope', 'test', 'deepseek-flash', 128)
        self.assertEqual(self.ledger.status()['calls'], [])
        provider = ProviderFixture(); self.addCleanup(provider.close)
        with self.assertRaises(BudgetError):
            ModelGateway(provider.profile, self.ledger, 'scope')

    def test_settlement_keeps_original_call_rates_after_a_later_price_review(self):
        call = self.ledger.reserve('scope', 'test', 'deepseek-flash', 128)
        snapshot = self.ledger.status()['calls'][0]['pricing']
        with patch('solve2ship.gateway.INPUT_RATE', 99), patch('solve2ship.gateway.OUTPUT_RATE', 99):
            self.ledger.settle(call, {'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
        status = self.ledger.status()
        self.assertAlmostEqual(status['committed_ceiling_cny'], .00028)
        self.assertEqual(status['calls'][0]['pricing'], snapshot)
        with self.store.connection(write=True) as db:
            with self.assertRaises(Exception):
                db.execute("UPDATE model_calls SET pricing_json='{}' WHERE id=?", (call,))

    def test_schema8_legacy_price_backfill_does_not_reset_usage_or_unknown_reservations(self):
        call = self.ledger.reserve('scope', 'test', 'deepseek-flash', 128)
        before = self.ledger.status()['committed_ceiling_cny']
        with self.store.connection(write=True) as db:
            db.execute('DROP TRIGGER pricing_no_update')
            db.execute('ALTER TABLE model_calls DROP COLUMN pricing_json')
            db.execute('ALTER TABLE executions DROP COLUMN runtime_json')
            db.execute('DROP TABLE service_exports')
            db.execute('DROP TABLE service_export_versions')
            db.execute('DROP TABLE service_budget_grants')
            db.execute('DROP TABLE quality_review_events')
            db.execute('DROP TABLE delivery_quality_decisions')
            db.execute('DROP TABLE criteria_attempts')
            db.execute('DROP TABLE criteria_bundles')
            db.execute('ALTER TABLE executions DROP COLUMN criteria_json')
            db.execute('PRAGMA user_version=8')
        migrated = Ledger(Store(self.path))
        status = migrated.status()
        self.assertEqual(status['committed_ceiling_cny'], before)
        self.assertEqual(status['calls'][0]['id'], call)
        self.assertEqual(status['calls'][0]['state'], 'reserved')
        self.assertTrue(status['calls'][0]['pricing']['legacy_snapshot_backfill'])
        self.assertEqual(status['calls'][0]['pricing']['verified_date'], '2026-11-07')

    def test_real_http_adapter_records_usage_and_never_puts_secret_in_evidence(self):
        provider = ProviderFixture(); self.addCleanup(provider.close)
        gateway = ModelGateway(provider.profile, self.ledger, 'scope', allow_fixture=True)
        value, call = gateway.json('Return JSON with a result key.', 'cancel', purpose='test', max_tokens=128)
        self.assertEqual(value, {'result': 'cancel'})
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]['thinking'], {'type': 'disabled'})
        self.assertEqual(self.ledger.status()['calls'][0]['id'], call)
        self.assertNotIn(provider.profile.api_key, json.dumps(self.ledger.status()))

    def test_errors_do_not_retry_and_ambiguous_billing_keeps_reservation(self):
        provider = ProviderFixture(); self.addCleanup(provider.close)
        gateway = ModelGateway(provider.profile, self.ledger, 'scope', allow_fixture=True)
        for mode in ['unauthorized', 'redirect', 'missing_usage']:
            provider.mode = mode
            before = len(provider.calls)
            with self.subTest(mode=mode), self.assertRaises(ProviderError):
                gateway.json('Return JSON.', 'test', purpose=mode, max_tokens=128)
            self.assertEqual(len(provider.calls), before + 1)
        self.assertEqual(len(self.ledger.status()['calls']), 3)
        self.assertTrue(all(row['state'] == 'reserved' for row in self.ledger.status()['calls']))

    def test_invalid_generated_content_still_accounts_for_real_usage(self):
        provider = ProviderFixture(); self.addCleanup(provider.close)
        gateway = ModelGateway(provider.profile, self.ledger, 'scope', allow_fixture=True)
        for mode in ['malformed', 'truncated']:
            provider.mode = mode
            with self.subTest(mode=mode), self.assertRaises(ProviderError):
                gateway.json('Return JSON.', 'test', purpose=mode, max_tokens=128)
        self.assertTrue(all(row['state'] == 'settled' for row in self.ledger.status()['calls']))

    def test_provider_key_echo_is_redacted_and_raw_content_is_not_logged(self):
        provider=ProviderFixture(); self.addCleanup(provider.close); provider.mode='echo_key'
        gateway=ModelGateway(provider.profile,self.ledger,'scope',allow_fixture=True)
        value,call_id=gateway.json('Return JSON.','test',purpose='test',max_tokens=128)
        self.assertEqual(value,{'result':'[REDACTED]'})
        with self.store.connection() as db:
            raw=db.execute('SELECT diagnostics_json FROM model_calls WHERE id=?',(call_id,)).fetchone()[0]
        self.assertNotIn(provider.profile.api_key,raw)
        self.assertNotIn('"content":',raw)


class SearchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.db'; self.store = Store(self.path)
        self.repository = SearchRepository(self.store)
        self.repository.ledger.configure('work', 100 * MICRO)
        self.definition = {'samples': [{'text': 'cancel'}, {'text': 'hi'}], 'output_specification': 'Output cancel or other.',
                           'budget': {'max_cost_cny': 10, 'max_candidate_executions': 10}}
        self.task = TaskRepository(self.store).save(self.definition, compile_task(self.definition)['content_sha256'])
        date = patch('solve2ship.gateway.time.strftime', return_value='2026-11-09'); date.start(); self.addCleanup(date.stop)

    def create(self, request_id=None):
        return self.repository.create(self.task['task_id'], 1, request_id or uuid.uuid4().hex, protocol='m2')

    def test_build_and_execute_two_families_via_protocol_fixture(self):
        provider = ProviderFixture(); self.addCleanup(provider.close)
        run = self.create(); search_id, owner = self.repository.claim()
        execute_search(self.repository, search_id, owner, ModelGateway(provider.profile, self.repository.ledger, search_id, allow_fixture=True))
        saved = self.repository.get(run['id'])
        self.assertEqual(saved['state'], 'succeeded')
        self.assertEqual({c['family'] for c in saved['candidates']}, {'rules.v1', 'prompt.v1'})
        self.assertEqual(len(saved['executions']), 4)
        self.assertTrue(all(row['label_agreement'] is None for row in saved['executions']))
        self.assertEqual(len(saved['budget']['calls']), 3)
        self.assertEqual(SearchRepository(Store(self.path)).get(search_id)['executions'], saved['executions'])
        for table in ['candidates', 'executions', 'search_events']:
            with self.store.connection() as db, self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM ' + table)

    def test_request_id_is_idempotent_and_cancel_fences_completion(self):
        key = uuid.uuid4().hex
        first = self.create(key)
        self.assertEqual(self.create(key)['id'], first['id'])
        search_id, owner = self.repository.claim()
        self.repository.cancel(search_id); self.repository.finish(search_id, owner)
        self.assertEqual(self.repository.get(search_id)['state'], 'cancelled')
        self.assertEqual(self.repository.get(search_id)['budget']['calls'], [])

    def test_expired_paid_search_is_interrupted_without_automatic_requeue(self):
        run = self.create(); search_id, owner = self.repository.claim()
        with self.store.connection(write=True) as db:
            db.execute('UPDATE searches SET lease_until=? WHERE id=?', (time.time() - 1, search_id))
        self.assertIsNone(self.repository.claim())
        self.repository.finish(search_id, owner)
        self.assertEqual(self.repository.get(run['id'])['state'], 'interrupted')

    def test_zero_contract_budget_blocks_before_any_model_call(self):
        value = dict(self.definition, budget={'max_cost_cny': 0, 'max_candidate_executions': 10})
        task = TaskRepository(self.store).save(value, compile_task(value)['content_sha256'])
        with self.assertRaises(ValueError):
            self.repository.create(task['task_id'], 1, uuid.uuid4().hex)
        self.assertEqual(self.repository.ledger.status()['calls'], [])

    def test_rule_language_is_bounded_data_and_registry_is_open(self):
        artifact = plan()['candidates'][0]['artifact']
        self.assertEqual(match_rules(artifact, {'text': 'CANCEL now'}), (True, 'cancel'))
        registry = builtin_registry()
        with self.assertRaises(ArtifactError):
            registry.validate({'family': 'python', 'name': 'x', 'rationale': 'x', 'artifact': {'code': 'print(1)'}})
        invalid = plan()['candidates'][0]
        invalid['artifact']['rules'][0]['op'] = 'eval'
        with self.assertRaises(ArtifactError):
            registry.validate(invalid)
        class FutureAdapter(RulesAdapter):
            family = 'future.v1'
        registry.register(FutureAdapter())
        self.assertIn('future.v1', registry.describe())


class SearchHTTPTests(unittest.TestCase):
    setUp = foundation.HTTPTests.setUp
    close = foundation.HTTPTests.close
    request = foundation.HTTPTests.request

    def test_http_create_read_cancel_and_budget(self):
        ledger = Ledger(self.store); ledger.configure('work', 100 * MICRO)
        value = {'samples': ['input'], 'output_specification': 'Output a result.', 'budget': {'max_cost_cny': 10, 'max_candidate_executions': 3}}
        task = TaskRepository(self.store).save(value, compile_task(value)['content_sha256'])
        body = {'task_id': task['task_id'], 'revision': 1, 'request_id': uuid.uuid4().hex}
        status, run, _ = self.request('/api/searches', body)
        self.assertEqual(status, 201)
        self.assertEqual(self.request('/api/searches', body)[1]['id'], run['id'])
        self.assertEqual(self.request('/api/searches/' + run['id'])[0], 200)
        self.assertEqual(self.request('/api/searches/' + run['id'] + '/cancel', {})[1]['state'], 'cancelled')
        self.assertEqual(self.request('/api/budget')[1]['limit_cny'], 100)
        self.assertEqual(self.request('/api/searches', {}, headers={'Origin': 'https://evil.example'})[0], 403)
