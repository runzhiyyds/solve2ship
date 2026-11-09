"""Standalone serving/accounting protocol fixtures, no real key or paid calls."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid
from urllib.error import HTTPError
from urllib.request import Request

from local_http import urlopen
from test_search import ProviderFixture
from solve2ship.candidates import PromptAdapter
from solve2ship.contracts import compile_task,canonical_json,sha256
from solve2ship.delivery import DeliveryRepository
from solve2ship.evaluation import partition_indices
from solve2ship.gateway import Ledger,ModelGateway,BudgetError,ProviderError,MICRO
from solve2ship.model_config import save_profile
from solve2ship.search import SearchRepository
from solve2ship.service_model import ServiceState,RequestConflict
from solve2ship.service_runtime import create_service
from solve2ship.store import Store
from solve2ship.tasks import TaskRepository


class ServiceModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.provider=ProviderFixture();self.addCleanup(self.provider.close)
        date=patch('solve2ship.gateway.time.strftime',return_value='2026-11-09');date.start();self.addCleanup(date.stop)
        self.candidate={'id':'fixture-only','family':'prompt.v1','artifact':{'system_prompt':'Return cancel.'}}
        self.candidate['artifact_sha256']=sha256(self.candidate['artifact'])
        self.state=ServiceState(self.root/'service',self.candidate)
        p=self.provider.profile
        save_profile(self.state.path/'model.json',{'base_url':p.base_url,'model':p.model,'api_key':p.api_key})

    def gateway(self):return self.state.gateway(allow_fixture=True)

    def test_default_zero_authorization_and_grant_cap_cannot_be_reset(self):
        with self.assertRaisesRegex(BudgetError,'budget_required'):self.gateway()
        self.state.authorize(3*MICRO,{'id':'fixture-grant','amount_micro':3*MICRO})
        with self.assertRaisesRegex(BudgetError,'allowance_exceeded'):self.state.authorize(4*MICRO)
        changed={**self.candidate,'artifact_sha256':'different'}
        with self.assertRaisesRegex(ValueError,'binding_mismatch'):ServiceState(self.state.path,changed)
        self.assertEqual(self.provider.calls,[])

    def test_success_is_cached_across_restart_without_new_charge(self):
        self.state.authorize(3*MICRO);rid=uuid.uuid4().hex
        result=self.state.predict(rid,{'text':'cancel'},PromptAdapter(),self.gateway())
        before=self.state.ledger.status();self.assertEqual(result,'cancel')
        restored=ServiceState(self.state.path,self.candidate)
        self.assertEqual(restored.predict(rid,{'text':'cancel'},PromptAdapter(),self.gateway()),result)
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(restored.ledger.status()['committed_ceiling_cny'],before['committed_ceiling_cny'])
        self.assertIn(rid,before['calls'][0]['purpose'])
        with self.assertRaises(RequestConflict):restored.predict(rid,{'text':'different'},PromptAdapter(),self.gateway())

    def test_unknown_charge_and_failed_request_are_not_replayed_after_restart(self):
        self.state.authorize(3*MICRO);self.provider.mode='missing_usage';rid=uuid.uuid4().hex
        with self.assertRaises(ProviderError):self.state.predict(rid,{},PromptAdapter(),self.gateway())
        restored=ServiceState(self.state.path,self.candidate)
        with self.assertRaises(RequestConflict):restored.predict(rid,{},PromptAdapter(),self.gateway())
        self.assertEqual(len(self.provider.calls),1);self.assertGreater(restored.ledger.status()['committed_ceiling_cny'],2)
        with self.assertRaises(BudgetError):restored.authorize(0)

    def test_concurrent_duplicate_requests_make_one_provider_submission(self):
        self.state.authorize(10*MICRO);rid=uuid.uuid4().hex;gateway=self.gateway()
        def submit(_):
            try:return self.state.predict(rid,{},PromptAdapter(),gateway)
            except RequestConflict:return 'fenced'
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(submit,range(8)))
        self.assertIn('other',results);self.assertTrue(set(results)<={'other','fenced'})
        self.assertEqual(len(self.provider.calls),1)

    def test_http_requires_request_identity_blocks_origins_and_returns_budget_error(self):
        self.state.authorize(3*MICRO)
        server=create_service(self.candidate,port=0,model_state=self.state,model_gateway=self.gateway())
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        address=f'http://127.0.0.1:{server.server_port}/predict'
        def post(body,headers=None):
            request=Request(address,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**(headers or {})})
            with urlopen(request) as response:return json.load(response)
        with self.assertRaises(HTTPError) as error:post({'input':{}})
        self.assertEqual(error.exception.code,422);self.assertEqual(self.provider.calls,[])
        body={'input':{'text':'cancel'},'request_id':uuid.uuid4().hex}
        self.assertEqual(post(body),{'result':'cancel'});self.assertEqual(post(body),{'result':'cancel'})
        self.assertEqual(len(self.provider.calls),1)
        self.state.authorize(260)
        with self.assertRaises(HTTPError) as error:post({**body,'request_id':uuid.uuid4().hex})
        self.assertEqual(error.exception.code,402)
        with self.assertRaises(HTTPError) as error:post(body,{'Origin':'https://foreign.invalid'})
        self.assertEqual(error.exception.code,403);self.assertEqual(len(self.provider.calls),1)

    def final_source(self):
        store=Store(self.root/'control.sqlite3');repo=SearchRepository(store);repo.ledger.configure('work',100*MICRO)
        definition={'samples':[{'text':'cancel','case':i} for i in range(20)],'output_specification':'Return cancel.',
                    'labels':[{'sample_index':i,'expected_output':'cancel'} for i in range(20)],
                    'budget':{'max_cost_cny':10,'max_candidate_executions':60}}
        saved=TaskRepository(store).save(definition,compile_task(definition)['content_sha256']);run=repo.create(saved['task_id'],1,uuid.uuid4().hex)
        sid,owner=repo.claim();candidate={**{k:self.candidate[k] for k in ('family','artifact')},'name':'Protocol fixture','rationale':'Not optimization'}
        cid=repo.add_candidate(sid,candidate,{'source':'protocol_fixture'})
        for i in partition_indices(compile_task(definition)['contract'])['validation']:
            repo.reserve_execution(sid,cid,i);repo.add_execution(sid,cid,i,'cancel',None,1,None,1,split='validation')
        with store.connection(write=True) as db:db.execute('INSERT INTO selections VALUES(?,?,?)',(sid,cid,canonical_json({'status':'protocol_fixture'})))
        repo.finish(sid,owner);p=self.provider.profile
        save_profile(store.path.parent/'model.json',{'base_url':p.base_url,'model':p.model,'api_key':p.api_key})
        with patch('solve2ship.delivery.ModelGateway',side_effect=lambda p,l,s:ModelGateway(p,l,s,allow_fixture=True)):
            delivery=DeliveryRepository(store).prepare(run['id'],uuid.uuid4().hex)
        return store,repo,delivery

    def test_final_model_calls_share_root_and_never_replay_after_profile_removal(self):
        store,repo,delivery=self.final_source();self.assertTrue(delivery['report']['gate_passed'])
        self.assertEqual(delivery['report']['model_calls'],4)
        self.assertEqual(repo.ledger.status(delivery['search_id'])['root_committed_ceiling_cny'],1040/MICRO)
        (store.path.parent/'model.json').unlink()
        with patch('solve2ship.delivery.ModelGateway',side_effect=AssertionError('Never replay')):
            self.assertEqual(DeliveryRepository(store).prepare(delivery['search_id'],uuid.uuid4().hex)['report'],delivery['report'])
        with self.assertRaisesRegex(BudgetError,'delivery_not_active'):
            repo.ledger.reserve('delivery:'+delivery['id'],'forbidden','deepseek-flash',32)

    def test_delegated_allowance_is_atomic_idempotent_and_counts_in_global_root_budget(self):
        store,repo,delivery=self.final_source()
        with ThreadPoolExecutor(max_workers=4) as pool:
            grants=list(pool.map(lambda _:repo.ledger.grant_service(delivery['id'],3*MICRO),range(4)))
        self.assertEqual(len({g['id'] for g in grants}),1)
        status=repo.ledger.status();self.assertEqual(status['delegated_ceiling_cny'],3)
        self.assertEqual(status['committed_ceiling_cny'],3.00104)
        self.assertEqual(repo.ledger.status(delivery['search_id'])['root_committed_ceiling_cny'],3.00104)
        with self.assertRaises(BudgetError):repo.ledger.configure('work',3*MICRO)
        with self.assertRaises(BudgetError):repo.ledger.grant_service(delivery['id'],4*MICRO)
        with store.connection(write=True) as db:
            with self.assertRaises(Exception):db.execute('DELETE FROM service_budget_grants')
