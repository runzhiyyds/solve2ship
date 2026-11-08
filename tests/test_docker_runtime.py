import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from solve2ship.candidates import ArtifactError, builtin_registry
from solve2ship.docker_runtime import DockerError,DockerSandbox,approved_image,configured_sandbox


class EngineFixture:
    """Protocol fixture only; actual isolation evidence comes from verify_docker.py."""
    def __init__(self):self.calls=[];self.config=None;self.mismatch=False;self.unavailable=False
    def status(self):
        if self.unavailable:raise DockerError('docker_unavailable')
        return {'engine_version':'fixture'}
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
