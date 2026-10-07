import json
from pathlib import Path
from .spatial import line_length_m, lines_intersect
from .normalize import stable_key


def seed_demo(store):
    if store.get_messages('demo'):
        return
    payload = json.loads((Path(__file__).resolve().parent.parent / 'fixtures' / 'synthetic_demo.json').read_text(encoding='utf-8'))
    for record in payload['data']['records']:
        store.ingest({'data': {'records': [record]}}, 'demo', fetched_at=record['datetime'])


def demo_route():
    normal = [[103.837, 1.325], [103.844, 1.326], [103.849, 1.327], [103.856, 1.328]]
    scenario = [[103.837, 1.325], [103.840, 1.333], [103.854, 1.334], [103.856, 1.328]]
    road = [[103.844, 1.326], [103.849, 1.327]]
    normal_length, scenario_length = line_length_m(normal), line_length_m(scenario)
    return {'synthetic': True, 'label': '人工合成路线与道路段；非 OneMap 实测，未核实道路通行情况',
        'normal': {'geometry': {'type': 'LineString', 'coordinates': normal}, 'summary': {'total_distance': normal_length, 'total_time': 300}, 'provider': 'hand_drawn_demo'},
        'scenario': {'geometry': {'type': 'LineString', 'coordinates': scenario}, 'summary': {'total_distance': scenario_length, 'total_time': 480}, 'provider': 'hand_drawn_demo'},
        'baseline_intersects': lines_intersect(normal, road), 'detour_intersects': lines_intersect(scenario, road),
        'delta_distance': round(scenario_length - normal_length, 1), 'distance_method': 'haversine on synthetic geometry',
        'time_assumption': '300/480 秒为手工设定情景参数，非实时路况',
        'road_segment': {'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': road},
                         'properties': {'verification_status': 'synthetic', 'label': '合成示范道路段，未经人工核对',
                             'message_key': stable_key({'sender': 'synthetic', 'identifier': 'demo-alert-1', 'sent': '2026-09-24T10:00:00+08:00'}),
                             'valid_from': '2026-09-24T10:00:00+08:00', 'valid_to': '2026-09-24T10:30:00+08:00'}}}
