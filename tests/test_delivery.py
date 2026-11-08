from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
import zipfile
from urllib.error import HTTPError
from urllib.request import Request
from local_http import urlopen
from unittest.mock import patch

from solve2ship.candidates import RulesAdapter
from solve2ship.contracts import canonical_json, compile_task, sha256
from solve2ship.delivery import DeliveryError, DeliveryRepository
from solve2ship.evaluation import partition_indices
from solve2ship.gateway import BudgetError, MICRO
from solve2ship.search import SearchError, SearchRepository
from solve2ship.store import Store
from solve2ship.server import create_server
from solve2ship.service_runtime import create_service
from solve2ship.service_export import export_service, exported_service
from solve2ship.tasks import TaskRepository
from test_evaluation import task_definition

spec = importlib.util.spec_from_file_location('verify_service', Path(__file__).resolve().parents[1] / 'scripts/verify_service.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state.db')
        self.searches = SearchRepository(self.store)
        self.searches.ledger.configure('work', 100*MICRO)
        self.deliveries = DeliveryRepository(self.store)

    def source(self, definition=None, artifact=None, family='rules.v1'):
        definition = definition or task_definition()
        task = TaskRepository(self.store).save(definition, compile_task(definition)['content_sha256'])
        run = self.searches.create(task['task_id'], 1, uuid.uuid4().hex)
        search_id, owner = self.searches.claim()
        artifact = artifact or {'rules': [{'path': ['text'], 'op': 'contains_any', 'values': ['cancel'], 'output': 'cancel'}], 'default': 'other'}
        candidate = {'family': family, 'name': 'test fixture only', 'rationale': 'trusted fixture, not real optimization', 'artifact': artifact}
        candidate_id = self.searches.add_candidate(search_id, candidate, {'source': 'test_fixture'})
        splits = partition_indices(compile_task(definition)['contract'])
        for index in splits['validation']:
            self.searches.reserve_execution(search_id, candidate_id, index)
            output = RulesAdapter().execute(artifact, definition['samples'][index], None)[0] if family == 'rules.v1' else 'other'
            self.searches.add_execution(search_id, candidate_id, index, output, None, 0.1, None, None, split='validation')
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO selections VALUES(?,?,?)', (search_id, candidate_id, canonical_json({'status': 'provisional', 'basis': 'test_fixture'})))
        self.searches.finish(search_id, owner)
        return self.searches.get(search_id), definition

    def prepare(self, run):
        return self.deliveries.prepare(run['id'], uuid.uuid4().hex)

    def test_frozen_rules_service_runs_independently_with_no_controller_or_secret(self):
        run, definition = self.source()
        result = self.prepare(run)
        self.assertEqual(result['state'], 'private_draft_ready')
        self.assertTrue(result['frozen']['frozen_before_final_execution'])
        self.assertEqual(result['report']['provided_label_matches'], result['report']['count'])
        self.assertFalse(result['report']['quality_is_verified_accuracy'])
        self.assertFalse(result['release_allowed'])
        cases = [{'input': definition['samples'][row['sample_index']], 'expected_output': row['output']} for row in run['executions']]
        # Local validation must never send task data to an inherited/system proxy.
        with patch('urllib.request.getproxies',return_value={'http':'http://127.0.0.1:9'}),patch('urllib.request.proxy_bypass',return_value=False):
            proof = verifier.verify(result['draft_path'], cases)
        self.assertTrue(proof['independent_process'])
        self.assertFalse(proof['state_database_required'])
        self.deliveries.record_proof(result['id'], proof)
        self.assertEqual(self.deliveries.get(result['id'])['proof'], proof)
        self.assertEqual(self.searches.get(run['id'])['executions'], run['executions'])

    def test_idempotent_concurrent_freeze_reserves_and_exposes_only_once(self):
        run, _ = self.source()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.prepare(run), range(2)))
        self.assertEqual(results[0]['id'], results[1]['id'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_reports').fetchone()[0], 1)
            final_count = db.execute('SELECT COUNT(*) FROM final_quarantine').fetchone()[0]
        self.assertEqual(self.searches.tree_execution_count(run['id']), len(run['executions'])+final_count)

    def test_exposed_final_data_cannot_be_retuned_after_reordering_or_relabelling(self):
        run, definition = self.source(); self.prepare(run)
        for value in [definition, {'samples': definition['samples'][::-1], 'output_specification': 'A different goal'}]:
            task = TaskRepository(self.store).save(value, compile_task(value)['content_sha256'])
            with self.assertRaisesRegex(SearchError, 'final_test_consumed'):
                self.searches.create(task['task_id'], 1, uuid.uuid4().hex)
        with self.assertRaisesRegex(SearchError, 'final_test_consumed'):
            self.searches.evolve(run['id'], run['candidates'][0]['id'], uuid.uuid4().hex)

    def test_failed_final_gate_is_retained_without_draft_or_replay(self):
        run, _ = self.source(artifact={'rules': [], 'default': 'wrong'})
        result = self.prepare(run)
        self.assertEqual(result['state'], 'final_checks_failed')
        self.assertIsNone(result['draft_path'])
        with patch.object(RulesAdapter, 'execute', side_effect=AssertionError('must not replay')):
            self.assertEqual(self.prepare(run)['report'], result['report'])

    def test_missing_final_data_labels_constraints_and_adapter_fail_before_exposure(self):
        for kind in ['small', 'unlabelled', 'hard', 'conflicting', 'prompt']:
            with self.subTest(kind=kind):
                definition = task_definition(kind != 'unlabelled')
                if kind == 'small':
                    definition['samples'] = definition['samples'][:2]; definition['labels'] = definition['labels'][:2]
                if kind == 'hard': definition['constraints'] = {'hard': ['external compliance review']}
                if kind == 'conflicting':
                    original_count = len(definition['samples'])
                    definition['samples'] += definition['samples'][:]
                    definition['labels'] += [{'sample_index': original_count+index, 'expected_output': 'conflict'} for index in range(original_count)]
                run, _ = self.source(definition, family='prompt.v1' if kind == 'prompt' else 'rules.v1')
                with self.assertRaises(DeliveryError): self.prepare(run)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM final_quarantine').fetchone()[0], 0)

    def test_overlapping_queued_search_fences_holdout_exposure(self):
        run, _ = self.source()
        self.searches.create(run['task_id'], 1, uuid.uuid4().hex)
        with self.assertRaisesRegex(DeliveryError, 'overlapping_search'):
            self.prepare(run)

    def test_insufficient_attempt_budget_does_not_expose_final(self):
        definition = task_definition(); definition['budget']['max_candidate_executions'] = 6
        run, _ = self.source(definition)
        with self.assertRaisesRegex(BudgetError, 'final_execution_budget'):
            self.prepare(run)

    def test_interrupted_final_run_is_not_replayed_and_reports_are_immutable(self):
        run, _ = self.source()
        with patch.object(RulesAdapter, 'execute', side_effect=RuntimeError('interruption')):
            with self.assertRaises(RuntimeError): self.prepare(run)
        result = self.prepare(run)
        self.assertEqual(result['state'], 'interrupted_or_in_progress')
        self.assertIsNone(result['report'])
        with self.store.connection(write=True) as db:
            with self.assertRaises(Exception): db.execute('DELETE FROM final_quarantine')

    def test_changed_bundle_source_is_not_executed(self):
        run, _ = self.source(); result = self.prepare(run)
        root = Path(result['draft_path'])
        (root/'runtime/service_runtime.py').write_text('raise RuntimeError("changed")')
        self.assertEqual(self.deliveries.get(result['id'])['state'], 'private_draft_incomplete')
        with self.assertRaisesRegex(ValueError, 'bundle_hash'):
            verifier.verify(root, [])

    def test_export_requires_proof_then_contains_license_without_database_or_credentials(self):
        run,definition=self.source();delivery=self.prepare(run)
        with self.assertRaisesRegex(DeliveryError,'proof_required'):export_service(self.store,delivery['id'])
        cases=[{'input':definition['samples'][row['sample_index']],'expected_output':row['output']} for row in run['executions']]
        proof=verifier.verify(delivery['draft_path'],cases);self.deliveries.record_proof(delivery['id'],proof)
        result=export_service(self.store,delivery['id'])
        self.assertEqual(export_service(self.store,delivery['id'])['id'],result['id'])
        self.assertTrue(result['archive_integrity_verified'])
        with zipfile.ZipFile(result['archive_path']) as bundle:
            names=bundle.namelist()
            self.assertIn('service/LICENSE',names);self.assertIn('service/NOTICE',names)
            self.assertIn('Chenyuan Guo',bundle.read('service/NOTICE').decode())
            self.assertFalse(any(name.endswith(('.sqlite3','model.json')) for name in names))
            self.assertIn('python@sha256:',bundle.read('service/Dockerfile').decode())
            provenance=json.loads(bundle.read('service/provenance.json'))
            self.assertIn('final_report_summary',provenance)
            self.assertNotIn('observations',provenance['final_report_summary'])
        Path(result['archive_path']).write_bytes(b'changed')
        self.assertFalse(exported_service(self.store,delivery['id'])['archive_integrity_verified'])

    def test_python_final_checks_keep_container_receipts_and_never_execute_source_on_host(self):
        from test_docker_runtime import EngineFixture
        from solve2ship.docker_runtime import DockerSandbox,approved_image
        definition=task_definition();definition['labels']=[{'sample_index':i,'expected_output':42} for i in range(20)]
        sentinel=self.store.path.parent/'must-not-execute'
        run,_=self.source(definition,artifact={'source':f'open({str(sentinel)!r},"w").write("unsafe")\ndef solve(x): return 42'},family='python.v1')
        sandbox=DockerSandbox(approved_image(),engine=EngineFixture());sandbox.accepted_engine_version='fixture'
        with patch('solve2ship.docker_runtime.configured_sandbox',return_value=sandbox):
            result=self.prepare(run)
        self.assertTrue(result['report']['gate_passed'])
        self.assertFalse(sentinel.exists())
        self.assertTrue(all(row['runtime']['provider']=='docker_local' and row['runtime']['container_removed'] for row in result['report']['observations']))

    def test_workflow_final_gate_packages_frozen_steps_without_host_execution(self):
        from test_docker_runtime import EngineFixture
        from solve2ship.docker_runtime import DockerSandbox,approved_image
        definition=task_definition();definition['labels']=[{'sample_index':i,'expected_output':42} for i in range(20)]
        artifact={'steps':[
            {'id':'normalize','family':'rules.v1','artifact':{'rules':[],'default':{'normalized':True}},'input':{'source':'input','path':[]}},
            {'id':'calculate','family':'rules.v1','artifact':{'rules':[],'default':42},'input':{'source':'normalize','path':[]}}
        ],'output':{'source':'calculate','path':[]}}
        run,_=self.source(definition,artifact=artifact,family='workflow.v1')
        sandbox=DockerSandbox(approved_image(),engine=EngineFixture());sandbox.accepted_engine_version='fixture'
        with patch('solve2ship.docker_runtime.configured_sandbox',return_value=sandbox):result=self.prepare(run)
        self.assertTrue(result['report']['gate_passed'])
        self.assertTrue((Path(result['draft_path'])/'runtime/workflow_adapter.py').is_file())
        self.assertTrue(all(len(row['runtime']['steps'])==2 for row in result['report']['observations']))
        self.assertEqual(result['frozen']['candidate']['artifact'],artifact)

    def test_export_refuses_a_credential_shaped_literal_in_the_frozen_artifact(self):
        value='sk-'+'a'*32
        definition=task_definition();definition['labels']=[{'sample_index':i,'expected_output':value} for i in range(20)]
        run,_=self.source(definition,artifact={'rules':[],'default':value});delivery=self.prepare(run)
        proof=verifier.verify(delivery['draft_path'],[{'input':{},'expected_output':value}])
        self.deliveries.record_proof(delivery['id'],proof)
        with self.assertRaisesRegex(DeliveryError,'possible_secret'):export_service(self.store,delivery['id'])
        self.assertIsNone(exported_service(self.store,delivery['id']))

    def test_failed_packaging_is_not_presented_as_a_ready_service_or_replayed(self):
        run, _ = self.source()
        with patch.object(DeliveryRepository, 'build_private_draft', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): self.prepare(run)
        with patch.object(RulesAdapter, 'execute', side_effect=AssertionError('must not repeat final test')):
            result = self.prepare(run)
        self.assertEqual(result['state'], 'private_draft_incomplete')
        self.assertTrue(result['report']['gate_passed'])
        self.assertFalse(result['draft_integrity_verified'])
        self.assertIsNone(result['draft_path'])

    def test_standalone_transport_rejects_unsafe_json_origin_and_oversize(self):
        artifact = {'rules': [], 'default': None}
        server = create_service({'id': 'test-only', 'family': 'rules.v1', 'artifact': artifact,
                                 'artifact_sha256': sha256(artifact)}, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        address = f'http://127.0.0.1:{server.server_port}'
        for raw in [b'{"input":NaN}', b'{"input":1,"input":2}', b'{"input":9007199254740992}', b'{"input":"\\ud800"}']:
            with self.subTest(raw=raw):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(address+'/predict', data=raw, headers={'Content-Type': 'application/json'}), timeout=2)
                self.assertEqual(error.exception.code, 422)
        for raw, headers, status in [(b'{"input":null}', {'Origin': 'https://other.test'}, 403),
                                     (b' '*262145, {}, 413)]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(address+'/predict', data=raw, headers={'Content-Type': 'application/json', **headers}), timeout=2)
            self.assertEqual(error.exception.code, status)
        with urlopen(Request(address+'/predict', data=b'{"input":null}', headers={'Content-Type': 'application/json'}), timeout=2) as response:
            self.assertEqual(json.load(response), {'result': None})

    def test_real_delivery_http_prepare_and_read_do_not_make_model_calls(self):
        run, _ = self.source()
        server = create_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        address = f'http://127.0.0.1:{server.server_port}/api/searches/{run["id"]}/delivery'
        with urlopen(address, timeout=2) as response:
            self.assertIsNone(json.load(response)['delivery'])
        req = Request(address, data=canonical_json({'request_id': uuid.uuid4().hex}).encode(),
                      headers={'Content-Type': 'application/json', 'X-STS-Request': '1'})
        with urlopen(req, timeout=2) as response:
            prepared = json.load(response)
            self.assertEqual(response.status, 201)
        with urlopen(address, timeout=2) as response:
            self.assertEqual(json.load(response)['delivery']['id'], prepared['id'])
        self.assertEqual(self.searches.ledger.status()['calls'], [])

    def test_request_id_conflict_does_not_select_a_different_candidate(self):
        run, _ = self.source(); request = uuid.uuid4().hex
        self.deliveries.prepare(run['id'], request)
        definition = task_definition(); definition['samples'] = [{'text': f'fresh input {i}'} for i in range(20)]
        other, _ = self.source(definition)
        with self.assertRaisesRegex(DeliveryError, 'idempotency_conflict'):
            self.deliveries.prepare(other['id'], request)
