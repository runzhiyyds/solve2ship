import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from solve2ship.contracts import compile_task
from solve2ship.evaluation import candidate_metrics, conservative_decision, partition_indices, wilson
from solve2ship.gateway import Ledger, MICRO, ModelGateway
from solve2ship.search import SearchRepository, execute_search
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository
from test_search import ProviderFixture


def task_definition(labelled=True):
    samples = [{'text': ('cancel' if index % 2 else 'hello') + f' unique-sentinel-{index}'} for index in range(20)]
    value = {'samples': samples, 'output_specification': 'Return cancel when the input asks to cancel, otherwise other.',
             'budget': {'max_cost_cny': 20, 'max_candidate_executions': 60}}
    if labelled:
        value['labels'] = [{'sample_index': i, 'expected_output': 'cancel' if i % 2 else 'other'} for i in range(20)]
    return value


class EvaluationPolicyTests(unittest.TestCase):
    def test_splits_are_deterministic_disjoint_and_duplicates_stay_together(self):
        definition = task_definition()
        definition['samples'].append(definition['samples'][0])
        contract = compile_task(definition)['contract']
        splits = partition_indices(contract)
        self.assertEqual(splits, partition_indices(contract))
        flat = [index for indices in splits.values() for index in indices]
        self.assertEqual(sorted(flat), list(range(21)))
        self.assertEqual(len(set(flat)), len(flat))
        self.assertTrue(splits['final_test'])
        self.assertEqual(next(role for role, indices in splits.items() if 0 in indices), next(role for role, indices in splits.items() if 20 in indices))

    def metrics(self, count, matched):
        return candidate_metrics([{'sample_index': index, 'label_agreement': int(index < matched), 'error': None,
                                   'judge': None, 'latency_ms': 1} for index in range(count)])

    def test_small_samples_and_proxy_judges_cannot_trigger_quality_pruning(self):
        weak, strong = self.metrics(2, 0), self.metrics(2, 2)
        self.assertEqual(conservative_decision(weak, [weak, strong])[0], 'active')
        self.assertEqual(wilson(0, 0), [0, 1])
        self.assertFalse(weak['quality_is_verified_accuracy'])
        self.assertEqual(weak['improvability_estimate']['source'], 'unvalidated_heuristic')

    def test_quality_pruning_requires_same_cases_and_separated_intervals(self):
        weak, strong = self.metrics(10, 0), self.metrics(10, 10)
        self.assertEqual(conservative_decision(weak, [weak, strong])[0], 'pruned')
        strong['label_agreement']['sample_indices'] = list(range(10, 20))
        self.assertEqual(conservative_decision(weak, [weak, strong])[0], 'active')

    def test_failed_runtime_is_paused_reversibly(self):
        metrics = candidate_metrics([{'sample_index': i, 'error': 'failure', 'latency_ms': 1} for i in range(2)])
        action, reason = conservative_decision(metrics, [metrics])
        self.assertEqual(action, 'paused')
        self.assertIn('reactivation_allowed', reason)


class EvaluationIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'state.db')
        self.repository = SearchRepository(self.store)
        self.repository.ledger.configure('work', 100 * MICRO)
        self.provider = ProviderFixture(); self.addCleanup(self.provider.close)
        date = patch('solve2ship.gateway.time.strftime', return_value='2026-11-09'); date.start(); self.addCleanup(date.stop)

    def run_task(self, definition):
        saved = TaskRepository(self.store).save(definition, compile_task(definition)['content_sha256'])
        run = self.repository.create(saved['task_id'], 1, uuid.uuid4().hex)
        search_id, owner = self.repository.claim()
        execute_search(self.repository, search_id, owner, ModelGateway(self.provider.profile, self.repository.ledger, search_id, allow_fixture=True))
        return self.repository.get(run['id'])

    def test_multi_fidelity_execution_never_exposes_final_data_to_planner_or_inference(self):
        definition = task_definition()
        run = self.run_task(definition)
        self.assertEqual(run['state'], 'succeeded')
        training = {row['sample_index'] for row in run['partitions'] if row['split'] == 'train'}
        final = {row['sample_index'] for row in run['partitions'] if row['split'] == 'final_test'}
        context = json.loads(self.provider.calls[0]['messages'][1]['content'])
        self.assertEqual({row['sample_index'] for row in context['training_examples']}, training)
        self.assertEqual({row['fidelity'] for row in run['executions']}, {'L1', 'L2'})
        self.assertTrue(all(row['split'] == 'validation' for row in run['executions']))
        transmitted = '\n'.join(call['messages'][1]['content'] for call in self.provider.calls)
        for index in final:
            self.assertNotIn(f'unique-sentinel-{index}"', transmitted)
        self.assertFalse(final & {row['sample_index'] for row in run['executions']})
        self.assertEqual(run['selection']['evidence']['status'], 'provisional')
        self.assertEqual(run['selection']['evidence']['basis'], 'provided_label_agreement_unverified')
        self.assertFalse(run['selection']['evidence']['final_test_evaluated'])

    def test_unlabelled_task_uses_explicit_uncalibrated_judge_evidence(self):
        run = self.run_task(task_definition(False))
        self.assertTrue(all(row['label_agreement'] is None for row in run['executions']))
        self.assertTrue(all(row['judge']['source'] == 'uncalibrated_model_judge' for row in run['executions']))
        self.assertEqual(run['selection']['evidence']['basis'], 'uncalibrated_model_judge_proxy')
        self.assertFalse(run['selection']['evidence']['quality_is_verified_accuracy'])

    def test_reactivation_reexecutes_frozen_artifacts_without_another_planner_call(self):
        original = self.run_task(task_definition())
        planner_calls = sum('solution planner' in call['messages'][0]['content'] for call in self.provider.calls)
        target = original['candidates'][0]
        revived = self.repository.reactivate(original['id'], target['id'], uuid.uuid4().hex)
        search_id, owner = self.repository.claim()
        execute_search(self.repository, search_id, owner, ModelGateway(self.provider.profile, self.repository.ledger, search_id, allow_fixture=True))
        rerun = self.repository.get(revived['id'])
        self.assertEqual(rerun['state'], 'succeeded')
        self.assertEqual({row['artifact_sha256'] for row in original['candidates']}, {row['artifact_sha256'] for row in rerun['candidates']})
        self.assertNotEqual(original['candidates'][0]['id'], rerun['candidates'][0]['id'])
        self.assertEqual(sum('solution planner' in call['messages'][0]['content'] for call in self.provider.calls), planner_calls)
        self.assertEqual(self.repository.get(original['id'])['executions'], original['executions'])

    def test_minimal_task_can_inherit_existing_authorization_but_explicit_zero_is_preserved(self):
        value={'samples':['only input'],'output_specification':'Return a JSON result.'}
        compiled=compile_task(value)
        self.assertEqual(compiled['contract']['definition']['budget']['mode'],'inherit_work')
        saved=TaskRepository(self.store).save(value,compiled['content_sha256'])
        run=self.repository.create(saved['task_id'],1,uuid.uuid4().hex)
        self.assertEqual(run['execution_limit'],120)
        self.assertEqual(run['budget']['limit_cny'],10)
        self.assertEqual(run['budget']['calls'],[])
        value['budget']={'max_cost_cny':0,'max_candidate_executions':120}
        compiled=compile_task(value)
        self.assertEqual(compiled['contract']['definition']['budget']['mode'],'explicit')
        saved=TaskRepository(self.store).save(value,compiled['content_sha256'])
        with self.assertRaises(ValueError):
            self.repository.create(saved['task_id'],1,uuid.uuid4().hex)
