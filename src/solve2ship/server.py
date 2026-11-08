"""Loopback-only development API and explicitly allowlisted static assets."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from socketserver import TCPServer
from urllib.parse import urlsplit

from . import __version__
from .contracts import ContractError, MAX_TASK_BYTES, compile_task, strict_json_loads
from .tasks import TaskConflict, TaskRepository
from .search import SearchError, SearchRepository
from .gateway import BudgetError
from .delivery import DeliveryError, DeliveryRepository
from .service_export import export_service
from .contracts import SandboxUnavailable


WEB = Path(__file__).resolve().parents[2] / "web"
ASSETS = {"/": ("index.html", "text/html"), "/app.mjs": ("app.mjs", "text/javascript"),
          "/i18n.mjs": ("i18n.mjs", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/task-ui.mjs": ("task-ui.mjs", "text/javascript"), '/search-ui.mjs': ('search-ui.mjs', 'text/javascript')}
JOB_PATH = re.compile(r"/api/jobs/([0-9a-f]{32})(/cancel)?\Z")
TASK_PATH = re.compile(r"/api/tasks/([0-9a-f]{32})(?:/revisions(?:/([1-9][0-9]{0,8}))?)?\Z")
SEARCH_PATH = re.compile(r'/api/searches/([0-9a-f]{32})(/cancel)?\Z')
REACTIVATE_PATH = re.compile(r'/api/searches/([0-9a-f]{32})/candidates/([0-9a-f]{32})/reactivate\Z')
EVOLVE_PATH = re.compile(r'/api/searches/([0-9a-f]{32})/candidates/([0-9a-f]{32})/evolve\Z')
ROLLBACK_PATH = re.compile(r'/api/searches/([0-9a-f]{32})/rollback\Z')
LINEAGE_PATH = re.compile(r'/api/searches/([0-9a-f]{32})/lineage\Z')
DELIVERY_PATH = re.compile(r'/api/searches/([0-9a-f]{32})/delivery\Z')
EXPORT_PATH = re.compile(r'/api/searches/([0-9a-f]{32})/delivery/export\Z')


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind performs reverse DNS even for 127.0.0.1.
        # This local service must also start when DNS is unavailable or stalled.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


def create_server(store, port=8000):
    tasks = TaskRepository(store)
    searches = SearchRepository(store)
    deliveries = DeliveryRepository(store)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # Do not log request paths, bodies or future user credentials.

        def send(self, status, payload, content_type="application/json"):
            body = json.dumps(payload).encode() if content_type == "application/json" else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if content_type == 'application/zip':
                self.send_header('Content-Disposition','attachment; filename="solve2ship-service.zip"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def safe_request(self, mutation=False):
            authority = f"127.0.0.1:{self.server.server_port}"
            hosts = {authority, f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in hosts:
                self.send(403, {"error": "Loopback Host required"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{host}" for host in hosts}:
                self.send(403, {"error": "Cross-origin requests denied"})
                return False
            if mutation and self.headers.get("X-STS-Request") != "1":
                self.send(403, {"error": "X-STS-Request header required"})
                return False
            return True

        def do_GET(self):
            if not self.safe_request():
                return
            path = urlsplit(self.path).path
            if path in ASSETS:
                name, mime = ASSETS[path]
                self.send(200, (WEB / name).read_bytes(), mime)
            elif path == "/api/health":
                from .docker_runtime import configured_sandbox
                try:
                    sandbox = configured_sandbox(store.path.parent/'sandbox.json')
                    isolated = sandbox is not None and sandbox.engine.status()['available']
                except Exception:
                    isolated = False
                self.send(200, {"status": "ok", "milestone": "M7-local", "version": __version__,
                                "candidate_execution_enabled": True, 'arbitrary_code_execution_enabled': isolated, 'code_runtime_scope': 'experimental_local_docker' if isolated else 'disabled'})
            elif path == "/api/jobs":
                self.send(200, {"jobs": store.list(), "limit": 100})
            elif path == "/api/tasks":
                self.send(200, {"tasks": tasks.list(), "limit": 100})
            elif path == '/api/budget':
                self.send(200, searches.ledger.status())
            elif path == '/api/searches':
                self.send(200, {'searches': searches.list()})
            elif match := DELIVERY_PATH.fullmatch(path):
                self.send(200, {'delivery': deliveries.for_search(match[1])})
            elif match := EXPORT_PATH.fullmatch(path):
                delivery = deliveries.for_search(match[1])
                export = delivery.get('export') if delivery else None
                if not export or not export['archive_integrity_verified']:
                    self.send(404, {'error':'export_not_available'})
                else:
                    self.send(200, Path(export['archive_path']).read_bytes(), 'application/zip')
            elif match := LINEAGE_PATH.fullmatch(path):
                try:
                    self.send(200, searches.lineage(match[1]))
                except KeyError:
                    self.send(404, {'error': 'search_not_found'})
            elif (match := SEARCH_PATH.fullmatch(path)) and not match[2]:
                try:
                    self.send(200, searches.get(match[1]))
                except KeyError:
                    self.send(404, {'error': 'search_not_found'})
            elif match := TASK_PATH.fullmatch(path):
                try:
                    if match[2]:
                        self.send(200, tasks.get_revision(match[1], int(match[2])))
                    elif not path.endswith('/revisions'):
                        self.send(200, tasks.get(match[1]))
                    else:
                        self.send(404, {"error": "Not found"})
                except KeyError:
                    self.send(404, {"error": "task_not_found"})
            elif (match := JOB_PATH.fullmatch(path)) and not match[2]:
                try:
                    self.send(200, store.get(match[1]))
                except KeyError:
                    self.send(404, {"error": "Job not found"})
            else:
                self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.safe_request(mutation=True):
                return
            if self.headers.get("Content-Type") != "application/json":
                self.send(415, {"error": "application/json required"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                self.send(400, {"error": "One Content-Length required"})
                return
            path = urlsplit(self.path).path
            limit = MAX_TASK_BYTES if path.startswith('/api/tasks') else 4096
            try:
                length = int(lengths[0])
                if not 0 <= length <= limit:
                    self.send(413, {"error": "task_too_large" if limit == MAX_TASK_BYTES else "Request body limit is 4096 bytes"})
                    return
                body = strict_json_loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                self.send(400, {"error": "Invalid JSON object"})
                return
            try:
                if match := EXPORT_PATH.fullmatch(path):
                    if body:
                        raise DeliveryError('invalid_delivery_request')
                    delivery = deliveries.for_search(match[1])
                    if not delivery:
                        raise DeliveryError('frozen_delivery_required')
                    self.send(201, export_service(store, delivery['id']))
                elif match := DELIVERY_PATH.fullmatch(path):
                    if body.keys() != {'request_id'}:
                        raise DeliveryError('invalid_delivery_request')
                    self.send(201, deliveries.prepare(match[1], body['request_id']))
                elif match := EVOLVE_PATH.fullmatch(path):
                    if body.keys() != {'request_id'}:
                        raise SearchError('invalid_evolution')
                    self.send(201, searches.evolve(match[1], match[2], body['request_id']))
                elif match := ROLLBACK_PATH.fullmatch(path):
                    if body.keys() != {'request_id'}:
                        raise SearchError('invalid_search_request')
                    self.send(201, searches.rollback(match[1], body['request_id']))
                elif match := REACTIVATE_PATH.fullmatch(path):
                    if body.keys() != {'request_id'}:
                        raise SearchError('invalid_reactivation')
                    self.send(201, searches.reactivate(match[1], match[2], body['request_id']))
                elif path == '/api/searches':
                    if body.keys() != {'task_id', 'revision', 'request_id'}:
                        raise SearchError('invalid_search_request')
                    self.send(201, searches.create(body['task_id'], body['revision'], body['request_id']))
                elif (match := SEARCH_PATH.fullmatch(path)) and match[2]:
                    if body:
                        raise SearchError('invalid_search_request')
                    self.send(200, searches.cancel(match[1]))
                elif path == "/api/tasks/preview":
                    if body.keys() != {"definition"}:
                        raise ContractError("invalid_field", "request")
                    self.send(200, compile_task(body['definition']))
                elif path == "/api/tasks":
                    if body.keys() != {"definition", "reviewed_sha256"}:
                        raise ContractError("invalid_field", "request")
                    self.send(201, tasks.save(body['definition'], body['reviewed_sha256']))
                elif (match := TASK_PATH.fullmatch(path)) and path.endswith('/revisions'):
                    if body.keys() != {"definition", "reviewed_sha256", "base_revision"}:
                        raise ContractError("invalid_field", "request")
                    self.send(201, tasks.save(body['definition'], body['reviewed_sha256'],
                                              task_id=match[1], base_revision=body['base_revision']))
                elif path == "/api/jobs":
                    if body != {"kind": "foundation_check"}:
                        raise ValueError("M0 accepts only kind=foundation_check, with no extra fields")
                    self.send(201, store.create())
                elif (match := JOB_PATH.fullmatch(path)) and match[2]:
                    if body:
                        raise ValueError("Cancel expects an empty object")
                    self.send(200, store.cancel(match[1]))
                else:
                    self.send(404, {"error": "Not found"})
            except ContractError as error:
                self.send(422, {"error": error.code, "field": error.field})
            except TaskConflict as error:
                self.send(409, {"error": str(error)})
            except (SearchError, BudgetError, DeliveryError) as error:
                self.send(422, {'error': str(error)})
            except SandboxUnavailable:
                self.send(422, {'error':'verified_sandbox_required'})
            except KeyError:
                self.send(404, {"error": "task_not_found" if path.startswith('/api/tasks') else "Job not found"})
            except ValueError as error:
                self.send(400, {"error": str(error)})

    return LoopbackServer(("127.0.0.1", port), Handler)
