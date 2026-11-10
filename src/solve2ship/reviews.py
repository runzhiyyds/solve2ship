"""Append-only, self-attested output reviews; never authenticated human truth."""

import json
import re
import time
import uuid

from .contracts import canonical_json, sha256,validate_json
from .gateway import effective_report


class ReviewError(ValueError):
    pass


class ReviewRepository:
    def __init__(self, store):
        self.store = store

    def packet(self, search_id, *, final=False):
        from .search import SearchRepository
        searches = SearchRepository(self.store)
        run = searches.get(search_id)
        task = searches.tasks.get_revision(run['task_id'], run['revision'])
        criteria = run.get('criteria_bundle')
        delivery_id, report = None, None
        if final:
            with self.store.connection() as db:
                d = db.execute('SELECT id,frozen_json FROM deliveries WHERE search_id=?', (search_id,)).fetchone()
                if d is None:
                    raise ReviewError('frozen_delivery_required')
                delivery_id = d['id']; frozen = json.loads(d['frozen_json'])
                row = db.execute('SELECT report_json FROM delivery_reports WHERE delivery_id=?', (delivery_id,)).fetchone()
                if row is None:
                    raise ReviewError('final_execution_not_complete')
                report = effective_report(db, delivery_id, json.loads(row[0]))
            if report.get('quality_mode') != 'operator_review':
                raise ReviewError('operator_review_mode_required')
            if frozen.get('review_policy') != 'all_successful_final_outputs_explicitly_accepted_v1':
                raise ReviewError('review_policy_not_supported')
            criteria = frozen.get('criteria_bundle')
            candidates = {frozen['candidate']['id']: frozen['candidate']}
            rows = [{**r, 'candidate_id': frozen['candidate']['id'], 'id': None, 'split': 'final_test_once'} for r in report['observations']]
        else:
            candidates = {c['id']: c for c in run['candidates']}
            rows = [r for r in run['executions'] if r['split'] in ('validation', 'train_smoke')]
        targets = []
        for row in rows:
            candidate = candidates[row['candidate_id']]
            snapshot = {'task_content_sha256': task['content_sha256'], 'candidate_id': candidate['id'],
                        'artifact_sha256': candidate['artifact_sha256'], 'sample_index': row['sample_index'],
                        'input_sha256': sha256(task['contract']['definition']['samples'][row['sample_index']]),
                        'output_sha256': sha256(row['output']), 'error': row['error'], 'execution_id': row['id'],
                        'delivery_id': delivery_id, 'split': row['split'],
                        'criteria_schema_sha256': criteria.get('schema_sha256') if criteria else None}
            targets.append({'execution_id': row['id'], 'sample_index': row['sample_index'], 'candidate_id': candidate['id'],
                            'input': task['contract']['definition']['samples'][row['sample_index']], 'output': row['output'],
                            'error': row['error'], 'judge': row.get('judge'), 'criteria': row.get('criteria'),
                            'snapshot_sha256': sha256(snapshot), 'snapshot': snapshot, 'latest_review': None})
        with self.store.connection() as db:
            events = [dict(r) for r in db.execute('SELECT * FROM quality_review_events WHERE search_id=? AND subject_kind=? ORDER BY rowid',
                       (search_id, 'final' if final else 'validation'))]
        for event in events:
            event['review'] = json.loads(event.pop('review_json'))
        latest = {}
        for event in events:
            key = event['sample_index'] if final else event['execution_id']
            latest[key] = event
        for target in targets:
            target['latest_review'] = latest.get(target['sample_index'] if final else target['execution_id'])
        judged = [t for t in targets if t['latest_review'] and t['latest_review']['review']['verdict'] != 'unsure'
                  and t['judge'] and t['judge'].get('status') == 'scored']
        summary = {'reviewed': sum(t['latest_review'] is not None for t in targets), 'total': len(targets),
                   'source': 'self_attested_operator', 'human_identity_verified': False, 'quality_is_verified_accuracy': False,
                   'judge_agreement': {'count': len(judged), 'matches': sum((t['judge']['score'] >= .8) ==
                       (t['latest_review']['review']['verdict'] == 'accept') for t in judged),
                       'threshold': .8, 'threshold_source': 'fixed_descriptive_policy_v1', 'calibrated_for_pruning': False}}
        packet = {'search_id': search_id, 'delivery_id': delivery_id, 'subject_kind': 'final' if final else 'validation',
                'targets': targets, 'events': events, 'summary': summary,
                'closed': bool(final and report['review_state'] != 'pending'),
                'review_state': report['review_state'] if final else 'open', 'semantic_quality_verified': False}
        packet['review_evidence_sha256'] = review_fingerprint(packet) if final else None
        return packet

    @staticmethod
    def _attestation(body, keys):
        validate_json(body)
        if type(body) is not dict or body.keys() != keys or body.get('attested') is not True:
            raise ReviewError('explicit_operator_attestation_required')
        if type(body['request_id']) is not str or not re.fullmatch('[0-9a-f]{32}', body['request_id']):
            raise ReviewError('invalid_review_request')
        for field, maximum in [('reviewer', 100), ('comment', 2000)]:
            if type(body[field]) is not str or len(body[field]) > maximum or (field == 'reviewer' and not body[field].strip()):
                raise ReviewError('invalid_review_request')

    def record(self, search_id, body, *, final=False):
        field = 'sample_index' if final else 'execution_id'
        self._attestation(body, {field, 'request_id', 'snapshot_sha256', 'previous_review_id', 'verdict', 'reviewer', 'comment', 'attested'})
        if type(body[field]) is not int or body['verdict'] not in ('accept', 'reject', 'unsure'):
            raise ReviewError('invalid_review_request')
        packet = self.packet(search_id, final=final)
        target = next((t for t in packet['targets'] if t[field] == body[field]), None)
        if target is None:
            raise ReviewError('review_subject_not_available')
        if body['snapshot_sha256'] != target['snapshot_sha256']:
            raise ReviewError('review_snapshot_changed')
        if target['error'] and body['verdict'] == 'accept':
            raise ReviewError('failed_output_cannot_be_accepted')
        if body['previous_review_id'] is not None and not body['comment'].strip():
            raise ReviewError('review_revision_reason_required')
        request_hash = sha256([search_id, final, body])
        with self.store.connection(write=True) as db:
            old = db.execute('SELECT request_sha256 FROM quality_review_events WHERE request_id=?', (body['request_id'],)).fetchone()
            if old:
                if old[0] != request_hash:
                    raise ReviewError('review_request_conflict')
            else:
                if final and db.execute('SELECT delivery_id FROM delivery_quality_decisions WHERE delivery_id=?', (packet['delivery_id'],)).fetchone():
                    raise ReviewError('quality_decision_closed')
                latest = db.execute('SELECT id FROM quality_review_events WHERE search_id=? AND subject_kind=? AND '+field+'=? ORDER BY rowid DESC LIMIT 1',
                    (search_id, packet['subject_kind'], body[field])).fetchone()
                if body['previous_review_id'] != (latest[0] if latest else None):
                    raise ReviewError('review_revision_conflict')
                review = {k: body[k] for k in ('verdict', 'reviewer', 'comment', 'attested')}
                review.update(source='self_attested_operator', human_identity_verified=False, quality_is_verified_accuracy=False)
                db.execute('INSERT INTO quality_review_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, body['request_id'], request_hash, time.time(), search_id, packet['subject_kind'],
                     target['execution_id'], packet['delivery_id'], target['sample_index'], target['snapshot_sha256'],
                     body['previous_review_id'], canonical_json(review)))
        return self.packet(search_id, final=final)

    def complete(self, search_id, body):
        self._attestation(body, {'request_id', 'review_evidence_sha256', 'reviewer', 'comment', 'attested'})
        from .delivery import DeliveryRepository
        with self.store.connection(write=True) as db:
            # IMMEDIATE transaction prevents amendments while the final review snapshot is chosen.
            packet = self.packet(search_id, final=True)
            delivery_id = packet['delivery_id']
            request_hash = sha256([search_id, body])
            by_request = db.execute('SELECT delivery_id,request_sha256 FROM delivery_quality_decisions WHERE request_id=?',(body['request_id'],)).fetchone()
            if by_request and (by_request['delivery_id']!=delivery_id or by_request['request_sha256']!=request_hash):
                raise ReviewError('quality_decision_request_conflict')
            existing = db.execute('SELECT request_sha256 FROM delivery_quality_decisions WHERE delivery_id=?', (delivery_id,)).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise ReviewError('quality_decision_closed')
            else:
                fingerprint = review_fingerprint(packet)
                if body['review_evidence_sha256'] != fingerprint:
                    raise ReviewError('review_snapshot_changed')
                if not packet['targets'] or any(not t['latest_review'] or t['latest_review']['review']['verdict'] == 'unsure' for t in packet['targets']):
                    raise ReviewError('all_final_outputs_need_review')
                accepted = sum(t['error'] is None and t['latest_review']['review']['verdict'] == 'accept' for t in packet['targets'])
                decision = {'gate_passed': accepted == len(packet['targets']), 'accepted_review_count': accepted,
                            'review_evidence_sha256': fingerprint, 'source': 'self_attested_operator',
                            'human_identity_verified': False, 'quality_is_verified_accuracy': False,
                            'reviewer': body['reviewer'], 'comment': body['comment'], 'attested': True}
                db.execute('INSERT INTO delivery_quality_decisions VALUES(?,?,?,?,?)',
                           (delivery_id, body['request_id'], request_hash, time.time(), canonical_json(decision)))
        delivery = DeliveryRepository(self.store).get(delivery_id)
        if delivery['report']['gate_passed'] and delivery['state'] == 'private_draft_incomplete':
            # This transition publishes no external service and never replays the final.
            try:
                DeliveryRepository(self.store).build_private_draft(delivery_id)
            except FileExistsError:
                pass  # Another completion claimed packaging; never overwrite a partial draft.
        return DeliveryRepository(self.store).get(delivery_id)


def review_fingerprint(packet):
    return sha256({'delivery_id': packet['delivery_id'], 'reviews': [
        [t['sample_index'], t['snapshot_sha256'], t['latest_review']['id'] if t['latest_review'] else None]
        for t in packet['targets']]})
