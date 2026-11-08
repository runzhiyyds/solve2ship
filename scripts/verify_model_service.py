"""Metered independent serving checks; no key/usage state in the source bundle."""

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from solve2ship.contracts import canonical_json,sha256
from solve2ship.delivery import DeliveryRepository,bundle_files
from solve2ship.gateway import MICRO
from solve2ship.model_config import load_profile,save_profile
from solve2ship.store import Store


def verify(store,delivery_id,grant_id,package=None):
    repository=DeliveryRepository(store);delivery=repository.get(delivery_id)
    draft=Path(delivery['draft_path']);package=Path(package) if package else draft
    source_manifest=json.loads((draft/'manifest.json').read_text())
    source_manifest_hash=hashlib.sha256((draft/'manifest.json').read_bytes()).hexdigest()
    if delivery['proof'] and source_manifest_hash!=delivery['proof']['manifest_sha256']:
        raise ValueError('frozen_serving_proof_mismatch')
    if not delivery['proof']:
        for name,digest in source_manifest['files_sha256'].items():
            if name=='runtime/__init__.py':
                if digest!=hashlib.sha256(b'').hexdigest():raise ValueError('unreviewed_runtime_source')
            elif name.startswith(('runtime/','runtimes/')):
                trusted=ROOT/'src/solve2ship'/Path(name).name if name.startswith('runtime/') else ROOT/name
                if not trusted.is_file() or hashlib.sha256(trusted.read_bytes()).hexdigest()!=digest:
                    raise ValueError('unreviewed_runtime_source')
    manifest=json.loads((package/'manifest.json').read_text());candidate=json.loads((package/'candidate.json').read_text())
    expected=bundle_files(candidate['family'])
    if package!=draft:expected=(expected-{'Dockerfile'})|{'runtime-image.json','LICENSE','NOTICE'}
    if set(manifest['files_sha256'])!=expected:raise ValueError('unexpected_bundle_files')
    if {str(p.relative_to(package)) for p in package.rglob('*') if p.is_file()}!=expected|{'manifest.json'}:
        raise ValueError('unexpected_bundle_files')
    for name,digest in manifest['files_sha256'].items():
        path=package/name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('bundle_changed')
        if name.startswith(('runtime/','runtimes/')) and digest!=source_manifest['files_sha256'][name]:raise ValueError('unreviewed_runtime_source')
    if candidate['artifact_sha256']!=delivery['frozen']['candidate']['artifact_sha256'] or sha256(candidate['artifact'])!=candidate['artifact_sha256']:
        raise ValueError('wrong_frozen_artifact')
    with store.connection() as db:grant=db.execute('SELECT * FROM service_budget_grants WHERE id=? AND delivery_id=?',(grant_id,delivery_id)).fetchone()
    if grant is None:raise ValueError('explicit_service_allowance_required')
    grant=dict(grant);grant['artifact_sha256']=candidate['artifact_sha256']
    state=Path.home()/'.local/share/solve2ship/services'/grant_id
    # One persistent state per grant, shared by private/ZIP verifications. Never
    # reinitialize a disappeared ledger and silently spend the same allowance twice.
    pointer=store.path.parent/'service-verification'/f'{grant_id}.json';pointer.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if pointer.exists() and not (state/'usage.sqlite3').is_file():raise ValueError('service_usage_state_missing_manual_review_required')
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    if state.is_symlink():raise ValueError('unsafe_service_state')
    if not (state/'model.json').exists():
        profile=load_profile(store.path.parent/'model.json')
        save_profile(state/'model.json',{'base_url':profile.base_url,'model':profile.model,'api_key':profile.api_key})
    grant_file=state/'grant.json';grant_file.write_text(canonical_json(grant));os.chmod(grant_file,0o600)
    source=repository.searches.get(delivery['search_id']);task=repository.searches.tasks.get_revision(source['task_id'],source['revision'])
    cases=[{'sample_index':row['sample_index'],'input':task['contract']['definition']['samples'][row['sample_index']],'output':row['output']}
           for row in source['executions'] if row['candidate_id']==delivery['candidate_id'] and row['error'] is None and row['split']=='validation']
    if not cases:raise ValueError('prior_validation_cases_required')
    environment={'PATH':os.defpath,'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}
    manifest_hash=hashlib.sha256((package/'manifest.json').read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='sts-model-independent-') as temporary:
        independent=Path(temporary)/'service';shutil.copytree(package,independent)
        def command(*args):
            result=subprocess.run([sys.executable,'-B','-E','-S','-m','runtime.service_model',*args,'--state-dir',str(state)],
                                  cwd=independent,env=environment,capture_output=True,text=True,check=True,timeout=15)
            return json.loads(result.stdout)
        command('authorize-budget','--cny',str(Decimal(grant['amount_micro'])/MICRO),'--grant-file',str(grant_file))
        pointer.write_text(canonical_json({'state_path':str(state),'grant_id':grant_id}));os.chmod(pointer,0o600)
        before=command('status')
        def start():
            process=subprocess.Popen([sys.executable,'-B','-E','-S','-m','runtime.service_runtime','--port','0','--state-dir',str(state)],
                cwd=independent,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            if not select.select([process.stdout],[],[],10)[0]:
                process.terminate();process.communicate(timeout=5);raise RuntimeError('service_startup_timeout')
            line=process.stdout.readline()
            if not line:
                process.communicate(timeout=5);raise RuntimeError('service_startup_failed')
            return process,'http://127.0.0.1:'+str(json.loads(line)['port'])
        def stop(process):
            process.terminate()
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.communicate(timeout=5)
        opener=build_opener(ProxyHandler({}))
        def request(path,value=None):
            with opener.open(Request(address+path,data=canonical_json(value).encode() if value is not None else None,
                    headers={'Content-Type':'application/json'}),timeout=150) as response:return json.load(response)
        process,address=start()
        try:
            health=request('/health');transport=request('/contract')
            if health['artifact_sha256']!=candidate['artifact_sha256'] or not transport['model_calls']:raise AssertionError('wrong_service_contract')
            requests=[]
            for case in cases:
                body={'request_id':sha256([grant_id,manifest_hash,case['sample_index']])[:32],'input':case['input']};requests.append(body)
                if request('/predict',body)!={'result':case['output']}:raise AssertionError('independent_model_serving_drift')
            status=command('status');request('/predict',requests[0]);replayed=command('status')
            if len(status['calls'])!=len(replayed['calls']):raise AssertionError('successful_request_rebilled')
            try:request('/predict',{'input':{}})
            except HTTPError as error:
                if error.code!=422:raise
            else:raise AssertionError('missing_request_id_accepted')
        finally:stop(process)
        process,address=start()
        try:
            restored=command('status')
            if restored['committed_ceiling_cny']!=status['committed_ceiling_cny']:raise AssertionError('restart_reset_budget')
            request('/predict',requests[0])
            body={**requests[0],'request_id':sha256([grant_id,manifest_hash,'new_post_restart_operation'])[:32]}
            if request('/predict',body)!={'result':cases[0]['output']}:raise AssertionError('restart_serving_drift')
            after=command('status')
        finally:stop(process)
    return {'status':'passed','independent_process':True,'copied_outside_repository':True,'sts_controller_required':False,
        'state_database_required':True,'experiment_database_required':False,'standalone_usage_database_required':True,
        'credentials_required':True,'credentials_bundled':False,'model_calls':len(after['calls'])-len(before['calls']),
        'grant_id':grant_id,'delegated_ceiling_cny':grant['amount_micro']/MICRO,'standalone_committed_ceiling_cny':after['committed_ceiling_cny'],
        'cached_replay_no_new_calls':True,'restart_budget_preserved':True,'automatic_failed_request_replay':False,
        'contract_cases':len(cases),'malformed_request_rejected':True,'manifest_sha256':manifest_hash,
        'artifact_sha256':candidate['artifact_sha256'],'docker_required':candidate['family']=='workflow.v1',
        'container_built':False,'cloud_deployed':False,'environment_keys':sorted(environment)}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('delivery_id');parser.add_argument('--allowance-cny',required=True);args=parser.parse_args()
    store=Store(ROOT/'.sts/state.sqlite3');repository=DeliveryRepository(store)
    grant=repository.searches.ledger.grant_service(args.delivery_id,int(Decimal(args.allowance_cny)*MICRO))
    delivery=repository.get(args.delivery_id)
    proof=delivery['proof'] or verify(store,args.delivery_id,grant['id'])
    if not delivery['proof']:repository.record_proof(args.delivery_id,proof)
    print(json.dumps(proof))
