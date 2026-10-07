import json
import os
import mimetypes
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .clients import fetch_flood, route, ServiceError
from .demo import demo_route
from .extract import extract_message


def make_server(store, port=8765, root=None):
    root = Path(root or Path(__file__).resolve().parent.parent).resolve()
    lock = threading.RLock()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root / 'web'), **kwargs)

        def send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == '/api/events':
                    with lock:
                        return self.send_json(store.events(query.get('source', ['demo'])[0], query.get('at', [None])[0]))
                if parsed.path == '/api/status':
                    with lock:
                        status = store.status()
                    status.update(onemap_configured=bool(os.getenv('ONEMAP_TOKEN')),
                        llm_configured=bool(os.getenv('LLM_API_KEY') and os.getenv('LLM_MODEL')),
                        baseline_mode='rules', llm_executed=False)
                    return self.send_json(status)
                if parsed.path == '/api/demo-route':
                    return self.send_json(demo_route())
                if parsed.path == '/report':
                    self.send_response(302)
                    self.send_header('Location', '/reports/index.html')
                    self.end_headers()
                    return
                if parsed.path.startswith('/reports/'):
                    path = (root / parsed.path.lstrip('/')).resolve()
                    if path.is_relative_to(root / 'reports') and path.is_file():
                        body = path.read_bytes()
                        self.send_response(200)
                        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_error(404)
                    return
                if parsed.path.startswith('/api/'):
                    return self.send_json({'error': '接口不存在'}, 404)
                super().do_GET()
            except ValueError as error:
                self.send_json({'error': str(error)}, 400)

        def do_POST(self):
            try:
                origin = self.headers.get('Origin')
                if origin and origin != f'http://{self.headers.get("Host")}':
                    return self.send_json({'error': '仅允许同源请求'}, 403)
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 65536:
                    raise ValueError('请求大小无效')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('请求必须为 JSON object')
                path = urlsplit(self.path).path
                if path == '/api/ingest':
                    with lock:
                        return self.send_json(fetch_flood(store, body.get('source', 'live'), body.get('date')))
                if path == '/api/route':
                    return self.send_json(route(body.get('start'), body.get('end'), body.get('route_type', 'drive'), body.get('via')))
                if path == '/api/extract':
                    source = body.get('source', 'demo')
                    with lock:
                        message = next((item for item in store.get_messages(source) if item['message_key'] == body.get('message_key')), None)
                    if message is None:
                        return self.send_json({'error': '找不到指定消息'}, 404)
                    result = extract_message(message, body.get('mode', 'rules'))
                    with lock:
                        store.save_extraction(source, message['message_key'], result)
                    return self.send_json(result)
                self.send_json({'error': '接口不存在'}, 404)
            except ServiceError as error:
                self.send_json({'error': str(error)}, 502)
            except (ValueError, TypeError, KeyError) as error:
                self.send_json({'error': str(error) if isinstance(error, ValueError) else '请求参数或响应结构无效'}, 400)
            except Exception:
                self.send_json({'error': '处理失败；请查看本地运行日志'}, 500)

        def log_message(self, format, *args):
            return

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)
