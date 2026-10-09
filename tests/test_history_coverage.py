import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from flood_agent.backfill import BackfillManager
from flood_agent.clients import ServiceError, fetch_flood
from flood_agent.normalize import normalize_payload
from flood_agent.server import make_server
from flood_agent.store import Store
from tests.fixtures import ALERT, CANCEL, payload, reading


class HistoricalNormalizationTests(unittest.TestCase):
    def test_empty_observation_is_skipped_but_empty_alert_is_kept(self):
        ordinary = payload(identifier='empty-observation', readings=[])
        ordinary['data']['records'][0]['item']['msgType'] = 'Observation'
        ordinary['data']['records'][0]['item'].pop('description', None)
        self.assertEqual(normalize_payload(ordinary, 'archive', 1, ALERT), [])

        explicit_alert = payload(identifier='zero-reading-alert', readings=[])
        normalized = normalize_payload(explicit_alert, 'archive', 2, ALERT)
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]['msg_type'], 'Alert')
        self.assertEqual(normalized[0]['readings'], [])
        self.assertIn('message_without_readings', normalized[0]['quality_flags'])

    def test_unknown_text_message_without_readings_is_retained_as_unknown(self):
        sample = payload(identifier='unknown-empty', readings=[])
        item = sample['data']['records'][0]['item']
        item['msgType'] = 'Update'
        item['description'] = 'Flood warning update with no mapped location.'
        normalized = normalize_payload(sample, 'archive', 1, ALERT)
        self.assertEqual(normalized[0]['msg_type'], 'Update')


class CoverageStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')

    def tearDown(self):
        self.store.connection.close()

    def test_coverage_has_unknown_dates_and_separates_zero_from_unattempted(self):
        batch = self.store.create_history_batch('2025-01-01', '2025-01-03')
        self.assertEqual(len(self.store.list_history_batch_days(batch)), 3)
        self.store.start_coverage_day(batch, '2025-01-01')
        self.store.finish_coverage_day(batch, '2025-01-01', {
            'record_count': 2, 'pages': 1, 'message_count': 0, 'snapshot_ids': [1]
        }, status='complete_zero')
        days = self.store.coverage('2025-01-01', '2025-01-03')['days']
        self.assertEqual([day['status'] for day in days], ['complete_zero', 'not_attempted', 'not_attempted'])
        self.assertEqual(days[0]['record_count'], 2)
        self.assertEqual(days[0]['message_count'], 0)
        self.assertIsNone(days[1]['message_count'])
        self.assertEqual(days[1]['observed_message_count_lower_bound'], 0)
        self.assertIsNone(days[1]['queried_undated_message_count'])
        self.assertIsNotNone(days[0]['fetch_completed_at'])
        self.assertIsNone(days[1]['fetch_completed_at'])

    def test_30_year_coverage_returns_continuous_inclusive_dates(self):
        result = self.store.coverage('1996-10-08', '2026-10-07')
        days = result['days']
        self.assertEqual(len(days), 10_957)
        self.assertEqual(days[0]['date'], '1996-10-08')
        self.assertEqual(days[-1]['date'], '2026-10-07')
        self.assertTrue(all(
            date.fromisoformat(right['date']) - date.fromisoformat(left['date']) == timedelta(days=1)
            for left, right in zip(days, days[1:])))
        self.assertTrue(all(day['status'] == 'not_attempted' and day['message_count'] is None for day in days))

    def test_30_year_history_batch_can_be_created(self):
        batch_id = self.store.create_history_batch('1996-10-08', '2026-10-07')
        batch = self.store.get_history_batch(batch_id)
        days = self.store.list_history_batch_days(batch_id)
        self.assertEqual(batch['total_days'], 10_957)
        self.assertEqual(len(days), 10_957)
        self.assertEqual(days[0]['date'], '1996-10-08')
        self.assertEqual(days[-1]['date'], '2026-10-07')

    def test_history_batch_rejects_ranges_over_12000_inclusive_days(self):
        start = date(1990, 1, 1)
        too_late = (start + timedelta(days=12_000)).isoformat()
        with self.assertRaisesRegex(ValueError, '最多覆盖 12,000 天'):
            self.store.create_history_batch(start.isoformat(), too_late)

    def test_coverage_rejects_ranges_over_12000_inclusive_days(self):
        start = date(1990, 1, 1)
        too_late = (start + timedelta(days=12_000)).isoformat()
        with self.assertRaisesRegex(ValueError, '最多查询 12,000 天'):
            self.store.coverage(start.isoformat(), too_late)

    def test_older_coverage_schema_migrates_new_query_audit_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'previous.sqlite3'
            connection = sqlite3.connect(db_path)
            connection.execute('''CREATE TABLE coverage_days (
                source TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL,
                batch_id TEXT, started_at TEXT, fetch_completed_at TEXT,
                record_count INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
                queried_message_count INTEGER NOT NULL DEFAULT 0,
                queried_alert_message_count INTEGER NOT NULL DEFAULT 0,
                queried_reading_count INTEGER NOT NULL DEFAULT 0,
                queried_unknown_msg_type_count INTEGER NOT NULL DEFAULT 0,
                snapshot_ids TEXT NOT NULL DEFAULT '[]', error TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(source,date))''')
            connection.execute('''INSERT INTO coverage_days(
                source,date,status,started_at,queried_message_count,queried_alert_message_count,
                queried_reading_count,queried_unknown_msg_type_count,updated_at)
                VALUES('archive','2025-01-01','failed','old-start',7,6,3,1,'old-row')''')
            connection.commit()
            connection.close()
            migrated = Store(db_path)
            try:
                columns = {row['name'] for row in migrated.connection.execute('PRAGMA table_info(coverage_days)')}
                self.assertIn('queried_explicit_alert_message_count', columns)
                self.assertIn('queried_undated_message_count', columns)
                self.assertIn('query_metrics_version', columns)
                old_value = migrated.connection.execute('''SELECT queried_explicit_alert_message_count
                    FROM coverage_days WHERE source='archive' AND date='2025-01-01' ''').fetchone()[0]
                self.assertIsNone(old_value)
                old_day = migrated.coverage('2025-01-01', '2025-01-01')['days'][0]
                self.assertEqual(old_day['query_metrics_version'], 0)
                self.assertIsNone(old_day['queried_message_count'])
                self.assertIsNone(old_day['queried_alert_message_count'])
                self.assertIsNone(old_day['queried_explicit_alert_message_count'])
                migrated.start_coverage_day('new-batch', '2025-01-01')
                restarted_value = migrated.connection.execute('''SELECT queried_explicit_alert_message_count
                    FROM coverage_days WHERE source='archive' AND date='2025-01-01' ''').fetchone()[0]
                self.assertEqual(restarted_value, 0)
            finally:
                migrated.connection.close()

    def test_legacy_explicit_default_schema_is_rebuilt_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'bad-explicit-default.sqlite3'
            connection = sqlite3.connect(db_path)
            connection.execute('''CREATE TABLE coverage_days (
                source TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'not_attempted',
                batch_id TEXT, started_at TEXT, fetch_completed_at TEXT,
                record_count INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
                queried_message_count INTEGER NOT NULL DEFAULT 0,
                queried_alert_message_count INTEGER NOT NULL DEFAULT 0,
                queried_explicit_alert_message_count INTEGER NOT NULL DEFAULT 0,
                queried_reading_count INTEGER NOT NULL DEFAULT 0,
                queried_unknown_msg_type_count INTEGER NOT NULL DEFAULT 0,
                snapshot_ids TEXT NOT NULL DEFAULT '[]', error TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(source,date))''')
            connection.execute('''INSERT INTO coverage_days(
                source,date,status,started_at,queried_message_count,queried_alert_message_count,
                queried_explicit_alert_message_count,queried_reading_count,updated_at)
                VALUES('archive','2025-01-02','complete_nonzero','old-start',4,4,0,2,'old-row')''')
            connection.commit()
            connection.close()

            migrated = Store(db_path)
            try:
                row = migrated.connection.execute('''SELECT * FROM coverage_days
                    WHERE source='archive' AND date='2025-01-02' ''').fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row['status'], 'complete_nonzero')
                self.assertEqual(row['queried_message_count'], 4)
                self.assertEqual(row['query_metrics_version'], 0)
                self.assertIsNone(row['queried_explicit_alert_message_count'])
                explicit_info = next(item for item in migrated.connection.execute(
                    'PRAGMA table_info(coverage_days)') if item['name'] == 'queried_explicit_alert_message_count')
                self.assertEqual(explicit_info['notnull'], 0)
                self.assertIsNone(explicit_info['dflt_value'])

                migrated.ingest(payload(identifier='primary-count', sent='2025-01-02T10:00:00+08:00'),
                                source='archive', fetched_at='2025-01-02T10:01:00+08:00')
                day = migrated.coverage('2025-01-02', '2025-01-02')['days'][0]
                self.assertEqual(day['message_count'], 1)
                self.assertEqual(day['query_metrics_version'], 0)
                self.assertIsNone(day['queried_message_count'])
                self.assertIsNone(day['queried_alert_message_count'])
                self.assertIsNone(day['queried_explicit_alert_message_count'])
            finally:
                migrated.connection.close()

    def test_rebuild_preserves_version_one_metrics_in_constrained_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'versioned-explicit-default.sqlite3'
            connection = sqlite3.connect(db_path)
            connection.execute('''CREATE TABLE coverage_days (
                source TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'not_attempted',
                batch_id TEXT, started_at TEXT, fetch_completed_at TEXT,
                record_count INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
                queried_message_count INTEGER NOT NULL DEFAULT 0,
                queried_alert_message_count INTEGER NOT NULL DEFAULT 0,
                queried_explicit_alert_message_count INTEGER NOT NULL DEFAULT 0,
                queried_reading_count INTEGER NOT NULL DEFAULT 0,
                queried_unknown_msg_type_count INTEGER NOT NULL DEFAULT 0,
                queried_undated_message_count INTEGER,
                query_metrics_version INTEGER NOT NULL DEFAULT 0,
                snapshot_ids TEXT NOT NULL DEFAULT '[]', error TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(source,date))''')
            connection.executemany('''INSERT INTO coverage_days(
                source,date,status,started_at,queried_message_count,queried_alert_message_count,
                queried_explicit_alert_message_count,queried_reading_count,
                queried_undated_message_count,query_metrics_version,updated_at)
                VALUES('archive',?, 'complete_nonzero','old-start',?,?,?, ?,0,?,'old-row')''', [
                    ('2025-01-03', 3, 3, 0, 2, 0),
                    ('2025-01-04', 4, 4, 2, 3, 1),
                ])
            connection.commit()
            connection.close()

            migrated = Store(db_path)
            try:
                version_zero = migrated.connection.execute('''SELECT * FROM coverage_days
                    WHERE source='archive' AND date='2025-01-03' ''').fetchone()
                version_one = migrated.connection.execute('''SELECT * FROM coverage_days
                    WHERE source='archive' AND date='2025-01-04' ''').fetchone()
                self.assertIsNone(version_zero['queried_explicit_alert_message_count'])
                self.assertEqual(version_zero['query_metrics_version'], 0)
                self.assertEqual(version_one['queried_explicit_alert_message_count'], 2)
                self.assertEqual(version_one['query_metrics_version'], 1)

                days = migrated.coverage('2025-01-03', '2025-01-04')['days']
                self.assertIsNone(days[0]['queried_message_count'])
                self.assertIsNone(days[0]['queried_explicit_alert_message_count'])
                self.assertEqual(days[1]['queried_message_count'], 4)
                self.assertEqual(days[1]['queried_explicit_alert_message_count'], 2)
            finally:
                migrated.connection.close()

    def test_daily_counts_use_sent_then_record_datetime_in_sgt_not_fetch_time(self):
        message = payload(identifier='sgt-boundary', sent='2024-12-31T16:30:00Z')
        self.store.ingest(message, source='archive', fetched_at='2025-01-03T12:00:00+08:00')
        unknown_date = payload(identifier='no-issued-date', readings=[reading()])
        del unknown_date['data']['records'][0]['datetime']
        del unknown_date['data']['records'][0]['item']['sent']
        self.store.ingest(unknown_date, source='archive', fetched_at='2025-01-03T12:00:00+08:00')
        result = self.store.coverage('2025-01-01', '2025-01-03')
        self.assertIsNone(result['days'][0]['message_count'])
        self.assertIsNone(result['days'][0]['alert_message_count'])
        self.assertEqual(result['days'][0]['observed_message_count_lower_bound'], 1)
        self.assertEqual(result['days'][0]['observed_alert_message_count_lower_bound'], 1)
        self.assertEqual(result['days'][0]['observed_explicit_alert_message_count_lower_bound'], 1)
        self.assertEqual(result['days'][0]['observed_reading_count_lower_bound'], 1)
        self.assertEqual(result['days'][2]['message_count'], None)
        self.assertEqual(result['days'][2]['observed_message_count_lower_bound'], 0)
        self.assertEqual(result['undated_message_count'], 1)

    def test_invalid_sent_uses_record_datetime(self):
        sample = payload(identifier='invalid-sent', sent='2025-01-01T01:00:00+08:00')
        sample['data']['records'][0]['item']['sent'] = 'not-a-date'
        self.store.ingest(sample, source='archive', fetched_at='2025-01-03T12:00:00+08:00')
        message = self.store.get_messages('archive')[0]
        self.assertEqual(message['sent'], '2025-01-01T01:00:00+08:00')
        self.assertIn('invalid_sent_replaced_by_record_datetime', message['quality_flags'])
        self.assertIsNone(self.store.coverage('2025-01-01', '2025-01-01')['days'][0]['message_count'])
        self.assertEqual(self.store.coverage('2025-01-01', '2025-01-01')['days'][0]['observed_message_count_lower_bound'], 1)

    def test_undated_messages_stay_out_of_calendar_and_are_counted_per_query(self):
        sample = payload(identifier='undated-alert', readings=[reading()])
        del sample['data']['records'][0]['datetime']
        del sample['data']['records'][0]['item']['sent']
        cancel = payload(identifier='undated-cancel', msg_type='Cancel', readings=[])
        del cancel['data']['records'][0]['datetime']
        del cancel['data']['records'][0]['item']['sent']
        response = {'code': 0, 'data': {'records': [
            sample['data']['records'][0], cancel['data']['records'][0]]}}
        result = fetch_flood(self.store, 'archive', '2025-01-01',
            lambda *args, **kwargs: (200, json.dumps(response)))
        self.assertEqual(result['undated_message_count'], 1)
        batch_id = self.store.create_history_batch('2025-01-01', '2025-01-01')
        self.store.start_coverage_day(batch_id, '2025-01-01')
        self.store.finish_coverage_day(batch_id, '2025-01-01', result, status='complete_nonzero')
        day = self.store.coverage('2025-01-01', '2025-01-01')['days'][0]
        self.assertEqual(day['status'], 'complete_nonzero')
        self.assertEqual(day['queried_undated_message_count'], 1)
        self.assertEqual(day['queried_message_count'], 1)
        self.assertEqual(day['message_count'], 0)
        self.assertEqual(day['reading_count'], 0)

        ingested = self.store.ingest(response, source='archive', fetched_at='2025-01-01T12:00:00+08:00')
        self.assertEqual(ingested['undated_message_count'], 1)
        self.assertEqual(self.store.coverage('2025-01-01', '2025-01-01')['undated_message_count'], 1)

    def test_undated_archive_message_is_not_deduped_across_requested_dates(self):
        sample = payload(identifier=None, readings=[reading()])
        del sample['data']['records'][0]['datetime']
        del sample['data']['records'][0]['item']['sent']
        requester = lambda *args, **kwargs: (200, json.dumps(sample))
        fetch_flood(self.store, 'archive', '2025-01-01', requester)
        fetch_flood(self.store, 'archive', '2025-01-02', requester)
        self.assertEqual(len(self.store.get_messages('archive')), 2)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 2)
        fetch_flood(self.store, 'archive', '2025-01-01', requester)
        self.assertEqual(len(self.store.get_messages('archive')), 2)

    def test_unattempted_dates_expose_counts_only_as_local_lower_bounds(self):
        self.store.ingest(payload(identifier='known-but-not-covered'), source='archive', fetched_at=ALERT)
        with_message = self.store.coverage('2025-01-01', '2025-01-01')['days'][0]
        no_message = self.store.coverage('2025-01-02', '2025-01-02')['days'][0]
        self.assertEqual(with_message['status'], 'not_attempted')
        self.assertIsNone(with_message['message_count'])
        self.assertEqual(with_message['observed_message_count_lower_bound'], 1)
        self.assertEqual(no_message['status'], 'not_attempted')
        self.assertIsNone(no_message['message_count'])
        self.assertEqual(no_message['observed_message_count_lower_bound'], 0)

    def test_cancel_excluded_empty_alert_included_and_unknown_type_counted(self):
        self.store.ingest(payload(identifier='known', readings=[reading()]), 'archive', ALERT)
        self.store.ingest(payload(identifier='zero', readings=[], sent=CANCEL), 'archive', CANCEL)
        unknown = payload(identifier='unknown', sent='2025-01-01T00:03:00+08:00')
        unknown['data']['records'][0]['item']['msgType'] = ''
        self.store.ingest(unknown, 'archive', '2025-01-01T00:03:00+08:00')
        self.store.ingest(payload(identifier='cancel', sent='2025-01-01T00:04:00+08:00', msg_type='Cancel', readings=[]),
                          'archive', '2025-01-01T00:04:00+08:00')
        day = self.store.coverage('2025-01-01', '2025-01-01')['days'][0]
        self.assertIsNone(day['message_count'])
        self.assertIsNone(day['alert_message_count'])
        self.assertEqual(day['observed_message_count_lower_bound'], 3)
        self.assertEqual(day['observed_alert_message_count_lower_bound'], 3)
        self.assertEqual(day['observed_explicit_alert_message_count_lower_bound'], 2)
        self.assertEqual(day['observed_unknown_msg_type_count_lower_bound'], 1)
        self.assertEqual(day['observed_reading_count_lower_bound'], 2)

    def test_events_optional_date_filter_keeps_existing_shape(self):
        self.store.ingest(payload(identifier='jan-1', sent='2025-01-01T23:59:00+08:00'), 'archive', ALERT)
        self.store.ingest(payload(identifier='jan-2', sent='2025-01-02T00:01:00+08:00'), 'archive', ALERT)
        result = self.store.events('archive', '2025-01-02T23:59:59+08:00', '2025-01-02')
        self.assertEqual(result['metadata']['message_count'], 1)
        self.assertEqual(len(result['features']), 1)
        self.assertEqual(len(result['timeline']), 1)

    def test_paginated_fetch_returns_complete_daily_counts_and_original_snapshots(self):
        first = payload(identifier='one')
        first['data']['paginationToken'] = 'page-2'
        second = payload(identifier='two', msg_type='Update')
        response = iter([first, second])
        result = fetch_flood(self.store, 'archive', '2025-01-01',
            lambda *args, **kwargs: (200, json.dumps(next(response))))
        self.assertTrue(result['complete'])
        self.assertEqual(result['pages'], 2)
        self.assertEqual(result['record_count'], 2)
        self.assertEqual(result['message_count'], 2)
        self.assertEqual(result['alert_message_count'], 2)  # legacy broad non-Cancel count
        self.assertEqual(result['explicit_alert_message_count'], 1)
        self.assertEqual(result['unknown_msg_type_count'], 1)
        self.assertEqual(len(result['snapshot_ids']), 2)
        batch = self.store.create_history_batch('2025-01-01', '2025-01-01')
        self.store.start_coverage_day(batch, '2025-01-01')
        self.store.finish_coverage_day(batch, '2025-01-01', result, status='complete_nonzero')
        day = self.store.coverage('2025-01-01', '2025-01-01')['days'][0]
        self.assertEqual(day['alert_message_count'], 2)
        self.assertEqual(day['queried_alert_message_count'], 2)
        self.assertEqual(day['explicit_alert_message_count'], 1)
        self.assertEqual(day['queried_explicit_alert_message_count'], 1)
        self.assertEqual(day['query_metrics_version'], 1)

    def test_fetch_contract_counts_zero_reading_alert_but_not_empty_observation_or_cancel(self):
        explicit_alert = payload(identifier='alert-without-locations', readings=[])
        ordinary = payload(identifier='empty-observation', readings=[])
        ordinary['data']['records'][0]['item']['msgType'] = 'Observation'
        ordinary['data']['records'][0]['item'].pop('description', None)
        cancel = payload(identifier='cancel-only', msg_type='Cancel', readings=[])
        combined = {'code': 0, 'data': {'records': [
            explicit_alert['data']['records'][0], ordinary['data']['records'][0], cancel['data']['records'][0]]}}
        result = fetch_flood(self.store, 'archive', '2025-01-01',
            lambda *args, **kwargs: (200, json.dumps(combined)))
        self.assertEqual(result['record_count'], 3)
        self.assertEqual(result['message_count'], 1)
        self.assertEqual(result['alert_message_count'], 1)
        self.assertEqual(result['explicit_alert_message_count'], 1)
        self.assertEqual(result['reading_count'], 0)
        self.assertEqual(result['messages_received'], 2)  # Alert and Cancel; no ordinary empty observation


class BackfillManagerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.calls = []

        def fake_fetch(store, source, day):
            self.calls.append(day)
            return {'complete': True, 'pages': 1, 'record_count': 0, 'messages_added': 0,
                    'snapshot_ids': [], 'message_count': 0, 'alert_message_count': 0,
                    'reading_count': 0, 'unknown_msg_type_count': 0}

        self.manager = BackfillManager(self.store, fake_fetch)

    def tearDown(self):
        self.store.connection.close()

    def wait(self, batch_id, manager=None):
        worker = (manager or self.manager).workers[batch_id]['thread']
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def test_budgeted_batch_resumes_from_first_unfinished_day(self):
        batch = self.manager.start('2025-01-01', '2025-01-03', budget=1)
        batch_id = batch['batch_id']
        self.wait(batch_id)
        self.assertEqual(self.calls, ['2025-01-01'])
        self.assertEqual(self.manager.status(batch_id)['status'], 'budget_exhausted')

        self.manager.start(batch_id=batch_id, budget=3)
        self.wait(batch_id)
        self.assertEqual(self.calls, ['2025-01-01', '2025-01-02', '2025-01-03'])
        self.assertEqual(self.manager.status(batch_id)['status'], 'completed')
        self.assertEqual([day['status'] for day in self.store.coverage('2025-01-01', '2025-01-03')['days']],
                         ['complete_zero'] * 3)

    def test_invalid_budget_does_not_create_a_batch(self):
        with self.assertRaises(ValueError):
            self.manager.start('2025-01-01', '2025-01-02', budget=0)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM history_batches').fetchone()[0], 0)

    def test_404_is_unavailable_and_does_not_become_zero(self):
        def missing(*args):
            raise ServiceError('HTTP 404', status_code=404)
        manager = BackfillManager(self.store, missing)
        batch = manager.start('2025-01-01', '2025-01-01')
        manager.workers[batch['batch_id']]['thread'].join(timeout=3)
        day = self.store.coverage('2025-01-01', '2025-01-01')['days'][0]
        self.assertEqual(day['status'], 'unavailable')
        self.assertIsNone(day['fetch_completed_at'])
        self.assertIsNone(day['message_count'])

    def test_unavailable_days_keep_batch_in_gap_state_until_explicit_retry(self):
        calls = []

        def fetch(store, source, day):
            calls.append(day)
            if day == '2025-01-01' and calls.count(day) == 1:
                raise ServiceError('HTTP 404', status_code=404)
            return {'complete': True, 'pages': 1, 'record_count': 0, 'messages_added': 0,
                    'snapshot_ids': [], 'message_count': 0, 'alert_message_count': 0,
                    'explicit_alert_message_count': 0, 'reading_count': 0,
                    'unknown_msg_type_count': 0}

        manager = BackfillManager(self.store, fetch)
        batch = manager.start('2025-01-01', '2025-01-02')
        batch_id = batch['batch_id']
        self.wait(batch_id, manager)
        self.assertEqual(manager.status(batch_id)['status'], 'completed_with_gaps')
        self.assertEqual(self.store.coverage('2025-01-01', '2025-01-02')['days'][0]['status'], 'unavailable')

        manager.start(batch_id=batch_id)
        self.wait(batch_id, manager)
        self.assertEqual(calls, ['2025-01-01', '2025-01-02'])
        self.assertEqual(manager.status(batch_id)['status'], 'completed_with_gaps')

        manager.start(batch_id=batch_id, retry_unavailable=True)
        self.wait(batch_id, manager)
        self.assertEqual(calls, ['2025-01-01', '2025-01-02', '2025-01-01'])
        self.assertEqual(manager.status(batch_id)['status'], 'completed')

    def test_page_failure_preserves_partial_messages_but_marks_day_incomplete(self):
        first = payload(identifier='partial-page')
        first['data']['paginationToken'] = 'next'
        responses = iter([(200, json.dumps(first))])

        def request(*args, **kwargs):
            try:
                return next(responses)
            except StopIteration:
                raise ServiceError('HTTP 503', status_code=503)

        manager = BackfillManager(self.store,
            lambda store, source, day: fetch_flood(store, source, day, request_fn=request))
        batch = manager.start('2025-01-01', '2025-01-01')
        manager.workers[batch['batch_id']]['thread'].join(timeout=3)
        day = self.store.coverage('2025-01-01', '2025-01-01')['days'][0]
        self.assertEqual(day['status'], 'incomplete')
        self.assertIsNone(day['fetch_completed_at'])
        self.assertIsNone(day['message_count'])
        self.assertEqual(day['observed_message_count_lower_bound'], 1)
        self.assertEqual(day['queried_message_count'], 1)
        self.assertEqual(day['query_metrics_version'], 1)
        self.assertEqual(day['queried_explicit_alert_message_count'], 1)
        self.assertIn('503', day['error'])
        self.assertEqual(len(day['snapshot_ids']), 2)  # returned page plus stored request failure


class CoverageApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(':memory:')
        cls.manager = BackfillManager(cls.store, lambda store, source, day: {
            'complete': True, 'pages': 1, 'record_count': 0, 'messages_added': 0,
            'snapshot_ids': [], 'message_count': 0, 'alert_message_count': 0,
            'reading_count': 0, 'unknown_msg_type_count': 0})
        cls.server = make_server(cls.store, port=0, backfill_manager=cls.manager)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.store.connection.close()

    def test_coverage_endpoint_and_date_filtered_event_endpoint(self):
        self.store.ingest(payload(), 'archive', ALERT)
        query = urlencode({'source': 'archive', 'start': '2025-01-01', 'end': '2025-01-02'})
        with urllib.request.urlopen(self.base + '/api/coverage?' + query) as response:
            body = json.load(response)
        self.assertEqual(body['source'], 'archive')
        self.assertEqual(len(body['days']), 2)
        self.assertEqual(body['days'][0]['status'], 'not_attempted')
        self.assertIsNone(body['days'][0]['message_count'])
        self.assertEqual(body['days'][0]['observed_message_count_lower_bound'], 1)
        query = urlencode({'source': 'archive', 'at': '2025-01-01T23:59:59+08:00', 'date': '2025-01-01'})
        with urllib.request.urlopen(self.base + '/api/events?' + query) as response:
            event = json.load(response)
        self.assertEqual(event['metadata']['message_count'], 1)

    def test_backfill_start_status_and_resume_endpoints(self):
        request = urllib.request.Request(self.base + '/api/backfill',
            data=json.dumps({'start': '2025-02-01', 'end': '2025-02-02', 'budget': 1}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 202)
            batch = json.load(response)
        batch_id = batch['batch_id']

        def read_status():
            with urllib.request.urlopen(self.base + '/api/backfill/' + batch_id) as response:
                return json.load(response)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = read_status()
            if status['status'] == 'budget_exhausted' and not status['worker_running']:
                break
            time.sleep(0.01)
        self.assertEqual(status['status'], 'budget_exhausted')
        request = urllib.request.Request(self.base + '/api/backfill/' + batch_id + '/resume',
            data=json.dumps({'budget': 1}).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 202)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = read_status()
            if not status['worker_running']:
                break
            time.sleep(0.01)
        self.assertEqual(status['status'], 'completed')
        days = self.store.coverage('2025-02-01', '2025-02-02')['days']
        self.assertEqual([day['status'] for day in days], ['complete_zero', 'complete_zero'])


if __name__ == '__main__':
    unittest.main()
