import hashlib
import json
import math
from datetime import datetime, timezone, timedelta


def parse_time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone(timedelta(hours=8)))
        return result.astimezone(timezone.utc)
    except ValueError:
        return None


def stable_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:24]


def parse_circle(value):
    flags = []
    try:
        parts = value.replace(',', ' ').split() if isinstance(value, str) else value
        latitude, longitude, radius = map(float, parts)
        if not all(math.isfinite(x) for x in (latitude, longitude, radius)) or radius < 0:
            raise ValueError()
        if not (1.1 <= latitude <= 1.6 and 103.5 <= longitude <= 104.2):
            return None, radius, ['outside_singapore_bounds']
        return [longitude, latitude], radius, flags
    except (TypeError, ValueError):
        return None, None, ['invalid_or_missing_circle']


def normalize_payload(payload, source, snapshot_id, fetched_at, request_context=None):
    if not isinstance(payload, dict) or payload.get('code', 0) != 0:
        raise ValueError('PUB 返回错误状态或非对象响应')
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('records'), list):
        raise ValueError('PUB 响应缺少 data.records 数组')
    messages = []
    request_context = request_context or {}
    for record in data['records']:
        if not isinstance(record, dict):
            continue
        item = record.get('item') or {}
        if not isinstance(item, dict):
            continue
        readings = item.get('readings') or []
        if not isinstance(readings, list):
            readings = []
        msg_type = str(item.get('msgType') or '').strip()
        has_message_text = any(str(item.get(key) or '').strip() for key in ('description', 'headline'))
        if not readings and msg_type.lower() != 'cancel' and msg_type.lower() != 'alert' and not has_message_text:
            continue
        identifier = item.get('identifier')
        supplied_sent = item.get('sent')
        record_datetime = record.get('datetime')
        sent = supplied_sent if parse_time(supplied_sent) else record_datetime
        sender = item.get('sender') or ''
        unique_readings = {stable_key(reading): reading for reading in readings if isinstance(reading, dict)}
        identity = {'sender': sender, 'identifier': identifier, 'sent': sent} if identifier else {
            'msgType': msg_type, 'sent': sent, 'sender': sender,
            'readings': [unique_readings[key] for key in sorted(unique_readings)],
            'description': item.get('description'), 'headline': item.get('headline'),
            'instruction': item.get('instruction'), 'expires': item.get('expires'),
            'references': item.get('references'),
            'request_date': request_context.get('date') if source == 'archive' and not parse_time(sent) else None}
        message = {'message_key': stable_key(identity), 'identifier': identifier, 'sender': sender,
                   'msg_type': msg_type, 'sent': sent, 'expires': item.get('expires'),
                   'references': item.get('references'),
                   'description': str(item.get('description') or item.get('headline') or ''),
                   'source': source, 'raw_snapshot_id': snapshot_id, 'fetched_at': fetched_at,
                   'api_updated_at': record.get('updatedTimestamp') or record.get('timestamp'),
                   'quality_flags': [] if identifier else ['missing_identifier'], 'readings': []}
        if not parse_time(supplied_sent) and record_datetime:
            message['quality_flags'].append('issued_time_from_record_datetime')
            if supplied_sent:
                message['quality_flags'].append('invalid_sent_replaced_by_record_datetime')
        if not parse_time(sent):
            message['quality_flags'].append('missing_or_invalid_sent')
        if not readings:
            message['quality_flags'].append('message_without_readings')
        for key in sorted(unique_readings):
            reading = unique_readings[key]
            area = reading.get('area') or {}
            if not isinstance(area, dict):
                area = {}
            geometry, radius, flags = parse_circle(area.get('circle'))
            message['readings'].append({'reading_key': stable_key([message['message_key'], key]),
                'coordinates': geometry, 'broadcast_radius_km': radius,
                'area_description': str(area.get('areaDesc') or reading.get('areaDesc') or ''),
                'description': str(reading.get('description') or reading.get('headline') or message['description']),
                'severity': reading.get('severity') or item.get('severity'),
                'expires': reading.get('expires') or item.get('expires'), 'quality_flags': flags})
        messages.append(message)
    return messages


def parse_references(value):
    if isinstance(value, str):
        return [tuple(part.split(',', 2)) for part in value.split() if part.count(',') >= 2]
    if isinstance(value, list):
        return [tuple(str(part.get(key) or '') for key in ('sender', 'identifier', 'sent')) for part in value if isinstance(part, dict)]
    return []


def message_status(message, messages, at, observation_start):
    sent = parse_time(message.get('sent'))
    if not sent or not message.get('identifier') or message['msg_type'].lower() != 'alert':
        return 'unknown'
    identity = (message['sender'], str(message['identifier']), sent)
    for cancel in messages:
        cancel_time = parse_time(cancel.get('sent'))
        if cancel['msg_type'].lower() != 'cancel' or not cancel_time or cancel_time > at:
            continue
        for sender, identifier, referenced_sent in parse_references(cancel.get('references')):
            if (sender, identifier, parse_time(referenced_sent)) == identity:
                return 'cancelled'
    expiry_values = [parse_time(reading.get('expires')) for reading in message['readings']]
    expiry = parse_time(message.get('expires'))
    if expiry and expiry <= at or expiry_values and all(value and value <= at for value in expiry_values):
        return 'expired'
    if not observation_start or sent < observation_start:
        return 'unknown'
    return 'observed_active'
