"""Explicit local provider/runtime protocol fixtures, never optimization results."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from solve2ship.candidates import ArtifactError,builtin_registry
from solve2ship.contracts import canonical_json,compile_task,sha256
from solve2ship.delivery import DeliveryError,DeliveryRepository
from solve2ship.evaluation import evaluate_candidates
from solve2ship.gateway import BudgetError,ModelGateway,ProviderError,MICRO,CONTEXT_CEILING,INPUT_RATE,OUTPUT_RATE
from solve2ship.invocation import CallTraceGateway,invocation_call_ids
from solve2ship.search import SearchRepository
from solve2ship.service_runtime import create_service
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository
from solve2ship.workflow_adapter import WorkflowAdapter
from test_search import ProviderFixture
from test_workflow_adapter import SandboxProtocolFixture


def workflow(count=2):
    return {'steps':[{'id':'step'+str(i),'family':'prompt.v1','artifact':{'system_prompt':'Protocol fixture: return cancel.'},
                     'input':{'source':'input' if i==0 else 'step'+str(i-1),'path':[]}} for i in range(count)],
            'output':{'source':'step'+str(count-1),'path':[]}}


class ModelWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.store=Store(Path(directory.name)/'state.db');self.repository=SearchRepository(self.store)
        self.repository.ledger.configure('work',100*MICRO)
        self.provider=ProviderFixture();self.addCleanup(self.provider.close)
        date=patch('solve2ship.gateway.time.strftime',return_value='2026-11-08');date.start();self.addCleanup(date.stop)
        definition={'samples':[{'text':'cancel','case':i} for i in range(20)],'output_specification':'Return cancel.',
                    'labels':[{'sample_index':i,'expected_output':'cancel'} for i in range(20)],
                    'budget':{'max_cost_cny':10,'max_candidate_executions':60}}
        self.contract=compile_task(definition)['contract']
        saved=TaskRepository(self.store).save(definition,compile_task(definition)['content_sha256'])
        self.run=self.repository.create(saved['task_id'],1,uuid.uuid4().hex)
        self.search_id,self.owner=self.repository.claim()
        self.gateway=ModelGateway(self.provider.profile,self.repository.ledger,self.search_id,allow_fixture=True)
        self.sandbox=SandboxProtocolFixture();self.adapter=WorkflowAdapter(self.sandbox)

    def test_all_successful_calls_have_step_and_invocation_attribution(self):
        output,last=self.adapter.execute(workflow(),{'text':'cancel'},self.gateway)
        receipt=self.adapter.runtime_receipt();calls=self.repository.ledger.status(self.search_id)['calls']
        self.assertEqual(output,'cancel');self.assertEqual(last,calls[-1]['id'])
        self.assertEqual(receipt['model_call_ids'],[row['id'] for row in calls])
        self.assertEqual([step['model_call_ids'] for step in receipt['steps']],[[calls[0]['id']],[calls[1]['id']]])
        self.assertEqual(receipt['model_calls'],2);self.assertEqual(len(self.sandbox.calls),0)
        self.assertEqual(sum(row['charged_micro'] for row in calls),520)

    def test_second_call_unknown_charge_is_retained_and_downstream_stops(self):
        original=self.gateway.json;count=0
        def response(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:self.provider.mode='missing_usage'
            return original(*args,**kwargs)
        with patch.object(self.gateway,'json',side_effect=response),self.assertRaises(ProviderError):
            self.adapter.execute(workflow(3),{},self.gateway)
        calls=self.repository.ledger.status(self.search_id)['calls'];receipt=self.adapter.runtime_receipt()
        self.assertEqual(len(self.provider.calls),2);self.assertEqual(len(receipt['steps']),2)
        self.assertEqual(receipt['model_call_ids'],[row['id'] for row in calls])
        self.assertEqual(calls[1]['state'],'reserved');self.assertIsNone(calls[1]['charged_micro'])
        self.assertGreater(self.repository.ledger.status()['committed_ceiling_cny'],2)
        self.assertEqual(receipt['steps'][1]['error'],'provider_usage_unverified')

    def test_budget_refusal_before_second_call_keeps_first_charge(self):
        self.repository.ledger.configure('work',CONTEXT_CEILING*INPUT_RATE+1024*OUTPUT_RATE)
        with self.assertRaises(BudgetError):self.adapter.execute(workflow(3),{},self.gateway)
        self.assertEqual(len(self.provider.calls),1)
        receipt=self.adapter.runtime_receipt();self.assertEqual(receipt['model_calls'],1)
        self.assertEqual(receipt['steps'][1]['error'],'budget_exhausted')
        self.assertEqual(receipt['steps'][1]['model_call_ids'],[])

    def test_cancellation_between_model_steps_never_submits_second_call(self):
        adapter=WorkflowAdapter(self.sandbox,lambda:len(self.provider.calls)<1)
        with self.assertRaisesRegex(ArtifactError,'cancelled'):adapter.execute(workflow(),{},self.gateway)
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(adapter.runtime_receipt()['status'],'cancelled')
        self.assertEqual(adapter.runtime_receipt()['model_calls'],1)

    def test_invalid_adapter_output_still_preserves_successful_provider_charge(self):
        original=self.gateway.json
        def invalid(*args,**kwargs):
            _,call_id=original(*args,**kwargs)
            return {'fixture_invalid_shape':True},call_id
        with patch.object(self.gateway,'json',side_effect=invalid),self.assertRaises(ArtifactError):
            self.adapter.execute(workflow(),{},self.gateway)
        calls=self.repository.ledger.status(self.search_id)['calls']
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['state'],'settled')
        self.assertEqual(self.adapter.runtime_receipt()['model_call_ids'],[calls[0]['id']])

    def test_equal_quality_ranking_sums_all_calls_instead_of_only_last_call(self):
        registry=builtin_registry(sandbox=self.sandbox)
        candidates=[{'family':'workflow.v1','name':'Explicit multi-call fixture','rationale':'Protocol test only','artifact':workflow()},
                    {'family':'prompt.v1','name':'Explicit one-call fixture','rationale':'Protocol test only','artifact':{'system_prompt':'Return cancel.'}}]
        built=[]
        for i,candidate in enumerate(candidates):
            cid=str(i)*32;self.repository.add_candidate(self.search_id,candidate,{'source':'protocol_fixture'},candidate_id=cid);built.append((cid,candidate))
        # Equal observed latency; if only the last call were counted, ID 000... would win.
        with patch('solve2ship.evaluation.time.perf_counter',return_value=1):
            evaluate_candidates(self.repository,self.search_id,self.owner,built,self.contract,self.gateway,registry,threading.Event())
        result=self.repository.get(self.search_id)
        self.assertEqual(result['selection']['candidate_id'],'1'*32)
        self.assertTrue(all(len(invocation_call_ids(row))==(2 if row['candidate_id']=='0'*32 else 1) for row in result['executions']))
        self.assertEqual(len(result['budget']['calls']),18)
        self.assertEqual(result['budget']['committed_ceiling_cny'],18*260/MICRO)

    def test_unavailable_standalone_model_runtime_refuses_before_final_exposure(self):
        candidate={'family':'workflow.v1','name':'Protocol fixture','rationale':'Not real optimization','artifact':workflow()}
        cid=self.repository.add_candidate(self.search_id,candidate,{'source':'protocol_fixture'})
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO selections VALUES(?,?,?)',(self.search_id,cid,canonical_json({'status':'protocol_fixture'})))
        self.repository.finish(self.search_id,self.owner)
        with self.assertRaisesRegex(DeliveryError,'model_profile_required'):
            DeliveryRepository(self.store).prepare(self.search_id,uuid.uuid4().hex)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM final_quarantine').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM deliveries').fetchone()[0],0)
        with self.assertRaisesRegex(ValueError,'service_model_configuration_required'):
            create_service({'id':cid,**candidate,'artifact_sha256':sha256(candidate['artifact'])},port=0)
        with self.assertRaisesRegex(ArtifactError,'model_gateway_required'):self.adapter.execute(workflow(),{},None)
        self.assertEqual(self.provider.calls,[])

    def test_legacy_id_is_retained_and_repeated_ids_are_not_double_counted(self):
        self.assertEqual(invocation_call_ids({'call_id':'legacy'}),{'legacy'})
        self.assertEqual(invocation_call_ids({'call_id':'last','runtime':{'model_call_ids':['first','last','first']}}),{'first','last'})
        calls=CallTraceGateway(self.gateway);calls.json('Fixture','cancel',purpose='fixture',max_tokens=32)
        calls.remember(calls.call_ids[0]);self.assertEqual(len(calls.call_ids),1)
