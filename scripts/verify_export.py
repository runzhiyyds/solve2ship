"""Verify an actual exported source ZIP and Docker HTTP service, locally only."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import URLError
from urllib.request import Request,build_opener,ProxyHandler
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from solve2ship.delivery import DeliveryRepository
from solve2ship.service_export import export_service
from solve2ship.store import Store
from verify_service import verify as verify_native


def verify(store,delivery_id):
    export=export_service(store,delivery_id)
    local_http=build_opener(ProxyHandler({}))
    if not export['archive_integrity_verified']:
        raise AssertionError('export_archive_changed')
    docker=shutil.which('docker') or '/Applications/Docker.app/Contents/Resources/bin/docker'
    environment=os.environ.copy();environment['PATH']=str(Path(docker).parent)+':'+environment.get('PATH','')
    # Never place credentials/task data in CLI arguments or build context.
    def command(*arguments):
        return subprocess.run([docker,*arguments],env=environment,check=True,capture_output=True,text=True,timeout=120).stdout.strip()
    name='sts-service-check-'+uuid.uuid4().hex
    image='sts-local-service:'+uuid.uuid4().hex
    network=name+'-network'
    with tempfile.TemporaryDirectory(prefix='sts-export-check-') as temporary:
        with zipfile.ZipFile(export['archive_path']) as archive:archive.extractall(temporary)
        package=Path(temporary)/'service'
        manifest=json.loads((package/'manifest.json').read_text())
        for path,digest in manifest['files_sha256'].items():
            if hashlib.sha256((package/path).read_bytes()).hexdigest()!=digest:raise AssertionError('bundle_file_changed')
        if not (package/'LICENSE').is_file() or 'Chenyuan Guo' not in (package/'NOTICE').read_text():
            raise AssertionError('missing_attribution')
        repository=DeliveryRepository(store);delivery=repository.get(delivery_id)
        source=repository.searches.get(delivery['search_id']);task=repository.searches.tasks.get_revision(source['task_id'],source['revision'])
        cases=[row for row in source['executions'] if row['candidate_id']==delivery['candidate_id'] and row['error'] is None and row['split']=='validation']
        if delivery['frozen']['candidate']['family']!='rules.v1':
            original=json.loads((Path(delivery['draft_path'])/'manifest.json').read_text())
            proof=verify_native(package,[{'input':task['contract']['definition']['samples'][row['sample_index']],'expected_output':row['output']} for row in cases],
                source_bundle=True,trusted_sources=original['files_sha256'])
            return {**proof,'export_id':export['id'],'archive_sha256':export['archive_sha256'],'delivery_id':delivery_id,'publicly_published':False}
        command('build','--network=none','--pull=false','-t',image,str(package))
        # This is trusted Rules serving with loopback ingress, not the generated-code
        # sandbox (which uses NetworkMode=none and never publishes a port).
        command('network','create','--label','sts.verification=true',network)
        try:
            command('run','-d','--name',name,'--network',network,'--read-only','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16777216',
                '--cap-drop=ALL','--security-opt=no-new-privileges:true','--memory=256m','--memory-swap=256m','--cpus=.5','--pids-limit=32',
                '-p','127.0.0.1::9000',image)
            inspection=json.loads(command('inspect',name))[0]
            host_port=inspection['NetworkSettings']['Ports']['9000/tcp'][0]['HostPort']
            address='http://127.0.0.1:'+host_port
            deadline=time.monotonic()+10
            while True:
                try:
                    with local_http.open(Request(address+'/health',headers={'Host':'localhost:9000'}),timeout=2) as response:health=json.load(response)
                    break
                except (URLError,OSError,http.client.HTTPException):
                    if time.monotonic()>deadline:raise
                    time.sleep(.05)
            for case in cases:
                body=json.dumps({'input':task['contract']['definition']['samples'][case['sample_index']]}).encode()
                with local_http.open(Request(address+'/predict',data=body,headers={'Host':'localhost:9000','Content-Type':'application/json'}),timeout=3) as response:
                    if json.load(response)!={'result':case['output']}:raise AssertionError('container_serving_drift')
            if not cases or health['artifact_sha256']!=delivery['frozen']['candidate']['artifact_sha256']:
                raise AssertionError('wrong_frozen_service')
            proof={'status':'passed','export_id':export['id'],'archive_sha256':export['archive_sha256'],
                'built_image_id':inspection['Image'],'candidate_id':delivery['candidate_id'],'contract_cases':len(cases),
                'python_version_in_container':command('exec',name,'python3','--version'),'non_root_user':inspection['Config']['User'],
                'read_only_root':inspection['HostConfig']['ReadonlyRootfs'],'network_internal':False,'port_bind_host':'127.0.0.1',
                'runtime_network_calls':False,'serving_network_policy':'bridge_with_loopback_ingress_not_code_sandbox',
                'model_calls':0,'credentials_required':False,'cloud_deployed':False,'publicly_published':False,
                'image_archive_distributed':False}
        finally:
            subprocess.run([docker,'rm','-f',name],env=environment,capture_output=True,timeout=15)
            subprocess.run([docker,'network','rm',network],env=environment,capture_output=True,timeout=15)
    return proof


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('delivery_id');parser.add_argument('--db',type=Path,default=ROOT/'.sts/state.sqlite3')
    parser.add_argument('--evidence-name',help='Separate evidence filename for a new milestone; preserve historical proofs')
    args=parser.parse_args()
    if args.evidence_name and (Path(args.evidence_name).name!=args.evidence_name or not args.evidence_name.endswith('.json')):
        parser.error('Evidence must be a JSON filename without a directory')
    result=verify(Store(args.db),args.delivery_id)
    family=DeliveryRepository(Store(args.db)).get(args.delivery_id)['frozen']['candidate']['family']
    evidence=('M7-workflow-service-proof.json' if family=='workflow.v1' else
              'M7-ML-service-proof.json' if family=='sklearn.classifier.v1' else
              'M7-service-proof.json' if result.get('docker_required') else 'M6-container-proof.json')
    reports=ROOT/'.sts/verification-reports';reports.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=reports/(args.evidence_name or evidence)
    if target.exists() and json.loads(target.read_text()).get('delivery_id') not in (None,args.delivery_id):
        raise ValueError('historical_evidence_must_not_be_overwritten_choose_a_new_name')
    target.write_text(json.dumps(result,indent=2)+'\n')
    os.chmod(target,0o600)
    print(json.dumps(result))
