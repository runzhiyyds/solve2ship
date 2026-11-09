"""DeepSeek-compatible HTTP adapter and persistent, conservative CNY reservations."""

import json
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener,ProxyHandler
from urllib.parse import urlsplit

from .contracts import sha256, strict_json_loads, validate_json


# Verified against DeepSeek official pricing on 2026-11-09. Micro-CNY per token,
# peak cache-miss input/output. Actual provider billing can be lower; this is a ceiling estimate.
INPUT_RATE = 2
OUTPUT_RATE = 8
CONTEXT_CEILING = 1048576
MICRO = 1000000
PRICING_VERIFIED_DATE = '2026-11-09'
PRICING_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'


def pricing_snapshot():
    return {'model': 'deepseek-flash', 'verified_date': PRICING_VERIFIED_DATE,
            'documented_model_version': 'DeepSeek-V4.1-Flash-1017',
            'currency': 'CNY', 'input_micro_per_token': INPUT_RATE,
            'output_micro_per_token': OUTPUT_RATE, 'context_ceiling': CONTEXT_CEILING,
            'source': PRICING_SOURCE}


class BudgetError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    def __init__(self, code, call_id=None):
        self.code, self.call_id = code, call_id
        super().__init__(code)


class Ledger:
    def __init__(self, store):
        self.store = store

    def configure(self, account, limit_micro):
        if type(limit_micro) is not int or limit_micro < 0:
            raise BudgetError('invalid_budget')
        with self.store.connection(write=True) as db:
            committed = self._committed(db, None if account == 'work' else account)
            if limit_micro < committed:
                raise BudgetError('budget_below_committed')
            db.execute('INSERT INTO budget_accounts VALUES(?,?) ON CONFLICT(id) DO UPDATE SET limit_micro=excluded.limit_micro',
                       (account, limit_micro))

    @staticmethod
    def _committed(db, scope):
        query = 'SELECT COALESCE(SUM(COALESCE(charged_micro,reserved_micro)),0) FROM model_calls'
        calls=db.execute(query + (' WHERE scope=?' if scope is not None else ''), (scope,) if scope is not None else ()).fetchone()[0]
        grants=db.execute('SELECT COALESCE(SUM(amount_micro),0) FROM service_budget_grants').fetchone()[0] if scope is None else 0
        return calls+grants

    @staticmethod
    def root_scope(db, scope):
        if scope and scope.startswith('delivery:'):
            delivery=db.execute('SELECT search_id FROM deliveries WHERE id=?',(scope.removeprefix('delivery:'),)).fetchone()
            if delivery:return Ledger.root_scope(db,delivery[0])
        row = db.execute('''WITH RECURSIVE ancestors(id,source_search_id) AS (
            SELECT id,source_search_id FROM searches WHERE id=? UNION ALL
            SELECT s.id,s.source_search_id FROM searches s JOIN ancestors a ON s.id=a.source_search_id)
            SELECT id FROM ancestors WHERE source_search_id IS NULL''', (scope,)).fetchone()
        return row[0] if row else scope

    @staticmethod
    def tree_scopes(db, root):
        scopes=[row[0] for row in db.execute('''WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL
            SELECT s.id FROM searches s JOIN tree ON s.source_search_id=tree.id) SELECT id FROM tree''', (root,))]
        scopes += ['delivery:'+row[0] for row in db.execute('SELECT id FROM deliveries WHERE search_id IN ('+','.join('?' for _ in scopes)+')',scopes)]
        return scopes

    def tree_committed(self, db, root):
        scopes = self.tree_scopes(db, root)
        calls=db.execute('SELECT COALESCE(SUM(COALESCE(charged_micro,reserved_micro)),0) FROM model_calls WHERE scope IN (' +
                          ','.join('?' for _ in scopes) + ')', scopes).fetchone()[0]
        return calls+db.execute('SELECT COALESCE(SUM(amount_micro),0) FROM service_budget_grants WHERE root_id=?',(root,)).fetchone()[0]

    def grant_service(self,delivery_id,amount_micro):
        if type(amount_micro) is not int or amount_micro<=0:raise BudgetError('invalid_service_allowance')
        with self.store.connection(write=True) as db:
            delivery=db.execute('SELECT d.search_id,r.report_json FROM deliveries d JOIN delivery_reports r ON r.delivery_id=d.id WHERE d.id=?',(delivery_id,)).fetchone()
            if delivery is None or not json.loads(delivery['report_json'])['gate_passed']:raise BudgetError('final_gate_required')
            existing=db.execute('SELECT * FROM service_budget_grants WHERE delivery_id=?',(delivery_id,)).fetchone()
            if existing:
                if existing['amount_micro']!=amount_micro:raise BudgetError('service_allowance_conflict')
                return dict(existing)
            root=self.root_scope(db,delivery['search_id'])
            for account,used in [('work',self._committed(db,None)),(root,self.tree_committed(db,root))]:
                limit=db.execute('SELECT limit_micro FROM budget_accounts WHERE id=?',(account,)).fetchone()
                if limit is None or used+amount_micro>limit[0]:raise BudgetError('service_allowance_exceeds_budget')
            grant={'id':uuid.uuid4().hex,'delivery_id':delivery_id,'root_id':root,'created_at':time.time(),'amount_micro':amount_micro}
            db.execute('INSERT INTO service_budget_grants VALUES(:id,:delivery_id,:root_id,:created_at,:amount_micro)',grant)
            return grant

    def reserve(self, scope, purpose, model, max_tokens):
        # Billing is enabled only for the model with a verified price snapshot.
        if model != 'deepseek-flash' or time.strftime('%Y-%m-%d', time.gmtime()) != PRICING_VERIFIED_DATE:
            raise BudgetError('price_snapshot_requires_review')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise BudgetError('invalid_token_limit')
        amount = CONTEXT_CEILING * INPUT_RATE + max_tokens * OUTPUT_RATE
        call_id = uuid.uuid4().hex
        with self.store.connection(write=True) as db:
            if scope.startswith('delivery:'):
                row=db.execute('SELECT d.id,r.delivery_id FROM deliveries d LEFT JOIN delivery_reports r ON r.delivery_id=d.id WHERE d.id=?',(scope.removeprefix('delivery:'),)).fetchone()
                if row is None or row[1] is not None:raise BudgetError('delivery_not_active')
            search = db.execute('SELECT state,lease_until FROM searches WHERE id=?', (scope,)).fetchone()
            if search is not None and (search['state'] != 'running' or search['lease_until'] is None or search['lease_until'] <= time.time()):
                raise BudgetError('search_not_active')
            for account in ('work', scope):
                row = db.execute('SELECT limit_micro FROM budget_accounts WHERE id=?', (account,)).fetchone()
                if row is None or self._committed(db, None if account == 'work' else scope) + amount > row[0]:
                    raise BudgetError('budget_exhausted')
            root = self.root_scope(db, scope)
            limit = db.execute('SELECT limit_micro FROM budget_accounts WHERE id=?', (root,)).fetchone()
            if limit is None or self.tree_committed(db, root) + amount > limit[0]:
                raise BudgetError('root_search_budget_exhausted')
            db.execute("INSERT INTO model_calls(id,scope,purpose,model,created_at,reserved_micro,state,pricing_json) VALUES(?,?,?,?,?,?,'reserved',?)",
                       (call_id, scope, purpose, model, time.time(), amount, json.dumps({**pricing_snapshot(), 'max_output_tokens': max_tokens})))
        return call_id

    def settle(self, call_id, response):
        usage = response.get('usage', {})
        if type(usage) is not dict:
            raise ProviderError('provider_usage_unverified', call_id)
        incoming, outgoing = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if type(incoming) is not int or type(outgoing) is not int or incoming < 0 or outgoing < 0:
            raise ProviderError('provider_usage_unverified', call_id)
        with self.store.connection(write=True) as db:
            row = db.execute('SELECT reserved_micro,pricing_json FROM model_calls WHERE id=?', (call_id,)).fetchone()
            if row is None:
                raise ProviderError('provider_usage_exceeds_reservation', call_id)
            snapshot = json.loads(row['pricing_json'])
            if incoming > snapshot['context_ceiling'] or outgoing > snapshot.get('max_output_tokens', 8192):
                raise ProviderError('provider_usage_unverified', call_id)
            amount = incoming * snapshot['input_micro_per_token'] + outgoing * snapshot['output_micro_per_token']
            if amount > row['reserved_micro']:
                raise ProviderError('provider_usage_exceeds_reservation', call_id)
            db.execute("UPDATE model_calls SET state='settled',charged_micro=?,usage_json=?,provider_id=?,fingerprint=? WHERE id=? AND state='reserved'",
                       (amount, json.dumps(usage), response.get('id'), response.get('system_fingerprint'), call_id))

    def uncertain(self, call_id, code):
        with self.store.connection(write=True) as db:
            db.execute("UPDATE model_calls SET error=? WHERE id=?", (code, call_id))

    def diagnostics(self, call_id, response):
        choices = response.get('choices')
        choice = choices[0] if type(choices) is list and choices and type(choices[0]) is dict else {}
        message = choice.get('message', {})
        content = message.get('content') if type(message) is dict else None
        detail = {'finish_reason': choice.get('finish_reason'), 'content_type': type(content).__name__,
                  'content_length': len(content) if type(content) is str else None,
                  'content_sha256': sha256(content) if type(content) is str else None,
                  'object_shaped_text': content.strip().startswith('{') and content.strip().endswith('}') if type(content) is str else False}
        with self.store.connection(write=True) as db:
            db.execute('UPDATE model_calls SET diagnostics_json=? WHERE id=?', (json.dumps(detail), call_id))

    def status(self, scope=None):
        account = 'work' if scope is None else scope
        with self.store.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT limit_micro FROM budget_accounts WHERE id=?', (account,)).fetchone()
            calls = [dict(r) for r in db.execute('SELECT * FROM model_calls' + (' WHERE scope=?' if scope is not None else '') + ' ORDER BY created_at',
                                                (scope,) if scope is not None else ())]
            for call in calls:
                raw_usage = call.pop('usage_json'); raw_diagnostics = call.pop('diagnostics_json')
                call['pricing'] = json.loads(call.pop('pricing_json'))
                call['usage'] = json.loads(raw_usage) if raw_usage else None
                diagnostics = json.loads(raw_diagnostics) if raw_diagnostics else None
                call['diagnostics'] = {key:value for key,value in diagnostics.items() if key != 'content'} if diagnostics else None
            root = self.root_scope(db, scope) if scope else None
            root_limit = db.execute('SELECT limit_micro FROM budget_accounts WHERE id=?', (root,)).fetchone() if root else None
            grants=[dict(r) for r in db.execute('SELECT * FROM service_budget_grants'+(' WHERE root_id=?' if root else ''),(root,) if root else ())]
            return {'currency': 'CNY', 'limit_cny': row[0] / MICRO if row else 0,
                    'committed_ceiling_cny': self._committed(db, scope) / MICRO,
                    'root_search_id': root, 'root_limit_cny': root_limit[0]/MICRO if root_limit else None,
                    'root_committed_ceiling_cny': self.tree_committed(db, root)/MICRO if root else None,
                    'pricing_source': PRICING_SOURCE,
                    'pricing_verified_date': PRICING_VERIFIED_DATE, 'provider_bill_verified': False, 'calls': calls,
                    'service_allowances':grants,'delegated_ceiling_cny':sum(r['amount_micro'] for r in grants)/MICRO}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ModelGateway:
    def __init__(self, profile, ledger, scope, *, allow_fixture=False):
        endpoint = urlsplit(profile.base_url)
        if not allow_fixture and (endpoint.scheme != 'https' or endpoint.hostname != 'api.deepseek.com' or endpoint.path not in ('', '/v1') or endpoint.port not in (None, 443)):
            raise BudgetError('unpriced_endpoint_requires_review')
        self.profile, self.ledger, self.scope = profile, ledger, scope
        self.fixture_transport=allow_fixture
        if allow_fixture and (endpoint.scheme!='http' or endpoint.hostname not in ('127.0.0.1','localhost')):
            raise BudgetError('loopback_fixture_required')

    def redact(self, value):
        if type(value) is str:
            return value.replace(self.profile.api_key, '[REDACTED]')
        if type(value) is list:
            return [self.redact(item) for item in value]
        if type(value) is dict:
            return {self.redact(key): self.redact(item) for key,item in value.items()}
        return value

    def json(self, system, user, *, purpose, max_tokens=2048):
        payload = {'model': self.profile.model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                   'max_tokens': max_tokens, 'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'},
                   'stream': False, 'temperature': 0}
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > 262144:
            raise ProviderError('model_input_too_large')
        call_id = self.ledger.reserve(self.scope, purpose, self.profile.model, max_tokens)
        request = Request(self.profile.base_url + '/chat/completions', data=body,
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.profile.api_key})
        try:
            # No redirects/retries: do not forward credentials or duplicate uncertain charges.
            handlers=[NoRedirect]+([ProxyHandler({})] if self.fixture_transport else [])
            with build_opener(*handlers).open(request, timeout=60) as connection:
                raw = connection.read(1048577)
                if len(raw) > 1048576:
                    raise ProviderError('provider_response_too_large', call_id)
                response = strict_json_loads(raw)
            validate_json(response, max_string_length=1048576)
            response = self.redact(response)
            if type(response) is not dict or response.get('model') != self.profile.model:
                raise ProviderError('provider_model_unverified', call_id)
            self.ledger.diagnostics(call_id, response)
            self.ledger.settle(call_id, response)
            choice = response['choices'][0]
            if choice['finish_reason'] != 'stop':
                raise ProviderError('provider_incomplete_output', call_id)
            value = self.redact(strict_json_loads(choice['message']['content']))
            validate_json(value)
            return value, call_id
        except HTTPError as error:
            code = 'provider_http_' + str(error.code)
        except (URLError, TimeoutError, OSError):
            code = 'provider_connection_uncertain'
        except ProviderError as error:
            code = error.code
        except (ValueError, KeyError, TypeError, IndexError):
            code = 'provider_invalid_response'
        self.ledger.uncertain(call_id, code)
        raise ProviderError(code, call_id)
