"""Explicitly synthetic CAP-like records, not observed flood events."""
import copy

START = '2025-01-01T00:00:00+08:00'
ALERT = '2025-01-01T00:01:00+08:00'
CANCEL = '2025-01-01T00:02:00+08:00'


def reading(circle=None, description='Flash flood at Test Road (Sample Avenue). Please avoid the area.'):
    return {'area': {'areaDesc': 'Test Road (Sample Avenue), Singapore',
                     'circle': circle if circle is not None else ['1.33201', '103.87015', '1']},
            'description': description, 'severity': 'Minor'}


def payload(identifier='test-alert', sent=ALERT, msg_type='Alert', readings=None, references=None):
    item = {'identifier': identifier, 'sender': 'PUB', 'sent': sent,
            'msgType': msg_type, 'readings': [reading()] if readings is None else copy.deepcopy(readings)}
    if references is not None:
        item['references'] = references
    return {'code': 0, 'data': {'records': [{'datetime': sent, 'item': item,
                                         'updatedTimestamp': sent}]}}


def cancellation():
    return payload('test-cancel', CANCEL, 'Cancel', [], f'PUB,test-alert,{ALERT}')
