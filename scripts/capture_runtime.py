import json
import sqlite3
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    with urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=20) as response:
        status = json.load(response)
    with sqlite3.connect(ROOT / 'data' / 'flood.sqlite3') as connection:
        for source in ('demo', 'live', 'archive'):
            rows = connection.execute('SELECT raw_text,error,parsed FROM snapshots WHERE source=? ORDER BY id', (source,)).fetchall()
            record_times = []
            records = 0
            for raw, error, parsed in rows:
                if raw and parsed:
                    payload = json.loads(raw)
                    batch = payload.get('data', {}).get('records', [])
                    records += len(batch)
                    record_times.extend(record.get('datetime') for record in batch if record.get('datetime'))
            status['sources'][source].update(snapshot_count=len(rows), raw_record_count=records,
                record_time_min=min(record_times) if record_times else None,
                record_time_max=max(record_times) if record_times else None,
                failed_snapshots=sum(bool(row[1]) for row in rows))
    (ROOT / 'experiments' / 'runtime_status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2),encoding='utf-8')
    print(json.dumps(status['sources']['live'], ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
