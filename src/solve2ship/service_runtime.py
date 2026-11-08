"""Reviewed standalone Rules HTTP template. Candidate artifacts are data only."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
import threading

from .candidates import RulesAdapter, PythonAdapter,PromptAdapter,HybridAdapter,ArtifactError,model_required
from .contracts import SandboxUnavailable
from .contracts import canonical_json, sha256, strict_json_loads, validate_json


MAX_BYTES = 262144


def create_service(candidate, port=9000, host='127.0.0.1', *, runtime_root=None,model_state=None,model_gateway=None):
    if candidate.get('family') not in ('rules.v1','python.v1','sklearn.classifier.v1','workflow.v1','prompt.v1','hybrid.v1') or sha256(candidate['artifact']) != candidate.get('artifact_sha256'):
        raise ValueError('unsupported_or_changed_artifact')
    if candidate['family']=='workflow.v1':
        from .workflow_adapter import WorkflowAdapter,requires_model
        WorkflowAdapter.validate(candidate['artifact'])
    needs_model=model_required(candidate['family'],candidate['artifact'])
    if needs_model and (model_state is None or model_gateway is None):raise ValueError('service_model_configuration_required')
    if candidate['family'] == RulesAdapter.family:
        adapter = RulesAdapter()
    elif candidate['family']=='prompt.v1':adapter=PromptAdapter()
    elif candidate['family']=='hybrid.v1':adapter=HybridAdapter()
    else:
        from .docker_runtime import DockerSandbox
        root = Path(runtime_root or Path(__file__).resolve().parents[1])
        configuration = candidate['runtime']
        sandbox = DockerSandbox(configuration['image'],runtime_root=root)
        sandbox.accepted_engine_version = configuration['engine_version']
        sandbox.engine.status()
        if candidate['family']=='workflow.v1':
            from .workflow_adapter import WorkflowAdapter
            adapter=WorkflowAdapter(sandbox)
        elif candidate['family']=='sklearn.classifier.v1':
            from .sklearn_adapter import SklearnAdapter
            adapter=SklearnAdapter(sandbox)
        else:
            adapter = PythonAdapter(sandbox)
    adapter.validate(candidate['artifact'])
    capacity = threading.BoundedSemaphore(2)

    class Server(ThreadingHTTPServer):
        def server_bind(self):
            TCPServer.server_bind(self)
            self.server_name = 'localhost'
            self.server_port = self.server_address[1]

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def send(self, status, value):
            body = canonical_json(value).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def safe(self):
            allowed = {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}
            if self.headers.get('Host') not in allowed or self.headers.get('Origin') is not None:
                self.send(403, {'error': 'local_machine_api_required'})
                return False
            return True

        def do_GET(self):
            if not self.safe():
                return
            if self.path == '/health':
                self.send(200, {'status': 'ok', 'candidate_id': candidate['id'],
                                'artifact_sha256': candidate['artifact_sha256'], 'family': candidate['family']})
            elif self.path == '/contract':
                self.send(200, {'request': {'input': 'JSON value'}, 'response': {'result': 'JSON value'},
                                'semantic_schema': 'unconfirmed', 'model_calls': needs_model,
                                'request_id_required':needs_model,'standalone_usage_database_required':needs_model,
                                'docker_required': candidate['family'] in ('python.v1','workflow.v1','sklearn.classifier.v1')})
            else:
                self.send(404, {'error': 'not_found'})

        def do_POST(self):
            self.close_connection = True
            if not self.safe():
                return
            if self.path != '/predict':
                self.send(404, {'error': 'not_found'})
                return
            if self.headers.get('Content-Type') != 'application/json':
                self.send(415, {'error': 'application_json_required'})
                return
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                self.send(400, {'error': 'one_content_length_required'})
                return
            try:
                length = int(lengths[0])
                if not 0 < length <= MAX_BYTES:
                    self.send(413, {'error': 'request_too_large'})
                    return
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError()
                body = strict_json_loads(raw)
                validate_json(body)
                expected={'input','request_id'} if needs_model else {'input'}
                if type(body) is not dict or body.keys() != expected:
                    raise ValueError()
                if not capacity.acquire(blocking=False):
                    self.send(503, {'error':'service_capacity_reached'});return
                try:
                    if needs_model:output=model_state.predict(body['request_id'],body['input'],adapter,model_gateway)
                    else:output, _ = adapter.execute(candidate['artifact'], body['input'], None)
                finally:
                    capacity.release()
            except Exception as error:
                if needs_model:
                    from .gateway import BudgetError,ProviderError
                    from .service_model import RequestConflict
                    if isinstance(error,RequestConflict):self.send(409,{'error':str(error)});return
                    if isinstance(error,BudgetError):self.send(402,{'error':str(error)});return
                    if isinstance(error,ProviderError):self.send(502,{'error':error.code});return
                if isinstance(error,(SandboxUnavailable,ArtifactError)):
                    self.send(503,{'error':'isolated_runtime_unavailable'});return
                if isinstance(error,(ValueError,TypeError,UnicodeError,RecursionError)):
                    self.send(422,{'error':'invalid_predict_request'});return
                self.send(500,{'error':'service_request_failed'});return
            self.send(200, {'result': output})

    return Server((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description='Private standalone STS Rules service; not production hosting')
    parser.add_argument('--port', type=int, default=9000)
    parser.add_argument('--host', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1')
    parser.add_argument('--state-dir',type=Path,default=Path('.sts'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    candidate = json.loads((root / 'candidate.json').read_text())
    state,gateway=None,None
    if model_required(candidate['family'],candidate['artifact']):
        from .service_model import ServiceState
        try:state=ServiceState(args.state_dir,candidate);gateway=state.gateway()
        except (OSError,ValueError,RuntimeError):
            parser.exit(1,'Configure an owner-only model profile and cumulative budget first / 请先配置仅所有者可读的模型连接和累计预算。\n')
    server = create_service(candidate, args.port, args.host,model_state=state,model_gateway=gateway)
    print(canonical_json({'port': server.server_port, 'candidate_id': candidate['id'], 'standalone': True}), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
