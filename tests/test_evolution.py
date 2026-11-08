from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from solve2ship.candidates import builtin_registry
from solve2ship.contracts import compile_task
from solve2ship.gateway import BudgetError, MICRO, ModelGateway
from solve2ship.search import SearchError, SearchRepository, SearchWorker
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository
from test_evaluation import task_definition
from test_search import plan, ProviderFixture


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name)/'state.db')
        self.repository = SearchRepository(self.store)
        self.repository.ledger.configure('work',100*MICRO)
        self.provider = ProviderFixture(); self.addCleanup(self.provider.close)
        definition = task_definition()
        self.task = TaskRepository(self.store).save(definition,compile_task(definition)['content_sha256'])
        self.worker = SearchWorker(self.store,Path(directory.name)/'not-a-real-key-file')
        for mock in [patch('solve2ship.gateway.time.strftime',return_value='2026-11-08'),
                     patch('solve2ship.search.load_profile',return_value=self.provider.profile),
                     patch('solve2ship.search.ModelGateway',side_effect=lambda profile,ledger,scope: ModelGateway(profile,ledger,scope,allow_fixture=True))]:
            mock.start(); self.addCleanup(mock.stop)

    def create(self):
        return self.repository.create(self.task['task_id'],1,uuid.uuid4().hex)

    def test_worker_automatically_mutates_and_retests_then_stops_at_saturated_cheap_leader(self):
        self.provider.mode='weak_plan'
        original=self.create(); self.assertTrue(self.worker.tick())
        roots=self.repository.list()
        child=next(row for row in roots if row['source_search_id']==original['id'])
        self.assertEqual(child['state'],'queued')
        self.assertTrue(self.worker.tick())
        revised=self.repository.get(child['id'])
        self.assertEqual(revised['state'],'succeeded')
        self.assertEqual(len(revised['mutations']),1)
        comparison=next(row['detail'] for row in revised['events'] if row['type']=='mutation_comparison')
        self.assertGreater(comparison['observed_match_delta'],0)
        self.assertFalse(comparison['final_test_used'])
        self.assertFalse(comparison['generalization_gain_claimed'])
        graph=self.repository.lineage(child['id'])
        self.assertEqual({edge['kind'] for edge in graph['edges']},{'changed','retested'})
        self.assertEqual(graph['root_search_id'],original['id'])
        self.assertEqual(len(self.repository.list()),2)
        self.assertEqual(self.repository.get(original['id'])['state'],'succeeded')
        self.assertLessEqual(revised['budget']['root_committed_ceiling_cny'],20)

    def test_regression_retains_original_artifact_and_supports_actual_rollback_reexecution(self):
        original=self.create(); self.worker.tick()
        before=self.repository.get(original['id'])
        parent=next(c for c in before['candidates'] if c['family']=='rules.v1')
        self.provider.mode='regression'
        child=self.repository.evolve(before['id'],parent['id'],uuid.uuid4().hex)
        self.worker.tick(); revised=self.repository.get(child['id'])
        comparison=next(row['detail'] for row in revised['events'] if row['type']=='mutation_comparison')
        self.assertTrue(comparison['regression_observed'])
        selected=next(c for c in revised['candidates'] if c['id']==revised['selection']['candidate_id'])
        self.assertEqual(selected['artifact_sha256'],parent['artifact_sha256'])
        restored=self.repository.rollback(before['id'],uuid.uuid4().hex)
        self.worker.tick(); restored=self.repository.get(restored['id'])
        self.assertEqual(len(restored['candidates']),1)
        self.assertEqual(restored['candidates'][0]['artifact_sha256'],parent['artifact_sha256'])
        self.assertTrue(restored['executions'])
        self.assertEqual(self.repository.get(before['id'])['executions'],before['executions'])

    def test_root_cancellation_stops_queued_descendants_and_future_automatic_mutation(self):
        self.provider.mode='weak_plan'; root=self.create(); self.worker.tick()
        self.assertTrue(any(row['source_search_id']==root['id'] and row['state']=='queued' for row in self.repository.list()))
        self.repository.cancel(root['id'])
        members=self.repository.list()
        self.assertTrue(all(row['state']!='queued' for row in members))
        source=self.repository.get(root['id']); candidate=source['candidates'][0]
        with self.assertRaises(SearchError):
            self.repository.evolve(source['id'],candidate['id'],uuid.uuid4().hex,automatic=True)
        self.assertFalse(self.worker.tick())

    def test_parent_and_children_share_one_money_budget(self):
        root=self.create(); self.repository.claim()
        self.repository.ledger.configure(root['id'],3*MICRO)
        self.repository.ledger.reserve(root['id'],'uncertain','deepseek-flash',128)
        child=self.repository.create(self.task['task_id'],1,uuid.uuid4().hex,source_search_id=root['id'])
        self.repository.claim()
        with self.assertRaises(BudgetError):
            self.repository.ledger.reserve(child['id'],'second','deepseek-flash',128)
        self.assertLessEqual(self.repository.ledger.status(root['id'])['root_committed_ceiling_cny'],3)

    def test_execution_slots_are_atomic_across_competing_workers(self):
        definition=task_definition(); definition['budget']['max_candidate_executions']=2
        task=TaskRepository(self.store).save(definition,compile_task(definition)['content_sha256'])
        root=self.repository.create(task['task_id'],1,uuid.uuid4().hex); self.repository.claim()
        candidates=[self.repository.add_candidate(root['id'],plan()['candidates'][0],{'source':'fixture'}) for _ in range(4)]
        slots=[(candidate,index) for candidate in candidates for index in [0,1]]
        def reserve(slot):
            try:
                self.repository.reserve_execution(root['id'],slot[0],slot[1]); return True
            except BudgetError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(reserve,slots))
        self.assertEqual(sum(results),2)
        self.assertEqual(self.repository.tree_execution_count(root['id']),2)

    def test_known_format_failure_gets_one_distinct_repair_generation(self):
        root=self.create(); self.worker.tick()
        source=self.repository.get(root['id']); parent=source['candidates'][0]
        self.provider.mode='invalid_mutation_once'
        failed=self.repository.evolve(source['id'],parent['id'],uuid.uuid4().hex)
        self.worker.tick()
        failure=self.repository.get(failed['id'])
        self.assertEqual(failure['state'],'failed')
        scheduled=next(event['detail']['child_search_id'] for event in failure['events'] if event['type']=='generation_repair_scheduled')
        self.worker.tick()
        repaired=self.repository.get(scheduled)
        self.assertEqual(repaired['state'],'succeeded')
        self.assertEqual(repaired['protocol_version'],'m4-repair')
        self.assertTrue(repaired['mutations'])
        self.assertEqual(self.repository.get(failed['id'])['state'],'failed')
        calls=[call for call in self.provider.calls if 'Improve this executable artifact' in call['messages'][0]['content']]
        self.assertEqual(len(calls),2)
        self.assertNotEqual(calls[0]['messages'][1]['content'],calls[1]['messages'][1]['content'])

    def test_uncertain_or_auth_failure_never_schedules_paid_repair(self):
        self.provider.mode='unauthorized'
        root=self.create(); self.worker.tick()
        failed=self.repository.get(root['id'])
        self.assertEqual(failed['state'],'failed')
        self.assertEqual(len(self.repository.list()),1)
        self.assertEqual(failed['budget']['calls'][0]['state'],'reserved')
        self.assertGreater(failed['budget']['root_committed_ceiling_cny'],2)
