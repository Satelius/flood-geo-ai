import json
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from flood_agent.clients import ServiceError, fetch_flood, request_json, route, decode_polyline
from flood_agent.extract import rules_extract, validate_candidates
from flood_agent.store import Store
from tests.fixtures import ALERT, payload


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')

    def tearDown(self):
        self.store.connection.close()

    def test_pagination(self):
        first = payload('first')
        first['data']['paginationToken'] = 'next'
        responses = iter([first, payload('second')])
        urls = []
        def request(url, headers=None):
            urls.append(url)
            return 200, json.dumps(next(responses))
        result = fetch_flood(self.store, 'archive', '2025-01-01', request)
        self.assertEqual(result['pages'], 2)
        self.assertEqual(result['messages_added'], 2)
        self.assertIn('paginationToken=next', urls[1])
        self.assertIn('date=2025-01-01', urls[1])

    def test_pagination_loop_stops_and_archives(self):
        page = payload()
        page['data']['paginationToken'] = 'loop'
        with self.assertRaises(ServiceError):
            fetch_flood(self.store, request_fn=lambda *args, **kwargs: (200, json.dumps(page)))
        self.assertEqual(self.store.status()['snapshot_count'], 2)
        self.assertEqual(len(self.store.get_messages('live')), 1)

    def test_http_failure_archived(self):
        def fail(*args, **kwargs):
            raise ServiceError('HTTP 403')
        with self.assertRaises(ServiceError):
            fetch_flood(self.store, request_fn=fail)
        row = self.store.connection.execute('SELECT * FROM snapshots').fetchone()
        self.assertEqual(row['error'], 'HTTP 403')
        self.assertEqual(row['parsed'], 0)
        self.assertIsNone(self.store.events('live')['metadata']['last_successful_fetch'])

    def test_429_retry_then_success(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return b'{"data":{"records":[]}}'
        error = urllib.error.HTTPError('https://example.test', 429, 'rate limited', {}, None)
        with patch('urllib.request.urlopen', side_effect=[error, Response()]) as mocked, patch('time.sleep'):
            self.assertEqual(request_json('https://example.test')[0], 200)
            self.assertEqual(mocked.call_count, 2)

    def test_nonretryable_http_error(self):
        error = urllib.error.HTTPError('https://example.test', 403, 'denied', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as mocked, patch('time.sleep'):
            with self.assertRaises(ServiceError): request_json('https://example.test')
            self.assertEqual(mocked.call_count, 1)

    def test_401_status_is_preserved_on_error_and_failure_snapshot(self):
        error = urllib.error.HTTPError('https://example.test', 401, 'unauthorized', {}, None)
        with patch('urllib.request.urlopen', side_effect=error), patch('time.sleep'):
            with self.assertRaises(ServiceError) as caught:
                request_json('https://example.test')
        self.assertEqual(caught.exception.status_code, 401)

        def unauthorized(*args, **kwargs):
            raise ServiceError('HTTP 401', status_code=401)
        with self.assertRaises(ServiceError):
            fetch_flood(self.store, 'archive', '2025-01-01', unauthorized)
        row = self.store.connection.execute('SELECT http_status FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(row['http_status'], 401)

    def test_exhausted_429(self):
        error = urllib.error.HTTPError('https://example.test', 429, 'rate limited', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as mocked, patch('time.sleep'):
            with self.assertRaises(ServiceError): request_json('https://example.test')
            self.assertEqual(mocked.call_count, 3)

    def test_bad_json_is_archived(self):
        with self.assertRaises(ValueError):
            fetch_flood(self.store, request_fn=lambda *args, **kwargs: (200, '{broken'))
        self.assertEqual(self.store.status()['snapshot_count'], 1)

    def test_concurrent_ingest_same_store(self):
        barrier = threading.Barrier(4)
        def ingest(index):
            barrier.wait()
            return self.store.ingest(payload(f'concurrent-{index}'), fetched_at=ALERT)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(ingest, range(4)))
        self.assertEqual(sum(result['messages_added'] for result in results), 4)
        self.assertEqual(len(self.store.get_messages('demo')), 4)


class EvidenceTests(unittest.TestCase):
    def test_rules_always_return_verbatim_evidence_candidates(self):
        text = 'Test Road (from First Avenue to Second Street). Flash flood at Third Road.'
        for candidate in rules_extract(text):
            for key in ('road_text', 'from_text', 'to_text', 'evidence'):
                self.assertIn(candidate[key], text)
            self.assertEqual(candidate['verification_status'], 'candidate')
            self.assertNotIn('geometry', candidate)

    def test_llm_hallucinated_evidence_rejected(self):
        candidate = {'road_text': 'Imaginary Road', 'evidence': 'Imaginary Road'}
        with self.assertRaises(ServiceError): validate_candidates([candidate], 'Test Road')

    def test_geometry_rejected_and_verified_status_removed(self):
        with self.assertRaises(ServiceError):
            validate_candidates([{'road_text': 'Test Road', 'evidence': 'Test Road', 'geometry': {}}], 'Test Road')
        result = validate_candidates([{'road_text': 'Test Road', 'evidence': 'Test Road', 'verification_status': 'verified'}], 'Test Road')
        self.assertEqual(result[0]['verification_status'], 'candidate')


class RoutingTests(unittest.TestCase):
    def test_polyline_reference_vector_and_truncated(self):
        self.assertEqual(decode_polyline('_p~iF~ps|U_ulLnnqC_mqNvxq`@'),
                         [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]])
        with self.assertRaises(ServiceError): decode_polyline('_p')

    def test_onemap_query_coordinate_order_and_summary(self):
        calls = []
        def encode(points):
            result = ''
            previous = [0, 0]
            for point in points:
                for index, value in enumerate(point):
                    current = round(value * 100000)
                    delta = current - previous[index]
                    previous[index] = current
                    value = ~(delta << 1) if delta < 0 else delta << 1
                    while value >= 32:
                        result += chr((32 | (value & 31)) + 63)
                        value >>= 5
                    result += chr(value + 63)
            return result
        def request(url, headers=None):
            calls.append((url, headers))
            return 200, json.dumps({'route_geometry': encode([[1.3, 103.8], [1.35, 103.85]]),
                                    'route_summary': {'total_distance': 100, 'total_time': 30}})
        with patch.dict('os.environ', {'ONEMAP_TOKEN': 'synthetic-token'}):
            result = route([103.8, 1.3], [103.9, 1.4], via=[103.85, 1.35], request_fn=request)
        self.assertEqual(len(calls), 2)
        self.assertIn('start=1.3%2C103.8', calls[0][0])
        self.assertIn('end=1.35%2C103.85', calls[0][0])
        self.assertEqual(result['summary'], {'total_distance': 200, 'total_time': 60})
        self.assertTrue(result['scenario'])

    def test_token_absent_and_unauthorized_response(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ServiceError): route([103.8, 1.3], [103.9, 1.4])
        def unauthorized(*args, **kwargs):
            raise ServiceError('HTTP 401')
        with patch.dict('os.environ', {'ONEMAP_TOKEN': 'synthetic-token'}):
            with self.assertRaises(ServiceError): route([103.8, 1.3], [103.9, 1.4], request_fn=unauthorized)


if __name__ == '__main__':
    unittest.main()
