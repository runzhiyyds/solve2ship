"""Workflow protocol fixtures only; actual Docker/search evidence is separate."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from solve2ship.candidates import ArtifactError,builtin_registry
from solve2ship.contracts import compile_task
from solve2ship.gateway import MICRO,ModelGateway
from solve2ship.evaluation import partition_indices
from solve2ship.search import SearchRepository,execute_search
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository
from solve2ship.workflow_adapter import WorkflowAdapter
from test_search import ProviderFixture,plan


class SandboxProtocolFixture:
    def __init__(self):self.calls=[];self.last=None;self.fail=False
    def clear_receipt(self):self.last=None
    def receipt(self):return self.last
    def execute(self,source,value):
        self.calls.append((source,value));self.last={'provider':'protocol_fixture_only','container_removed':True}
        if self.fail:raise ArtifactError('fixture_runtime_failed')
        return value


def artifact():
    return {'steps':[
        {'id':'first','family':'python.v1','artifact':{'source':'fixture_identity_not_executed'},'input':{'source':'input','path':[]}},
        {'id':'second','family':'python.v1','artifact':{'source':'fixture_identity_not_executed'},
         'input':{'object':{'copied':{'source':'first','path':['text']},'original':{'source':'input','path':['text']}}}}
    ],'output':{'source':'second','path':['copied']}}


class WorkflowTests(unittest.TestCase):
    def test_binding_composition_and_real_step_receipt_shape(self):
        sandbox=SandboxProtocolFixture();adapter=WorkflowAdapter(sandbox)
        self.assertEqual(adapter.execute(artifact(),{'text':'cancel'},None),('cancel',None))
        receipt=adapter.runtime_receipt();self.assertEqual(len(sandbox.calls),2)
        self.assertEqual(receipt['steps'][1]['output'],{'copied':'cancel','original':'cancel'})
        self.assertEqual(receipt['model_calls'],0)
        self.assertTrue(all(row['runtime']['provider']=='protocol_fixture_only' for row in receipt['steps']))
        self.assertNotIn('workflow.v1',builtin_registry().adapters)
        with self.assertRaisesRegex(ArtifactError,'sandbox'):WorkflowAdapter(None)

    def test_forward_cycle_unknown_family_and_unbounded_steps_refused_before_execution(self):
        mutations=[]
        value=artifact();value['steps'][0]['input']['source']='second';mutations.append(value)
        value=artifact();value['steps'][1]['id']='first';mutations.append(value)
        value=artifact();value['steps'][0]['family']='unregistered.v1';mutations.append(value)
        value=artifact();value['steps']*=3;mutations.append(value)
        value=artifact();value['steps'][0]['input']['path']=[True];mutations.append(value)
        value=artifact();value['steps'][1]['family']='workflow.v1';mutations.append(value)
        sandbox=SandboxProtocolFixture();adapter=WorkflowAdapter(sandbox)
        for value in mutations:
            with self.subTest(value=value),self.assertRaises(ArtifactError):adapter.execute(value,{},None)
        self.assertEqual(sandbox.calls,[])

    def test_runtime_failure_stops_pipeline_and_keeps_receipt(self):
        sandbox=SandboxProtocolFixture();sandbox.fail=True;adapter=WorkflowAdapter(sandbox)
        with self.assertRaises(ArtifactError):adapter.execute(artifact(),{'text':'cancel'},None)
        self.assertEqual(len(sandbox.calls),1)
        self.assertEqual(adapter.runtime_receipt()['steps'][0]['status'],'failed')
        self.assertIsNotNone(adapter.runtime_receipt()['steps'][0]['runtime'])

    def test_missing_reference_does_not_reuse_prior_container_receipt(self):
        sandbox=SandboxProtocolFixture();adapter=WorkflowAdapter(sandbox)
        with self.assertRaisesRegex(ArtifactError,'path_missing'):adapter.execute(artifact(),{},None)
        receipt=adapter.runtime_receipt()['steps'];self.assertEqual(len(receipt),2)
        self.assertIsNone(receipt[1]['runtime'])
        self.assertEqual(len(sandbox.calls),1)

    def test_cancellation_between_steps_and_bounded_output_evidence(self):
        sandbox=SandboxProtocolFixture();adapter=WorkflowAdapter(sandbox,lambda:len(sandbox.calls)<1)
        with self.assertRaisesRegex(ArtifactError,'cancelled'):adapter.execute(artifact(),{'text':'x'*9000},None)
        self.assertEqual(len(sandbox.calls),1)
        step=adapter.runtime_receipt()['steps'][0]
        self.assertTrue(step['output_omitted']);self.assertNotIn('output',step)

    def test_binding_expansion_is_bounded_before_hashing_or_tool_execution(self):
        sandbox=SandboxProtocolFixture();adapter=WorkflowAdapter(sandbox);value=artifact()
        value['steps'][0]['input']={'object':{str(i):{'source':'input','path':['text']} for i in range(16)}}
        with self.assertRaisesRegex(ArtifactError,'too_large'):adapter.execute(value,{'text':'x'*8192},None)
        self.assertEqual(sandbox.calls,[])
        self.assertEqual(adapter.runtime_receipt()['status'],'failed')

    def test_search_persists_workflow_step_evidence_without_final_exposure(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        store=Store(Path(directory.name)/'state.db');repository=SearchRepository(store);repository.ledger.configure('work',100*MICRO)
        definition={'samples':[{'text':'cancel','case':i} for i in range(20)],'output_specification':'Return cancel.',
            'labels':[{'sample_index':i,'expected_output':'cancel'} for i in range(20)],
            'budget':{'max_cost_cny':10,'max_candidate_executions':60}}
        saved=TaskRepository(store).save(definition,compile_task(definition)['content_sha256'])
        fixture_plan={'candidates':[{'family':'workflow.v1','name':'Protocol fixture','rationale':'Explicit test only','artifact':artifact()},
                                    copy.deepcopy(plan()['candidates'][0])]}
        provider=ProviderFixture();self.addCleanup(provider.close)
        sandbox=SandboxProtocolFixture();registry=builtin_registry(sandbox=sandbox)
        run=repository.create(saved['task_id'],1,uuid.uuid4().hex);search_id,owner=repository.claim()
        with patch('test_search.plan',return_value=fixture_plan),patch('solve2ship.gateway.time.strftime',return_value='2026-11-09'):
            execute_search(repository,search_id,owner,ModelGateway(provider.profile,repository.ledger,search_id,allow_fixture=True),registry)
        result=repository.get(run['id']);self.assertEqual(result['state'],'succeeded')
        workflow=next(c for c in result['candidates'] if c['family']=='workflow.v1')
        rows=[r for r in result['executions'] if r['candidate_id']==workflow['id']]
        self.assertTrue(all(len(row['runtime']['steps'])==2 for row in rows))
        self.assertEqual(repository.tree_execution_count(search_id),len(result['executions']))
        self.assertFalse(set(r['sample_index'] for r in rows)&set(partition_indices(compile_task(definition)['contract'])['final_test']))
