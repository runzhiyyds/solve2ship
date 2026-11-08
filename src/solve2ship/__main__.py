import argparse
import getpass
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import signal
import sys
import threading

from .server import create_server
from .store import Store
from .worker import Worker
from .model_config import ProfileError, load_profile, save_profile
from .search import SearchWorker
from .gateway import BudgetError, Ledger, MICRO


def main():
    parser = argparse.ArgumentParser(description="Solve2Ship local development server and model profile tools")
    parser.add_argument('command', nargs='?', choices=['serve', 'configure-model', 'model-status', 'authorize-budget'], default='serve')
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, default=Path(".sts/state.sqlite3"))
    parser.add_argument('--cny', help='Explicit cumulative model-call budget; never resets existing usage')
    args = parser.parse_args()
    profile_path = Path('.sts/model.json')
    if args.command == 'authorize-budget':
        try:
            amount = Decimal(args.cny or '')
            if not amount.is_finite() or not Decimal(0) <= amount <= Decimal(1000000):
                raise ValueError()
            ledger = Ledger(Store(args.db)); ledger.configure('work',int(amount*MICRO))
            status = ledger.status()
            print(json.dumps({key:status[key] for key in ('currency','limit_cny','committed_ceiling_cny','provider_bill_verified')}))
        except (InvalidOperation,ValueError,BudgetError):
            parser.exit(1,'Use authorize-budget --cny with a finite non-negative amount.\n')
        return
    if args.command != 'serve':
        try:
            if args.command == 'configure-model':
                if not sys.stdin.isatty() or not sys.stderr.isatty():
                    parser.exit(1, 'Run configure-model in an interactive terminal; API keys must not be passed in command arguments or pipes.\n')
                profile = save_profile(profile_path, {
                    'base_url': input('API base URL (no key in URL): ').strip(),
                    'model': input('Model name: ').strip(),
                    'api_key': getpass.getpass('API key (hidden; stored owner-only on this machine): '),
                })
            else:
                profile = load_profile(profile_path)
            print(json.dumps(profile.status()))
        except FileNotFoundError:
            print(json.dumps({'configured': False, 'connection_verified': False}))
        except (ProfileError, OSError):
            parser.exit(1, 'Model profile could not be loaded/saved. Check values and owner-only file permissions.\n')
        return
    store = Store(args.db)
    server = create_server(store, args.port)
    worker = Worker(store)
    search_worker = SearchWorker(store, profile_path)
    # shutdown() must run outside serve_forever's thread.
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker.start()
    search_worker.start()
    print(f"Solve2Ship M7.6 local preview: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        worker.close()
        search_worker.close()
        server.server_close()


if __name__ == "__main__":
    main()
