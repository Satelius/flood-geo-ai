import json
import os
import mimetypes
import re
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .backfill import BackfillManager
from .clients import fetch_flood, route, ServiceError
from .demo import demo_route
from .extract import extract_message


def make_server(store, port=8765, root=None, backfill_manager=None):
    root = Path(root or Path(__file__).resolve().parent.parent).resolve()
    lock = threading.RLock()
    backfills = backfill_manager or BackfillManager(store)
    artifact_files = {
        '/artifacts/history-availability.json': root / 'experiments' / 'history_availability.json',
        '/artifacts/history-availability-zh.md': root / 'experiments' / 'history_availability_zh.md',
        '/artifacts/corpus-profile-zh.md': root / 'experiments' / 'corpus_profile_zh.md',
        '/artifacts/report-generator.py': root / 'scripts' / 'report_history_availability.py',
        '/artifacts/README.md': root / 'README.md',
    }
    history_probe_root = (root / 'data' / 'research' / 'history-probe').resolve()

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

        def send_artifact(self, body, path, content_type=None):
            content_type = content_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            if content_type.startswith(('text/', 'application/json')) and 'charset=' not in content_type:
                content_type += '; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == '/api/events':
                    with lock:
                        return self.send_json(store.events(query.get('source', ['demo'])[0],
                            query.get('at', [None])[0], query.get('date', [None])[0]))
                if parsed.path == '/api/coverage':
                    start, end = query.get('start', [None])[0], query.get('end', [None])[0]
                    if not start or not end:
                        raise ValueError('coverage 查询需要 start 和 end')
                    with lock:
                        return self.send_json(store.coverage(start, end, query.get('source', ['archive'])[0]))
                if parsed.path.startswith('/api/backfill/'):
                    batch_id = parsed.path.removeprefix('/api/backfill/')
                    batch = backfills.status(batch_id)
                    if batch is None:
                        return self.send_json({'error': '找不到历史回填批次'}, 404)
                    return self.send_json(batch)
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
                    self.send_header('Location', '/reports/history.html')
                    self.end_headers()
                    return
                if parsed.path.startswith('/artifacts/'):
                    artifact = artifact_files.get(parsed.path)
                    if artifact is not None:
                        artifact = artifact.resolve()
                        if artifact.is_relative_to(root) and artifact.is_file():
                            return self.send_artifact(artifact.read_bytes(), artifact)
                        self.send_error(404)
                        return
                    if parsed.path == '/artifacts/history-probe/manifest.json':
                        dates = []
                        if history_probe_root.is_relative_to(root) and history_probe_root.is_dir():
                            for date_directory in sorted(history_probe_root.iterdir()):
                                if not date_directory.is_dir() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_directory.name):
                                    continue
                                resolved_date_directory = date_directory.resolve()
                                if not resolved_date_directory.is_relative_to(history_probe_root):
                                    continue
                                try:
                                    if datetime.strptime(date_directory.name, '%Y-%m-%d').strftime('%Y-%m-%d') != date_directory.name:
                                        continue
                                except ValueError:
                                    continue
                                page_names = sorted(path.name for path in date_directory.iterdir()
                                    if re.fullmatch(r'page-\d{3}\.json', path.name)
                                    and path.resolve().is_relative_to(resolved_date_directory)
                                    and path.is_file())
                                dates.append({'date': date_directory.name, 'pages': [
                                    f'/artifacts/history-probe/{date_directory.name}/{name}' for name in page_names]})
                        body = json.dumps({'dates': dates}, ensure_ascii=False).encode('utf-8')
                        return self.send_artifact(body, Path('manifest.json'), 'application/json; charset=utf-8')
                    page_match = re.fullmatch(
                        r'/artifacts/history-probe/(\d{4}-\d{2}-\d{2})/(page-\d{3}\.json)', parsed.path)
                    if page_match:
                        date_part, filename = page_match.groups()
                        try:
                            if datetime.strptime(date_part, '%Y-%m-%d').strftime('%Y-%m-%d') != date_part:
                                self.send_error(404)
                                return
                        except ValueError:
                            self.send_error(404)
                            return
                        artifact = (history_probe_root / date_part / filename).resolve()
                        if (history_probe_root.is_relative_to(root)
                                and artifact.is_relative_to(history_probe_root) and artifact.is_file()):
                            return self.send_artifact(artifact.read_bytes(), artifact, 'application/json; charset=utf-8')
                    self.send_error(404)
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
                    source, day = body.get('source', 'live'), body.get('date')
                    if source == 'archive' and day:
                        batch_id = store.create_history_batch(day, day)
                        store.start_coverage_day(batch_id, day)
                        try:
                            result = fetch_flood(store, source, day)
                        except ServiceError as error:
                            state = 'incomplete' if error.incomplete else 'unavailable' if error.status_code == 404 else 'failed'
                            store.finish_coverage_day(batch_id, day, error.progress, status=state, error=str(error))
                            store.set_history_batch_status(batch_id, 'completed_with_gaps', str(error))
                            raise
                        state = 'complete_nonzero' if result['message_count'] else 'complete_zero'
                        store.finish_coverage_day(batch_id, day, result, status=state)
                        store.set_history_batch_status(batch_id, 'completed')
                        return self.send_json(result | {'batch_id': batch_id})
                    with lock:
                        return self.send_json(fetch_flood(store, source, day))
                if path == '/api/backfill':
                    return self.send_json(backfills.start(start_date=body.get('start'),
                        end_date=body.get('end'), budget=body.get('budget'),
                        batch_id=body.get('batch_id'),
                        retry_unavailable=body.get('retry_unavailable', False)), 202)
                if path.endswith('/pause') and path.startswith('/api/backfill/'):
                    batch_id = path.removeprefix('/api/backfill/').removesuffix('/pause')
                    return self.send_json(backfills.pause(batch_id))
                if path.endswith('/resume') and path.startswith('/api/backfill/'):
                    batch_id = path.removeprefix('/api/backfill/').removesuffix('/resume')
                    return self.send_json(backfills.start(batch_id=batch_id,
                        budget=body.get('budget'), retry_unavailable=body.get('retry_unavailable', False)), 202)
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
