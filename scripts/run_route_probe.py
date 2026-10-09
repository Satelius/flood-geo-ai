import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run import load_environment
from flood_agent.clients import route, request_json, ServiceError


def distance_to_center(coordinates, center):
    scale = 111195.0
    cosine = math.cos(math.radians(center[1]))
    points = [((x - center[0]) * scale * cosine, (y - center[1]) * scale) for x, y in coordinates]
    distances = []
    for (x, y), (u, v) in zip(points, points[1:]):
        dx, dy = u - x, v - y
        t = min(1, max(0, -(x * dx + y * dy) / (dx * dx + dy * dy))) if dx or dy else 0
        distances.append(math.hypot(x + t * dx, y + t * dy))
    return round(min(distances), 2) if distances else None


def main():
    load_environment()
    start, end, via = [103.8691, 1.3293], [103.8725, 1.3365], [103.8648, 1.3355]
    center = [103.87015, 1.33201]
    evidence_directory = ROOT / 'data' / 'research'
    responses = []

    def capture_request(url, headers=None, body=None):
        status, raw = request_json(url, headers, body)
        index = len(responses) + 1
        filename = f'onemap-route-response-{index}.json'
        (evidence_directory / filename).write_text(raw, encoding='utf-8')
        responses.append({'url': url, 'http_status': status, 'raw_path': 'data/research/' + filename})
        return status, raw

    results = []
    for label, waypoint in [('常规驾车路线', None), ('经人工选择途经点的驾车路线情景', via)]:
        started = time.perf_counter()
        try:
            result = route(start, end, 'drive', waypoint, request_fn=capture_request)
            result.update(success=True, latency_ms=round((time.perf_counter()-started)*1000, 2),
                          min_distance_to_alert_broadcast_center_m=distance_to_center(result['geometry']['coordinates'], center))
        except ServiceError as error:
            result = {'success': False, 'error': str(error), 'latency_ms': round((time.perf_counter()-started)*1000, 2)}
        results.append({'name': label, 'via': waypoint, **result})
        time.sleep(2)
    comparison = {}
    if all(result['success'] for result in results):
        comparison = {'delta_distance_m': round(results[1]['summary']['total_distance']-results[0]['summary']['total_distance'], 2),
                      'delta_provider_duration_s': round(results[1]['summary']['total_time']-results[0]['summary']['total_time'], 2),
                      'distance_ratio': round(results[1]['summary']['total_distance']/results[0]['summary']['total_distance'], 4)}
    report = {'recorded_at': datetime.now(timezone.utc).isoformat(), 'provider': 'OneMap', 'route_type': 'drive',
              'start': start, 'end': end, 'alert_broadcast_center': center, 'routes': results,
              'responses': responses, 'comparison': comparison,
              'limitations': ['真实调用发生于2026-10-05（新加坡时间）；历史警报来自2025-05-22，两者不是同时刻交通观测。',
                              '途经点人工选定，OneMap分别计算两段；没有证明最优绕行、真实封路或避洪成功。',
                              '最小距离仅针对警报广播中心；不表示距实际积水边界的距离。',
                              '时间为OneMap返回的估计；分段求和不代表洪水实际造成的延误。']}
    destination = ROOT / 'experiments' / 'onemap_routes.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'routes': [{k: v for k, v in result.items() if k != 'geometry'} for result in results],
                      'comparison': comparison, 'response_count': len(responses)}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
