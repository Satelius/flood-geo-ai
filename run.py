import argparse
import json
import os
import threading
from pathlib import Path

from flood_agent.clients import fetch_flood
from flood_agent.demo import seed_demo
from flood_agent.server import make_server
from flood_agent.store import Store


def load_environment():
    path = Path('.env')
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    load_environment()
    parser = argparse.ArgumentParser(description='Flood Event Agent baseline')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', default='data/flood.sqlite3')
    parser.add_argument('--poll', action='store_true')
    parser.add_argument('--import-archive', metavar='FILE')
    args = parser.parse_args()
    store = Store(args.db)
    seed_demo(store)
    if args.import_archive:
        store.ingest(json.loads(Path(args.import_archive).read_text(encoding='utf-8')), source='archive')
    server = make_server(store, args.port)
    if args.poll:
        def poll():
            while True:
                try:
                    fetch_flood(Store(args.db), 'live')
                except Exception as error:
                    print('采集未完成：', type(error).__name__)
                threading.Event().wait(120)
        threading.Thread(target=poll, daemon=True).start()
    print(f'原型：http://127.0.0.1:{args.port}  中文报告：http://127.0.0.1:{args.port}/report')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
