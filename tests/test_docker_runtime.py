import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from solve2ship.candidates import ArtifactError, builtin_registry
from solve2ship.docker_runtime import (DockerError, DockerSandbox, approved_image,
    configured_sandbox, resolve_docker_endpoint)


class EngineFixture:
    """Protocol fixture only; actual isolation evidence comes from verify_docker.py."""
    def __init__(self):self.calls=[];self.config=None;self.mismatch=False;self.unavailable=False
    def status(self):
        if self.unavailable:raise DockerError('docker_unavailable')
        return {'engine_version':'fixture','endpoint_sha256':'fixture-endpoint',
                'endpoint_source':'fixture','context_name':'fixture'}
    def request(self,method,path,body=None,**kwargs):
        self.calls.append((method,path,body))
        if method=='POST' and path.endswith('/create'):
            self.config=body;return {'Id':'a'*64}
        if method=='GET' and path.endswith('/json'):
            return {'HostConfig':self.config['HostConfig'],'Config':self.config,
                    'Mounts':[{'Source':'/host'}] if self.mismatch else [],'State':{'Running':False,'OOMKilled':False,'ExitCode':0}}
        if '/logs?' in path:
            value=b'{"result":42}';return bytes([1,0,0,0])+len(value).to_bytes(4,'big')+value
        return None


class DockerBoundaryTests(unittest.TestCase):
    def test_explicit_socket_wins_without_desktop_path(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / 'engine.sock'
            with patch.dict('os.environ', {'DOCKER_HOST': 'tcp://127.0.0.1:2375', 'DOCKER_CONTEXT': 'missing'}, clear=False):
                endpoint = resolve_docker_endpoint(socket_path)
            self.assertEqual(endpoint['source'], 'explicit_socket')
            self.assertEqual(endpoint['socket_path'], str(socket_path))
            self.assertEqual(endpoint['uri'], 'unix://' + str(socket_path))

    def test_context_metadata_selects_colima_style_local_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory); meta = config / 'contexts' / 'meta' / ('a' * 64)
            meta.mkdir(mode=0o700, parents=True)
            (meta / 'meta.json').write_text(json.dumps({
                'Name': 'colima', 'Endpoints': {'docker': {'Host': 'unix:///tmp/colima/docker.sock'}}
            })); (meta / 'meta.json').chmod(0o600)
            with patch.dict('os.environ', {'DOCKER_CONFIG': str(config), 'DOCKER_CONTEXT': 'colima',
                                           'DOCKER_HOST': 'unix:///should-not-win.sock'}, clear=False):
                endpoint = resolve_docker_endpoint()
            self.assertEqual(endpoint['context_name'], 'colima')
            self.assertEqual(endpoint['socket_path'], '/tmp/colima/docker.sock')
            self.assertEqual(endpoint['source'], 'docker_context_env')

    def test_config_current_context_is_used_when_environment_is_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory); meta = config / 'contexts' / 'meta' / ('b' * 64)
            # Docker CLI commonly creates owner-owned, world-readable context
            # metadata.  Endpoint selection must accept that normal layout
            # while still rejecting group/other writes.
            meta.mkdir(mode=0o755, parents=True)
            (meta / 'meta.json').write_text(json.dumps({
                'Name': 'linux-engine', 'Endpoints': {'docker': {'Host': 'unix:///var/run/sts-engine.sock'}}
            })); (meta / 'meta.json').chmod(0o644)
            (config / 'config.json').write_text(json.dumps({'currentContext': 'linux-engine'}));
            (config / 'config.json').chmod(0o644)
            with patch.dict('os.environ', {'DOCKER_CONFIG': str(config)}, clear=False):
                with patch.dict('os.environ', {'DOCKER_CONTEXT': '', 'DOCKER_HOST': ''}):
                    endpoint = resolve_docker_endpoint()
            self.assertEqual(endpoint['source'], 'docker_config_context')
            self.assertEqual(endpoint['socket_path'], '/var/run/sts-engine.sock')

    def test_remote_and_invalid_endpoints_fail_closed(self):
        for value in ('tcp://127.0.0.1:2375', 'ssh://host/run/docker.sock', 'npipe:////./pipe/docker_engine', 'relative.sock'):
            with self.subTest(value=value), patch.dict('os.environ', {'DOCKER_HOST': value, 'DOCKER_CONTEXT': ''}):
                with self.assertRaisesRegex(DockerError, 'unsupported_docker_endpoint'):
                    resolve_docker_endpoint()

    def test_generated_source_is_data_until_sent_to_docker_never_host_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel=Path(directory)/'must-not-exist'
            source=f'open({str(sentinel)!r},"w").write("unsafe")\ndef solve(x): return 42'
            fixture=EngineFixture();sandbox=DockerSandbox(approved_image(),engine=fixture)
            self.assertEqual(sandbox.execute(source,None),42)
            self.assertFalse(sentinel.exists())
            self.assertEqual(json.loads(fixture.config['Env'][0].split('=',1)[1])['source'],source)
            self.assertTrue(sandbox.receipt()['container_removed'])

    def test_missing_engine_never_falls_back_to_running_source_on_host(self):
        fixture=EngineFixture();fixture.unavailable=True
        sandbox=DockerSandbox(approved_image(),engine=fixture)
        with self.assertRaisesRegex(DockerError,'unavailable'):sandbox.execute('raise AssertionError("HOST")',None)
        self.assertEqual(fixture.calls,[])

    def test_endpoint_change_requires_fresh_runtime_review(self):
        fixture=EngineFixture();sandbox=DockerSandbox(approved_image(),engine=fixture)
        sandbox.accepted_endpoint_sha256='different-endpoint'
        with self.assertRaisesRegex(DockerError,'endpoint_review_required'):
            sandbox.execute('def solve(x): return x',None)
        self.assertEqual(fixture.calls,[])

    def test_unexpected_host_mount_is_refused_before_start_and_cleaned_up(self):
        fixture=EngineFixture();fixture.mismatch=True;sandbox=DockerSandbox(approved_image(),engine=fixture)
        with self.assertRaisesRegex(DockerError,'policy_mismatch'):sandbox.execute('def solve(x): return x',None)
        self.assertFalse(any(path.endswith('/start') for _,path,_ in fixture.calls))
        self.assertEqual(fixture.calls[-1][0],'DELETE')

    def test_large_payload_and_unreviewed_image_are_refused(self):
        fixture=EngineFixture();sandbox=DockerSandbox(approved_image(),engine=fixture)
        with self.assertRaisesRegex(DockerError,'input_limit'):sandbox.execute('def solve(x): return x','x'*70000)
        self.assertEqual(fixture.calls,[])
        with self.assertRaisesRegex(DockerError,'unapproved'):DockerSandbox('python:latest')

    def test_python_family_requires_explicit_sandbox_and_bounded_artifact(self):
        self.assertNotIn('python.v1',builtin_registry().adapters)
        registry=builtin_registry(sandbox=DockerSandbox(approved_image(),engine=EngineFixture()))
        self.assertIn('python.v1',registry.adapters)
        with self.assertRaises(ArtifactError):registry.adapters['python.v1'].validate({'source':'x'*17000})

    def test_profile_permissions_and_unverified_activation_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sandbox.json';self.assertIsNone(configured_sandbox(path))
            path.write_text('{}');path.chmod(0o644)
            with self.assertRaisesRegex(DockerError,'permissions'):configured_sandbox(path)
            path.chmod(0o600)
            with self.assertRaisesRegex(DockerError,'acceptance'):configured_sandbox(path)
