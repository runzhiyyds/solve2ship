"""Independent trusted-template service verifier. Never runs generated scripts."""

import argparse
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
from urllib.request import Request, build_opener, ProxyHandler


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from solve2ship.contracts import canonical_json
from solve2ship.delivery import bundle_files, DeliveryRepository
from solve2ship.store import Store


def verify(draft, cases, *, source_bundle=False, trusted_sources=None):
    draft = Path(draft)
    manifest = json.loads((draft / 'manifest.json').read_text())
    candidate = json.loads((draft / 'candidate.json').read_text())
    allowed = bundle_files(candidate['family'])
    if source_bundle:
        allowed = allowed | {'LICENSE','NOTICE'}
        if candidate['family']!='rules.v1':allowed=(allowed-{'Dockerfile'})|{'runtime-image.json'}
    if set(manifest['files_sha256']) != allowed:
        raise ValueError('unexpected_bundle_files')
    actual = {str(path.relative_to(draft)) for path in draft.rglob('*') if path.is_file()}
    if actual != allowed | {'manifest.json'} or any(path.is_symlink() for path in draft.rglob('*')):
        raise ValueError('unexpected_bundle_files')
    for name, digest in manifest['files_sha256'].items():
        if hashlib.sha256((draft / name).read_bytes()).hexdigest() != digest:
            raise ValueError('bundle_hash_mismatch')
    for name in ['contracts.py', 'candidates.py', 'service_runtime.py']:
        actual=(draft/'runtime'/name).read_bytes()
        accepted=(hashlib.sha256(actual).hexdigest()==trusted_sources.get('runtime/'+name)) if trusted_sources else actual==(ROOT/'src/solve2ship'/name).read_bytes()
        if not accepted:
            raise ValueError('unreviewed_executable_template')
    if (draft / 'runtime/__init__.py').read_bytes() != b'':
        raise ValueError('unreviewed_executable_template')
    if candidate['family'] not in ('rules.v1','python.v1','sklearn.classifier.v1','workflow.v1','prompt.v1','hybrid.v1'):
        raise ValueError('unsupported_serving_family')
    from solve2ship.candidates import model_required
    if model_required(candidate['family'],candidate['artifact']):raise ValueError('use_metered_model_service_verifier')
    if candidate['family'] in ('python.v1','workflow.v1','sklearn.classifier.v1'):
        sources=[('runtime/docker_runtime.py','src/solve2ship/docker_runtime.py'),('runtimes/python/runner.py','runtimes/python/runner.py')]
        if candidate['family']=='workflow.v1':sources += [('runtime/'+name,'src/solve2ship/'+name) for name in ('workflow_adapter.py','invocation.py','gateway.py')]
        if candidate['family']=='workflow.v1':sources += [('runtime/'+name,'src/solve2ship/'+name) for name in ('service_model.py','model_config.py','store.py')]
        if candidate['family']=='sklearn.classifier.v1':
            sources+=[('runtime/sklearn_adapter.py','src/solve2ship/sklearn_adapter.py')]+[(f'runtimes/sklearn/{name}',f'runtimes/sklearn/{name}') for name in ('train.py','predict.py','requirements.lock','Dockerfile')]
        for local,source in sources:
            actual=(draft/local).read_bytes()
            accepted=hashlib.sha256(actual).hexdigest()==trusted_sources.get(local) if trusted_sources else actual==(ROOT/source).read_bytes()
            if not accepted:raise ValueError('unreviewed_executable_template')
    # No DB, STS package, source tree, profile or inherited environment in the copy.
    with tempfile.TemporaryDirectory(prefix='sts-independent-') as temporary:
        independent = Path(temporary) / 'service'
        shutil.copytree(draft, independent)
        environment = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}
        process = subprocess.Popen([sys.executable, '-B', '-E', '-S', '-m', 'runtime.service_runtime', '--port', '0'],
                                   cwd=independent, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if not select.select([process.stdout], [], [], 10)[0]:
                raise RuntimeError('independent_startup_timeout')
            startup = json.loads(process.stdout.readline())
            address = f"http://127.0.0.1:{startup['port']}"
            local_http=build_opener(ProxyHandler({}))

            def request(path, value=None):
                req = Request(address+path, data=canonical_json(value).encode() if value is not None else None,
                              headers={'Content-Type': 'application/json'})
                with local_http.open(req, timeout=5) as response:
                    return json.load(response)

            health = request('/health')
            if health['artifact_sha256'] != candidate['artifact_sha256']:
                raise AssertionError('wrong_frozen_artifact')
            transport = request('/contract')
            if transport['model_calls'] is not False:
                raise AssertionError('unexpected_model_dependency')
            for case in cases:
                if request('/predict', {'input': case['input']}) != {'result': case['expected_output']}:
                    raise AssertionError('serving_output_drift')
            try:
                request('/predict', {'unexpected': 'value'})
            except HTTPError as error:
                if error.code != 422:
                    raise
            else:
                raise AssertionError('malformed_request_accepted')
            return {'status': 'passed', 'independent_process': True, 'copied_outside_repository': True,
                    'environment_keys': sorted(environment), 'sts_controller_required': False,
                    'state_database_required': False, 'credentials_required': False,
                    'docker_required':candidate['family']!='rules.v1',
                    'model_calls': 0, 'contract_cases': len(cases), 'malformed_request_rejected': True,
                    'artifact_sha256': candidate['artifact_sha256'],
                    'manifest_sha256': hashlib.sha256((draft / 'manifest.json').read_bytes()).hexdigest(),
                    'python_version': sys.version.split()[0], 'container_built': False, 'cloud_deployed': False}
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('delivery_id')
    parser.add_argument('--db', type=Path, default=ROOT / '.sts/state.sqlite3')
    args = parser.parse_args()
    repository = DeliveryRepository(Store(args.db))
    delivery = repository.get(args.delivery_id)
    if delivery['proof']:
        print(canonical_json(delivery['proof']))
        return
    source = repository.searches.get(delivery['search_id'])
    task = repository.searches.tasks.get_revision(source['task_id'], source['revision'])
    # Serving equivalence checks use prior validation outputs, never final feedback.
    cases = [{'input': task['contract']['definition']['samples'][row['sample_index']], 'expected_output': row['output']}
             for row in source['executions'] if row['candidate_id'] == delivery['candidate_id'] and row['error'] is None and row['split'] == 'validation']
    if not cases or not delivery['draft_path']:
        parser.exit(1, 'A frozen private draft and prior validation outputs are required.\n')
    proof = verify(delivery['draft_path'], cases)
    repository.record_proof(args.delivery_id, proof)
    print(canonical_json(proof))


if __name__ == '__main__':
    main()
