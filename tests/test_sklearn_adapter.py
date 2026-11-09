import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from solve2ship.candidates import ArtifactError,builtin_registry
from solve2ship.contracts import compile_task
from solve2ship.evaluation import partition_indices
from solve2ship.gateway import ModelGateway,MICRO
from solve2ship.search import SearchRepository,execute_search
from solve2ship.sklearn_adapter import SklearnAdapter,eligible_numeric_task
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository
from test_search import ProviderFixture,plan


class SandboxFixture:
    """Protocol-only fixture. No sklearn fit or optimization result is claimed."""
    runtime_root=Path(__file__).resolve().parents[1]
    def __init__(self):self.calls=[];self.last=None
    def clear_receipt(self):self.last=None
    def receipt(self):return self.last
    def execute(self,source,payload):
        self.calls.append((source,copy.deepcopy(payload)))
        self.last={'provider':'protocol_fixture_only','container_removed':True}
        if 'labels' in payload:
            return {'model':'opaque-fixture-model-not-a-real-estimator','sklearn_version':'1.9.1',
                    'training_count':len(payload['features']),'feature_count':len(payload['features'][0]),'random_state':0,'warnings':[]}
        return 'positive'


def proposal():
    return {'family':'sklearn.classifier.v1','name':'protocol fixture model','rationale':'fixture only',
            'artifact':{'feature_path':['features'],'algorithm':'logistic_regression','params':{},'model':None}}


class SklearnBoundaryTests(unittest.TestCase):
    def test_fitted_blob_stays_opaque_on_host_and_bad_input_clears_prior_receipt(self):
        sandbox=SandboxFixture();adapter=SklearnAdapter(sandbox)
        rows=[{'sample_index':i,'input':{'features':[i,1]},'expected_output':'positive' if i%2 else 'negative'} for i in range(6)]
        with patch('pickle.loads',side_effect=AssertionError('must not unpickle on host')):
            fitted,evidence=adapter.build(proposal()['artifact'],rows)
            self.assertEqual(adapter.execute(fitted,{'features':[5,1]},None)[0],'positive')
        self.assertEqual(evidence['training_sample_indices'],list(range(6)))
        with self.assertRaises(ArtifactError):adapter.execute(fitted,{'missing':[]},None)
        self.assertIsNone(adapter.runtime_receipt())

    def test_unbounded_or_unknown_configuration_is_rejected(self):
        for replacement in [{'algorithm':'arbitrary_import'},{'params':{'C':0}},{'feature_path':['x']*6},{'algorithm':'mlp','params':{'hidden_layers':[1000]}}]:
            with self.subTest(replacement=replacement),self.assertRaises(ArtifactError):
                SklearnAdapter.validate({**proposal()['artifact'],**replacement})

    def test_insufficient_or_conflicting_labels_do_not_invent_a_trained_model(self):
        sandbox=SandboxFixture();adapter=SklearnAdapter(sandbox)
        rows=[{'sample_index':i,'input':{'features':[0,1]},'expected_output':'positive' if i%2 else 'negative'} for i in range(6)]
        with self.assertRaisesRegex(ArtifactError,'training_labels'):adapter.build(proposal()['artifact'],rows)
        self.assertEqual(sandbox.calls,[])

    def test_numeric_eligibility_does_not_use_final_examples(self):
        definition={'samples':[{'text':'train'}]*4+[{'features':[1,2]}]*4,'output_specification':'Return the class.',
                    'labels':[{'sample_index':i,'expected_output':'a' if i%2 else 'b'} for i in range(8)]}
        contract=compile_task(definition)['contract']
        with patch('solve2ship.evaluation.partition_indices',return_value={'train':list(range(4)),'validation':[],'final_test':list(range(4,8))}):
            self.assertFalse(eligible_numeric_task(contract))

    def test_search_build_receives_train_only_and_frozen_reactivation_does_not_refit(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        store=Store(Path(directory.name)/'state.db');repository=SearchRepository(store);repository.ledger.configure('work',100*MICRO)
        definition={'samples':[{'features':[i,1]} for i in range(20)],'output_specification':'Return a class label.',
                    'labels':[{'sample_index':i,'expected_output':'positive' if i%2 else 'negative'} for i in range(20)],
                    'budget':{'max_cost_cny':20,'max_candidate_executions':60}}
        saved=TaskRepository(store).save(definition,compile_task(definition)['content_sha256'])
        provider=ProviderFixture();self.addCleanup(provider.close)
        fixture_plan={'candidates':[proposal(),plan()['candidates'][1]]}
        sandbox=SandboxFixture();registry=builtin_registry();registry.register(SklearnAdapter(sandbox))
        run=repository.create(saved['task_id'],1,uuid.uuid4().hex)
        with patch('test_search.plan',return_value=fixture_plan),patch('solve2ship.gateway.time.strftime',return_value='2026-11-09'):
            search_id,owner=repository.claim()
            execute_search(repository,search_id,owner,ModelGateway(provider.profile,repository.ledger,search_id,allow_fixture=True),registry)
            result=repository.get(search_id)
            self.assertEqual(result['state'],'succeeded')
            fit_payload=next(payload for _,payload in sandbox.calls if 'labels' in payload)
            splits=partition_indices(compile_task(definition)['contract'])
            self.assertEqual({row[0] for row in fit_payload['features']},set(splits['train']))
            self.assertFalse(set(splits['final_test'])&{row[0] for _,payload in sandbox.calls if 'features' in payload and 'labels' not in payload for row in [payload['features']]})
            self.assertEqual(repository.tree_execution_count(search_id),len(result['executions'])+1)
            target=next(c for c in result['candidates'] if c['family']=='sklearn.classifier.v1')
            repository.reactivate(search_id,target['id'],uuid.uuid4().hex)
            child,child_owner=repository.claim()
            execute_search(repository,child,child_owner,ModelGateway(provider.profile,repository.ledger,child,allow_fixture=True),registry)
            self.assertEqual(sum('labels' in payload for _,payload in sandbox.calls),1)
            self.assertEqual(repository.get(child)['state'],'succeeded')
