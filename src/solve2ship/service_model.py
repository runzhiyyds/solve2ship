"""Independent owner-only credentials, usage budget and paid-request fencing."""

import argparse
from decimal import Decimal,InvalidOperation
import getpass
import json
from pathlib import Path
import re
import sys
import time

from .contracts import canonical_json,sha256,validate_json
from .gateway import Ledger,ModelGateway,BudgetError,MICRO,ProviderError
from .model_config import load_profile,save_profile
from .store import Store
from .invocation import CallTraceGateway,invocation_runtime


class RequestConflict(ValueError):pass


class ServiceState:
    def __init__(self,path,candidate):
        self.path=Path(path);self.candidate=candidate
        self.store=Store(self.path/'usage.sqlite3');self.ledger=Ledger(self.store)
        with self.store.connection(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS service_binding(id INTEGER PRIMARY KEY CHECK(id=1),artifact_sha256 TEXT NOT NULL,grant_id TEXT,grant_limit INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS service_requests(id TEXT PRIMARY KEY,input_sha256 TEXT NOT NULL,state TEXT NOT NULL,result_json TEXT,error TEXT,created_at REAL NOT NULL,evidence_json TEXT)')
            binding=db.execute('SELECT artifact_sha256 FROM service_binding WHERE id=1').fetchone()
            if binding and binding[0]!=candidate['artifact_sha256']:raise ValueError('service_artifact_binding_mismatch')

    def authorize(self,amount_micro,grant=None):
        if type(amount_micro) is not int or amount_micro<0:raise BudgetError('invalid_service_budget')
        with self.store.connection(write=True) as db:
            binding=db.execute('SELECT * FROM service_binding WHERE id=1').fetchone()
            if not binding:
                if grant and (grant['amount_micro']<amount_micro):raise BudgetError('service_allowance_exceeded')
                db.execute('INSERT INTO service_binding VALUES(1,?,?,?)',(self.candidate['artifact_sha256'],grant['id'] if grant else None,grant['amount_micro'] if grant else None))
            elif grant and binding['grant_id']!=grant['id']:
                raise BudgetError('service_allowance_binding_conflict')
            elif binding['grant_limit'] is not None and amount_micro>binding['grant_limit']:
                raise BudgetError('service_allowance_exceeded')
            if self.ledger._committed(db,None)>amount_micro:raise BudgetError('budget_below_committed')
            for account in ('work','service'):
                db.execute('INSERT INTO budget_accounts VALUES(?,?) ON CONFLICT(id) DO UPDATE SET limit_micro=excluded.limit_micro',(account,amount_micro))

    def gateway(self,*,allow_fixture=False):
        profile=load_profile(self.path/'model.json')
        if self.ledger.status()['limit_cny']<=0:raise BudgetError('service_budget_required')
        return ModelGateway(profile,self.ledger,'service',allow_fixture=allow_fixture)

    def predict(self,request_id,value,adapter,gateway):
        if type(request_id) is not str or not re.fullmatch('[0-9a-f]{32}',request_id):raise ValueError('service_request_id_required')
        digest=sha256(value)
        with self.store.connection(write=True) as db:
            existing=db.execute('SELECT * FROM service_requests WHERE id=?',(request_id,)).fetchone()
            if existing:
                if existing['input_sha256']!=digest:raise RequestConflict('service_request_id_conflict')
                if existing['state']=='succeeded':return json.loads(existing['result_json'])
                raise RequestConflict('service_request_not_replayable')
            db.execute("INSERT INTO service_requests VALUES(?,?,'started',NULL,NULL,?,NULL)",(request_id,digest,time.time()))
        class RequestGateway:
            def json(self,*args,**kwargs):
                kwargs['purpose']='service_request:'+request_id+':'+kwargs['purpose']
                return gateway.json(*args,**kwargs)
        calls=CallTraceGateway(RequestGateway())
        try:
            output,call_id=adapter.execute(self.candidate['artifact'],value,calls);calls.remember(call_id)
            validate_json(output);encoded=canonical_json(output)
            if len(encoded.encode())>262144:raise ValueError('service_output_too_large')
        except Exception as error:
            code=error.code if isinstance(error,ProviderError) else 'service_request_failed'
            with self.store.connection(write=True) as db:db.execute("UPDATE service_requests SET state='failed',error=?,evidence_json=? WHERE id=?",(code,canonical_json(invocation_runtime(adapter,calls.call_ids)),request_id))
            raise
        with self.store.connection(write=True) as db:db.execute("UPDATE service_requests SET state='succeeded',result_json=?,evidence_json=? WHERE id=?",(encoded,canonical_json(invocation_runtime(adapter,calls.call_ids)),request_id))
        return output


def main():
    parser=argparse.ArgumentParser(description='Standalone service setup; English / 简体中文')
    parser.add_argument('command',choices=['configure-model','authorize-budget','status'])
    parser.add_argument('--state-dir',type=Path,default=Path('.sts'))
    parser.add_argument('--cny');parser.add_argument('--grant-file',type=Path);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];candidate=json.loads((root/'candidate.json').read_text());state=ServiceState(args.state_dir,candidate)
    if args.command=='configure-model':
        if not sys.stdin.isatty() or not sys.stderr.isatty():parser.error('Use a hidden interactive prompt / 请在交互式终端隐藏输入密钥')
        profile=save_profile(args.state_dir/'model.json',{'base_url':input('API base URL / API 地址: ').strip(),
            'model':input('Model / 模型: ').strip(),'api_key':getpass.getpass('API key (hidden) / 密钥（隐藏）: ')})
        print(json.dumps(profile.status()));return
    if args.command=='authorize-budget':
        value=Decimal(args.cny or '')
        if not value.is_finite() or not 0<=value<=1000000:raise ValueError('invalid_service_budget')
        grant=json.loads(args.grant_file.read_text()) if args.grant_file else None
        if grant and grant.get('artifact_sha256')!=candidate['artifact_sha256']:raise ValueError('service_grant_artifact_mismatch')
        state.authorize(int(value*MICRO),grant)
    print(json.dumps(state.ledger.status(),ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except (ValueError,InvalidOperation,BudgetError,OSError):
        raise SystemExit('Service configuration failed; check profile, allowance and persistent state / 服务配置失败，请检查连接、额度和持久化状态。') from None
