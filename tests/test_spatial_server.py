import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from flood_agent.demo import demo_route
from flood_agent.server import make_server
from flood_agent.spatial import line_length_m, lines_intersect
from flood_agent.store import Store
from tests.fixtures import ALERT, CANCEL, payload


class SpatialTests(unittest.TestCase):
    def test_length_equator_and_empty(self):
        self.assertAlmostEqual(line_length_m([[0, 0], [1, 0]]), 111194.9, places=1)
        self.assertEqual(line_length_m([]), 0)
        self.assertEqual(line_length_m([[103.8, 1.3]]), 0)

    def test_intersection_cross_overlap_touch_and_disjoint(self):
        cases = [([[0, 0], [2, 2]], [[0, 2], [2, 0]], True),
                 ([[0, 0], [2, 0]], [[1, 0], [3, 0]], True),
                 ([[0, 0], [1, 0]], [[1, 0], [1, 1]], True),
                 ([[0, 0], [1, 0]], [[2, 0], [3, 0]], False),
                 ([[0, 0], [1, 0]], [[0, 1], [1, 1]], False),
                 ([], [[0, 0], [1, 1]], False)]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                self.assertEqual(lines_intersect(left, right), expected)
                self.assertEqual(lines_intersect(right, left), expected)

    def test_demo_route_has_explicit_synthetic_exposure(self):
        result = demo_route()
        self.assertTrue(result['synthetic'])
        self.assertTrue(result['baseline_intersects'])
        self.assertFalse(result['detour_intersects'])
        self.assertGreater(result['delta_distance'], 0)
        self.assertEqual(result['road_segment']['properties']['verification_status'], 'synthetic')


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(':memory:')
        cls.store.ingest(payload(), fetched_at=ALERT)
        cls.server = make_server(cls.store, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.store.connection.close()

    def test_events_and_status_json(self):
        with urllib.request.urlopen(self.base + '/api/events?' + urlencode({'source': 'demo', 'at': CANCEL})) as response:
            body = json.load(response)
        self.assertEqual(len(body['features']), 1)
        with urllib.request.urlopen(self.base + '/api/status') as response:
            status = json.load(response)
        self.assertEqual(status['baseline_mode'], 'rules')
        self.assertFalse(status['llm_executed'])

    def test_invalid_query_returns_400(self):
        for path in ('/api/events?source=bad', '/api/events?at=invalid'):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()

    def test_rules_extraction_roundtrip(self):
        message_key = self.store.get_messages('demo')[0]['message_key']
        request = urllib.request.Request(self.base + '/api/extract',
            data=json.dumps({'source': 'demo', 'message_key': message_key, 'mode': 'rules'}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        self.assertFalse(result['llm_used'])
        self.assertTrue(result['candidates'])

    def test_cross_origin_mutation_rejected(self):
        request = urllib.request.Request(self.base + '/api/ingest', data=b'{}',
            headers={'Origin': 'https://other.example', 'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_artifact_whitelist_and_history_probe_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            fixture_files = {
                'experiments/history_availability.json': '{"days":[]}\n',
                'experiments/history_availability_zh.md': '# history fixture\n',
                'experiments/corpus_profile_zh.md': '# corpus fixture\n',
                'scripts/report_history_availability.py': 'history_availability = True\n',
                'README.md': '# artifact fixture\n',
                'data/research/history-probe/2024-10-08/page-001.json': '{"date":"2024-10-08"}\n',
            }
            for relative_path, body in fixture_files.items():
                path = fixture_root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding='utf-8')

            server = make_server(self.store, port=0, root=fixture_root)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                self._assert_artifact_routes(base)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def _assert_artifact_routes(self, base):
        allowed_paths = (
            '/artifacts/history-availability.json',
            '/artifacts/history-availability-zh.md',
            '/artifacts/corpus-profile-zh.md',
            '/artifacts/report-generator.py',
            '/artifacts/README.md',
            '/artifacts/history-probe/2024-10-08/page-001.json',
        )
        for path in allowed_paths:
            with self.subTest(path=path), urllib.request.urlopen(base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(int(response.headers['Content-Length']), 0)

        with urllib.request.urlopen(base + '/artifacts/history-probe/manifest.json') as response:
            manifest = json.load(response)
        self.assertIn('2024-10-08', {entry['date'] for entry in manifest['dates']})
        with urllib.request.urlopen(base + '/artifacts/report-generator.py') as response:
            self.assertIn(b'history_availability', response.read())

        rejected_paths = (
            '/artifacts/.env',
            '/artifacts/data/flood.sqlite3',
            '/artifacts/not-allowlisted.json',
            '/artifacts/%2e%2e/.env',
            '/artifacts/history-probe/2024-10-08/%2e%2e/%2e%2e/README.md',
            '/artifacts/history-probe/2024-13-40/page-001.json',
            '/artifacts/history-probe/2024-10-08/page-0001.json',
        )
        for path in rejected_paths:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + path)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()

    def test_report_redirects_to_history_report(self):
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, response, code, message, headers, new_url):
                return None

        opener = urllib.request.build_opener(NoRedirectHandler)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(self.base + '/report')
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(caught.exception.headers['Location'], '/reports/history.html')
        caught.exception.close()


if __name__ == '__main__':
    unittest.main()
