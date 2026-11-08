"""Evidence-guided artifact edits, immutable diffs, paired validation comparisons."""

import difflib
import json

from .candidates import ArtifactError
from .contracts import canonical_json, sha256
from .invocation import invocation_call_ids


def mutate_candidate(repository, search_id, source, parent, contract, gateway, registry, *, format_repair=False):
    labels = {row['sample_index']: row['expected_output'] for row in contract['definition']['labels']}
    rows = [row for row in source['executions'] if row['candidate_id'] == parent['id'] and row['split'] != 'final_test']
    bad = [row for row in rows if row['error'] or row['label_agreement'] == 0 or (row.get('judge') and row['judge'].get('score', 1) < 0.95)]
    evidence = []
    for row in (bad or rows)[:5]:
        index = row['sample_index']
        evidence.append({'sample_index': index, 'input': contract['definition']['samples'][index], 'prior_output': row['output'],
                         'error': row['error'], 'expected_output': labels.get(index), 'label_present': index in labels,
                         'judge': row.get('judge'), 'source': 'validation_feedback_not_final_test'})
    adapter=registry.adapters[parent['family']]
    editable=adapter.editable_artifact(parent['artifact']) if hasattr(adapter,'editable_artifact') else parent['artifact']
    context = {'goal': contract['definition']['output_specification'], 'constraints': contract['definition']['constraints'],
               'parent_candidate': {key: parent[key] for key in ('family', 'name', 'artifact')},
               'feedback': evidence, 'adapter': registry.describe()[parent['family']]}
    context['parent_candidate']['artifact']=editable
    if format_repair:
        context['format_repair_feedback'] = 'A prior generation was rejected. Return one syntactically valid JSON object with unique keys, exact schema and no markdown. This is a new generation, not an HTTP retry.'
    response, call_id = gateway.json(
        'Improve this executable artifact using the supplied validation feedback. Do not claim an unmeasured gain. '
        'Use the language of the goal for human-readable names and rationale; keep artifact schema keys unchanged. '
        'Keep the same family. Return one complete JSON object with unique keys, escaped string quotes and no markdown. Return JSON {"candidate":{"family":string,"name":string,"rationale":string,"artifact":object},'
        '"mutation_reason":string}. Respect the exact registered adapter schema; program text is allowed only for an explicitly registered isolated-code adapter. Never add host tools.',
        canonical_json(context), purpose='candidate_mutator', max_tokens=4096)
    with repository.store.connection(write=True) as db:
        repository.event(db, search_id, 'mutation_proposal', {'response': response, 'call_id': call_id,
                                                           'source': 'unverified_model_hypothesis', 'final_test_exposed': False})
    if type(response) is not dict or type(response.get('mutation_reason')) is not str:
        raise ArtifactError('invalid_mutation_response')
    candidate = response.get('candidate')
    registry.validate(candidate)
    proposed=adapter.editable_artifact(candidate['artifact']) if hasattr(adapter,'editable_artifact') else candidate['artifact']
    if candidate['family'] != parent['family'] or sha256(proposed) == sha256(editable):
        raise ArtifactError('invalid_or_noop_mutation')
    child_id,candidate = repository.build_candidate(search_id, candidate, {'source': 'model_generated_mutation', 'parent_candidate_id': parent['id'],
          'mutation_call_id': call_id, 'revision': parent['provenance'].get('revision', 1)+1,
          'license_review': 'not_approved_for_distribution'},registry,contract)
    before = json.dumps(parent['artifact'], ensure_ascii=False, sort_keys=True, indent=2).splitlines(keepends=True)
    after = json.dumps(candidate['artifact'], ensure_ascii=False, sort_keys=True, indent=2).splitlines(keepends=True)
    diff = ''.join(difflib.unified_diff(before, after, fromfile=parent['artifact_sha256'], tofile=sha256(candidate['artifact'])))
    with repository.store.connection(write=True) as db:
        db.execute('INSERT INTO mutations VALUES(?,?,?,?,?,?,?)', (search_id, parent['id'], child_id, response['mutation_reason'][:4000],
                    diff, parent['artifact_sha256'], sha256(candidate['artifact'])))
    return child_id, candidate


def record_comparison(repository, search_id, source):
    current = repository.get(search_id)
    for mutation in current['mutations']:
        before = {row['sample_index']: row for row in source['executions'] if row['candidate_id'] == mutation['parent_candidate_id']}
        after = {row['sample_index']: row for row in current['executions'] if row['candidate_id'] == mutation['child_candidate_id']}
        paired = sorted(index for index in before.keys() & after.keys()
                        if before[index]['label_agreement'] is not None and after[index]['label_agreement'] is not None)
        old = sum(before[index]['label_agreement'] for index in paired)
        new = sum(after[index]['label_agreement'] for index in paired)
        with repository.store.connection(write=True) as db:
            repository.event(db, search_id, 'mutation_comparison', {'parent_candidate_id': mutation['parent_candidate_id'],
                'child_candidate_id': mutation['child_candidate_id'], 'paired_validation_indices': paired,
                'provided_label_matches_before': old if paired else None, 'provided_label_matches_after': new if paired else None,
                'observed_match_delta': new-old if paired else None, 'regression_observed': new < old if paired else None,
                'prior_champion_archived': source['selection']['candidate_id'] if source['selection'] else None,
                'generalization_gain_claimed': False, 'final_test_used': False})


def choose_automatic_target(repository, record):
    if record['state'] != 'succeeded' or record['protocol_version'] not in ('m3', 'm4','m3-repair','m4-repair') or not record['selection']:
        return None
    if any(row['state'] != 'settled' for row in record['budget']['calls']):
        return None
    with repository.store.connection() as db:
        root = repository.ledger.root_scope(db, record['id'])
        scopes = repository.ledger.tree_scopes(db, root)
        if db.execute('SELECT stop_requested FROM searches WHERE id=?', (root,)).fetchone()[0]:
            return None
        count = db.execute('SELECT COUNT(*) FROM mutations WHERE search_id IN (' + ','.join('?' for _ in scopes) + ')', scopes).fetchone()[0]
    if count >= 2:
        return None
    task = repository.tasks.get_revision(record['task_id'], record['revision'])['contract']
    remaining = record['execution_limit'] - repository.tree_execution_count(record['id'])
    from .evaluation import partition_indices
    split = partition_indices(task)
    if remaining < min(10, len(split['validation'] or split['train']))*(len(record['candidates'])+1):
        return None
    winner = record['selection']['candidate_id']
    winner_rows = [row for row in record['executions'] if row['candidate_id'] == winner]
    if winner_rows and all(row['error'] is None and not invocation_call_ids(row) and
                           (row['label_agreement'] == 1 or (row.get('judge') and row['judge'].get('score', 0) >= 0.99)) for row in winner_rows):
        return None
    choices = []
    for candidate in record['candidates']:
        rows = [row for row in record['executions'] if row['candidate_id'] == candidate['id']]
        bad = sum(bool(row['label_agreement'] == 0 or (row.get('judge') and row['judge'].get('score', 1) < 0.8)) for row in rows)
        if bad and not any(row['error'] for row in rows):
            choices.append((bad, candidate['id']))
    return max(choices)[1] if choices else None
