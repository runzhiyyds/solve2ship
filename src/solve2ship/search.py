"""Real multi-family build/execute slice. No automatic paid restart or fake metrics."""

import json
import re
import threading
import time
import uuid

from .candidates import ArtifactError, builtin_registry
from .contracts import canonical_json, sha256
from .gateway import BudgetError, Ledger, ModelGateway, ProviderError, MICRO
from .model_config import load_profile
from .tasks import TaskRepository
from .evaluation import evaluate_candidates, partition_indices
from .evolution import choose_automatic_target, mutate_candidate, record_comparison
from .invocation import CallTraceGateway,invocation_runtime


TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}


class SearchError(ValueError):
    pass


class SearchRepository:
    def __init__(self, store):
        self.store, self.tasks, self.ledger = store, TaskRepository(store), Ledger(store)

    @staticmethod
    def event(db, search_id, kind, detail):
        db.execute('INSERT INTO search_events(search_id,at,type,detail_json) VALUES(?,?,?,?)',
                   (search_id, time.time(), kind, canonical_json(detail)))

    def create(self, task_id, revision, request_id, *, protocol='m3', source_search_id=None, mutation_target=None, explicit_restart=False):
        if (type(task_id) is not str or not re.fullmatch('[0-9a-f]{32}', task_id) or type(revision) is not int
                or revision < 1 or type(request_id) is not str or not re.fullmatch('[0-9a-f]{32}', request_id)):
            raise SearchError('invalid_search_request')
        task = self.tasks.get_revision(task_id, revision)
        budget = task['contract']['definition']['budget']
        now, search_id = time.time(), uuid.uuid4().hex
        with self.store.connection(write=True) as db:
            if source_search_id:
                root = self.ledger.root_scope(db, source_search_id)
                root_state = db.execute('SELECT stop_requested FROM searches WHERE id=?', (root,)).fetchone()
                if root_state and root_state[0]:
                    if not explicit_restart:
                        raise SearchError('search_cancelled')
                    db.execute('UPDATE searches SET stop_requested=0 WHERE id=?', (root,))
            existing = db.execute('SELECT * FROM searches WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing['task_id'] != task_id or existing['revision'] != revision or existing['source_search_id'] != source_search_id or existing['mutation_target'] != mutation_target:
                    raise SearchError('search_idempotency_conflict')
                search_id = existing['id']
            else:
                # Exposed final inputs cannot be reused for tuning, even after reordering
                # or changing a task's goal/labels. Fresh independent data is required.
                exposed = {row[0] for row in db.execute('SELECT input_sha256 FROM final_quarantine')}
                if any(sha256(sample) in exposed for sample in task['contract']['definition']['samples']):
                    raise SearchError('final_test_consumed_new_data_required')
                global_limit = db.execute("SELECT limit_micro FROM budget_accounts WHERE id='work'").fetchone()
                if global_limit is None:
                    raise BudgetError('work_budget_required')
                if source_search_id:
                    root = self.ledger.root_scope(db, source_search_id)
                    cny = db.execute('SELECT limit_micro FROM budget_accounts WHERE id=?', (root,)).fetchone()[0]/MICRO
                    executions = db.execute('SELECT execution_limit FROM searches WHERE id=?', (root,)).fetchone()[0]
                    budget_source = 'inherited_root_search'
                elif budget.get('mode', 'explicit') == 'inherit_work':
                    remaining = max(0, global_limit[0] - self.ledger._committed(db, None))
                    cny, executions, budget_source = min(10, remaining/MICRO), 120, 'authorized_work_budget_policy'
                else:
                    cny, executions, budget_source = budget.get('max_cost_cny', 0), budget['max_candidate_executions'], 'explicit_task_budget'
                if cny <= 0 or executions < 2:
                    raise SearchError('positive_search_budget_required')
                if cny * MICRO > global_limit[0]:
                    raise BudgetError('work_budget_required')
                repairing = protocol in ('m3-repair', 'm4-repair')
                if repairing:
                    scopes = self.ledger.tree_scopes(db, self.ledger.root_scope(db, source_search_id))
                    repairs = db.execute("SELECT COUNT(*) FROM search_events WHERE type='generation_repair_scheduled' AND search_id IN (" + ','.join('?' for _ in scopes) + ')', scopes).fetchone()[0]
                    if repairs >= 1:
                        raise SearchError('generation_repair_limit_reached')
                db.execute('INSERT INTO budget_accounts VALUES(?,?)', (search_id, int(cny * MICRO)))
                db.execute("INSERT INTO searches(id,task_id,revision,request_id,created_at,updated_at,state,protocol_version,source_search_id,mutation_target,execution_limit) VALUES(?,?,?,?,?,?,'queued',?,?,?,?)",
                           (search_id, task_id, revision, request_id, now, now, protocol, source_search_id, mutation_target, executions))
                self.event(db, search_id, 'queued', {'task_content_sha256': task['content_sha256'], 'protocol': protocol,
                           'effective_budget': {'source': budget_source, 'currency': 'CNY', 'limit_cny': cny, 'execution_limit': executions}})
                if repairing:
                    self.event(db, source_search_id, 'generation_repair_scheduled', {'child_search_id': search_id,
                               'strategy': 'new_generation_with_format_feedback', 'uncertain_billing_retried': False})
        return self.get(search_id)

    def get(self, search_id):
        with self.store.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM searches WHERE id=?', (search_id,)).fetchone()
            if row is None:
                raise KeyError('Search not found')
            result = dict(row)
            result.pop('owner'); result.pop('lease_until')
            result['candidates'] = [dict(r) for r in db.execute('SELECT * FROM candidates WHERE search_id=? ORDER BY rowid', (search_id,))]
            for candidate in result['candidates']:
                candidate['artifact'] = json.loads(candidate.pop('artifact_json'))
                candidate['provenance'] = json.loads(candidate.pop('provenance_json'))
            result['executions'] = [dict(r) for r in db.execute('SELECT * FROM executions WHERE search_id=? ORDER BY id', (search_id,))]
            for execution in result['executions']:
                raw = execution.pop('output_json')
                execution['output'] = json.loads(raw) if raw is not None else None
                raw_judge = execution.pop('judge_json')
                execution['judge'] = json.loads(raw_judge) if raw_judge else None
                raw_runtime = execution.pop('runtime_json')
                execution['runtime'] = json.loads(raw_runtime) if raw_runtime else None
                raw_criteria = execution.pop('criteria_json')
                execution['criteria'] = json.loads(raw_criteria) if raw_criteria else None
            result['events'] = [dict(r) for r in db.execute('SELECT * FROM search_events WHERE search_id=? ORDER BY sequence', (search_id,))]
            for event in result['events']:
                event['detail'] = json.loads(event.pop('detail_json'))
            result['partitions'] = [dict(r) for r in db.execute('SELECT sample_index,split FROM partitions WHERE search_id=? ORDER BY sample_index', (search_id,))]
            result['decisions'] = [dict(r) for r in db.execute('SELECT * FROM candidate_decisions WHERE search_id=? ORDER BY id', (search_id,))]
            for decision in result['decisions']:
                decision['metrics'] = json.loads(decision.pop('metrics_json'))
            selection = db.execute('SELECT * FROM selections WHERE search_id=?', (search_id,)).fetchone()
            result['selection'] = None if selection is None else {'candidate_id': selection['candidate_id'], 'evidence': json.loads(selection['evidence_json'])}
            result['mutations'] = [dict(row) for row in db.execute('SELECT * FROM mutations WHERE search_id=?', (search_id,))]
            delivery = db.execute('SELECT id FROM deliveries WHERE search_id=?', (search_id,)).fetchone()
            result['delivery_id'] = delivery[0] if delivery else None
            from .criteria import get_bundle
            result['criteria_bundle'] = get_bundle(self, search_id)
        result['budget'] = self.ledger.status(search_id)
        result['scope_note'] = 'Real execution; provided-label agreement and uncalibrated judges are not verified accuracy. Selection evidence describes the search-time state; consult delivery for later final checks. No automatic public service release.'
        return result

    def reactivate(self, search_id, candidate_id, request_id):
        source = self.get(search_id)
        if source['state'] not in TERMINAL or not any(c['id'] == candidate_id for c in source['candidates']):
            raise SearchError('invalid_reactivation')
        # New run re-executes all frozen competitors, including the requested candidate.
        # No old score/decision is overwritten and no new planner call is needed.
        return self.create(source['task_id'], source['revision'], request_id, source_search_id=search_id, explicit_restart=True)

    def lineage(self, search_id):
        self.get(search_id)
        with self.store.connection() as db:
            db.execute('BEGIN')
            root = self.ledger.root_scope(db, search_id)
            scopes = self.ledger.tree_scopes(db, root)
            marks = ','.join('?' for _ in scopes)
            rows = db.execute('SELECT id,search_id,family,name,artifact_sha256,provenance_json FROM candidates WHERE search_id IN ('+marks+') ORDER BY rowid LIMIT 1001', scopes).fetchall()
            nodes = []
            for row in rows[:1000]:
                node = dict(row); provenance = json.loads(node.pop('provenance_json'))
                node['parent_candidate_id'] = provenance.get('parent_candidate_id')
                node['revision'] = provenance.get('revision', 1)
                nodes.append(node)
            mapping = {node['id']: node for node in nodes}
            edges = [{'parent': node['parent_candidate_id'], 'child': node['id'],
                      'kind': 'changed' if mapping[node['parent_candidate_id']]['artifact_sha256'] != node['artifact_sha256'] else 'retested'}
                     for node in nodes if node['parent_candidate_id'] in mapping]
            return {'root_search_id': root, 'nodes': nodes, 'edges': edges, 'truncated': len(rows)>1000}

    def evolve(self, search_id, candidate_id, request_id, *, automatic=False):
        source = self.get(search_id)
        if source['state'] not in TERMINAL or not any(c['id'] == candidate_id for c in source['candidates']):
            raise SearchError('invalid_evolution')
        task = self.tasks.get_revision(source['task_id'], source['revision'])['contract']
        split = partition_indices(task)
        cases = min(10, len(split['validation'] or split['train']))
        remaining = source['execution_limit'] - self.tree_execution_count(search_id)
        if remaining < cases * (len(source['candidates'])+1):
            raise SearchError('insufficient_full_revalidation_budget')
        return self.create(source['task_id'], source['revision'], request_id, protocol='m4', source_search_id=search_id,
                           mutation_target=candidate_id, explicit_restart=not automatic)

    def rollback(self, search_id, request_id):
        source = self.get(search_id)
        if source['state'] not in TERMINAL or not source['selection'] or not source['selection']['candidate_id']:
            raise SearchError('no_archived_champion')
        return self.create(source['task_id'], source['revision'], request_id, protocol='rollback', source_search_id=search_id,
                           mutation_target=source['selection']['candidate_id'], explicit_restart=True)

    def tree_execution_count(self, search_id):
        with self.store.connection() as db:
            root = self.ledger.root_scope(db, search_id)
            return db.execute('SELECT COUNT(*) FROM execution_slots WHERE root_id=?', (root,)).fetchone()[0]

    def reserve_execution(self, search_id, candidate_id, index):
        with self.store.connection(write=True) as db:
            state = db.execute('SELECT state FROM searches WHERE id=?', (search_id,)).fetchone()
            if state is None or state[0] != 'running':
                raise BudgetError('search_not_active')
            root = self.ledger.root_scope(db, search_id)
            limit = db.execute('SELECT execution_limit FROM searches WHERE id=?', (root,)).fetchone()[0]
            used = db.execute('SELECT COUNT(*) FROM execution_slots WHERE root_id=?', (root,)).fetchone()[0]
            if used >= limit:
                raise BudgetError('execution_budget_exhausted')
            db.execute('INSERT INTO execution_slots VALUES(?,?,?,?)', (search_id, candidate_id, index, root))

    def list(self, task_id=None):
        with self.store.connection() as db:
            rows = db.execute('SELECT id,task_id,revision,created_at,updated_at,state,error,protocol_version,source_search_id FROM searches' +
                              (' WHERE task_id=?' if task_id else '') + ' ORDER BY created_at DESC LIMIT 100',
                              (task_id,) if task_id else ())
            return [dict(row) for row in rows]

    def cancel(self, search_id):
        with self.store.connection(write=True) as db:
            row = db.execute('SELECT state FROM searches WHERE id=?', (search_id,)).fetchone()
            if row is None:
                raise KeyError('Search not found')
            root = self.ledger.root_scope(db, search_id)
            db.execute('UPDATE searches SET stop_requested=1 WHERE id=?', (root,))
            for member in self.ledger.tree_scopes(db, root):
                changed = db.execute("UPDATE searches SET state='cancelled',owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND state IN ('queued','running')", (time.time(), member)).rowcount
                if changed:
                    self.event(db, member, 'cancelled', {'inflight_calls_may_still_be_billed': True, 'root_search_id': root})
        return self.get(search_id)

    def claim(self):
        now = time.time()
        with self.store.connection(write=True) as db:
            for row in db.execute("SELECT id FROM searches WHERE state='running' AND lease_until<=?", (now,)).fetchall():
                db.execute("UPDATE searches SET state='interrupted',owner=NULL,lease_until=NULL,error='interrupted_no_automatic_paid_retry',updated_at=? WHERE id=?", (now, row['id']))
                self.event(db, row['id'], 'interrupted', {'billing_reservations_retained': True})
            row = db.execute("SELECT id FROM searches WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            owner = uuid.uuid4().hex
            db.execute("UPDATE searches SET state='running',owner=?,lease_until=?,updated_at=? WHERE id=?", (owner, now + 120, now, row['id']))
            self.event(db, row['id'], 'started', {})
            return row['id'], owner

    def active(self, search_id, owner):
        with self.store.connection() as db:
            return db.execute("SELECT 1 FROM searches WHERE id=? AND owner=? AND state='running' AND lease_until>?",
                              (search_id, owner, time.time())).fetchone() is not None

    def heartbeat(self, search_id, owner):
        with self.store.connection(write=True) as db:
            db.execute("UPDATE searches SET lease_until=? WHERE id=? AND owner=? AND state='running' AND lease_until>?",
                       (time.time() + 120, search_id, owner, time.time()))

    def finish(self, search_id, owner, error=None):
        with self.store.connection(write=True) as db:
            state = 'failed' if error else 'succeeded'
            changed = db.execute("UPDATE searches SET state=?,error=?,owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND owner=? AND state='running' AND lease_until>?",
                                 (state, error, time.time(), search_id, owner, time.time())).rowcount
            if changed:
                self.event(db, search_id, state, {'error': error})

    def add_candidate(self, search_id, candidate, provenance, *, candidate_id=None):
        candidate_id = candidate_id or uuid.uuid4().hex
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?)', (candidate_id, search_id, candidate['family'], candidate['name'],
                       candidate['rationale'], canonical_json(candidate['artifact']), sha256(candidate['artifact']), canonical_json(provenance)))
            self.event(db, search_id, 'candidate_built', {'candidate_id': candidate_id, 'family': candidate['family']})
        return candidate_id

    def build_candidate(self,search_id,candidate,provenance,registry,contract):
        adapter=registry.adapters[candidate['family']]
        candidate_id=uuid.uuid4().hex
        if hasattr(adapter,'build'):
            self.reserve_execution(search_id,candidate_id,-1)
            definition=contract['definition'];labels={row['sample_index']:row['expected_output'] for row in definition['labels']}
            training=[{'sample_index':index,'input':definition['samples'][index],
                       **({'expected_output':labels[index]} if index in labels else {})} for index in partition_indices(contract)['train']]
            try:
                artifact,evidence=adapter.build(candidate['artifact'],training)
                candidate={**candidate,'artifact':artifact};registry.validate(candidate)
                provenance={**provenance,'build':evidence}
                with self.store.connection(write=True) as db:
                    self.event(db,search_id,'candidate_build_succeeded',{'candidate_id':candidate_id,'family':candidate['family'],'build':evidence})
            except ArtifactError as error:
                with self.store.connection(write=True) as db:
                    self.event(db,search_id,'candidate_build_failed',{'candidate_id':candidate_id,'family':candidate['family'],'error':str(error),
                        'runtime':adapter.runtime_receipt() if hasattr(adapter,'runtime_receipt') else None})
                raise
        return self.add_candidate(search_id,candidate,provenance,candidate_id=candidate_id),candidate

    def add_execution(self, search_id, candidate_id, index, output, error, latency, call_id, agreement, *, fidelity='smoke', split='exploration', judge=None, runtime=None, criteria=None):
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO executions(search_id,candidate_id,sample_index,output_json,error,latency_ms,call_id,label_agreement,fidelity,split,judge_json,runtime_json,criteria_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (search_id, candidate_id, index, canonical_json(output) if error is None else None, error, latency, call_id, agreement,
                        fidelity, split, canonical_json(judge) if judge else None, canonical_json(runtime) if runtime else None, canonical_json(criteria) if criteria else None))


def execute_search(repository, search_id, owner, gateway, registry=None, stop=None):
    registry = registry or builtin_registry()
    stop = stop or threading.Event()
    record = repository.get(search_id)
    task = repository.tasks.get_revision(record['task_id'], record['revision'])
    contract = task['contract']
    definition = contract['definition']
    from .criteria import prepare_bundle
    prepare_bundle(repository, record, contract, gateway)
    if record['protocol_version'] != 'm2':
        splits = partition_indices(contract)
        with repository.store.connection(write=True) as db:
            for role, indices in splits.items():
                db.executemany('INSERT INTO partitions VALUES(?,?,?)', [(search_id, index, role) for index in indices])
            repository.event(db, search_id, 'evaluation_protocol', {'version': 'split-v1', 'splits': splits,
                             'final_test_sealed': True, 'label_correctness_verified': False})
    if record['source_search_id'] and record['protocol_version'] != 'm3-repair':
        previous = repository.get(record['source_search_id'])
        if record['protocol_version'] == 'm4-repair':
            for _ in range(16):
                if any(candidate['id'] == record['mutation_target'] for candidate in previous['candidates']):
                    break
                if not previous['source_search_id']:
                    raise ArtifactError('mutation_parent_unavailable')
                previous = repository.get(previous['source_search_id'])
        built = []
        for original in previous['candidates']:
            if record['protocol_version'] == 'rollback' and original['id'] != record['mutation_target']:
                continue
            candidate = {key: original[key] for key in ('family', 'name', 'rationale', 'artifact')}
            registry.validate(candidate)
            candidate_id = repository.add_candidate(search_id, candidate, {'source': 'reactivated_frozen_artifact',
                         'parent_candidate_id': original['id'], 'source_search_id': previous['id'],
                         'revision': original['provenance'].get('revision', 1),
                         'license_review': 'not_approved_for_distribution'})
            built.append((candidate_id, candidate))
        if record['protocol_version'] in ('m4','m4-repair'):
            parent = next(candidate for candidate in previous['candidates'] if candidate['id'] == record['mutation_target'])
            built.append(mutate_candidate(repository, search_id, previous, parent, contract, gateway, registry,
                         format_repair=record['protocol_version'] == 'm4-repair'))
            if stop.is_set() or not repository.active(search_id, owner):
                return
        evaluate_candidates(repository, search_id, owner, built, contract, gateway, registry, stop)
        if record['protocol_version'] in ('m4','m4-repair'):
            record_comparison(repository, search_id, previous)
        return
    system = ('You are the Solve2Ship solution planner. Build 2 or 3 executable candidates from at least 2 different registered families. '
              'Choose approaches based on the task; no user workflow is required. Return JSON only: '
              'Use the language of the task specification for human-readable names and rationale; keep artifact schema keys unchanged. '
              '{"candidates":[{"family":string,"name":string,"rationale":string,"artifact":object}]}. '
              'Use only exact registered artifact schemas. Program text is allowed only for an explicitly registered isolated-code adapter. Never add unregistered host tools. Registered adapters: ' + canonical_json(registry.describe()))
    context = {'output_specification': definition['output_specification'], 'samples': definition['samples'][:20],
               'labels': [label for label in definition['labels'] if label['sample_index'] < 20],
               'constraints': definition['constraints'], 'technical_preferences': definition['technical_preferences']}
    if record['protocol_version'] != 'm2':
        labels = {row['sample_index']: row['expected_output'] for row in definition['labels']}
        context.pop('samples'); context.pop('labels')
        context['training_examples'] = [{'sample_index': index, 'input': definition['samples'][index],
             **({'expected_output': labels[index], 'label_source': 'provided_unverified'} if index in labels else {})} for index in splits['train'][:20]]
        if record['protocol_version'] == 'm3-repair':
            context['format_repair_feedback'] = 'The previous distinct generation was rejected. Produce one valid JSON object with unique keys, exact registered schemas and no markdown.'
    plan, planner_call = gateway.json(system, canonical_json(context), purpose='candidate_planner', max_tokens=4096)
    if not repository.active(search_id, owner) or stop.is_set():
        return
    with repository.store.connection(write=True) as db:
        repository.event(db, search_id, 'planner_output', {'plan': plan, 'planner_call_id': planner_call})
    candidates = plan.get('candidates') if type(plan) is dict else None
    if type(candidates) is not list or not 2 <= len(candidates) <= 3:
        raise ArtifactError('invalid_planner_output')
    accepted = []
    for candidate in candidates:
        try:
            registry.validate(candidate)
            accepted.append(candidate)
        except (ArtifactError, ValueError, TypeError):
            with repository.store.connection(write=True) as db:
                repository.event(db, search_id, 'artifact_rejected', {'candidate': candidate, 'reason': 'schema_validation_failed'})
    candidates = accepted
    if len({c['family'] for c in candidates}) < 2:
        raise ArtifactError('insufficient_candidate_diversity')
    built=[]
    for candidate in candidates:
        try:
            built.append(repository.build_candidate(search_id,candidate,{'source':'model_generated_artifact',
                'planner_call_id':planner_call,'task_content_sha256':task['content_sha256'],'license_review':'not_approved_for_distribution'},registry,contract))
        except ArtifactError:
            continue
    if len({candidate['family'] for _,candidate in built})<2:
        raise ArtifactError('insufficient_built_candidate_diversity')
    if record['protocol_version'] != 'm2':
        evaluate_candidates(repository, search_id, owner, built, contract, gateway, registry, stop)
        return
    maximum = definition['budget']['max_candidate_executions']
    sample_count = min(3, len(definition['samples']), maximum // len(candidates))
    if sample_count < 1:
        raise SearchError('execution_budget_insufficient')
    labels = {row['sample_index']: row['expected_output'] for row in definition['labels']}
    successful_candidates = set()
    for candidate_id, candidate in built:
        for index, sample in enumerate(definition['samples'][:sample_count]):
            if stop.is_set() or not repository.active(search_id, owner):
                return
            started = time.perf_counter()
            repository.reserve_execution(search_id, candidate_id, index)
            output, call_id, error, agreement = None, None, None, None
            calls=CallTraceGateway(gateway);adapter=registry.adapters[candidate['family']]
            try:
                output, call_id = adapter.execute(candidate['artifact'], sample, calls)
                calls.remember(call_id)
                agreement = int(canonical_json(output) == canonical_json(labels[index])) if index in labels else None
                successful_candidates.add(candidate_id)
            except ProviderError as exception:
                error, call_id = exception.code, exception.call_id
            except BudgetError:
                error = 'budget_exhausted'
            except (ArtifactError, ValueError, TypeError, KeyError):
                error = 'candidate_execution_failed'
            repository.add_execution(search_id, candidate_id, index, output, error,
                                     (time.perf_counter() - started) * 1000, call_id, agreement,runtime=invocation_runtime(adapter,calls.call_ids))
    if len(successful_candidates) < len(built):
        raise SearchError('insufficient_successful_executions')
    repository.finish(search_id, owner)


class SearchWorker:
    def __init__(self, store, profile_path):
        self.repository, self.profile_path = SearchRepository(store), profile_path
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='sts-search-worker', daemon=True)
        from .docker_runtime import configured_sandbox
        from .contracts import SandboxUnavailable
        try:
            sandbox = configured_sandbox(store.path.parent/'sandbox.json')
            if sandbox is not None:
                sandbox.reap_stale()
        except (SandboxUnavailable,OSError,ValueError):
            pass  # Missing/offline Docker never falls back to host execution.

    def tick(self):
        claim = self.repository.claim()
        if claim is None:
            return False
        search_id, owner = claim
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(10):
                self.repository.heartbeat(search_id, owner)
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            profile = load_profile(self.profile_path)
            gateway = ModelGateway(profile, self.repository.ledger, search_id)
            from .docker_runtime import configured_sandbox
            from .contracts import SandboxUnavailable
            try:
                sandbox = configured_sandbox(self.repository.store.path.parent/'sandbox.json')
            except (SandboxUnavailable,OSError,ValueError):
                sandbox=None
            sandbox_status='not_configured'
            if sandbox is not None:
                sandbox.stop = self.stop
                from .contracts import SandboxUnavailable
                try:
                    facts=sandbox.engine.status()
                    if facts['engine_version']!=sandbox.accepted_engine_version:
                        raise SandboxUnavailable('engine_review_required')
                    sandbox_status='available_reviewed_local_docker'
                except SandboxUnavailable:
                    sandbox=None;sandbox_status='configured_but_unavailable_or_changed'
            registry=builtin_registry(sandbox=sandbox,step_guard=lambda:self.repository.active(search_id,owner) and not self.stop.is_set())
            from .sklearn_adapter import configured_ml_sandbox,eligible_numeric_task,SklearnAdapter
            try:
                ml=configured_ml_sandbox(self.repository.store.path.parent/'ml-runtime.json')
            except (SandboxUnavailable,OSError,ValueError):
                ml=None
            task=self.repository.tasks.get_revision(self.repository.get(search_id)['task_id'],self.repository.get(search_id)['revision'])
            if ml is not None and eligible_numeric_task(task['contract']):
                from .contracts import SandboxUnavailable
                try:
                    if ml.engine.status()['engine_version']!=ml.accepted_engine_version:raise SandboxUnavailable('ml_engine_review_required')
                    ml.stop=self.stop;registry.register(SklearnAdapter(ml))
                except SandboxUnavailable:pass
            with self.repository.store.connection(write=True) as db:
                self.repository.event(db,search_id,'runtime_capabilities',{'registered_families':list(registry.adapters),
                    'sandbox_status':sandbox_status,'generated_code_host_fallback':False})
            execute_search(self.repository, search_id, owner, gateway, registry=registry, stop=self.stop)
            completed = self.repository.get(search_id)
            target = choose_automatic_target(self.repository, completed)
            if target and not self.stop.is_set():
                request_id = sha256(['auto-evolve-v1', search_id, target])[:32]
                child = self.repository.evolve(search_id, target, request_id, automatic=True)
                with self.repository.store.connection(write=True) as db:
                    self.repository.event(db, search_id, 'evolution_scheduled', {'child_search_id': child['id'], 'target_candidate_id': target})
        except ProviderError as error:
            self.repository.finish(search_id, owner, error.code)
            self.repair_known_generation(search_id, error.code)
        except BudgetError:
            self.repository.finish(search_id, owner, 'budget_exhausted_or_price_review_required')
        except ArtifactError as error:
            self.repository.finish(search_id, owner, str(error))
            self.repair_known_generation(search_id, str(error))
        except Exception:
            self.repository.finish(search_id, owner, 'search_failed')
            with self.repository.store.connection(write=True) as db:
                self.repository.event(db, search_id, 'worker_error', {'code': 'search_or_automatic_policy_failed', 'details_redacted': True})
        finally:
            heartbeat_stop.set(); thread.join(timeout=1)
        return True

    def repair_known_generation(self, search_id, code):
        allowed = {'provider_invalid_response','provider_incomplete_output','invalid_planner_output','invalid_mutation_response',
                   'invalid_rule_artifact','invalid_prompt_artifact','invalid_hybrid_artifact','insufficient_candidate_diversity','invalid_candidate','invalid_rule'}
        if code not in allowed or self.stop.is_set():
            return
        record = self.repository.get(search_id)
        if record['protocol_version'] not in ('m3','m4') or not record['budget']['calls'] or any(call['state'] != 'settled' for call in record['budget']['calls']):
            return
        try:
            self.repository.create(record['task_id'],record['revision'],sha256(['format-repair-v1',search_id])[:32],
                protocol=record['protocol_version']+'-repair',source_search_id=search_id,mutation_target=record['mutation_target'])
        except (SearchError,BudgetError):
            return

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                # Preserve reservations and lease; never silently retry a paid search.
                pass
            self.stop.wait(0.3)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
