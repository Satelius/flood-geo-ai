"""Save reproducible public-service probes without credentials in the output."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUB = 'https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts'


def probe(url, name):
    start = time.perf_counter()
    entry = {'name': name, 'url': url, 'fetched_at': datetime.now(timezone.utc).isoformat(),
             'scope': '单页服务连通性探测；不代表完整观测窗口'}
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'FloodGeoAI-baseline/0.1', 'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read()
            entry['http_status'] = response.status
        destination = ROOT / 'data' / 'research' / (name + '.json')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        entry.update(raw_path=destination.relative_to(ROOT).as_posix(), sha256=hashlib.sha256(raw).hexdigest())
        payload = json.loads(raw)
        records = payload.get('data', {}).get('records', [])
        readings = [reading for record in records for reading in record.get('item', {}).get('readings', [])]
        entry.update(success=payload.get('code') == 0, record_count=len(records), reading_count=len(readings),
                     pagination_remaining=bool(payload.get('data', {}).get('paginationToken')),
                     record_times=[record.get('datetime') for record in records],
                     schema_observations={'missing_message_identifier': sum(not record.get('item', {}).get('identifier') for record in records if record.get('item', {}).get('readings')),
                                          'string_circle_values': sum(any(isinstance(v, str) for v in reading.get('area', {}).get('circle', [])) for reading in readings),
                                          'headlines': sorted({reading.get('headline', '') for reading in readings})})
    except urllib.error.HTTPError as error:
        entry.update(success=False, http_status=error.code, error='HTTP response rejected')
    except (OSError, ValueError):
        entry.update(success=False, error='Transport or JSON decoding failed')
    entry['latency_ms'] = round((time.perf_counter() - start) * 1000, 3)
    return entry


def main():
    results = [probe(PUB + '?date=2025-05-22', 'pub-2025-05-22')]
    time.sleep(2)
    results.append(probe(PUB, 'pub-live-2026-10-05'))
    route_file = ROOT / 'experiments' / 'onemap_routes.json'
    route_result = json.loads(route_file.read_text(encoding='utf-8')) if route_file.exists() else None
    route_status = {'status': 'not_executed', 'reason': '尚未进行真实 OneMap 路由实验；模拟响应测试不等于真实服务验证'}
    if route_result:
        route_status = {'status': 'executed_success' if all(r['success'] for r in route_result['routes']) else 'executed_with_errors',
                        'evidence': 'experiments/onemap_routes.json', 'response_count': len(route_result['responses'])}
    report = {'recorded_at': datetime.now(timezone.utc).isoformat(), 'pub': results,
              'onemap': route_status,
              'llm': {'status': 'not_executed', 'reason': '没有为本次实验配置 LLM 凭据；规则基线与可选适配器分别记录'}}
    destination = ROOT / 'experiments' / 'service_probe.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
