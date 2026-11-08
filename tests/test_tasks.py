from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from local_http import urlopen

import test_foundation as foundation
from solve2ship.contracts import ContractError, compile_task, sha256, strict_json_loads
from solve2ship.store import Store
from solve2ship.store import SCHEMA_VERSION
from solve2ship.tasks import TaskConflict, TaskRepository
from solve2ship.worker import Worker


def definition():
    return {'samples': [{'text': 'cancel my order'}, {'text': 'where is my order?'}],
            'output_specification': 'Return JSON with intent and a short explanation.\nPreserve the input language.'}


class CompilerTests(unittest.TestCase):
    def test_minimum_input_has_no_workflow_or_label_requirement(self):
        preview = compile_task(definition())
        contract = preview['contract']
        self.assertEqual(contract['evaluation']['supervision_mode'], 'specification_driven')
        self.assertEqual(contract['evaluation']['label_count'], 0)
        self.assertFalse(contract['semantic_interpretation'])
        self.assertFalse(contract['evaluation']['quality_claims_allowed'])
        self.assertEqual(contract['definition']['output_specification'], definition()['output_specification'])
        self.assertEqual(contract['output_schema']['status'], 'unconfirmed')
        self.assertIn('no_ground_truth', contract['unresolved'])
        self.assertEqual(preview['content_sha256'], sha256(contract))

    def test_hashes_stable_across_key_order_and_input_detached(self):
        value = definition()
        first = compile_task(value)
        self.assertEqual(first, compile_task(dict(reversed(list(value.items())))))
        value['samples'][0]['text'] = 'changed'
        second = compile_task(value)
        self.assertNotEqual(first['content_sha256'], second['content_sha256'])
        self.assertNotEqual(first['contract']['dataset']['sha256'], second['contract']['dataset']['sha256'])
        self.assertEqual(first['contract']['definition']['samples'][0]['text'], 'cancel my order')
        self.assertEqual(compile_task(first['contract']['definition']), first)

    def test_json_jsonl_and_inline_are_equivalent(self):
        value = definition()
        expected = compile_task(value)
        for kind, content in [('json', json.dumps(value['samples'])),
                              ('jsonl', '\n'.join(json.dumps(row) for row in value['samples']) + '\n\n')]:
            with self.subTest(kind=kind):
                value['samples'] = {'format': kind, 'content': content}
                self.assertEqual(compile_task(value), expected)

    def test_jsonl_keeps_unicode_line_separator_inside_strings(self):
        value = definition()
        value['samples'] = {'format': 'jsonl', 'content': '"a\u2028b"\n"c"'}
        self.assertEqual(compile_task(value)['contract']['definition']['samples'], ['a\u2028b', 'c'])

    def test_partial_labels_and_explicit_null_are_preserved(self):
        value = definition()
        value['labels'] = [{'sample_index': 0, 'expected_output': None}]
        contract = compile_task(value)['contract']
        self.assertEqual(contract['evaluation']['supervision_mode'], 'hybrid')
        self.assertIsNone(contract['definition']['labels'][0]['expected_output'])
        self.assertEqual(contract['evaluation']['label_source'], 'user_provided_unverified')
        value['labels'].append({'sample_index': 1, 'expected_output': {'intent': 'query'}})
        self.assertEqual(compile_task(value)['contract']['evaluation']['supervision_mode'], 'supervised')

    def test_domain_objects_are_not_implicitly_label_envelopes(self):
        value = definition()
        value['samples'] = [{'input': 'a', 'expected_output': 'domain data'}]
        self.assertEqual(compile_task(value)['contract']['evaluation']['label_count'], 0)

    def test_constraints_preferences_resources_and_budget_are_declarations(self):
        value = definition()
        value.update(constraints={'hard': ['Return valid JSON'], 'soft': ['Prefer low latency']},
                     technical_preferences=['Consider rules first'], resources=['Internal lookup API'],
                     budget={'max_cost_usd': 2.5, 'max_candidate_executions': 10})
        contract = compile_task(value)['contract']
        self.assertEqual(contract['definition']['constraints']['hard'], ['Return valid JSON'])
        self.assertEqual(contract['definition']['budget']['max_cost_usd'], 2.5)
        self.assertEqual(contract['budget_enforcement'], 'not_started')
        self.assertIn('constraints_not_enforced', contract['unresolved'])
        self.assertIn('resources_unverified', contract['unresolved'])

    def test_invalid_definitions_are_rejected(self):
        cases = [({}, 'invalid_samples'), ({'samples': []}, 'invalid_samples'),
                 ({'samples': ['x'] * 501}, 'invalid_samples'),
                 ({'output_specification': '   '}, 'invalid_specification'),
                 ({'output_specification': 'a' * 20001}, 'invalid_specification'),
                 ({'title': ''}, 'invalid_field'), ({'workflow': {}}, 'invalid_field'),
                 ({'constraints': {'hard': [1]}}, 'invalid_field'),
                 ({'technical_preferences': ['']}, 'invalid_field'),
                 ({'budget': {'max_cost_usd': True}}, 'invalid_field'),
                 ({'budget': {'max_candidate_executions': 1.5}}, 'invalid_field'),
                 ({'budget': {'max_cost_usd': -1}}, 'invalid_field'),
                 ({'resources': 'API'}, 'invalid_field')]
        for changes, code in cases:
            value = definition() if changes else {}
            value.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ContractError) as error:
                compile_task(value)
            self.assertEqual(error.exception.code, code)

    def test_invalid_labels_are_rejected(self):
        cases = [None, [{'sample_index': 0}], [{'sample_index': True, 'expected_output': 1}],
                 [{'sample_index': -1, 'expected_output': 1}], [{'sample_index': 2, 'expected_output': 1}],
                 [{'sample_index': 0, 'expected_output': 1}] * 2]
        for labels in cases:
            with self.subTest(labels=labels), self.assertRaises(ContractError):
                compile_task(dict(definition(), labels=labels))

    def test_json_abuse_and_lossy_values_are_rejected(self):
        for text in ['{"key":1,"key":2}', '[NaN]', '[Infinity]', '{', '[' * 1500]:
            with self.subTest(text=text[:50]), self.assertRaises(ContractError):
                strict_json_loads(text)
        nested = 'value'
        for _ in range(25):
            nested = [nested]
        for sample in [float('inf'), float('nan'), 9007199254740992, '\ud800', nested, {'x': set()}]:
            with self.subTest(sample=repr(sample)), self.assertRaises(ContractError):
                compile_task(dict(definition(), samples=[sample]))
        value = dict(definition(), samples={'format': 'json', 'content': '[{"x":1,"x":2}]'})
        with self.assertRaises(ContractError):
            compile_task(value)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.db'
        self.store = Store(self.path)
        self.tasks = TaskRepository(self.store)

    def save(self, value=None, **kwargs):
        value = definition() if value is None else value
        return self.tasks.save(value, compile_task(value)['content_sha256'], **kwargs)

    def test_versioned_contract_survives_restart_and_original_cannot_change(self):
        original = self.save()
        changed = dict(definition(), output_specification='Return only the intent.')
        revised = self.save(changed, task_id=original['task_id'], base_revision=1)
        restarted = TaskRepository(Store(self.path))
        self.assertEqual(restarted.get_revision(original['task_id'], 1), original)
        self.assertEqual(restarted.get_revision(original['task_id'], 2), revised)
        self.assertEqual(revised['parent_sha256'], original['content_sha256'])
        self.assertEqual(restarted.get(original['task_id'])['revision'], 2)
        self.assertEqual(len(restarted.get(original['task_id'])['revisions']), 2)
        self.assertEqual(len(restarted.list()), 1)
        for statement in ['UPDATE task_revisions SET revision=10', 'DELETE FROM task_revisions']:
            with self.store.connection() as db, self.assertRaises(sqlite3.IntegrityError):
                db.execute(statement)

    def test_changed_preview_or_missing_review_cannot_save(self):
        original = definition()
        reviewed = compile_task(original)['content_sha256']
        original['output_specification'] = 'Changed'
        for digest in [None, '', reviewed]:
            with self.subTest(digest=digest), self.assertRaises(TaskConflict):
                self.tasks.save(original, digest)
        self.assertEqual(self.tasks.list(), [])

    def test_simultaneous_revisions_have_one_winner(self):
        original = self.save()
        def revise(index):
            value = dict(definition(), title=f'Revision {index}')
            try:
                return self.tasks.save(value, compile_task(value)['content_sha256'],
                                       task_id=original['task_id'], base_revision=1)
            except TaskConflict:
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(revise, range(8)))
        self.assertEqual(sum(row is not None for row in results), 1)
        self.assertEqual(len(self.tasks.get(original['task_id'])['revisions']), 2)

    def test_migration_keeps_v1_jobs_and_events(self):
        path = Path(self.directory.name) / 'v1.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript((Path(__file__).parent / 'fixtures/schema_v1.sql').read_text())
            db.execute("INSERT INTO jobs VALUES('existing','foundation_check','queued',1,1,0,NULL,NULL,NULL,NULL)")
            db.execute("INSERT INTO events(job_id,at,type,detail) VALUES('existing',1,'queued','{}')")
        migrated = Store(path)
        self.assertEqual(migrated.get('existing')['state'], 'queued')
        self.assertEqual(migrated.get('existing')['events'][0]['type'], 'queued')
        Worker(migrated).tick()
        self.assertEqual(migrated.get('existing')['result']['checks']['schema_version'], SCHEMA_VERSION)
        value = definition()
        saved = TaskRepository(migrated).save(value, compile_task(value)['content_sha256'])
        self.assertEqual(saved['revision'], 1)

    def test_migration_failure_rolls_back_schema_and_version(self):
        path = Path(self.directory.name) / 'v1-failure.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript((Path(__file__).parent / 'fixtures/schema_v1.sql').read_text())
        original_migrate = Store.migrate
        def fail(db, script):
            original_migrate(db, script)
            raise RuntimeError('injected migration failure')
        with patch.object(Store, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            Store(path)
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='task_revisions'").fetchone())

    def test_concurrent_v1_startup_migrates_once_without_losing_data(self):
        path = Path(self.directory.name) / 'concurrent-v1.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript((Path(__file__).parent / 'fixtures/schema_v1.sql').read_text())
            db.execute("INSERT INTO jobs VALUES('keep','foundation_check','queued',1,1,0,NULL,NULL,NULL,NULL)")
        with ThreadPoolExecutor(max_workers=4) as executor:
            stores = list(executor.map(lambda _: Store(path), range(4)))
        self.assertTrue(all(store.get('keep')['state'] == 'queued' for store in stores))
        with stores[0].connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)


class TaskHTTPTests(unittest.TestCase):
    setUp = foundation.HTTPTests.setUp
    close = foundation.HTTPTests.close
    request = foundation.HTTPTests.request

    def test_preview_save_revision_and_history_via_real_http(self):
        value = definition()
        status, preview, _ = self.request('/api/tasks/preview', {'definition': value})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('/api/tasks')[1]['tasks'], [])
        status, saved, _ = self.request('/api/tasks', {'definition': value, 'reviewed_sha256': preview['content_sha256']})
        self.assertEqual(status, 201)
        path = '/api/tasks/' + saved['task_id']
        self.assertEqual(self.request(path)[1]['contract'], preview['contract'])
        value['output_specification'] = 'Return a single intent string.'
        _, second, _ = self.request('/api/tasks/preview', {'definition': value})
        revision = {'definition': value, 'reviewed_sha256': second['content_sha256'], 'base_revision': 1}
        self.assertEqual(self.request(path + '/revisions', revision)[0], 201)
        self.assertEqual(self.request(path + '/revisions', revision)[0], 409)
        self.assertEqual(self.request(path + '/revisions/1')[1], saved)
        self.assertEqual(len(self.request(path)[1]['revisions']), 2)
        self.assertEqual(self.request('/api/tasks')[1]['tasks'][0]['revision'], 2)

    def test_validation_review_guard_unknown_revision_and_origin(self):
        self.assertEqual(self.request('/api/tasks', {'definition': definition(), 'reviewed_sha256': 'wrong'})[0], 409)
        self.assertEqual(self.request('/api/tasks/preview', {'definition': {}})[0], 422)
        self.assertEqual(self.request('/api/tasks/preview', {'definition': definition(), 'ignored': True})[0], 422)
        self.assertEqual(self.request('/api/tasks/preview', {'definition': definition()}, headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/tasks/' + 'f' * 32 + '/revisions/1')[0], 404)
        self.assertEqual(self.request('/api/tasks', raw=b'{"a":1,"a":2}')[0], 400)

    def test_large_task_has_own_limit_without_weakening_job_limit(self):
        value = dict(definition(), output_specification='a' * 10000)
        self.assertEqual(self.request('/api/tasks/preview', {'definition': value})[0], 200)
        self.assertEqual(self.request('/api/tasks/preview', raw=b'a' * 262145)[0], 413)
        self.assertEqual(self.request('/api/jobs', raw=b'a' * 4097)[0], 413)


class TaskProcessTests(unittest.TestCase):
    start = foundation.ProcessTests.start
    stop_process = foundation.ProcessTests.stop_process

    def test_saved_contract_is_identical_after_actual_cli_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            store = Store(path)
            value = definition()
            saved = TaskRepository(store).save(value, compile_task(value)['content_sha256'])
            for _ in range(2):
                process, base = self.start(path)
                try:
                    with urlopen(base + '/api/tasks/' + saved['task_id'] + '/revisions/1', timeout=2) as response:
                        self.assertEqual(json.load(response), saved)
                finally:
                    self.stop_process(process)
