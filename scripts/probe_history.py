"""Probe a stratified set of PUB history dates and preserve each raw page."""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flood_agent.clients import FLOOD_URL, ServiceError, request_json
from run import load_environment

DATES = [
    '2024-10-08', '2024-11-15', '2025-01-15', '2025-03-15',
    '2025-05-22', '2025-07-15', '2025-09-02', '2025-09-04',
    '2025-11-15', '2026-01-15', '2026-03-15', '2026-05-15',
    '2026-07-15', '2026-09-15', '2026-10-07',
]
OUTPUT = ROOT / 'data' / 'research' / 'history-probe'


def count_records(records):
    readings = [reading for record in records for reading in (record.get('item') or {}).get('readings', [])]
    return len(readings), len(records) - sum(bool((record.get('item') or {}).get('readings')) for record in records)


def probe_date(date):
    pages, page_token, seen = [], None, set()
    result = {'date': date, 'status': 'running', 'pages': 0, 'record_count': 0,
              'reading_count': 0, 'empty_records': 0, 'returned_timestamps': [], 'errors': []}
    while len(pages) < 100:
        params = {'date': date}
        if page_token:
            params['paginationToken'] = page_token
        url = FLOOD_URL + '?' + urlencode(params)
        try:
            http_status, raw = request_json(url, headers={'x-api-key': os.environ['DATA_GOV_API_KEY']} if os.getenv('DATA_GOV_API_KEY') else {})
            payload = json.loads(raw)
        except (ServiceError, ValueError) as error:
            result['errors'].append(type(error).__name__ + ': ' + str(error))
            result['status'] = 'failed' if not pages else 'incomplete'
            return result
        page_number = len(pages) + 1
        day_dir = OUTPUT / date
        day_dir.mkdir(parents=True, exist_ok=True)
        raw_path = day_dir / f'page-{page_number:03d}.json'
        raw_path.write_text(raw, encoding='utf-8')
        data = payload.get('data')
        if http_status != 200 or payload.get('code') != 0 or not isinstance(data, dict) or not isinstance(data.get('records'), list):
            result['errors'].append('HTTP/API/schema result not complete')
            result['status'] = 'failed' if not pages else 'incomplete'
            return result
        records = data['records']
        reading_count, empty_records = count_records(records)
        pages.append({'number': page_number, 'http_status': http_status, 'record_count': len(records),
                      'reading_count': reading_count, 'empty_records': empty_records,
                      'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                      'raw_path': raw_path.relative_to(ROOT).as_posix()})
        result['record_count'] += len(records)
        result['reading_count'] += reading_count
        result['empty_records'] += empty_records
        result['returned_timestamps'].extend(record.get('datetime') for record in records if record.get('datetime'))
        next_token = data.get('paginationToken') or payload.get('paginationToken')
        if not next_token:
            if result['reading_count']:
                result['status'] = 'complete_with_readings'
            elif result['record_count']:
                result['status'] = 'complete_records_no_readings'
            else:
                result['status'] = 'complete_empty_response'
            break
        if next_token in seen:
            result['errors'].append('pagination token repeated')
            result['status'] = 'incomplete'
            break
        seen.add(next_token)
        page_token = next_token
        time.sleep(2)
    else:
        result['errors'].append('page limit 100 reached')
        result['status'] = 'incomplete'
    result['pages'] = len(pages)
    result['page_details'] = pages
    result['last_page_complete'] = result['status'].startswith('complete_')
    result['returned_min'] = min(result['returned_timestamps']) if result['returned_timestamps'] else None
    result['returned_max'] = max(result['returned_timestamps']) if result['returned_timestamps'] else None
    result['probed_at_utc'] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    load_environment()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = ROOT / 'experiments' / 'history_availability.json'
    previous = {}
    if destination.exists():
        previous = {day['date']: day for day in json.loads(destination.read_text(encoding='utf-8')).get('days', [])}
    for index, date in enumerate(DATES, 1):
        if date in previous and previous[date].get('status', '').startswith('complete_'):
            print(f'[{index}/{len(DATES)}] {date}: existing complete probe reused')
            continue
        day = probe_date(date)
        previous[date] = day
        destination.write_text(json.dumps({'window': {'start': '2024-10-08', 'end': '2026-10-07'},
                                           'sampling': 'stratified date probes; sampled dates only, not continuous coverage',
                                           'days': [previous[key] for key in sorted(previous)]}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"[{index}/{len(DATES)}] {date}: {day['status']}, {day['pages']} page(s), {day['record_count']} record(s), {day['reading_count']} reading(s)")
        if index < len(DATES):
            time.sleep(2)


if __name__ == '__main__':
    main()
