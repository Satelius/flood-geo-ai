import json
import tempfile
import unittest
from pathlib import Path

from flood_agent.normalize import parse_circle, parse_time, parse_references
from flood_agent.store import Store
from tests.fixtures import ALERT, CANCEL, START, cancellation, payload, reading


class CoordinateTests(unittest.TestCase):
    def test_string_numbers_and_order(self):
        self.assertEqual(parse_circle(['1.33201', '103.87015', '1']), ([103.87015, 1.33201], 1.0, []))

    def test_string_circle(self):
        self.assertEqual(parse_circle('1.33201,103.87015,1')[0], [103.87015, 1.33201])

    def test_invalid_nonfinite_and_radius(self):
        for circle in (None, [], 'bad', [1.3, 103.8], [float('nan'), 103.8, 1],
                       [1.3, float('inf'), 1], [1.3, 103.8, -1], [1.3, 103.8, 'NaN']):
            with self.subTest(circle=circle):
                coordinates, _, flags = parse_circle(circle)
                self.assertIsNone(coordinates)
                self.assertTrue(flags)

    def test_outside_singapore_and_swapped(self):
        for circle in ([51, 0, 1], [103.8, 1.3, 1], [0, 103.8, 1]):
            self.assertEqual(parse_circle(circle)[2], ['outside_singapore_bounds'])

    def test_timezone_normalization(self):
        self.assertEqual(parse_time(ALERT), parse_time('2024-12-31T16:01:00Z'))
        self.assertIsNone(parse_time('bad'))

    def test_reference_forms(self):
        self.assertEqual(parse_references(f'PUB,test-alert,{ALERT}'), [('PUB', 'test-alert', ALERT)])
        self.assertEqual(parse_references([{'sender': 'PUB', 'identifier': 'test-alert', 'sent': ALERT}]),
                         [('PUB', 'test-alert', ALERT)])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')

    def tearDown(self):
        self.store.connection.close()
        self.temp.cleanup()

    def seed_window(self, source='demo'):
        self.store.ingest(payload(readings=[], msg_type=''), source, START)

    def test_empty_records_and_readings(self):
        self.store.ingest({'data': {'records': []}})
        self.store.ingest(payload(readings=[]))
        result = self.store.events()
        self.assertEqual(result['features'], [])
        self.assertEqual(result['metadata']['message_count'], 0)

    def test_deduplication_preserves_raw_snapshots(self):
        for _ in range(3):
            self.store.ingest(payload(), fetched_at=ALERT)
        self.assertEqual(len(self.store.get_messages('demo')), 1)
        self.assertEqual(self.store.status()['snapshot_count'], 3)

    def test_multi_reading_and_stable_keys(self):
        first = reading()
        second = reading([1.35, 103.9, 2], 'Flash flood at Second Road.')
        self.store.ingest(payload(readings=[first, second, first]), fetched_at=ALERT)
        before = self.store.events(at=CANCEL)
        self.store.ingest(payload(readings=[second, first]), fetched_at=CANCEL)
        after = self.store.events(at=CANCEL)
        self.assertEqual(before['metadata']['message_count'], 1)
        self.assertEqual(before['metadata']['reading_count'], 2)
        self.assertEqual(before['features'], after['features'])

    def test_missing_identifier_unknown_stable(self):
        for _ in range(2):
            self.store.ingest(payload(identifier=None), fetched_at=ALERT)
        result = self.store.events(at=CANCEL)
        self.assertEqual(result['metadata']['message_count'], 1)
        self.assertEqual(result['features'][0]['properties']['status'], 'unknown')
        self.assertIn('missing_identifier', result['features'][0]['properties']['quality_flags'])

    def test_missing_sent_uses_record_time(self):
        sample = payload()
        del sample['data']['records'][0]['item']['sent']
        self.store.ingest(sample, fetched_at=CANCEL)
        result = self.store.events(at=ALERT)
        self.assertEqual(result['metadata']['message_count'], 1)

    def test_same_content_on_different_dates_is_not_merged(self):
        first = payload(identifier=None)
        second = payload(identifier=None, sent='2025-01-02T00:01:00+08:00')
        for sample in (first, second):
            del sample['data']['records'][0]['item']['sent']
            self.store.ingest(sample, fetched_at='2025-01-03T00:00:00+08:00')
        self.assertEqual(len(self.store.get_messages('demo')), 2)

    def test_histogram_uses_singapore_calendar_day(self):
        self.store.ingest(payload(sent='2024-12-31T16:01:00Z'), fetched_at=ALERT)
        result = self.store.events(at=CANCEL)
        self.assertEqual(result['histogram'][0]['date'], '2025-01-01')

    def test_real_archive_datetime_is_used_in_replay(self):
        sample = json.loads(Path('data/research/pub-2025-05-22.json').read_text(encoding='utf-8'))
        self.store.ingest(sample, 'archive', '2026-10-05T00:00:00+08:00')
        result = self.store.events('archive', '2025-05-22T20:20:00+08:00')
        self.assertEqual(result['metadata']['message_count'], 1)
        self.assertEqual(result['features'][0]['geometry']['coordinates'], [103.87015, 1.33201])
        self.assertEqual(result['features'][0]['properties']['status'], 'unknown')

    def test_malformed_record_and_area(self):
        sample = payload()
        sample['data']['records'].insert(0, 'unexpected')
        sample['data']['records'][1]['item']['readings'][0]['area'] = 'unexpected'
        self.store.ingest(sample, fetched_at=ALERT)
        self.assertEqual(self.store.events(at=CANCEL)['metadata']['unlocated_reading_count'], 1)

    def test_invalid_location_keeps_message(self):
        self.store.ingest(payload(readings=[reading(['bad', 103.8, 1])]), fetched_at=ALERT)
        result = self.store.events(at=CANCEL)
        self.assertEqual(result['features'], [])
        self.assertEqual(result['metadata']['message_count'], 1)
        self.assertEqual(result['metadata']['unlocated_reading_count'], 1)

    def test_alert_cancel_historical_replay(self):
        self.seed_window()
        self.store.ingest(payload(), fetched_at=ALERT)
        self.store.ingest(cancellation(), fetched_at=CANCEL)
        self.assertEqual(self.store.events(at=START)['features'], [])
        self.assertEqual(self.store.events(at=ALERT)['features'][0]['properties']['status'], 'observed_active')
        self.assertEqual(self.store.events(at=CANCEL)['features'][0]['properties']['status'], 'cancelled')

    def test_cancel_requires_exact_sender_identifier_and_sent(self):
        self.seed_window()
        self.store.ingest(payload(), fetched_at=ALERT)
        sample = cancellation()
        sample['data']['records'][0]['item']['references'] = f'Other,test-alert,{ALERT}'
        self.store.ingest(sample, fetched_at=CANCEL)
        self.assertEqual(self.store.events(at=CANCEL)['features'][0]['properties']['status'], 'observed_active')

    def test_source_isolation(self):
        self.seed_window('live')
        self.store.ingest(payload(), 'live', ALERT)
        self.store.ingest(cancellation(), 'demo', CANCEL)
        result = self.store.events('live', CANCEL)
        self.assertEqual(result['features'][0]['properties']['status'], 'observed_active')
        self.assertFalse(result['metadata']['synthetic'])

    def test_cold_start_is_unknown(self):
        self.store.ingest(payload(), fetched_at=CANCEL)
        self.assertEqual(self.store.events(at=CANCEL)['features'][0]['properties']['status'], 'unknown')

    def test_expiry(self):
        self.seed_window()
        sample = payload()
        sample['data']['records'][0]['item']['readings'][0]['expires'] = CANCEL
        self.store.ingest(sample, fetched_at=ALERT)
        self.assertEqual(self.store.events(at=CANCEL)['features'][0]['properties']['status'], 'expired')

    def test_raw_provenance_and_location_semantics(self):
        stored = self.store.ingest(payload(), fetched_at=ALERT)
        feature = self.store.events(at=CANCEL)['features'][0]
        self.assertEqual(feature['geometry']['type'], 'Point')
        self.assertEqual(feature['properties']['raw_snapshot_id'], stored['snapshot_id'])
        self.assertEqual(feature['properties']['location_role'], 'alert_broadcast_center')
        self.assertEqual(feature['properties']['broadcast_radius_km'], 1)
        raw = self.store.connection.execute('SELECT raw_text FROM snapshots WHERE id=?', (stored['snapshot_id'],)).fetchone()[0]
        self.assertEqual(json.loads(raw), payload())

    def test_malformed_payload_keeps_raw(self):
        with self.assertRaises(ValueError):
            self.store.ingest({'data': {}})
        self.assertEqual(self.store.status()['snapshot_count'], 1)


if __name__ == '__main__':
    unittest.main()
