import json
import os
import math
import time
import urllib.error
import urllib.parse
import urllib.request

FLOOD_URL = 'https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts'


class ServiceError(ValueError):
    pass


def request_json(url, headers=None, body=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={'Accept': 'application/json', 'User-Agent': 'FloodGeoAI-baseline/0.1',
                 'Content-Type': 'application/json', **(headers or {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            if error.code in (429, 502, 503, 504) and attempt < 2:
                try:
                    delay = min(10, max(2, float(error.headers.get('Retry-After', 2 * (attempt + 1)))))
                except (TypeError, ValueError):
                    delay = 2 * (attempt + 1)
                time.sleep(delay)
                continue
            if error.code == 401:
                raise ServiceError('外部服务 HTTP 401；token 可能无效或过期，请更新本地凭据') from None
            raise ServiceError(f'外部服务返回 HTTP {error.code}；请检查凭据、限流或网络') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise ServiceError('外部服务连接失败或超时') from None


def fetch_flood(store, source='live', date=None, request_fn=None):
    if source not in ('live', 'archive') or (source == 'live' and date):
        raise ValueError('实时采集使用 live；指定日期使用 archive')
    if source == 'archive' and not date:
        raise ValueError('archive 采集需要 date')
    requester = request_fn or request_json
    token, seen, pages, added = None, set(), 0, 0
    while pages < 100:
        params = {'date': date} if date else {}
        if token:
            params['paginationToken'] = token
        headers = {'x-api-key': os.environ['DATA_GOV_API_KEY']} if os.getenv('DATA_GOV_API_KEY') else {}
        try:
            status, raw = requester(FLOOD_URL + ('?' + urllib.parse.urlencode(params) if params else ''), headers=headers)
        except ServiceError as error:
            store.save_raw(source, None, params, status=None, error=str(error))
            raise
        snapshot_id = store.save_raw(source, raw, params, status=status)
        result = store.process_snapshot(snapshot_id)
        pages += 1
        added += result['messages_added']
        payload = json.loads(raw)
        token = payload.get('data', {}).get('paginationToken') or payload.get('paginationToken')
        if not token:
            return {'pages': pages, 'messages_added': added, 'source': source, 'complete': True}
        if token in seen:
            raise ServiceError('分页 token 循环；停止采集，已保留收到的快照')
        seen.add(token)
        if request_fn is None:
            time.sleep(2)
    raise ServiceError('分页超过 100 页；停止采集，已保留收到的快照')


def decode_polyline(encoded):
    coordinates, index, latitude, longitude = [], 0, 0, 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            result, shift = 0, 0
            while True:
                if index >= len(encoded) or shift > 35:
                    raise ServiceError('OneMap 路线编码无效')
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 31) << shift
                shift += 5
                if byte < 32:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        coordinates.append([longitude / 1e5, latitude / 1e5])
    return coordinates


def validate_coordinate(value):
    from .normalize import parse_circle
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('坐标必须为 [经度,纬度]')
    coordinates, _, _ = parse_circle([value[1], value[0], 0])
    if coordinates is None:
        raise ValueError('坐标必须位于新加坡合理范围')
    return coordinates


def route(start, end, route_type='drive', via=None, request_fn=None):
    if route_type not in ('drive', 'walk', 'cycle'):
        raise ValueError('首版支持 drive、walk、cycle')
    points = [validate_coordinate(start)]
    if via is not None:
        points.append(validate_coordinate(via))
    points.append(validate_coordinate(end))
    token = os.getenv('ONEMAP_TOKEN')
    if not token:
        raise ServiceError('未配置 ONEMAP_TOKEN；真实 OneMap 路由尚未接通，可使用标注的离线示范')
    coordinates, distance, duration = [], 0, 0
    for left, right in zip(points, points[1:]):
        params = {'start': f'{left[1]},{left[0]}', 'end': f'{right[1]},{right[0]}', 'routeType': route_type}
        _, raw = (request_fn or request_json)('https://www.onemap.gov.sg/api/public/routingsvc/route?' + urllib.parse.urlencode(params), headers={'Authorization': token})
        payload = json.loads(raw)
        if not payload.get('route_geometry'):
            raise ServiceError('OneMap 未返回可用路线；请检查 token 或地点')
        segment = decode_polyline(payload['route_geometry'])
        if len(segment) < 2:
            raise ServiceError('OneMap 路线点不足')
        for point in segment:
            validate_coordinate(point)
        coordinates.extend(segment[1:] if coordinates and coordinates[-1] == segment[0] else segment)
        summary = payload.get('route_summary') or {}
        distance += float(summary.get('total_distance') or 0)
        duration += float(summary.get('total_time') or 0)
        if not all(math.isfinite(value) and value >= 0 for value in (distance, duration)):
            raise ServiceError('OneMap 路线统计无效')
    return {'geometry': {'type': 'LineString', 'coordinates': coordinates},
            'summary': {'total_distance': distance, 'total_time': duration},
            'provider': 'OneMap', 'scenario': via is not None,
            'label': '经候选点的路线情景，未证明道路封闭或保证避洪' if via else 'OneMap 常规路线，未证明道路洪水影响'}
