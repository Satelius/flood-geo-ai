import json
import sqlite3
import threading
from functools import wraps
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .normalize import normalize_payload, message_status, parse_time


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
        ''')
        self.connection.commit()

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
            messages = normalize_payload(json.loads(row['raw_text']), row['source'], row['id'], row['fetched_at'])
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
        return {'snapshot_id': snapshot_id, 'messages_added': added, 'messages_received': len(messages)}

    @synchronized
    def ingest(self, payload, source='demo', fetched_at=None):
        snapshot_id = self.save_raw(source, json.dumps(payload, ensure_ascii=False), fetched_at=fetched_at)
        return self.process_snapshot(snapshot_id)

    @synchronized
    def get_messages(self, source):
        return [json.loads(row['body']) for row in self.connection.execute('SELECT body FROM messages WHERE source=? ORDER BY message_key', (source,))]

    @synchronized
    def events(self, source='demo', at=None):
        if source not in ('demo', 'live', 'archive'):
            raise ValueError('无效 source')
        moment = parse_time(at) if at else datetime.now(timezone.utc)
        if moment is None:
            raise ValueError('at 必须是 ISO 时间')
        snapshots = list(self.connection.execute('SELECT * FROM snapshots WHERE source=? AND parsed=1 ORDER BY fetched_at', (source,)))
        observation = parse_time(snapshots[0]['fetched_at']) if snapshots else None
        messages = [message for message in self.get_messages(source) if (parse_time(message['sent']) or parse_time(message['fetched_at'])) <= moment]
        cancel_messages = [message for message in messages if message['msg_type'].lower() == 'cancel']
        features, timeline, histogram = [], [], {}
        counts = dict.fromkeys(('observed_active', 'cancelled', 'expired', 'unknown'), 0)
        unlocated = 0
        for message in messages:
            status = message_status(message, cancel_messages, moment, observation)
            if message['msg_type'].lower() != 'cancel':
                counts[status] += 1
            issued_time = parse_time(message['sent']) or parse_time(message['fetched_at'])
            date = issued_time.astimezone(timezone(timedelta(hours=8))).date().isoformat()
            bucket = histogram.setdefault(date, {'date': date, 'message_count': 0, 'reading_count': 0, 'alert_message_count': 0, 'cancel_message_count': 0})
            bucket['message_count'] += 1
            if message['msg_type'].lower() == 'cancel':
                bucket['cancel_message_count'] += 1
            else:
                bucket['alert_message_count'] += 1
                bucket['reading_count'] += len(message['readings'])
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
            'message_count': len(messages), 'reading_count': sum(len(message['readings']) for message in messages if message['msg_type'].lower() != 'cancel'),
            'alert_message_count': sum(message['msg_type'].lower() != 'cancel' for message in messages),
            'cancel_message_count': sum(message['msg_type'].lower() == 'cancel' for message in messages),
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
