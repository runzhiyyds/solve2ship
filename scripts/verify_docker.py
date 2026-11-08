"""Actual Docker isolation checks and explicit local adapter activation."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from solve2ship.docker_runtime import DockerEngine, DockerError, DockerSandbox, POLICY_VERSION, approved_image


def verify(image=None):
    image=image or approved_image()
    engine = DockerEngine()
    environment = engine.status()
    sandbox = DockerSandbox(image, engine=engine)
    receipts = []
    probe = '''def solve(value):
    import os, pathlib, socket, resource
    files = {p: pathlib.Path(p).exists() for p in value['host_paths']}
    try:
        open('/write-probe','w').write('denied')
        readonly=False
    except OSError:
        readonly=True
    try:
        socket.create_connection(('1.1.1.1',443),timeout=.2).close()
        network=False
    except OSError:
        network=True
    return {'uid':os.getuid(),'host_paths':files,'readonly':readonly,'network_denied':network,
      'credential_environment':[k for k in os.environ if k!='GPG_KEY' and ('KEY' in k or 'TOKEN' in k or k.startswith('STS_'))],
      'base_image_public_gpg_key_present':'GPG_KEY' in os.environ,
      'memory_max':pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip(),
      'cpu_max':pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
      'pids_max':pathlib.Path('/sys/fs/cgroup/pids.max').read_text().strip(),
      'cpu_rlimit':list(resource.getrlimit(resource.RLIMIT_CPU))}
'''
    facts = sandbox.execute(probe, {'host_paths': [str(ROOT/'.sts/model.json'), '/var/run/docker.sock', '/host']})
    receipts.append(sandbox.receipt())
    if (facts['uid'] != 65534 or any(facts['host_paths'].values()) or not facts['readonly'] or not facts['network_denied']
            or facts['credential_environment'] or facts['memory_max'] != '268435456' or facts['pids_max'] != '16'
            or facts['cpu_rlimit'] != [3,3]):
        raise AssertionError('isolation_probe_failed')
    quota, period = map(int, facts['cpu_max'].split())
    if quota/period != .5:
        raise AssertionError('cpu_cgroup_not_enforced')
    errors = {}
    for name, source, expected, duration in [
        ('timeout','def solve(x):\n while True: pass','sandbox_timeout',1),
        ('memory','def solve(x):\n return bytearray(512*1024*1024)','sandbox_memory_limit',5),
        ('stdout','def solve(x):\n print("x"*100000)\n return 1','docker_response_limit',5),
    ]:
        attempt = DockerSandbox(image, engine=engine, timeout=duration)
        try:
            attempt.execute(source, None)
            raise AssertionError(name+'_limit_not_observed')
        except DockerError as error:
            if str(error) != expected:
                raise
            errors[name] = str(error)
        receipts.append(attempt.receipt())
    if not all(row['container_removed'] for row in receipts):
        raise AssertionError('container_cleanup_failed')
    # Installed-image inventory only; this is not a vulnerability/legal certification.
    inventory = sandbox.execute('''def solve(x):
 import pathlib,platform,hashlib
 blocks=pathlib.Path('/var/lib/dpkg/status').read_text().split('\\n\\n')
 packages=[]
 for block in blocks:
  fields=dict(line.split(': ',1) for line in block.splitlines() if ': ' in line and not line.startswith(' '))
  if fields.get('Status')=='install ok installed': packages.append({'name':fields['Package'],'version':fields['Version']})
 return {'python':platform.python_version(),'debian_packages':packages,'copyright_files':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in pathlib.Path('/usr/share/doc').glob('*/copyright') if p.is_file()]}
''',None)
    receipts.append(sandbox.receipt())
    return {'review_date':'2026-11-08','status':'passed','policy':POLICY_VERSION,'engine':environment,
            'image':image,'facts':facts,'limit_failures_observed':errors,'receipts':receipts,
            'host_execution_fallback':False,'public_multi_tenant_security_certified':False}, inventory


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    evidence,inventory=verify()
    report='docker-isolation.json' if args.activate else 'docker-isolation-latest-check.json'
    reports=ROOT/'.sts/runtime-acceptance';reports.mkdir(parents=True,exist_ok=True,mode=0o700)
    (reports/report).write_text(json.dumps(evidence,indent=2)+'\n')
    (reports/'docker-runtime-inventory.json').write_text(json.dumps(inventory,indent=2)+'\n')
    for path in reports.iterdir():
        if path.is_file():os.chmod(path,0o600)
    if args.activate:
        config={'policy':POLICY_VERSION,'image':evidence['image'],'isolation_checks_passed':True,
                'acceptance_sha256':hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest(),'review_date':'2026-11-08',
                'runner_sha256':hashlib.sha256((ROOT/'runtimes/python/runner.py').read_bytes()).hexdigest(),
                'adapter_sha256':hashlib.sha256((ROOT/'src/solve2ship/docker_runtime.py').read_bytes()).hexdigest()}
        path=ROOT/'.sts/sandbox.json'
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as stream:json.dump(config,stream)
        os.chmod(path,0o600)
    print(json.dumps({'status':evidence['status'],'policy':POLICY_VERSION,'activated':args.activate,'python':inventory['python'],'limit_checks':evidence['limit_failures_observed']}))


if __name__=='__main__':main()
