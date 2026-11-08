"""Local Docker Engine adapter. Generated source is sent only to containers."""

import http.client
import hashlib
import json
from pathlib import Path
import re
import socket
import stat
import os
import threading
import time
import uuid
from urllib.parse import urlencode

from .contracts import SandboxUnavailable, canonical_json, sha256, strict_json_loads, validate_json


ROOT = Path(__file__).resolve().parents[2]
POLICY_VERSION = 'docker-local-v1'
MAX_OUTPUT = 65536
MAX_PAYLOAD = 65536
ADAPTER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class DockerError(SandboxUnavailable):
    def __init__(self, code, *, diagnostic=None):
        super().__init__(code)
        self.diagnostic = diagnostic  # Local setup diagnostics, never execution DTO/logs.


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DockerEngine:
    def __init__(self, socket_path=None):
        self.socket_path = str(socket_path or Path.home()/'.docker/run/docker.sock')

    def request(self, method, path, body=None, *, raw=False, limit=1048576, content_type='application/json', timeout=5):
        try:
            info = os.stat(self.socket_path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid not in (0, os.getuid()):
                raise DockerError('untrusted_docker_socket')
            connection = UnixHTTPConnection(self.socket_path, timeout)
            try:
                payload = body if isinstance(body, bytes) else canonical_json(body).encode() if body is not None else None
                connection.request(method, path, payload, {'Content-Type': content_type})
                response = connection.getresponse()
                data = response.read(limit+1)
                if len(data) > limit:
                    raise DockerError('docker_response_limit')
                if response.status >= 300:
                    raise DockerError('docker_operation_failed', diagnostic=data.decode(errors='replace')[:2000])
                return data if raw else json.loads(data) if data else None
            finally:
                connection.close()
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise DockerError('docker_unavailable') from error

    def status(self):
        version = self.request('GET', '/version')
        info = self.request('GET', '/v1.45/info')
        security = info.get('SecurityOptions', [])
        if version.get('Os') != 'linux' or not any('seccomp' in value for value in security):
            raise DockerError('docker_security_policy_unavailable')
        return {'available': True, 'engine_version': version['Version'], 'architecture': version['Arch'],
                'security_options': security, 'remote_engine_allowed': False}


def approved_image(root=ROOT):
    inventory = json.loads((Path(root)/'dependencies.json').read_text())
    rows = inventory.get('container_images', [])
    if not rows or not re.fullmatch(r'python@sha256:[0-9a-f]{64}', rows[0]['reference']):
        raise DockerError('reviewed_runtime_image_required')
    return rows[0]['reference']


def image_is_packaged(reference,root=ROOT):
    rows=json.loads((Path(root)/'dependencies.json').read_text()).get('container_images',[])
    return any(row['reference']==reference and row.get('runtime_id')=='sklearn' for row in rows)


class DockerSandbox:
    def __init__(self, image, *, engine=None, timeout=5, runtime_root=ROOT):
        images=json.loads((Path(runtime_root)/'dependencies.json').read_text()).get('container_images',[])
        allowed={row['reference'] for row in images if re.fullmatch(r'(?:python@)?sha256:[0-9a-f]{64}',row['reference'])}
        if image not in allowed or type(timeout) is not int or not 1 <= timeout <= 10:
            raise DockerError('unapproved_runtime_configuration')
        self.image, self.engine, self.timeout = image, engine or DockerEngine(), timeout
        self.local = threading.local()
        self.accepted_engine_version = None
        self.stop = None
        self.runtime_root = Path(runtime_root)

    def reap_stale(self):
        filters = canonical_json({'label':['sts.sandbox='+POLICY_VERSION]})
        rows = self.engine.request('GET','/v1.45/containers/json?'+urlencode({'all':'1','filters':filters}))
        removed = []
        for row in rows:
            if row['Created'] < time.time()-30 and re.fullmatch('[0-9a-f]{64}',row['Id']):
                self.engine.request('DELETE','/v1.45/containers/'+row['Id']+'?force=1&v=1')
                removed.append(row['Id'])
        return removed

    def execute(self, source, sample):
        self.local.receipt = None
        if type(source) is not str or not 1 <= len(source.encode('utf-8')) <= 16384:
            raise DockerError('invalid_python_source')
        payload = canonical_json({'source': source, 'input': sample})
        if len(payload.encode()) > MAX_PAYLOAD:
            raise DockerError('sandbox_input_limit')
        status = self.engine.status()
        if self.accepted_engine_version and status['engine_version'] != self.accepted_engine_version:
            raise DockerError('sandbox_engine_review_required')
        runner = (self.runtime_root/'runtimes/python/runner.py').read_text()
        nonce = uuid.uuid4().hex
        config = {'Image': self.image, 'Cmd': ['python3', '-I', '-S', '-B', '-u', '-c', runner],
                  'User': '65534:65534', 'WorkingDir': '/tmp', 'NetworkDisabled': True,
                  'Env': ['STS_TASK='+payload,'STS_IMAGE_PACKAGES='+('1' if image_is_packaged(self.image,self.runtime_root) else '0'),
                          'OPENBLAS_NUM_THREADS=1','OMP_NUM_THREADS=1','MKL_NUM_THREADS=1','NUMEXPR_NUM_THREADS=1'],
                  'Labels': {'sts.sandbox': POLICY_VERSION, 'sts.nonce': nonce},
                  'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                    'SecurityOpt': ['no-new-privileges:true'], 'Memory': 268435456, 'MemorySwap': 268435456,
                    'NanoCpus': 500000000, 'PidsLimit': 16, 'IpcMode': 'private',
                    'Tmpfs': {'/tmp': 'rw,noexec,nosuid,nodev,size=16777216,mode=1777'},
                    'Ulimits': [{'Name': 'nofile', 'Soft': 64, 'Hard': 64}, {'Name': 'core', 'Soft': 0, 'Hard': 0}],
                    'LogConfig': {'Type': 'local', 'Config': {'max-size': '1m', 'max-file': '1', 'compress': 'false'}}}}
        identifier = self.engine.request('POST', '/v1.45/containers/create', config)['Id']
        if not re.fullmatch(r'[0-9a-f]{64}', identifier):
            raise DockerError('invalid_container_identity')
        path = '/v1.45/containers/'+identifier
        error, output, observed = None, None, None
        started = time.monotonic()
        try:
            observed = self.engine.request('GET', path+'/json')
            host = observed['HostConfig']
            if (observed['Config']['User'] != '65534:65534' or observed['Config']['Labels'].get('sts.nonce') != nonce
                    or host['NetworkMode'] != 'none' or not host['ReadonlyRootfs'] or host['Memory'] != 268435456
                    or host['MemorySwap'] != 268435456 or host['NanoCpus'] != 500000000 or host['PidsLimit'] != 16
                    or host['CapDrop'] != ['ALL'] or 'no-new-privileges:true' not in host['SecurityOpt']
                    or observed.get('Mounts') or host.get('Privileged') or host.get('PortBindings')):
                raise DockerError('container_policy_mismatch')
            self.engine.request('POST', path+'/start')
            while True:
                observed = self.engine.request('GET', path+'/json')
                if not observed['State']['Running']:
                    break
                if self.stop is not None and self.stop.is_set():
                    raise DockerError('sandbox_cancelled')
                if time.monotonic()-started >= self.timeout:
                    raise DockerError('sandbox_timeout')
                time.sleep(.025)
            if observed['State']['OOMKilled']:
                raise DockerError('sandbox_memory_limit')
            if observed['State']['ExitCode'] != 0:
                raise DockerError('sandbox_program_failed')
            data = self.engine.request('GET', path+'/logs?stdout=1&stderr=1', raw=True, limit=MAX_OUTPUT+1024)
            stdout, stderr = bytearray(), bytearray()
            while data:
                if len(data) < 8:
                    raise DockerError('invalid_container_output')
                stream, size = data[0], int.from_bytes(data[4:8], 'big')
                if stream not in (1, 2) or size > len(data)-8:
                    raise DockerError('invalid_container_output')
                (stdout if stream == 1 else stderr).extend(data[8:8+size]); data = data[8+size:]
            if stderr or len(stdout) > MAX_OUTPUT:
                raise DockerError('sandbox_output_limit_or_stderr')
            value = strict_json_loads(stdout)
            validate_json(value)
            if type(value) is not dict or value.keys() != {'result'}:
                raise DockerError('invalid_container_output')
            output = value['result']
            return output
        except (DockerError, ValueError, KeyError, TypeError) as exception:
            error = str(exception) if isinstance(exception, DockerError) else 'invalid_container_output'
            raise DockerError(error) from exception
        finally:
            # Force removal is constrained to the container ID just created here.
            removed = False
            try:
                self.engine.request('DELETE', path+'?force=1&v=1')
                removed = True
            finally:
                self.local.receipt = {'provider': 'docker_local', 'policy': POLICY_VERSION,
                    'container_id': identifier, 'image': self.image, 'source_sha256': sha256(source),
                    'engine_version': status['engine_version'],
                    'wall_time_ms': (time.monotonic()-started)*1000, 'error': error,
                    'container_removed': removed, 'host_mounts': False, 'network': 'none',
                    'non_root': True, 'memory_bytes': 268435456, 'cpu_limit': .5, 'pid_limit': 16,
                    'readonly_root': True, 'model_credentials_supplied': False}

    def receipt(self):
        return getattr(self.local, 'receipt', None)

    def clear_receipt(self):self.local.receipt=None


def configured_sandbox(path):
    path = Path(path)
    if not path.exists():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DockerError('sandbox_profile_permissions')
    config = json.loads(path.read_text())
    if config.get('policy') != POLICY_VERSION or not config.get('isolation_checks_passed'):
        raise DockerError('sandbox_acceptance_required')
    evidence = json.loads((path.parent/'runtime-acceptance/docker-isolation.json').read_text())
    if (config.get('acceptance_sha256') != hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()
            or config.get('runner_sha256') != hashlib.sha256((ROOT/'runtimes/python/runner.py').read_bytes()).hexdigest()
            or config.get('adapter_sha256') != ADAPTER_SHA256):
        raise DockerError('sandbox_acceptance_requires_review')
    sandbox = DockerSandbox(config['image'])
    sandbox.accepted_engine_version = evidence['engine']['engine_version']
    return sandbox
