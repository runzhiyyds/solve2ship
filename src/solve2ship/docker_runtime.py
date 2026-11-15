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
from urllib.parse import urlencode, urlsplit

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


def _safe_json_file(path, *, maximum=262144):
    """Read local Docker metadata without following links or exposing contents.

    Docker CLI creates ``config.json`` and context metadata with ordinary
    owner-readable permissions (often 0644).  We accept those files when they
    are owned by the current user and cannot be modified by group/other users;
    secrets are parsed only for endpoint selection and never returned.
    """
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > maximum):
            raise DockerError('untrusted_docker_configuration')
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = None
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise DockerError('docker_configuration_too_large')
        value = json.loads(raw)
        if type(value) is not dict:
            raise DockerError('invalid_docker_configuration')
        return value
    except DockerError:
        raise
    except (OSError, ValueError, UnicodeError) as error:
        raise DockerError('docker_configuration_unavailable') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _docker_config_dir():
    configured = os.environ.get('DOCKER_CONFIG')
    return Path(configured).expanduser() if configured else Path.home() / '.docker'


def _local_socket_uri(value):
    """Normalize a local Unix endpoint and reject remote Docker transports."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise DockerError('unsupported_docker_endpoint')
    parsed = urlsplit(value)
    if parsed.scheme != 'unix' or parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise DockerError('unsupported_docker_endpoint')
    path = parsed.path
    if not path.startswith('/') or any(part == '..' for part in Path(path).parts):
        raise DockerError('unsupported_docker_endpoint')
    return 'unix://' + path, path


def _context_endpoint(config_dir, context_name):
    if context_name == 'default':
        return 'unix:///var/run/docker.sock', None
    metadata_root = config_dir / 'contexts' / 'meta'
    try:
        entries = list(metadata_root.iterdir())
    except OSError as error:
        raise DockerError('docker_context_unavailable') from error
    matches = []
    for entry in entries[:256]:
        try:
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                continue
            metadata = _safe_json_file(entry / 'meta.json')
        except DockerError:
            continue
        if metadata.get('Name') == context_name:
            matches.append(metadata)
    if len(matches) != 1:
        raise DockerError('docker_context_unavailable')
    endpoint = ((matches[0].get('Endpoints') or {}).get('docker') or {}).get('Host')
    return _local_socket_uri(endpoint)[0], _local_socket_uri(endpoint)[1]


def resolve_docker_endpoint(socket_path=None):
    """Resolve the same local endpoint selection model as Docker CLI.

    Explicit settings fail closed. We never invoke Docker CLI or rely on a
    Docker Desktop path; Colima and Linux Engine are selected through the
    normal Docker context/host configuration.
    """
    source, context_name, uri = None, None, None
    if socket_path is not None:
        path = Path(socket_path).expanduser()
        if not path.is_absolute():
            raise DockerError('unsupported_docker_endpoint')
        uri, path = _local_socket_uri('unix://' + str(path))
        source = 'explicit_socket'
    else:
        context_env = os.environ.get('DOCKER_CONTEXT')
        host_env = os.environ.get('DOCKER_HOST')
        config_dir = _docker_config_dir()
        if context_env:
            context_name = context_env
            uri, path = _context_endpoint(config_dir, context_name)
            source = 'docker_context_env'
        elif host_env:
            uri, path = _local_socket_uri(host_env)
            source = 'docker_host_env'
        else:
            config_path = config_dir / 'config.json'
            try:
                config = _safe_json_file(config_path)
            except DockerError as error:
                if error.args and error.args[0] == 'docker_configuration_unavailable' and not config_path.exists():
                    config = {}
                else:
                    raise
            context_name = config.get('currentContext') or 'default'
            if not isinstance(context_name, str) or not context_name:
                raise DockerError('invalid_docker_configuration')
            uri, path = _context_endpoint(config_dir, context_name)
            source = 'docker_config_context' if context_name != 'default' else 'default_socket'
    return {'uri': uri, 'socket_path': path, 'source': source, 'context_name': context_name,
            'endpoint_sha256': hashlib.sha256(uri.encode()).hexdigest()}


class DockerEngine:
    def __init__(self, socket_path=None):
        endpoint = resolve_docker_endpoint(socket_path)
        self.socket_path = endpoint['socket_path']
        self.endpoint_uri = endpoint['uri']
        self.endpoint_source = endpoint['source']
        self.context_name = endpoint['context_name']
        self.endpoint_sha256 = endpoint['endpoint_sha256']

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
                'security_options': security, 'remote_engine_allowed': False,
                'endpoint_uri': self.endpoint_uri, 'endpoint_source': self.endpoint_source,
                'context_name': self.context_name, 'endpoint_sha256': self.endpoint_sha256}


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
        self.accepted_endpoint_sha256 = None
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
        if self.accepted_endpoint_sha256 and status.get('endpoint_sha256') != self.accepted_endpoint_sha256:
            raise DockerError('sandbox_endpoint_review_required')
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
                    'endpoint_sha256': status.get('endpoint_sha256'),
                    'endpoint_source': status.get('endpoint_source'),
                    'context_name': status.get('context_name'),
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
    if not evidence['engine'].get('endpoint_sha256'):
        raise DockerError('sandbox_acceptance_requires_review')
    sandbox.accepted_endpoint_sha256 = evidence['engine']['endpoint_sha256']
    return sandbox
