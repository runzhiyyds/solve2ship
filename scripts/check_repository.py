"""Scoped repository hygiene checks; not a comprehensive license/security audit."""

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', action='store_true')
    parser.add_argument('--public',action='store_true',help='Check the product-only source distribution')
    args = parser.parse_args()
    errors = []
    required = ['AGENTS.md', 'CODEX_CONTEXT.md', 'STS_PROJECT_HISTORY.md', 'README.md', 'README.zh-CN.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'docs/PUBLISHING.md',
                'docs/PRODUCT_VISION.md', 'docs/VISION_COVERAGE.md', 'docs/ARCHITECTURE.md',
                'docs/DEVELOPMENT_PLAN.md', 'docs/OPEN_SOURCE_COMPLIANCE.md',
                'docs/THIRD_PARTY_DEPENDENCIES.md', 'LICENSE', 'NOTICE', 'docs/SEARCH_ENGINE.md',
                'docs/EVALUATION_PROTOCOL.md', 'docs/DELIVERY_SPEC.md',
                'docs/milestones/M0_ACCEPTANCE.md', 'docs/milestones/M1_ACCEPTANCE.md',
                'docs/TASK_CONTRACT.md', 'docs/MODEL_CONFIGURATION.md', 'docs/milestones/M2_ACCEPTANCE.md',
                'docs/milestones/M3_ACCEPTANCE.md', 'docs/milestones/M4_ACCEPTANCE.md', 'docs/milestones/M5_ACCEPTANCE.md', 'docs/milestones/M6_PRIVATE_ACCEPTANCE.md', 'docs/BLOCKERS.md', 'docs/milestones/M6_EXPORT_ACCEPTANCE.md', 'docs/milestones/M7_PYTHON_ACCEPTANCE.md', 'docs/milestones/M7_ML_ACCEPTANCE.md', 'docs/milestones/M7_WORKFLOW_ACCEPTANCE.md', 'runtimes/python/runner.py']
    public=args.public or not (ROOT/'AGENTS.md').is_file()
    if public:
        required=['README.md','README.zh-CN.md','LICENSE','NOTICE','CHANGELOG.md','CONTRIBUTING.md',
                  'docs/USAGE.md','docs/PRODUCT_ARCHITECTURE.md','docs/DEPENDENCIES.md','docs/SECURITY.md',
                  'src/solve2ship/__main__.py','runtimes/python/runner.py','Makefile']
    for name in required:
        if not (ROOT / name).is_file():
            errors.append(f'Missing required document: {name}')
    if not public:
        coverage = (ROOT / 'docs/VISION_COVERAGE.md').read_text()
        for index in range(1, 17):
            if f'V{index:02d}' not in coverage:
                errors.append(f'Missing vision coverage: V{index:02d}')
    manifest = json.loads((ROOT / 'dependencies.json').read_text())
    if manifest['application_packages'] or manifest['vendored_sources']:
        errors.append('New dependencies require a reviewed inventory/checker update')
    if manifest['repository_license'] != 'Apache-2.0' or not manifest.get('license_approved_by_owner'):
        errors.append('Repository license must match the owner-approved Apache-2.0 decision')
    import hashlib
    if hashlib.sha256((ROOT / 'LICENSE').read_bytes()).hexdigest() != manifest.get('license_sha256'):
        errors.append('Repository LICENSE differs from reviewed official text')
    if args.release and not manifest.get('copyright_holder'):
        errors.append('RELEASE BLOCKED: owner copyright attribution is pending')
    for item in manifest.get('container_images', []):
        if not re.fullmatch(r'(?:python@)?sha256:[0-9a-f]{64}', item['reference']) or not item.get('distribution_duties'):
            errors.append('Container image requires immutable reference and distribution review')
    packages=manifest.get('container_python_packages',[])
    if packages:
        lock=(ROOT/'runtimes/sklearn/requirements.lock').read_text()
        expected={row['name']+'=='+row['version']+' --hash=sha256:'+row['sha256'] for row in packages}
        if expected!={line for line in lock.splitlines() if line and not line.startswith('#')}:
            errors.append('ML image requirements differ from reviewed dependency inventory')
        if any(not row.get('license_files') or not row.get('distribution_duties') for row in packages):
            errors.append('ML image packages require license-file provenance and distribution review')
    workflow = (ROOT / '.github/workflows/check.yml').read_text()
    for action in manifest['development_actions']:
        if not re.fullmatch(r'[0-9a-f]{40}', action['commit']):
            errors.append(f'Action commit is not pinned: {action["name"]}')
        if f'{action["name"]}@{action["commit"]}' not in workflow:
            errors.append(f'Action inventory/workflow mismatch: {action["name"]}')
    reviewed_actions={row['name']+'@'+row['commit'] for row in manifest['development_actions']}
    for path in (ROOT/'.github/workflows').glob('*.yml'):
        for used in re.findall(r'uses:\s*([^\s#]+)',path.read_text()):
            if used not in reviewed_actions:errors.append(f'Unreviewed CI action in {path.relative_to(ROOT)}')

    for path in (ROOT / 'src').rglob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [(node.module or '').split('.')[0]]
            for name in names:
                if name not in sys.stdlib_module_names:
                    errors.append(f'Unreviewed dependency {name} in {path.relative_to(ROOT)}')
            if isinstance(node, ast.Call):
                called = ast.unparse(node.func)
                if called in {'exec', 'eval', 'compile', 'os.system', 'subprocess.run', 'subprocess.Popen','pickle.load','pickle.loads','joblib.load'}:
                    errors.append(f'Forbidden control-plane execution API {called}')

    # Scan tracked and new, non-ignored text; never print the suspected secret itself.
    listing = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                             cwd=ROOT, check=True, capture_output=True).stdout
    patterns = [re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
                re.compile(r'\b(?:sk-proj-|sk-ant-)[A-Za-z0-9_-]{20,}'), re.compile(r'\bsk-[A-Za-z0-9]{24,}\b')]
    secret=None
    if (ROOT/'.sts/model.json').exists():
        sys.path.insert(0,str(ROOT/'src'))
        from solve2ship.model_config import load_profile
        secret=load_profile(ROOT/'.sts/model.json').api_key
    for name in set(listing.decode().split('\0')) - {''}:
        path = ROOT / name
        if not path.is_file():
            continue
        if public and (path.name.lower() in {'agents.md','agent.md','codex_context.md','sts_project_history.md'}
                       or name.startswith(('docs/milestones/','docs/decisions/'))
                       or name in {'docs/DEVELOPMENT_PLAN.md','docs/VISION_COVERAGE.md','docs/BLOCKERS.md','docs/PUBLISHING.md'}):
            errors.append(f'Development process file must stay local: {name}')
        if name.startswith('.sts/') or path.suffix.lower() in {'.sqlite','.sqlite3','.db','.zip','.whl'} or path.name=='.env':
            errors.append(f'Private state or unreviewed archive must not be published: {name}')
        content = path.read_text(errors='replace')
        if secret and secret in content:errors.append(f'Configured model credential detected in {name}')
        if any(pattern.search(content) for pattern in patterns):
            errors.append(f'Potential credential pattern in {name}')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('Document, dependency, execution-boundary and scoped credential checks passed.')
    print('Apache-2.0 license and owner attribution checked; image redistribution and deployment are separate gates.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
