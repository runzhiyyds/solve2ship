"""Explicit protocol fixtures. No real model or generated host execution here."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from solve2ship.candidates import ArtifactError, builtin_registry
from solve2ship.contracts import compile_task, sha256
from solve2ship.evaluation import partition_indices
from solve2ship.gateway import BudgetError, ModelGateway, MICRO, ProviderError, CONTEXT_CEILING, INPUT_RATE, OUTPUT_RATE
from solve2ship.search import SearchRepository, execute_search
from solve2ship.service_model import ServiceState
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository
from solve2ship.tool_agent import ToolAgentAdapter
from test_search import ProviderFixture, plan
from test_workflow_adapter import SandboxProtocolFixture


def artifact():
    return {'instructions': 'Protocol fixture only: use frozen tools, then return the observation.',
            'max_steps': 3, 'tools': [
                {'id': 'echo', 'description': 'Return JSON argument unchanged.', 'family': 'python.v1',
                 'artifact': {'source': 'NOT_EXECUTABLE_FIXTURE_CODE'}},
                {'id': 'literal', 'description': 'Return cancel.', 'family': 'rules.v1',
                 'artifact': {'rules': [], 'default': 'cancel'}}]}


class ToolAgentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'state.db')
        self.repository = SearchRepository(self.store)
        self.repository.ledger.configure('work', 100 * MICRO)
        self.provider = ProviderFixture(); self.addCleanup(self.provider.close)
        date = patch('solve2ship.gateway.time.strftime', return_value='2026-11-08')
        date.start(); self.addCleanup(date.stop)
        definition = {'samples': [{'text': 'cancel', 'case': i} for i in range(20)],
                      'output_specification': 'Return cancel.',
                      'labels': [{'sample_index': i, 'expected_output': 'cancel'} for i in range(20)],
                      'budget': {'max_cost_cny': 10, 'max_candidate_executions': 60}}
        self.contract = compile_task(definition)['contract']
        saved = TaskRepository(self.store).save(definition, compile_task(definition)['content_sha256'])
        self.run = self.repository.create(saved['task_id'], 1, uuid.uuid4().hex)
        self.search_id, self.owner = self.repository.claim()
        self.gateway = ModelGateway(self.provider.profile, self.repository.ledger, self.search_id, allow_fixture=True)
        self.original_json = self.gateway.json
        self.sandbox = SandboxProtocolFixture()
        self.adapter = ToolAgentAdapter(self.sandbox)

    def scripted(self, action=None):
        def respond(system, user, **kwargs):
            value, call_id = self.original_json(system, user, **kwargs)
            if kwargs['purpose'].startswith('candidate_agent_decision:') or ':candidate_agent_decision:' in kwargs['purpose']:
                context = json.loads(user)
                if action is not None:
                    return action(context), call_id
                if not context['observations']:
                    return {'tool': 'echo' if context['input'].get('code') else 'literal', 'input': context['input'].get('text')}, call_id
                return {'result': context['observations'][-1]['output']}, call_id
            return value, call_id
        return patch.object(self.gateway, 'json', side_effect=respond)

    def test_model_selects_tools_and_observes_results_with_complete_call_attribution(self):
        with self.scripted():
            output, last = self.adapter.execute(artifact(), {'text': 'cancel', 'code': True}, self.gateway)
        receipt = self.adapter.runtime_receipt()
        self.assertEqual(output, 'cancel'); self.assertEqual(len(self.sandbox.calls), 1)
        self.assertEqual(receipt['stop_reason'], 'final_answer'); self.assertEqual(receipt['tool_calls'], 1)
        calls = self.repository.ledger.status()['calls']
        self.assertEqual(receipt['model_call_ids'], [c['id'] for c in calls]); self.assertEqual(last, calls[-1]['id'])
        self.assertEqual(receipt['steps'][0]['runtime']['provider'], 'protocol_fixture_only')
        self.assertEqual(json.loads(self.provider.calls[-1]['messages'][1]['content'])['observations'][0]['output'], 'cancel')
        with self.scripted():
            self.assertEqual(self.adapter.execute(artifact(), {'text': 'other'}, self.gateway)[0], 'cancel')
        self.assertEqual(len(self.sandbox.calls), 1)
        self.assertEqual(self.adapter.runtime_receipt()['steps'][0]['tool'], 'literal')

    def test_invalid_configuration_is_rejected_without_paid_calls_or_host_code(self):
        variants = []
        for field, value in [('max_steps', True), ('max_steps', 5), ('instructions', ''), ('tools', [])]:
            a = artifact(); a[field] = value; variants.append(a)
        for value in ('agent.tools.v1', 'shell.v1', 'prompt.v1'):
            a = artifact(); a['tools'][0]['family'] = value; variants.append(a)
        a = artifact(); a['tools'][1]['id'] = 'echo'; variants.append(a)
        for a in variants:
            with self.assertRaises(ArtifactError): self.adapter.execute(a, {}, self.gateway)
        self.assertEqual(self.provider.calls, []); self.assertEqual(self.sandbox.calls, [])
        self.assertNotIn('agent.tools.v1', builtin_registry().adapters)
        with self.assertRaises(ArtifactError): ToolAgentAdapter(None)

    def test_unknown_action_and_tool_do_not_execute_and_are_still_billed(self):
        for action in ({'tool': 'shell', 'input': {}}, {'result': 1, 'tool': 'echo'}, {'tool': [], 'input': 1}):
            with self.scripted(lambda _: action), self.assertRaisesRegex(ArtifactError, 'invalid_agent_action'):
                self.adapter.execute(artifact(), {}, self.gateway)
            self.assertEqual(self.adapter.runtime_receipt()['model_calls'], 1)
        self.assertEqual(len(self.provider.calls), 3); self.assertEqual(self.sandbox.calls, [])

    def test_exhaustion_has_no_forced_final_call_or_useless_last_tool(self):
        with self.scripted(lambda _: {'tool': 'echo', 'input': 'value'}), self.assertRaisesRegex(ArtifactError, 'agent_step_limit'):
            self.adapter.execute(artifact(), {}, self.gateway)
        self.assertEqual(len(self.provider.calls), 3); self.assertEqual(len(self.sandbox.calls), 2)
        self.assertEqual(self.adapter.runtime_receipt()['stop_reason'], 'agent_step_limit')

    def test_cancellation_after_decision_stops_tool_submission(self):
        adapter = ToolAgentAdapter(self.sandbox, lambda: len(self.provider.calls) == 0)
        with self.scripted(), self.assertRaisesRegex(ArtifactError, 'agent_cancelled'):
            adapter.execute(artifact(), {'text': 'cancel', 'code': True}, self.gateway)
        self.assertEqual(len(self.provider.calls), 1); self.assertEqual(self.sandbox.calls, [])
        self.assertEqual(adapter.runtime_receipt()['status'], 'cancelled')

    def test_budget_unknown_charge_and_tool_failure_never_trigger_hidden_retries(self):
        self.repository.ledger.configure('work', CONTEXT_CEILING * INPUT_RATE + 1024 * OUTPUT_RATE)
        with self.scripted(), self.assertRaises(BudgetError):
            self.adapter.execute(artifact(), {'text': 'cancel'}, self.gateway)
        self.assertEqual(len(self.provider.calls), 1)
        self.repository.ledger.configure('work', 100 * MICRO)
        self.provider.mode = 'missing_usage'
        with self.assertRaises(ProviderError): self.adapter.execute(artifact(), {}, self.gateway)
        receipt = self.adapter.runtime_receipt(); calls = self.repository.ledger.status()['calls']
        self.assertEqual(receipt['model_call_ids'], [calls[-1]['id']]); self.assertEqual(calls[-1]['state'], 'reserved')
        self.provider.mode = 'normal'; self.sandbox.fail = True
        with self.scripted(), self.assertRaises(ArtifactError):
            self.adapter.execute(artifact(), {'code': True}, self.gateway)
        self.assertEqual(self.adapter.runtime_receipt()['model_calls'], 1)
        self.assertEqual(len(self.sandbox.calls), 1)

    def test_large_input_refuses_before_call_and_large_output_evidence_is_bounded(self):
        with self.assertRaises(ValueError): self.adapter.execute(artifact(), 'x' * 70000, self.gateway)
        self.assertEqual(self.provider.calls, [])
        with self.scripted(lambda _: {'result': 'x' * 9000}):
            self.adapter.execute(artifact(), {}, self.gateway)
        row = self.adapter.runtime_receipt()['steps'][0]
        self.assertTrue(row['output_omitted']); self.assertNotIn('output', row)

    def test_search_executes_agent_against_other_families_and_preserves_holdout(self):
        candidate = {'family': 'agent.tools.v1', 'name': 'Protocol fixture', 'rationale': 'Not real optimization', 'artifact': artifact()}
        original_plan = plan(); original_plan['candidates'].append(candidate)
        with patch('test_search.plan', return_value=original_plan), self.scripted():
            execute_search(self.repository, self.search_id, self.owner, self.gateway, builtin_registry(sandbox=self.sandbox))
        result = self.repository.get(self.search_id)
        agent = next(c for c in result['candidates'] if c['family'] == 'agent.tools.v1')
        rows = [r for r in result['executions'] if r['candidate_id'] == agent['id']]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r['runtime']['stop_reason'] == 'final_answer' for r in rows))
        self.assertTrue(all(len(r['runtime']['model_call_ids']) == 2 for r in rows))
        self.assertFalse(set(r['sample_index'] for r in rows) & set(partition_indices(self.contract)['final_test']))

    def test_standalone_request_fencing_caches_whole_agent_invocation(self):
        candidate = {'id': 'a' * 32, 'family': 'agent.tools.v1', 'artifact': artifact(), 'artifact_sha256': sha256(artifact())}
        state = ServiceState(self.store.path.parent / 'independent', candidate); state.authorize(10 * MICRO)
        gateway = ModelGateway(self.provider.profile, state.ledger, 'service', allow_fixture=True)
        self.gateway = gateway; self.original_json = gateway.json
        with self.scripted():
            self.assertEqual(state.predict('b' * 32, {'text': 'cancel'}, self.adapter, gateway), 'cancel')
            restored = ServiceState(state.path, candidate)
            self.assertEqual(restored.predict('b' * 32, {'text': 'cancel'}, self.adapter, gateway), 'cancel')
        self.assertEqual(len(self.provider.calls), 2)
        self.assertTrue(all(c['purpose'].startswith('service_request:' + 'b' * 32) for c in state.ledger.status()['calls']))

    def test_model_agent_final_packages_all_trusted_modules_and_once_only_call_scopes(self):
        from solve2ship.delivery import DeliveryRepository, bundle_files
        from solve2ship.model_config import save_profile
        from types import SimpleNamespace
        candidate = {'family': 'agent.tools.v1', 'name': 'Protocol fixture', 'rationale': 'Not real optimization', 'artifact': artifact()}
        cid = self.repository.add_candidate(self.search_id, candidate, {'source': 'protocol_fixture'})
        from solve2ship.contracts import canonical_json
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO selections VALUES(?,?,?)', (self.search_id, cid, canonical_json({'status': 'protocol_fixture'})))
        self.repository.finish(self.search_id, self.owner)
        p = self.provider.profile
        save_profile(self.store.path.parent / 'model.json', {'base_url': p.base_url, 'model': p.model, 'api_key': p.api_key})
        self.sandbox.engine = SimpleNamespace(status=lambda: {'engine_version': 'fixture-only-engine'})
        self.sandbox.accepted_engine_version = 'fixture-only-engine'
        self.sandbox.image = 'python@sha256:' + 'a' * 64
        def gateway(profile, ledger, scope):
            return ModelGateway(profile, ledger, scope, allow_fixture=True)
        with patch('solve2ship.delivery.ModelGateway', side_effect=gateway), patch('solve2ship.docker_runtime.configured_sandbox', return_value=self.sandbox):
            delivery = DeliveryRepository(self.store).prepare(self.search_id, uuid.uuid4().hex)
        self.assertEqual(delivery['report']['provided_label_matches'], 4)
        self.assertEqual(delivery['report']['model_calls'], 4)
        self.assertTrue(delivery['draft_integrity_verified'])
        draft = Path(delivery['draft_path'])
        self.assertEqual(set(json.loads((draft / 'manifest.json').read_text())['files_sha256']), bundle_files('agent.tools.v1'))
        self.assertTrue((draft / 'runtime/tool_agent.py').is_file())
        self.assertTrue(all(call['scope'] == 'delivery:' + delivery['id'] for call in self.repository.ledger.status()['calls']))
        # No model is needed to import the standalone setup CLI or inspect a zero budget.
        import subprocess
        output = subprocess.check_output([__import__('sys').executable, '-B', '-E', '-S', '-m', 'runtime.service_model', 'status'], cwd=draft)
        self.assertEqual(json.loads(output)['limit_cny'], 0)

    def test_loopback_http_service_executes_tool_then_fences_cached_operation(self):
        from types import SimpleNamespace
        import threading
        from urllib.request import Request
        from local_http import urlopen
        from solve2ship.contracts import canonical_json
        from solve2ship.service_runtime import create_service
        candidate = {'id': 'c' * 32, 'family': 'agent.tools.v1', 'artifact': artifact(), 'artifact_sha256': sha256(artifact()),
                     'runtime': {'image': 'fixture-image', 'engine_version': 'fixture-only-engine'}}
        state = ServiceState(self.store.path.parent / 'http-service', candidate); state.authorize(10 * MICRO)
        gateway = ModelGateway(self.provider.profile, state.ledger, 'service', allow_fixture=True)
        self.gateway = gateway; self.original_json = gateway.json
        self.sandbox.engine = SimpleNamespace(status=lambda: {'engine_version': 'fixture-only-engine'})
        with patch('solve2ship.docker_runtime.DockerSandbox', return_value=self.sandbox):
            server = create_service(candidate, port=0, model_state=state, model_gateway=gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            address = 'http://127.0.0.1:' + str(server.server_port)
            body = canonical_json({'request_id': 'd' * 32, 'input': {'text': 'cancel', 'code': True}}).encode()
            with self.scripted():
                for _ in range(2):
                    with urlopen(Request(address + '/predict', data=body, headers={'Content-Type': 'application/json'})) as response:
                        self.assertEqual(json.load(response), {'result': 'cancel'})
            self.assertEqual(len(self.provider.calls), 2); self.assertEqual(len(self.sandbox.calls), 1)
            with urlopen(address + '/contract') as response:
                self.assertTrue(json.load(response)['docker_required'])
        finally:
            server.shutdown(); server.server_close(); thread.join()
