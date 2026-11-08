"""One-time holdout freeze and private Rules-service draft, never release/export."""

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from .candidates import RulesAdapter, PythonAdapter, ArtifactError
from .contracts import canonical_json, sha256
from .evaluation import partition_indices
from .gateway import BudgetError
from .search import SearchRepository


class DeliveryError(ValueError):
    pass


BUNDLE_FILES = {'candidate.json', 'contract.json', 'requirements.txt', 'README.md', 'Dockerfile', 'provenance.json',
                'runtime/__init__.py', 'runtime/contracts.py', 'runtime/candidates.py', 'runtime/service_runtime.py'}


def bundle_files(family):
    extra={'runtime/docker_runtime.py','runtimes/python/runner.py','dependencies.json'} if family in ('python.v1','sklearn.classifier.v1','workflow.v1') else set()
    if family=='workflow.v1':extra|={'runtime/workflow_adapter.py','runtime/invocation.py','runtime/gateway.py'}
    if family=='sklearn.classifier.v1':
        extra|={'runtime/sklearn_adapter.py','runtimes/sklearn/train.py','runtimes/sklearn/predict.py','runtimes/sklearn/requirements.lock','runtimes/sklearn/Dockerfile'}
    return BUNDLE_FILES|extra


class DeliveryRepository:
    def __init__(self, store):
        self.store, self.searches = store, SearchRepository(store)
        self.draft_root = store.path.parent / 'drafts'

    def get(self, delivery_id):
        with self.store.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if row is None:
                raise KeyError('delivery_not_found')
            value = dict(row)
            value['frozen'] = json.loads(value.pop('frozen_json'))
            for table, key, column in [('delivery_reports', 'report', 'report_json'), ('delivery_proofs', 'proof', 'proof_json')]:
                result = db.execute(f'SELECT {column} FROM {table} WHERE delivery_id=?', (delivery_id,)).fetchone()
                value[key] = json.loads(result[0]) if result else None
        root = self.draft_root / delivery_id
        intact = False
        try:
            manifest = json.loads((root / 'manifest.json').read_text())
            intact = (type(manifest) is dict and type(manifest.get('files_sha256')) is dict
                      and manifest['delivery_id'] == delivery_id and set(manifest['files_sha256']) == bundle_files(value['frozen']['candidate']['family'])
                      and all(not (root / name).is_symlink() and hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                              for name, digest in manifest['files_sha256'].items()))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        value['state'] = ('interrupted_or_in_progress' if not value['report'] else
                          'final_checks_failed' if not value['report']['gate_passed'] else
                          'private_draft_ready' if intact else 'private_draft_incomplete')
        from .service_export import exported_service, license_review
        value['export'] = exported_service(self.store, delivery_id)
        value['release_allowed'] = bool(value['export'] and value['export']['archive_integrity_verified'])
        value['public_publish_authorized'] = False
        value['release_blockers'] = [] if value['release_allowed'] else ['local_source_bundle_review_and_export_pending']
        try:
            license_review()
        except DeliveryError:
            value['release_blockers'].append('owner_license_review_required')
        value['draft_integrity_verified'] = intact
        value['draft_path'] = str(root.absolute()) if intact else None
        return value

    def for_search(self, search_id):
        with self.store.connection() as db:
            row = db.execute('SELECT id FROM deliveries WHERE search_id=?', (search_id,)).fetchone()
        return self.get(row[0]) if row else None

    def prepare(self, search_id, request_id):
        if type(request_id) is not str or not re.fullmatch('[0-9a-f]{32}', request_id):
            raise DeliveryError('invalid_delivery_request')
        source = self.searches.get(search_id)
        task = self.searches.tasks.get_revision(source['task_id'], source['revision'])
        contract = task['contract']
        selection = source['selection']
        if source['state'] != 'succeeded' or not selection or not selection['candidate_id']:
            raise DeliveryError('validated_selection_required')
        candidate = next(c for c in source['candidates'] if c['id'] == selection['candidate_id'])
        if candidate['family']=='workflow.v1':
            from .workflow_adapter import WorkflowAdapter,requires_model
            WorkflowAdapter.validate(candidate['artifact'])
            if requires_model(candidate['artifact']):
                raise DeliveryError('model_backed_serving_runtime_required')
        # Registry integration stays extensible; only this serving adapter is accepted.
        if candidate['family'] not in ('rules.v1','python.v1','sklearn.classifier.v1','workflow.v1'):
            raise DeliveryError('standalone_adapter_not_available_for_family')
        adapter = RulesAdapter()
        runtime_configuration = None
        if candidate['family'] in ('python.v1','sklearn.classifier.v1','workflow.v1'):
            from .docker_runtime import configured_sandbox
            from .sklearn_adapter import configured_ml_sandbox,SklearnAdapter
            sandbox = configured_ml_sandbox(self.store.path.parent/'ml-runtime.json') if candidate['family']=='sklearn.classifier.v1' else configured_sandbox(self.store.path.parent/'sandbox.json')
            if sandbox is None:
                raise DeliveryError('verified_sandbox_required')
            if sandbox.engine.status()['engine_version']!=sandbox.accepted_engine_version:
                raise DeliveryError('runtime_review_required')
            if candidate['family']=='workflow.v1':
                from .workflow_adapter import WorkflowAdapter
                adapter=WorkflowAdapter(sandbox)
            else:adapter = SklearnAdapter(sandbox) if candidate['family']=='sklearn.classifier.v1' else PythonAdapter(sandbox)
            runtime_configuration = {'image':sandbox.image,'engine_version':sandbox.accepted_engine_version}
        adapter.validate(candidate['artifact'])
        if contract['definition']['constraints']['hard']:
            raise DeliveryError('executable_hard_constraint_checks_required')
        splits = partition_indices(contract)
        unique = {}
        for index in splits['final_test']:
            unique.setdefault(sha256(contract['definition']['samples'][index]), index)
        if not unique:
            raise DeliveryError('independent_final_data_required')
        labels = {row['sample_index']: row['expected_output'] for row in contract['definition']['labels']}
        if any(index not in labels for index in unique.values()):
            raise DeliveryError('final_quality_checks_or_human_review_required')
        label_groups = {}
        for index, output in labels.items():
            label_groups.setdefault(sha256(contract['definition']['samples'][index]), set()).add(canonical_json(output))
        if any(len(label_groups[digest]) != 1 for digest in unique):
            raise DeliveryError('conflicting_final_labels')
        delivery_id = uuid.uuid4().hex
        with self.store.connection(write=True) as db:
            existing = db.execute('SELECT id,search_id FROM deliveries WHERE request_id=? OR search_id=?', (request_id, search_id)).fetchone()
            if existing:
                if existing['search_id'] != search_id:
                    raise DeliveryError('delivery_idempotency_conflict')
                delivery_id = existing['id']
            else:
                exposed = {row[0] for row in db.execute('SELECT input_sha256 FROM final_quarantine')}
                if exposed & {sha256(sample) for sample in contract['definition']['samples']}:
                    raise DeliveryError('final_test_already_consumed')
                # Fence all concurrent searches containing these inputs before exposure.
                active = db.execute("SELECT t.contract_json FROM searches s JOIN task_revisions t ON t.task_id=s.task_id AND t.revision=s.revision WHERE s.state IN ('queued','running')").fetchall()
                if any(set(unique) & {sha256(sample) for sample in json.loads(row[0])['definition']['samples']} for row in active):
                    raise DeliveryError('overlapping_search_must_finish_or_cancel')
                root = self.searches.ledger.root_scope(db, search_id)
                used = db.execute('SELECT COUNT(*) FROM execution_slots WHERE root_id=?', (root,)).fetchone()[0]
                maximum = db.execute('SELECT execution_limit FROM searches WHERE id=?', (root,)).fetchone()[0]
                if used + len(unique) > maximum:
                    raise BudgetError('final_execution_budget_insufficient')
                frozen = {'candidate': candidate, 'task_content_sha256': task['content_sha256'],
                          'dataset_sha256': contract['dataset']['sha256'], 'final_indices': list(unique.values()),
                          'selection_evidence': selection['evidence'], 'frozen_before_final_execution': True,
                          'output_specification': contract['definition']['output_specification'],
                          'input_schema': contract['dataset']['input_schema'], 'semantic_output_schema': 'unconfirmed',
                          'runtime_configuration':runtime_configuration}
                db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?)',
                           (delivery_id, request_id, search_id, candidate['id'], source['task_id'], source['revision'], time.time(), canonical_json(frozen)))
                db.executemany('INSERT INTO final_quarantine VALUES(?,?)', [(digest, delivery_id) for digest in unique])
                db.executemany('INSERT INTO execution_slots VALUES(?,?,?,?)', [(search_id, candidate['id'], index, root) for index in unique.values()])
                self.searches.event(db, search_id, 'candidate_frozen_for_delivery', {'delivery_id': delivery_id,
                                     'candidate_id': candidate['id'], 'artifact_sha256': candidate['artifact_sha256']})
                existing = None
        if existing:
            return self.get(delivery_id)  # No replay after interrupted exposure.
        observations = []
        for index in unique.values():
            started = time.perf_counter()
            output,error,agreement=None,None,None
            try:
                output, _ = adapter.execute(candidate['artifact'], contract['definition']['samples'][index], None)
                agreement=int(canonical_json(output)==canonical_json(labels[index]))
            except ArtifactError as exception:
                error=str(exception)
            observations.append({'sample_index': index, 'output': output, 'label_agreement': agreement,'error':error,
                                 'runtime':adapter.runtime_receipt() if hasattr(adapter,'runtime_receipt') else None,
                                 'latency_ms': (time.perf_counter() - started)*1000})
        report = {'split': 'final_test_once', 'observations': observations,
                  'provided_label_matches': sum(row['label_agreement'] or 0 for row in observations), 'count': len(observations),
                  'gate_passed': all(row['label_agreement'] for row in observations),
                  'label_source': 'provided_labels_unverified', 'quality_is_verified_accuracy': False,
                  'small_sample_generalization_unknown': True, 'used_for_tuning': False, 'model_calls': 0}
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO delivery_reports VALUES(?,?)', (delivery_id, canonical_json(report)))
        if report['gate_passed']:
            self.build_private_draft(delivery_id)
        return self.get(delivery_id)

    def build_private_draft(self, delivery_id):
        delivery = self.get(delivery_id)
        if not delivery['report'] or not delivery['report']['gate_passed']:
            raise DeliveryError('final_gate_not_passed')
        self.draft_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = self.draft_root / delivery_id
        root.mkdir(mode=0o700)  # Refuse overwrite/symlink/rebuild of a frozen draft.
        package = root / 'runtime'
        package.mkdir(mode=0o700)
        frozen = delivery['frozen']
        candidate = frozen['candidate']
        # Allowlisted repository-authored source only; generated content is JSON.
        sources = Path(__file__).resolve().parent
        for name in ['contracts.py', 'candidates.py', 'service_runtime.py']:
            (package / name).write_bytes((sources / name).read_bytes())
        if candidate['family'] in ('python.v1','sklearn.classifier.v1','workflow.v1'):
            (package/'docker_runtime.py').write_bytes((sources/'docker_runtime.py').read_bytes())
            (root/'runtimes/python').mkdir(parents=True,mode=0o700)
            (root/'runtimes/python/runner.py').write_bytes((sources.parents[1]/'runtimes/python/runner.py').read_bytes())
            inventory=json.loads((sources.parents[1]/'dependencies.json').read_text())
            (root/'dependencies.json').write_text(canonical_json({'container_images':inventory['container_images'],
                'container_python_packages':inventory.get('container_python_packages',[])})+'\n')
            if candidate['family']=='workflow.v1':
                for name in ('workflow_adapter.py','invocation.py','gateway.py'):
                    (package/name).write_bytes((sources/name).read_bytes())
            if candidate['family']=='sklearn.classifier.v1':
                (package/'sklearn_adapter.py').write_bytes((sources/'sklearn_adapter.py').read_bytes())
                (root/'runtimes/sklearn').mkdir(parents=True,mode=0o700)
                for name in ('train.py','predict.py','requirements.lock','Dockerfile'):
                    (root/'runtimes/sklearn'/name).write_bytes((sources.parents[1]/'runtimes/sklearn'/name).read_bytes())
        (package / '__init__.py').write_text('')
        portable = {key: candidate[key] for key in ['id', 'family', 'artifact', 'artifact_sha256']}
        if frozen.get('runtime_configuration'):
            portable['runtime']=frozen['runtime_configuration']
        (root / 'candidate.json').write_text(canonical_json(portable)+'\n')
        (root / 'contract.json').write_text(canonical_json({key: frozen[key] for key in ['task_content_sha256', 'dataset_sha256', 'output_specification', 'input_schema', 'semantic_output_schema']})+'\n')
        (root / 'requirements.txt').write_text('# Python 3.13.3 stdlib; no external application packages\n')
        (root / 'README.md').write_text('''# Private standalone service draft

This working directory is frozen before final export review. Apache-2.0 and owner
attribution apply to STS; final LICENSE/NOTICE and distribution metadata are added
to the reviewed source ZIP. No model key, raw input dataset or DB is included.
Rules are bounded JSON data; Python source executes only in separate restricted
Docker containers, behind the trusted host HTTP gateway. Python-family service
requires the Engine version/image recorded in candidate.json and dependencies.json.
Run from this directory using installed Python 3.13+:

    python3 -m runtime.service_runtime --port 9000
    curl http://127.0.0.1:9000/health
    curl http://127.0.0.1:9000/predict -H 'Content-Type: application/json' --data '{"input":"hello"}'

POST /predict accepts exactly {"input": JSON value}, returns {"result": JSON value}.
GET /contract describes transport; task semantics/output schema remain unconfirmed.
No STS control process, DB, PYTHONPATH, credentials, model/network calls or installed
packages are needed. This local stdlib HTTP server is not production hosting.
The Docker recipe is unbuilt: choose/review an official Python base image digest
and its incorporated licenses first, then bind any host port to 127.0.0.1 only.
No cloud deployment or container-isolation acceptance is claimed.
''')
        (root / 'Dockerfile').write_text('''# Draft only. No base image adopted, pulled, built or deployment validated.
ARG PYTHON_BASE
FROM ${PYTHON_BASE}
WORKDIR /service
COPY --chown=65534:65534 runtime /service/runtime
COPY --chown=65534:65534 candidate.json contract.json /service/
USER 65534:65534
EXPOSE 9000
CMD ["python3", "-m", "runtime.service_runtime", "--port", "9000", "--host", "0.0.0.0"]
''')
        (root / 'provenance.json').write_text(canonical_json({'candidate_provenance': candidate['provenance'],
            'artifact_sha256': candidate['artifact_sha256'], 'source_search_id': delivery['search_id'],
            'trusted_template_sources': ['src/solve2ship/'+name for name in ['contracts.py', 'candidates.py', 'service_runtime.py']],
            'repository_license': 'Apache-2.0', 'distribution_approved': False, 'copied_upstream_sources': [],
            'application_packages': [], 'runtime': {'name': 'CPython', 'tested_version': '3.13.3', 'license': 'PSF-2.0', 'bundled': False},
            'container_base': {'status': 'not_selected_or_built', 'incorporated_notices_review': 'pending'}})+'\n')
        hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob('*') if path.is_file()}
        (root / 'manifest.json').write_text(canonical_json({'schema_version': 1, 'delivery_id': delivery_id,
              'private_draft': True, 'release_allowed': False, 'files_sha256': hashes})+'\n')
        for path in root.rglob('*'):
            if path.is_file():
                os.chmod(path, 0o600)
        return root

    def record_proof(self, delivery_id, proof):
        # CLI/tests provide actual independent-process evidence, never a fabricated flag.
        self.get(delivery_id)
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO delivery_proofs VALUES(?,?)', (delivery_id, canonical_json(proof)))
