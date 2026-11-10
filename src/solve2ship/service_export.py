"""Reviewed local source-bundle export. No image archive or public publishing."""

import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import time
import uuid
import zipfile

from .contracts import canonical_json
from .delivery import bundle_files, DeliveryError, DeliveryRepository
from .candidates import model_required


ROOT = Path(__file__).resolve().parents[2]


def license_review():
    policy = json.loads((ROOT/'dependencies.json').read_text())
    if (policy['repository_license'] != 'Apache-2.0' or not policy.get('license_approved_by_owner')
            or not policy.get('copyright_holder') or hashlib.sha256((ROOT/'LICENSE').read_bytes()).hexdigest() != policy['license_sha256']):
        raise DeliveryError('owner_license_review_required')
    return policy


def exported_service(store, delivery_id):
    with store.connection() as db:
        row = db.execute('SELECT receipt_json FROM service_export_versions WHERE delivery_id=? ORDER BY created_at DESC LIMIT 1', (delivery_id,)).fetchone()
    if not row:
        return None
    receipt = json.loads(row[0])
    archive = store.path.parent/'exports'/receipt['id']/'service.zip'
    receipt['archive_integrity_verified'] = archive.is_file() and hashlib.sha256(archive.read_bytes()).hexdigest() == receipt['archive_sha256']
    receipt['archive_path'] = str(archive.absolute()) if receipt['archive_integrity_verified'] else None
    return receipt


def export_service(store, delivery_id):
    repository = DeliveryRepository(store)
    delivery = repository.get(delivery_id)
    policy = license_review()
    if (not delivery['draft_integrity_verified'] or not delivery['report'] or not delivery['report']['gate_passed']
            or not delivery['proof'] or delivery['proof'].get('status') != 'passed'):
        raise DeliveryError('independent_service_proof_required')
    original = Path(delivery['draft_path'])
    family=delivery['frozen']['candidate']['family']
    needs_model=model_required(family,delivery['frozen']['candidate']['artifact'])
    required_version = 4 if delivery['report'].get('quality_mode')=='operator_review' else (3 if needs_model else 2)
    needs_docker=family in ('python.v1','workflow.v1','sklearn.classifier.v1','agent.tools.v1')
    if hashlib.sha256((original/'manifest.json').read_bytes()).hexdigest() != delivery['proof']['manifest_sha256']:
        raise DeliveryError('frozen_serving_proof_mismatch')
    export_root = store.path.parent/'exports'
    export_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Serialize local file publication/receipt; no model call, network or image build.
    with store.connection(write=True) as db:
        existing = db.execute('SELECT receipt_json FROM service_export_versions WHERE delivery_id=? ORDER BY created_at DESC LIMIT 1', (delivery_id,)).fetchone()
        if existing and json.loads(existing[0]).get('export_template_version',1)>=required_version:
            identifier = json.loads(existing[0])['id']
        else:
            identifier = uuid.uuid4().hex
            root = export_root/identifier
            root.mkdir(mode=0o700)
            with tempfile.TemporaryDirectory(dir=root, prefix='stage-') as temporary:
                package = Path(temporary)/'service';package.mkdir(mode=0o700)
                for name in sorted(bundle_files(family)-{'Dockerfile','README.md','provenance.json'}):
                    target = package/name;target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((original/name).read_bytes())
                for name in ('LICENSE','NOTICE'):
                    (package/name).write_bytes((ROOT/name).read_bytes())
                runtime_reference=delivery['frozen'].get('runtime_configuration',{} ) or {}
                image = next((row for row in policy['container_images'] if row['reference']==runtime_reference.get('image')),policy['container_images'][0])
                dockerfile=('FROM '+image['reference']+'\nWORKDIR /service\n'
                    'COPY --chown=65534:65534 runtime /service/runtime\n'
                    'COPY --chown=65534:65534 candidate.json contract.json LICENSE NOTICE /service/\n'
                    'USER 65534:65534\nEXPOSE 9000\n'
                    'CMD ["python3","-B","-E","-S","-m","runtime.service_runtime","--host","0.0.0.0","--port","9000"]\n')
                if family=='rules.v1':
                    (package/'Dockerfile').write_text(dockerfile)
                else:
                    (package/'runtime-image.json').write_text(canonical_json(image if needs_docker else {'docker_required':False})+'\n')
                (package/'README.md').write_text('''# Solve2Ship standalone Rules service

Apache-2.0. Copyright 2026 郭辰远 (Chenyuan Guo). Preserve LICENSE and NOTICE.
The frozen artifact is bounded JSON; reviewed runtime code interprets it.

Run with installed Python 3.13+ from this directory:

    python3 -B -E -S -m runtime.service_runtime --port 9000
    curl http://127.0.0.1:9000/health
    curl http://127.0.0.1:9000/predict -H 'Content-Type: application/json' --data '{"input":{"tier":"gold"}}'

GET /contract describes transport. POST /predict accepts exactly {input: JSON value}
and returns {result: JSON value}. Semantic output schema remains unconfirmed.
No STS controller, experiment DB, dataset, credentials or model calls are required.
Use this as a local development service; public production hosting is not validated.

Optional Docker build uses the exact base digest in Dockerfile. Bind serving ports
only to loopback; retain the base image's incorporated third-party notices. This
archive contains application source, not a Python/OS binary or container image.
Image archive redistribution and public/cloud deployment have separate review gates.
See provenance.json for the exact runtime reference and installed-component inventory.
''')
                if family!='rules.v1':
                    (package/'README.md').write_text('''# Solve2Ship standalone isolated program/model service

Apache-2.0. Copyright 2026 郭辰远 (Chenyuan Guo). Preserve LICENSE and NOTICE.
Run the trusted HTTP gateway using installed Python 3.13+ and the locally reviewed
Docker Engine version recorded in candidate.json. The Engine must already have the
digest-pinned Python image listed in runtime-image.json. This bundle does not include
or publish an OS/interpreter image archive. Preserve upstream notices when pulling
or redistributing runtime images; their licenses remain separate from STS.

    python3 -B -E -S -m runtime.service_runtime --port 9000
    curl http://127.0.0.1:9000/health
    curl http://127.0.0.1:9000/predict -H 'Content-Type: application/json' --data @request.json

Create request.json containing {"input": your actual task input}; sample datasets
are not included in this archive. See contract.json for the task goal and
candidate.json for a fitted classifier feature_path/model where applicable.
POST /predict accepts {input: JSON value}, returns {result: JSON value}. No STS
controller, experiment DB, dataset, model key or model call is needed. Each request
runs the frozen program or fitted model ONLY in a separate non-root, network-disabled,
read-only Docker container, with CPU/memory/PID/output/deadline limits and cleanup.
Workflow data bindings and bounded Rules steps run in the trusted gateway; each
Python step uses its own restricted container. Fitted model loading also stays
inside the container; never unpickle it on the host.
The host gateway executes reviewed code only; it requires local Docker socket
access. Generated-code containers never receive that socket, host mounts or keys.
Two requests may execute concurrently; excess requests return 503. Runtime failures
do not fall back to host execution. Transport permits loopback clients only.

This local developer runtime is not certified for hostile public multi-tenant use.
No gateway Docker image, cloud deployment or public endpoint is claimed. Use the
native gateway plus isolated Docker execution path described here; the explicit
runtime dependency is part of the delivery contract. Semantic schema remains
unconfirmed and provided-label checks are not a general accuracy guarantee.
''')
                if needs_model:
                    (package/'README.md').write_text('''# Standalone model-backed service / 独立模型服务

Apache-2.0. Copyright 2026 郭辰远 (Chenyuan Guo). Preserve LICENSE/NOTICE.
The source bundle contains NO key, budget authorization, experiment DB or dataset.
Use installed Python 3.13+ from this directory. Configure your own profile at the
hidden prompt, then explicitly authorize a cumulative budget in a separate state
directory. This operator authorization is independent of the original STS project;
it must not be assumed to create extra budget for an existing grant.

    python3 -B -E -S -m runtime.service_model configure-model --state-dir ../service-state
    python3 -B -E -S -m runtime.service_model authorize-budget --cny YOUR_EXPLICIT_LIMIT --state-dir ../service-state
    python3 -B -E -S -m runtime.service_runtime --port 9000 --state-dir ../service-state
    python3 -B -E -S -m runtime.service_model status --state-dir ../service-state

The current reviewed model is DeepSeek Flash; pricing expires by UTC review date.
Each call reserves conservative headroom before submission. Unknown charges remain
reserved across restart. A delegated allowance cannot be raised by reconfiguration.
Unused delegated headroom stays reserved in the original project budget; it is not
a verified provider bill or an automatic refund. Use one persistent state per grant.

POST /predict requires exactly {"request_id":"32 lowercase hex characters","input":...}.
Use one new request ID per intended operation. Same ID/input after success returns
the saved result without another model call. Reused IDs with changed input or a
failed/interrupted operation return 409 and are never replayed automatically.
State contains owner-only local profile/usage SQLite, not an encrypted production
vault. Retain it on restart. No STS controller or experiment database is required.

Code/model-loading steps still need the Engine/image in candidate.json and run only
inside constrained Docker containers. No candidate container receives model keys or
Docker sockets. Gateway model requests use the reviewed metered adapter. Keep this
local developer service on loopback; no production tenancy/cloud hosting is claimed.

中文：源码不含密钥、预算授权、实验数据库或样本。密钥只在终端隐藏输入，并明确
设置累计预算；新操作者的授权不能默认为原项目增加预算。委托额度受原项目总预算
约束，未用额度仍保留。保留独立用量数据库才能在重启后保持计费与请求防重放。
相同编号的成功请求只返回缓存；失败/中断或输入冲突返回 409，不自动重跑。状态为
仅所有者可读的本机明文，尚非生产保险库。代码步骤仍只在受限 Docker 内执行。
''')
                inventory = ROOT/'.sts/runtime-acceptance/docker-runtime-inventory.json'
                metadata = {'repository_license': 'Apache-2.0', 'copyright_holder': policy['copyright_holder'],
                    'candidate_id': delivery['candidate_id'], 'artifact_sha256': delivery['frozen']['candidate']['artifact_sha256'],
                    'original_candidate_provenance': delivery['frozen']['candidate']['provenance'],
                    'distribution_review': 'local_source_bundle_reviewed_template_and_frozen_artifact',
                    'family':family,'docker_engine_required':needs_docker,'model_service_configuration_required':needs_model,
                    'standalone_default_authorized_cny':0,'credentials_bundled':False,
                    'container_python_packages':policy.get('container_python_packages',[]) if family=='sklearn.classifier.v1' else [],
                    'legal_originality_certified': False, 'source_search_id': delivery['search_id'],
                    'frozen_manifest_sha256': delivery['proof']['manifest_sha256'],
                    'independent_startup_proof': delivery['proof'],
                    'final_report_summary':{key:delivery['report'][key] for key in ('split','count','provided_label_matches','label_source','quality_is_verified_accuracy','gate_passed')},
                    'quality_review_summary':{key:delivery['report'].get(key) for key in ('quality_mode','review_state','accepted_review_count','review_evidence_sha256','human_identity_verified')},
                    'final_report_sha256':hashlib.sha256(canonical_json(delivery['report']).encode()).hexdigest(),
                    'runtime_reference': image if needs_docker or family=='rules.v1' else {'kind':'installed_cpython','version_requirement':'3.13+'}, 'application_packages': [], 'copied_upstream_sources': [],
                    'runtime_inventory': json.loads(inventory.read_text()) if inventory.is_file() else None,
                    'binary_runtime_bundled': False, 'container_image_archive_bundled': False,
                    'formal_spdx_sbom': False, 'public_cloud_deployed': False}
                (package/'provenance.json').write_text(canonical_json(metadata)+'\n')
                profile=store.path.parent/'model.json'
                secret=None
                if profile.exists():
                    from .model_config import load_profile
                    secret=load_profile(profile).api_key
                patterns=[re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),re.compile(r'\bsk-[A-Za-z0-9_-]{24,}\b')]
                for path in package.rglob('*'):
                    if path.is_file():
                        text=path.read_text()
                        if (secret and secret in text) or any(pattern.search(text) for pattern in patterns):
                            raise DeliveryError('possible_secret_in_bundle_review_required')
                hashes = {str(path.relative_to(package)):hashlib.sha256(path.read_bytes()).hexdigest() for path in package.rglob('*') if path.is_file()}
                (package/'manifest.json').write_text(canonical_json({'schema_version':1,'export_id':identifier,
                    'delivery_id':delivery_id,'license':'Apache-2.0','files_sha256':hashes})+'\n')
                archive = root/'service.zip'
                with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as bundle:
                    for path in sorted(package.rglob('*')):
                        if path.is_file():bundle.write(path,str(Path('service')/path.relative_to(package)))
                os.chmod(archive,0o600)
            receipt = {'id':identifier,'delivery_id':delivery_id,'created_at':time.time(),'license':'Apache-2.0',
                'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                'distribution_scope':'local_application_source_bundle','credential_pattern_checks_passed':True,
                'export_template_version':required_version,
                'image_archive_distributed':False,'publicly_published':False}
            db.execute('INSERT INTO service_export_versions VALUES(?,?,?,?)',(identifier,delivery_id,time.time(),canonical_json(receipt)))
    return exported_service(store,delivery_id)
