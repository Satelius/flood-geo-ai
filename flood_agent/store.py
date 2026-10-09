import json
import sqlite3
import threading
import uuid
from functools import wraps
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from .normalize import normalize_payload, message_status, parse_time

MAX_COVERAGE_DAYS = 12_000


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def synchronized(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return function(self, *args, **kwargs)
    return wrapped


class Store:
    def __init__(self, path='data/flood.sqlite3'):
        self.path = str(path)
        self.lock = threading.RLock()
        if self.path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
          request_json TEXT NOT NULL, http_status INTEGER, raw_text TEXT, error TEXT,
          parsed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages (
          source TEXT NOT NULL, message_key TEXT NOT NULL, body TEXT NOT NULL,
          PRIMARY KEY(source, message_key));
        CREATE TABLE IF NOT EXISTS extractions (
          id INTEGER PRIMARY KEY, source TEXT, message_key TEXT, created_at TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS history_batches (
          batch_id TEXT PRIMARY KEY, source TEXT NOT NULL, start_date TEXT NOT NULL,
          end_date TEXT NOT NULL, status TEXT NOT NULL, budget INTEGER,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS coverage_days (
          source TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'not_attempted',
          batch_id TEXT, started_at TEXT, fetch_completed_at TEXT,
          record_count INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
          queried_message_count INTEGER NOT NULL DEFAULT 0,
          queried_alert_message_count INTEGER NOT NULL DEFAULT 0,
          queried_explicit_alert_message_count INTEGER,
          queried_reading_count INTEGER NOT NULL DEFAULT 0,
          queried_unknown_msg_type_count INTEGER NOT NULL DEFAULT 0,
          queried_undated_message_count INTEGER,
          query_metrics_version INTEGER NOT NULL DEFAULT 0,
          snapshot_ids TEXT NOT NULL DEFAULT '[]', error TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(source, date));
        CREATE INDEX IF NOT EXISTS coverage_days_batch ON coverage_days(batch_id, date);
        ''')
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            coverage_info = {row['name']: dict(row) for row in self.connection.execute('PRAGMA table_info(coverage_days)')}
            coverage_columns = set(coverage_info)
            if 'queried_explicit_alert_message_count' not in coverage_columns:
                self.connection.execute('''ALTER TABLE coverage_days ADD COLUMN
                    queried_explicit_alert_message_count INTEGER''')
            if 'queried_undated_message_count' not in coverage_columns:
                self.connection.execute('''ALTER TABLE coverage_days ADD COLUMN
                    queried_undated_message_count INTEGER''')
            if 'query_metrics_version' not in coverage_columns:
                self.connection.execute('''ALTER TABLE coverage_days ADD COLUMN
                    query_metrics_version INTEGER NOT NULL DEFAULT 0''')
            explicit_column = coverage_info.get('queried_explicit_alert_message_count')
            if explicit_column and (explicit_column['notnull'] or explicit_column['dflt_value'] is not None):
                self._rebuild_coverage_days()
            else:
                self.connection.execute('CREATE INDEX IF NOT EXISTS coverage_days_batch ON coverage_days(batch_id, date)')
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _rebuild_coverage_days(self):
        columns = (
            'source', 'date', 'status', 'batch_id', 'started_at', 'fetch_completed_at',
            'record_count', 'pages', 'queried_message_count', 'queried_alert_message_count',
            'queried_explicit_alert_message_count', 'queried_reading_count',
            'queried_unknown_msg_type_count', 'queried_undated_message_count',
            'query_metrics_version', 'snapshot_ids', 'error', 'updated_at')
        defaults = {
            'status': "'not_attempted'", 'record_count': '0', 'pages': '0',
            'queried_message_count': '0', 'queried_alert_message_count': '0',
            'queried_reading_count': '0', 'queried_unknown_msg_type_count': '0',
            'query_metrics_version': '0', 'snapshot_ids': "'[]'", 'updated_at': "''"
        }
        self.connection.execute('ALTER TABLE coverage_days RENAME TO coverage_days_legacy')
        self.connection.execute('''CREATE TABLE coverage_days (
          source TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'not_attempted',
          batch_id TEXT, started_at TEXT, fetch_completed_at TEXT,
          record_count INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
          queried_message_count INTEGER NOT NULL DEFAULT 0,
          queried_alert_message_count INTEGER NOT NULL DEFAULT 0,
          queried_explicit_alert_message_count INTEGER,
          queried_reading_count INTEGER NOT NULL DEFAULT 0,
          queried_unknown_msg_type_count INTEGER NOT NULL DEFAULT 0,
          queried_undated_message_count INTEGER,
          query_metrics_version INTEGER NOT NULL DEFAULT 0,
          snapshot_ids TEXT NOT NULL DEFAULT '[]', error TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(source, date))''')
        old_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(coverage_days_legacy)')}
        select_expressions = []
        for column in columns:
            if column == 'queried_explicit_alert_message_count':
                if 'query_metrics_version' in old_columns:
                    select_expressions.append(
                        'CASE WHEN query_metrics_version=1 THEN queried_explicit_alert_message_count ELSE NULL END')
                else:
                    select_expressions.append('NULL')
            elif column == 'query_metrics_version':
                select_expressions.append(column if column in old_columns else '0')
            elif column in old_columns:
                select_expressions.append(column)
            else:
                select_expressions.append(defaults.get(column, 'NULL'))
        column_list = ','.join(columns)
        values = ','.join(select_expressions)
        self.connection.execute(
            f'INSERT INTO coverage_days ({column_list}) SELECT {values} FROM coverage_days_legacy')
        self.connection.execute('DROP TABLE coverage_days_legacy')
        self.connection.execute('CREATE INDEX IF NOT EXISTS coverage_days_batch ON coverage_days(batch_id, date)')

    @synchronized
    def save_raw(self, source, raw_text, request=None, status=200, error=None, fetched_at=None):
        if source not in ('demo', 'live', 'archive'):
            raise ValueError('source 必须为 demo、live 或 archive')
        cursor = self.connection.execute('INSERT INTO snapshots(source,fetched_at,request_json,http_status,raw_text,error) VALUES(?,?,?,?,?,?)',
            (source, fetched_at or utc_now(), json.dumps(request or {}), status, raw_text, error))
        self.connection.commit()
        return cursor.lastrowid

    @synchronized
    def process_snapshot(self, snapshot_id):
        row = self.connection.execute('SELECT * FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
        try:
            request_context = json.loads(row['request_json'])
            messages = normalize_payload(json.loads(row['raw_text']), row['source'], row['id'],
                                         row['fetched_at'], request_context)
        except (ValueError, TypeError):
            self.connection.execute('UPDATE snapshots SET error=? WHERE id=?', ('响应无法按 PUB schema 解析', snapshot_id))
            self.connection.commit()
            raise
        added = 0
        for message in messages:
            cursor = self.connection.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?)',
                (row['source'], message['message_key'], json.dumps(message, ensure_ascii=False)))
            added += cursor.rowcount
        self.connection.execute('UPDATE snapshots SET parsed=1 WHERE id=?', (snapshot_id,))
        self.connection.commit()
        counted = [message for message in messages if message['msg_type'].lower() != 'cancel']
        summaries = [{'message_key': message['message_key'], 'msg_type': message['msg_type'],
                      'reading_count': len(message['readings']),
                      'has_valid_sent': parse_time(message.get('sent')) is not None} for message in messages]
        return {'snapshot_id': snapshot_id, 'messages_added': added, 'messages_received': len(messages),
                'message_count': len(counted),
                'alert_message_count': len(counted),
                'explicit_alert_message_count': sum(message['msg_type'].lower() == 'alert' for message in counted),
                'reading_count': sum(len(message['readings']) for message in counted),
                'unknown_msg_type_count': sum(message['msg_type'].lower() not in ('alert', 'cancel') for message in counted),
                'undated_message_count': sum(
                    not summary['has_valid_sent'] and summary['msg_type'].lower() != 'cancel'
                    for summary in summaries),
                'message_summaries': summaries}

    @synchronized
    def ingest(self, payload, source='demo', fetched_at=None):
        snapshot_id = self.save_raw(source, json.dumps(payload, ensure_ascii=False), fetched_at=fetched_at)
        return self.process_snapshot(snapshot_id)

    @synchronized
    def get_messages(self, source):
        return [json.loads(row['body']) for row in self.connection.execute('SELECT body FROM messages WHERE source=? ORDER BY message_key', (source,))]

    @staticmethod
    def validate_date(value):
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError('日期必须为 YYYY-MM-DD') from None
        if parsed.isoformat() != value:
            raise ValueError('日期必须为 YYYY-MM-DD')
        return parsed

    @synchronized
    def create_history_batch(self, start_date, end_date, budget=None):
        start = self.validate_date(start_date)
        end = self.validate_date(end_date)
        if end < start:
            raise ValueError('end 必须不早于 start')
        day_count = (end - start).days + 1
        if day_count > MAX_COVERAGE_DAYS:
            raise ValueError(f'单个历史回填批次最多覆盖 {MAX_COVERAGE_DAYS:,} 天')
        if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or budget < 1):
            raise ValueError('budget 必须是正整数')
        batch_id = uuid.uuid4().hex
        now = utc_now()
        self.connection.execute('INSERT INTO history_batches VALUES(?,?,?,?,?,?,?,?,NULL)',
            (batch_id, 'archive', start_date, end_date, 'queued', budget, now, now))
        for offset in range(day_count):
            day = (start + timedelta(days=offset)).isoformat()
            self.connection.execute('''INSERT OR IGNORE INTO coverage_days(source,date,status,batch_id,updated_at)
                VALUES('archive',?,'not_attempted',?,?)''', (day, batch_id, now))
        self.connection.execute('''UPDATE coverage_days SET batch_id=?,updated_at=?
            WHERE source='archive' AND date BETWEEN ? AND ? AND status!='running' ''',
            (batch_id, now, start_date, end_date))
        self.connection.commit()
        return batch_id

    @synchronized
    def get_history_batch(self, batch_id):
        row = self.connection.execute('SELECT * FROM history_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if row is None:
            return None
        batch = dict(row)
        counts = {row['status']: row['count'] for row in self.connection.execute(
            'SELECT status,COUNT(*) AS count FROM coverage_days WHERE batch_id=? GROUP BY status', (batch_id,))}
        batch['day_status_counts'] = counts
        batch['attempted_days'] = sum(count for status, count in counts.items() if status != 'not_attempted')
        batch['total_days'] = (self.validate_date(batch['end_date']) - self.validate_date(batch['start_date'])).days + 1
        return batch

    @synchronized
    def list_history_batch_days(self, batch_id, retry_unavailable=False):
        batch = self.get_history_batch(batch_id)
        if batch is None:
            raise ValueError('找不到历史回填批次')
        statuses = ['not_attempted', 'failed', 'incomplete', 'running']
        if retry_unavailable:
            statuses.append('unavailable')
        placeholders = ','.join('?' for _ in statuses)
        rows = self.connection.execute(f'''SELECT date,status FROM coverage_days
            WHERE batch_id=? AND status IN ({placeholders}) ORDER BY date''', (batch_id, *statuses)).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def set_history_batch_status(self, batch_id, status, error=None):
        self.connection.execute('UPDATE history_batches SET status=?,updated_at=?,error=? WHERE batch_id=?',
                                (status, utc_now(), error, batch_id))
        self.connection.commit()

    @synchronized
    def start_coverage_day(self, batch_id, day):
        now = utc_now()
        self.connection.execute('''UPDATE coverage_days SET status='running',batch_id=?,started_at=?,
            fetch_completed_at=NULL,error=NULL,record_count=0,pages=0,queried_message_count=0,
            queried_alert_message_count=0,queried_explicit_alert_message_count=0,
            queried_reading_count=0,queried_unknown_msg_type_count=0,
            queried_undated_message_count=NULL,query_metrics_version=0,snapshot_ids='[]',updated_at=?
            WHERE source='archive' AND date=?''',
            (batch_id, now, now, day))
        self.connection.commit()
        return now

    @synchronized
    def finish_coverage_day(self, batch_id, day, result, status='complete_nonzero', error=None):
        now = utc_now()
        completed = now if status in ('complete_nonzero', 'complete_zero') else None
        required_query_metrics = {
            'message_count', 'alert_message_count', 'explicit_alert_message_count',
            'reading_count', 'unknown_msg_type_count', 'undated_message_count'}
        metrics_version = 1 if required_query_metrics.issubset(result) else 0
        self.connection.execute('''UPDATE coverage_days SET status=?,fetch_completed_at=?,record_count=?,pages=?,
            queried_message_count=?,queried_alert_message_count=?,queried_reading_count=?,
            queried_explicit_alert_message_count=?,queried_unknown_msg_type_count=?,
            queried_undated_message_count=?,query_metrics_version=?,snapshot_ids=?,error=?,updated_at=?
            WHERE source='archive' AND date=? AND batch_id=?''',
            (status, completed, int(result.get('record_count', 0)), int(result.get('pages', 0)),
             int(result.get('message_count', 0)), int(result.get('alert_message_count', 0)),
             int(result.get('reading_count', 0)), int(result.get('explicit_alert_message_count', 0)),
             int(result.get('unknown_msg_type_count', 0)),
             int(result.get('undated_message_count', 0)) if result.get('undated_message_count') is not None else None,
             metrics_version, json.dumps(result.get('snapshot_ids', [])), error, now, day, batch_id))
        self.connection.commit()

    @synchronized
    def recover_running_days(self, batch_id):
        now = utc_now()
        self.connection.execute('''UPDATE coverage_days SET status='incomplete',error=?,updated_at=?
            WHERE batch_id=? AND status='running' ''', ('进程在日期任务完成前退出；可重新采集此日期', now, batch_id))
        self.connection.commit()

    @synchronized
    def coverage(self, start_date, end_date, source='archive'):
        if source not in ('archive', 'live'):
            raise ValueError('coverage source 必须为 archive 或 live')
        start = self.validate_date(start_date)
        end = self.validate_date(end_date)
        if end < start:
            raise ValueError('end 必须不早于 start')
        if (end - start).days + 1 > MAX_COVERAGE_DAYS:
            raise ValueError(f'单次 coverage 最多查询 {MAX_COVERAGE_DAYS:,} 天')
        rows = {row['date']: dict(row) for row in self.connection.execute(
            'SELECT * FROM coverage_days WHERE source=? AND date BETWEEN ? AND ?',
            (source, start_date, end_date))}
        counts = defaultdict(lambda: {'message_count': 0, 'alert_message_count': 0,
            'explicit_alert_message_count': 0, 'reading_count': 0, 'unknown_msg_type_count': 0})
        undated_message_count = 0
        sgt = timezone(timedelta(hours=8))
        for message in self.get_messages(source):
            if message['msg_type'].lower() == 'cancel':
                continue
            sent = parse_time(message.get('sent'))
            if sent is None:
                undated_message_count += 1
                continue
            message_date = sent.astimezone(sgt).date().isoformat()
            if message_date < start_date or message_date > end_date:
                continue
            bucket = counts[message_date]
            bucket['message_count'] += 1
            bucket['alert_message_count'] += 1
            bucket['explicit_alert_message_count'] += message['msg_type'].lower() == 'alert'
            bucket['reading_count'] += len(message['readings'])
            bucket['unknown_msg_type_count'] += message['msg_type'].lower() not in ('alert', 'cancel')
        days = []
        for offset in range((end - start).days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            task = rows.get(day, {})
            item = {'date': day, 'status': task.get('status', 'not_attempted'),
                    'fetch_completed_at': task.get('fetch_completed_at'),
                    'record_count': task.get('record_count') if task.get('started_at') else None,
                    'error': task.get('error'), 'pages': task.get('pages') if task.get('started_at') else None,
                    'batch_id': task.get('batch_id')}
            metric_names = ('message_count', 'alert_message_count', 'explicit_alert_message_count',
                            'reading_count', 'unknown_msg_type_count')
            if item['status'] in ('complete_zero', 'complete_nonzero'):
                item.update(counts[day])
            else:
                item.update({name: None for name in metric_names})
            item.update({f'observed_{name}_lower_bound': counts[day][name] for name in metric_names})
            query_attempted = bool(task.get('started_at'))
            query_metrics_current = query_attempted and task.get('query_metrics_version') == 1
            item.update(query_metrics_version=task.get('query_metrics_version') if query_attempted else None,
                queried_message_count=task.get('queried_message_count') if query_metrics_current else None,
                queried_alert_message_count=task.get('queried_alert_message_count') if query_metrics_current else None,
                queried_reading_count=task.get('queried_reading_count') if query_metrics_current else None,
                queried_explicit_alert_message_count=task.get('queried_explicit_alert_message_count') if query_metrics_current else None,
                queried_unknown_msg_type_count=task.get('queried_unknown_msg_type_count') if query_metrics_current else None,
                queried_undated_message_count=task.get('queried_undated_message_count') if query_metrics_current else None,
                snapshot_ids=json.loads(task.get('snapshot_ids', '[]')))
            days.append(item)
        return {'source': source, 'start': start_date, 'end': end_date, 'days': days,
                'undated_message_count': undated_message_count}

    @synchronized
    def events(self, source='demo', at=None, date_filter=None):
        if source not in ('demo', 'live', 'archive'):
            raise ValueError('无效 source')
        moment = parse_time(at) if at else datetime.now(timezone.utc)
        if moment is None:
            raise ValueError('at 必须是 ISO 时间')
        snapshots = list(self.connection.execute('SELECT * FROM snapshots WHERE source=? AND parsed=1 ORDER BY fetched_at', (source,)))
        observation = parse_time(snapshots[0]['fetched_at']) if snapshots else None
        messages = [message for message in self.get_messages(source) if (parse_time(message['sent']) or parse_time(message['fetched_at'])) <= moment]
        cancel_messages = [message for message in messages if message['msg_type'].lower() == 'cancel']
        display_messages = messages
        if date_filter is not None:
            selected_date = self.validate_date(date_filter).isoformat()
            sgt = timezone(timedelta(hours=8))
            display_messages = [message for message in messages if parse_time(message.get('sent')) is not None
                and parse_time(message['sent']).astimezone(sgt).date().isoformat() == selected_date]
        features, timeline, histogram = [], [], {}
        counts = dict.fromkeys(('observed_active', 'cancelled', 'expired', 'unknown'), 0)
        unlocated = 0
        for message in display_messages:
            status = message_status(message, cancel_messages, moment, observation)
            if message['msg_type'].lower() != 'cancel':
                counts[status] += 1
            issued_time = parse_time(message['sent']) or parse_time(message['fetched_at'])
            date = issued_time.astimezone(timezone(timedelta(hours=8))).date().isoformat()
            bucket = histogram.setdefault(date, {'date': date, 'message_count': 0, 'reading_count': 0,
                'alert_message_count': 0, 'explicit_alert_message_count': 0,
                'unknown_msg_type_count': 0, 'cancel_message_count': 0})
            bucket['message_count'] += 1
            if message['msg_type'].lower() == 'cancel':
                bucket['cancel_message_count'] += 1
            else:
                bucket['alert_message_count'] += 1
                bucket['reading_count'] += len(message['readings'])
                bucket['explicit_alert_message_count'] += message['msg_type'].lower() == 'alert'
                bucket['unknown_msg_type_count'] += message['msg_type'].lower() not in ('alert', 'cancel')
            timeline.append({key: message[key] for key in ('message_key', 'msg_type', 'sent', 'description')} | {'status': status, 'reading_count': len(message['readings'])})
            if message['msg_type'].lower() == 'cancel':
                continue
            for reading in message['readings']:
                if reading['coordinates'] is None:
                    unlocated += 1
                    continue
                reading_status = status
                if status == 'observed_active' and parse_time(reading['expires']) and parse_time(reading['expires']) <= moment:
                    reading_status = 'expired'
                properties = {key: message[key] for key in ('message_key', 'sent', 'msg_type', 'source', 'raw_snapshot_id')}
                properties.update({key: value for key, value in reading.items() if key != 'coordinates'})
                properties.update(status=reading_status, synthetic=source == 'demo', location_role='alert_broadcast_center',
                                  quality_flags=message['quality_flags'] + reading['quality_flags'])
                features.append({'type': 'Feature', 'id': reading['reading_key'], 'geometry': {'type': 'Point', 'coordinates': reading['coordinates']}, 'properties': properties})
        metadata = {'source': source, 'synthetic': source == 'demo', 'at': moment.isoformat(),
            'observation_start': snapshots[0]['fetched_at'] if snapshots else None,
            'last_successful_fetch': snapshots[-1]['fetched_at'] if snapshots else None,
            'message_count': len(display_messages), 'reading_count': sum(len(message['readings']) for message in display_messages if message['msg_type'].lower() != 'cancel'),
            'alert_message_count': sum(message['msg_type'].lower() != 'cancel' for message in display_messages),
            'explicit_alert_message_count': sum(message['msg_type'].lower() == 'alert' for message in display_messages),
            'unknown_msg_type_count': sum(message['msg_type'].lower() not in ('alert', 'cancel') for message in display_messages),
            'cancel_message_count': sum(message['msg_type'].lower() == 'cancel' for message in display_messages),
            'unlocated_reading_count': unlocated, 'status_counts': counts,
            'coverage_note': '仅反映本地观测窗口；广播中心和半径不表示淹水范围。'}
        return {'type': 'FeatureCollection', 'features': features, 'metadata': metadata,
                'timeline': sorted(timeline, key=lambda item: item['sent'] or ''), 'histogram': [histogram[key] for key in sorted(histogram)]}

    @synchronized
    def status(self):
        return {'sources': {source: self.events(source)['metadata'] for source in ('demo', 'live', 'archive')},
                'snapshot_count': self.connection.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]}

    @synchronized
    def save_extraction(self, source, message_key, body):
        self.connection.execute('INSERT INTO extractions(source,message_key,created_at,body) VALUES(?,?,?,?)',
            (source, message_key, utc_now(), json.dumps(body, ensure_ascii=False)))
        self.connection.commit()
