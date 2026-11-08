from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request
from local_http import urlopen
from unittest.mock import patch

from solve2ship.contracts import CandidateRef, DisabledSandbox, SandboxUnavailable, TaskInput
from solve2ship.server import create_server
from solve2ship.store import Store
from solve2ship.worker import Worker


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'jobs.sqlite3'
        self.store = Store(self.path)

    def test_real_check_and_restart_preserve_result_and_events(self):
        job = self.store.create()
        self.assertTrue(Worker(self.store).tick())
        saved = Store(self.path).get(job['id'])
        self.assertEqual(saved['state'], 'succeeded')
        self.assertEqual(saved['result']['checks']['sqlite_integrity'], 'ok')
        self.assertFalse(saved['result']['checks']['arbitrary_code_execution_enabled'])
        self.assertEqual([e['type'] for e in saved['events']], ['queued', 'started', 'succeeded'])
        self.assertNotIn('accuracy', json.dumps(saved))

    def test_claim_is_exclusive_across_connections(self):
        self.store.create()
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(lambda _: Store(self.path).claim(), range(8)))
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_cancel_queued_and_running_is_idempotent_and_fenced(self):
        queued = self.store.create()['id']
        self.store.cancel(queued)
        self.assertIsNone(self.store.claim())
        job_id = self.store.create()['id']
        _, owner = self.store.claim()
        self.store.cancel(job_id)
        self.store.cancel(job_id)
        self.assertFalse(self.store.finish(job_id, owner, result={'unexpected': True}))
        saved = self.store.get(job_id)
        self.assertEqual(saved['state'], 'cancelled')
        self.assertEqual(sum(e['type'] == 'cancelled' for e in saved['events']), 1)

    def test_expired_lease_recovers_and_old_owner_cannot_finish(self):
        job_id = self.store.create()['id']
        _, old_owner = self.store.claim(now=100, lease_seconds=10)
        restarted = Store(self.path)
        self.assertIsNone(restarted.claim(now=109))
        _, new_owner = restarted.claim(now=110)
        self.assertFalse(restarted.finish(job_id, old_owner, result={}, now=111))
        self.assertTrue(restarted.finish(job_id, new_owner, result={}, now=111))
        self.assertIn('requeued', [e['type'] for e in restarted.get(job_id)['events']])

    def test_expired_completion_rejected_even_without_reclaim(self):
        job_id = self.store.create()['id']
        _, owner = self.store.claim(now=100, lease_seconds=1)
        self.assertFalse(self.store.finish(job_id, owner, result={}, now=101))

    def test_repeated_worker_loss_is_bounded(self):
        job_id = self.store.create()['id']
        for now in [100, 131, 162]:
            self.assertIsNotNone(Store(self.path).claim(now=now))
        self.assertIsNone(self.store.claim(now=193))
        self.assertEqual(self.store.get(job_id)['state'], 'failed')
        self.assertEqual(self.store.get(job_id)['events'][-1]['type'], 'recovery_exhausted')

    def test_terminal_success_not_cancelled(self):
        job_id = self.store.create()['id']
        Worker(self.store).tick()
        self.assertEqual(self.store.cancel(job_id)['state'], 'succeeded')

    def test_failure_records_safe_error_no_invented_result(self):
        job_id = self.store.create()['id']
        with patch('solve2ship.worker.foundation_check', side_effect=RuntimeError('secret-value')):
            Worker(self.store).tick()
        job = self.store.get(job_id)
        self.assertEqual(job['state'], 'failed')
        self.assertIsNone(job['result'])
        self.assertNotIn('secret-value', json.dumps(job))

    def test_unknown_kind_and_missing_job_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create('run_python')
        with self.assertRaises(KeyError):
            self.store.cancel('unknown')

    def test_events_append_only(self):
        self.store.create()
        with self.store.connection() as db:
            for statement in ['UPDATE events SET type="fake"', 'DELETE FROM events']:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)

    def test_unknown_schema_refused(self):
        with self.store.connection() as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError):
            Store(self.path)

    def test_family_is_open_and_sandbox_fails_closed(self):
        task = TaskInput(samples=('input',), output_specification='Describe the output')
        self.assertEqual(task.labels, ())
        candidate = CandidateRef('candidate', 'rev', 'future-neural-family', 'a' * 64, 'future-runtime')
        with self.assertRaises(SandboxUnavailable):
            DisabledSandbox().execute(candidate)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'state.db')
        self.server = create_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, body=None, headers=None, raw=None):
        default = {'Content-Type': 'application/json', 'X-STS-Request': '1'}
        if headers:
            default.update(headers)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = Request(self.base + path, data=data, headers=default)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as response_error:
            response = response_error
        with response:
            payload = response.read()
            if response.headers['Content-Type'].startswith('application/json'):
                payload = json.loads(payload)
            return response.status, payload, response.headers

    def test_actual_http_worker_cycle(self):
        worker = Worker(self.store)
        worker.start()
        self.addCleanup(worker.close)
        status, job, _ = self.request('/api/jobs', {'kind': 'foundation_check'})
        self.assertEqual(status, 201)
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, saved, _ = self.request('/api/jobs/' + job['id'])
            if saved['state'] == 'succeeded':
                break
            time.sleep(0.02)
        self.assertEqual(saved['state'], 'succeeded')
        self.assertEqual(saved['result']['evidence_kind'], 'infrastructure_diagnostic')
        self.assertEqual(self.request('/api/jobs')[1]['jobs'][0]['id'], job['id'])

    def test_cancel_and_missing_job(self):
        _, job, _ = self.request('/api/jobs', {'kind': 'foundation_check'})
        status, saved, _ = self.request('/api/jobs/' + job['id'] + '/cancel', {})
        self.assertEqual((status, saved['state']), (200, 'cancelled'))
        self.assertEqual(self.request('/api/jobs/' + 'f' * 32)[0], 404)

    def test_cross_origin_and_dns_rebinding_guard(self):
        self.assertEqual(self.request('/api/health', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('/api/jobs', {}, headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/jobs', {}, headers={'X-STS-Request': ''})[0], 403)

    def test_malformed_and_unsupported_payloads(self):
        for body in [[], {}, {'kind': 'candidate'}, {'kind': 'foundation_check', 'code': 'print(1)'}]:
            self.assertEqual(self.request('/api/jobs', body)[0], 400)
        self.assertEqual(self.request('/api/jobs', raw=b'{')[0], 400)
        self.assertEqual(self.request('/api/jobs', raw=b'x' * 4097)[0], 413)
        self.assertEqual(self.request('/api/jobs', {}, headers={'Content-Type': 'text/plain'})[0], 415)

    def test_static_allowlist_and_security_headers(self):
        for path in ['/', '/app.mjs', '/i18n.mjs', '/style.css']:
            status, _, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path in ['/../CODEX_CONTEXT.md', '/.sts/state.db', '/api/execute']:
            self.assertEqual(self.request(path)[0], 404)
        self.assertFalse(self.request('/api/health')[1]['arbitrary_code_execution_enabled'])

    def test_local_bind_never_requires_reverse_dns(self):
        with patch('socket.getfqdn', side_effect=AssertionError('DNS must not be used')):
            server = create_server(self.store, 0)
            server.server_close()


class ProcessTests(unittest.TestCase):
    def test_cli_process_restart_and_interrupted_job_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            store = Store(path)
            job_id = store.create()['id']
            # Simulate a prior worker dying while owning an actual persisted lease.
            store.claim(lease_seconds=1)
            first = self.start(path)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with urlopen(first[1] + '/api/jobs/' + job_id, timeout=2) as response:
                        saved = json.load(response)
                    if saved['state'] == 'succeeded':
                        break
                    time.sleep(0.05)
                self.assertEqual(saved['state'], 'succeeded')
                self.assertEqual(saved['attempt'], 2)
                self.assertIn('requeued', [e['type'] for e in saved['events']])
            finally:
                self.stop_process(first[0])
            second = self.start(path)
            try:
                with urlopen(second[1] + '/api/jobs/' + job_id, timeout=2) as response:
                    self.assertEqual(json.load(response), saved)
            finally:
                self.stop_process(second[0])

    def start(self, path):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / 'src'))
        process = subprocess.Popen([sys.executable, '-m', 'solve2ship', '--port', '0', '--db', str(path)],
                                   cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            line = lines.get(timeout=5).strip()
            self.assertTrue(line.startswith('Solve2Ship ') and ': http://127.0.0.1:' in line, line)
        except BaseException:
            self.stop_process(process)
            raise
        return process, line.split(': ', 1)[1]

    def stop_process(self, process):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(process.returncode, 0)
