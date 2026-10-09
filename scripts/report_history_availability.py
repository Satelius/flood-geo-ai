"""Generate the history report from saved probes and the local completed backfill."""
import html
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flood_agent.normalize import normalize_payload, parse_time


SOURCE = ROOT / 'experiments' / 'history_availability.json'
MARKDOWN = ROOT / 'experiments' / 'history_availability_zh.md'
WEBPAGE = ROOT / 'reports' / 'history.html'
CORPUS_PROFILE = ROOT / 'experiments' / 'corpus_profile_zh.md'
CORPUS_PROFILE_JSON = ROOT / 'experiments' / 'corpus_profile.json'
DATABASE = ROOT / 'data' / 'flood.sqlite3'
BATCH_ID = '5bef63d4c09d465287bc14a43aa1cfcb'
TARGET_START = '1996-10-08'
TARGET_END = '2026-10-07'
TARGET_DAYS = 10957


def classify(day):
    if day.get('errors'):
        return day.get('status', 'failed')
    if day.get('reading_count', 0):
        return 'complete_with_readings'
    if day.get('record_count', 0):
        return 'complete_records_no_readings'
    return 'complete_empty_response'


def label(status):
    return {
        'complete_empty_response': '请求完成；API 原始 records 为空',
        'complete_records_no_readings': '请求完成；有原始 records、无地点条目',
        'complete_with_readings': '请求完成；有地点条目',
        'failed': '请求失败',
        'incomplete': '分页未完成',
    }.get(status, '结果未知')


def count_text(value):
    return f'{value:,}' if value is not None else '未知'


def canonical_sample_profile(data):
    """Count unique normalized messages from the saved probe pages; never fetches or writes raw data."""
    messages_by_key = {}
    messages_by_date = Counter()
    raw_message_types = Counter()
    raw_reading_lengths = Counter()
    raw_records = 0
    for day in data['days']:
        per_day = {}
        for page in day.get('page_details', []):
            raw_path = ROOT / page['raw_path']
            if not raw_path.is_file():
                raise FileNotFoundError(f'抽样原始页面不存在：{page["raw_path"]}')
            payload = json.loads(raw_path.read_text(encoding='utf-8'))
            records = payload.get('data', {}).get('records', [])
            for record in records:
                raw_records += 1
                item = record.get('item') if isinstance(record, dict) else None
                item = item if isinstance(item, dict) else {}
                msg_type = item.get('msgType')
                raw_message_types[(str(msg_type).strip().lower() if msg_type is not None and str(msg_type).strip() else 'missing')] += 1
                readings = item.get('readings')
                raw_reading_lengths[len(readings) if isinstance(readings, list) else 'missing'] += 1
            messages = normalize_payload(payload, 'archive', page['number'], day.get('probed_at_utc', ''))
            for message in messages:
                per_day[message['message_key']] = message
                messages_by_key[message['message_key']] = message
        messages_by_date[day['date']] = len(per_day)
    message_types = Counter((message.get('msg_type') or 'missing').strip().lower()
                            for message in messages_by_key.values())
    reading_entries = sum(len(message.get('readings', [])) for message in messages_by_key.values())
    return {
        'messages': len(messages_by_key),
        'non_cancel_messages': sum(message.get('msg_type', '').lower() != 'cancel'
                                   for message in messages_by_key.values()),
        'explicit_alert_messages': message_types['alert'],
        'cancel_messages': message_types['cancel'],
        'missing_msg_type_messages': message_types['missing'],
        'reading_entries': reading_entries,
        'messages_by_date': messages_by_date,
        'raw_records': raw_records,
        'raw_message_types': raw_message_types,
        'raw_reading_lengths': raw_reading_lengths,
    }


def render_corpus_snapshot():
    if not CORPUS_PROFILE.is_file():
        return '<li>本地语料画像尚未生成；请先运行 scripts/analyze_corpus.py。</li>'
    source = CORPUS_PROFILE.read_text(encoding='utf-8')
    section = source.split('## 数据范围与计数口径', 1)[-1].split('\n## ', 1)[0]
    items = []
    for line in section.splitlines():
        line = line.strip()
        if line.startswith('- '):
            items.append(f'<li>{html.escape(line[2:])}</li>')
        elif line.startswith('**样本解释：**'):
            items.append(f'<li><strong>样本解释：</strong>{html.escape(line.removeprefix("**样本解释：**").strip())}</li>')
    if not items:
        return '<li>语料画像文件中没有可读取的计数摘要。</li>'
    return ''.join(items)


def load_backfill_summary():
    if not BATCH_ID or not DATABASE.is_file():
        return None
    uri = 'file:' + quote(DATABASE.resolve().as_posix(), safe='/:') + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        batch = connection.execute(
            'SELECT * FROM history_batches WHERE batch_id=?', (BATCH_ID,)).fetchone()
        if batch is None:
            return None
        days = [dict(row) for row in connection.execute('''SELECT date,status,pages,record_count,
            queried_message_count,queried_explicit_alert_message_count,
            queried_unknown_msg_type_count,queried_reading_count,query_metrics_version
            FROM coverage_days WHERE batch_id=? ORDER BY date''', (BATCH_ID,))]
        message_rows = connection.execute(
            "SELECT body FROM messages WHERE source='archive'").fetchall()
    finally:
        connection.close()

    start, end = batch['start_date'], batch['end_date']
    sgt = timezone(timedelta(hours=8))
    messages_by_date = {}
    for row in message_rows:
        message = json.loads(row['body'])
        sent = parse_time(message.get('sent'))
        if sent is None:
            continue
        day = sent.astimezone(sgt).date().isoformat()
        if day < start or day > end or str(message.get('msg_type') or '').lower() == 'cancel':
            continue
        count = messages_by_date.setdefault(day, {
            'messages': 0, 'explicit_alerts': 0, 'unknown_types': 0, 'readings': 0})
        msg_type = str(message.get('msg_type') or '').lower()
        count['messages'] += 1
        count['explicit_alerts'] += msg_type == 'alert'
        count['unknown_types'] += msg_type not in ('alert', 'cancel')
        count['readings'] += len(message.get('readings') or [])

    status_counts = Counter(day['status'] for day in days)
    record_days = [day for day in days if (day.get('record_count') or 0) > 0]
    empty_record_days = [day for day in days if day.get('record_count') == 0]
    alert_days = [day for day in days if day['status'] == 'complete_nonzero']
    query_metrics_current = bool(days) and all(day['query_metrics_version'] == 1 for day in days)
    batch_metrics = {
        'messages': sum(day.get('queried_message_count') or 0 for day in days),
        'explicit_alerts': sum(day.get('queried_explicit_alert_message_count') or 0 for day in days),
        'unknown_types': sum(day.get('queried_unknown_msg_type_count') or 0 for day in days),
        'readings': sum(day.get('queried_reading_count') or 0 for day in days),
    } if query_metrics_current else None
    archive_message_delta_days = []
    if batch_metrics is not None:
        for day in days:
            batch_count = day.get('queried_message_count')
            local_count = messages_by_date.get(day['date'], {}).get('messages', 0)
            if batch_count is not None and local_count != batch_count:
                archive_message_delta_days.append({
                    'date': day['date'],
                    'batch_messages': batch_count,
                    'local_messages': local_count,
                    'delta': local_count - batch_count,
                    'local_unknown_types': messages_by_date.get(day['date'], {}).get('unknown_types', 0),
                })
    profile = json.loads(CORPUS_PROFILE_JSON.read_text(encoding='utf-8')) if CORPUS_PROFILE_JSON.is_file() else {}
    return {
        'batch_id': batch['batch_id'], 'status': batch['status'], 'error': batch['error'],
        'start': start, 'end': end, 'total_days': len(days),
        'complete_days': status_counts['complete_zero'] + status_counts['complete_nonzero'],
        'status_counts': dict(status_counts),
        'pages': sum(day.get('pages') or 0 for day in days),
        'raw_records': sum(day.get('record_count') or 0 for day in days),
        'days_with_raw_records': len(record_days), 'empty_record_days': len(empty_record_days),
        'zero_message_days_with_records': sum(
            day['status'] == 'complete_zero' and (day.get('record_count') or 0) > 0
            for day in days),
        'first_raw_record_day': record_days[0]['date'] if record_days else None,
        'last_raw_record_day': record_days[-1]['date'] if record_days else None,
        'batch_query_metrics_current': query_metrics_current,
        'batch_query_metrics': batch_metrics,
        'messages_by_date': messages_by_date,
        'local_archive_messages': sum(item['messages'] for item in messages_by_date.values()),
        'local_archive_explicit_alerts': sum(item['explicit_alerts'] for item in messages_by_date.values()),
        'local_archive_unknown_types': sum(item['unknown_types'] for item in messages_by_date.values()),
        'local_archive_readings': sum(item['readings'] for item in messages_by_date.values()),
        'alert_days': [{
            'date': day['date'], 'raw_records': day.get('record_count') or 0,
            'messages': day.get('queried_message_count') if day['query_metrics_version'] == 1 else None,
            'explicit_alerts': day.get('queried_explicit_alert_message_count') if day['query_metrics_version'] == 1 else None,
            'unknown_types': day.get('queried_unknown_msg_type_count') if day['query_metrics_version'] == 1 else None,
            'readings': day.get('queried_reading_count') if day['query_metrics_version'] == 1 else None}
            for day in alert_days],
        'archive_message_delta': (
            sum(item['messages'] for item in messages_by_date.values()) - batch_metrics['messages']
            if batch_metrics is not None else None),
        'archive_message_delta_days': archive_message_delta_days,
        'record_datetime_span': profile.get('record_date_span_sgt', {}),
        'corpus_generated_at': profile.get('generated_at_utc'),
        'corpus_snapshot_count': profile.get('snapshot_count'),
        'corpus_snapshots_by_source': profile.get('snapshots_by_source', {}),
        'unique_records': profile.get('unique_records_in_window'),
        'record_sightings': profile.get('record_sightings_in_window'),
        'duplicate_sightings': profile.get('duplicate_sightings_in_window'),
        'unique_item_types': profile.get('unique_item_type_values', {}),
        'unique_readings': profile.get('unique_reading_objects'),
        'empty_readings_records': profile.get('unique_record_empty_states', {}).get('item.readings.empty_array'),
        'alert_like_messages': profile.get('unique_alert_like_messages'),
        'cancel_messages': profile.get('explicit_msgType_cancel_messages'),
        'corpus_explicit_alert_messages': profile.get('explicit_msgType_alert_messages'),
        'corpus_unknown_type_messages': profile.get('readings_with_missing_msgType_messages'),
        'road_pattern_counts': profile.get('road_description_pattern_counts', {}),
        'updated_at': batch['updated_at'],
    }


def render_batch_markdown(summary):
    if summary is None:
        return ['## 30 年逐日回填结果', '', '本地数据库中没有找到当前批次或批次统计。', '']
    statuses = summary['status_counts']
    status_names = {
        'completed': '已完成', 'completed_with_gaps': '已完成但有缺口',
        'failed': '失败', 'running': '运行中', 'queued': '排队中',
    }
    days = summary['alert_days']
    metrics = summary['batch_query_metrics']
    if metrics is None:
        batch_metrics_line = '- 本批次逐日消息审计指标版本不一致，汇总值标为未知；不能用本地消息表替代。'
    else:
        batch_metrics_line = (
            f"- 本批次逐日审计指标（query_metrics_version=1）：{metrics['messages']:,} 条非 Cancel 消息"
            f"（明确 Alert {metrics['explicit_alerts']:,} 条、类型不明 {metrics['unknown_types']:,} 条），"
            f"共 {metrics['readings']:,} 个地点条目。"
        )
    local_note = (
        f"本地 archive 消息表按有效发布时间归日为 {summary['local_archive_messages']:,} 条非 Cancel 消息"
        f"（明确 Alert {summary['local_archive_explicit_alerts']:,} 条、类型不明 {summary['local_archive_unknown_types']:,} 条），"
        f"含 {summary['local_archive_readings']:,} 个地点条目。"
    )
    if summary['archive_message_delta']:
        mismatch_dates = '、'.join(
            f"{item['date']}（本地 {item['local_messages']} 条、批次 {item['batch_messages']} 条）"
            for item in summary['archive_message_delta_days'])
        local_note += (
            f"比本批次逐日审计多 {summary['archive_message_delta']:,} 条；差额来自已有 archive 快照，"
            f"不属于本批次查询审计计数。差额日期：{mismatch_dates}。"
        )
        if any(item['date'] == '2025-05-22' and item['delta'] > 0
               for item in summary['archive_message_delta_days']):
            local_note += (
                '2025-05-22 的额外条目来自未带 date 参数的旧快照；它与批次消息发布时间和地点描述相同，'
                '但 message_key 不同，因此当前本地消息表仍分别计数，应作为疑似重复人工复核。'
            )
    lines = [
        '## 30 年逐日回填结果', '',
        f"- 批次 `{summary['batch_id']}`：{status_names.get(summary['status'], summary['status'])}；查询完整 {summary['complete_days']:,}/{summary['total_days']:,} 天。",
        f"- 每日状态：{statuses.get('complete_nonzero', 0):,} 个含警报消息的日期，{statuses.get('complete_zero', 0):,} 个完整查询但没有规范化警报消息的日期；失败、未完成、不可用或未尝试共 {summary['total_days'] - summary['complete_days']:,} 天。",
        f"- 分页共 {summary['pages']:,} 页，API 原始 records 共 {summary['raw_records']:,} 条；{summary['days_with_raw_records']:,} 个请求日期返回了原始 records，{summary['empty_record_days']:,} 个日期的 records 为空。",
        f"- 另有 {summary['zero_message_days_with_records']:,} 个日期返回了原始 records，但没有规范化非 Cancel 警报消息；因此 `complete_zero` 不等于原始 `records=[]`。",
        f"- 原始记录的 SGT 时间戳范围为 {summary['record_datetime_span'].get('first') or '未知'} 至 {summary['record_datetime_span'].get('last') or '未知'}；这不是 30 年的历史档案覆盖证据。",
        batch_metrics_line,
        f'- {local_note}消息和地点条目都不是已核实的洪水事件数。',
        '',
        '空 `records` 表示接口本次没有返回原始记录；即使查询成功，也不能据此推断这些日期现实中没有洪水。PUB 没有公布历史保留年限，完整查询天数不等于完整历史数据覆盖。',
    ]
    if days:
        lines += ['', '### 有警报消息的日期', '',
                  '| 日期 | 请求返回的原始 records | 本批次非 Cancel 消息 | 明确 Alert | 类型不明 | 地点条目 |',
                  '|---|---:|---:|---:|---:|---:|']
        for day in days:
            lines.append('| {} | {:,} | {} | {} | {} | {} |'.format(
                day['date'], day['raw_records'], count_text(day['messages']),
                count_text(day['explicit_alerts']), count_text(day['unknown_types']),
                count_text(day['readings'])))
    if summary.get('unique_records') is not None:
        lines += [
            '', '### 按本地留存 record.datetime 统计的语料画像', '',
            f"- 画像生成于 {summary['corpus_generated_at']}；覆盖所选 30 年窗口内 {summary['corpus_snapshot_count']:,} 个 archive/live 快照，排除 synthetic demo。",
            f"- 原始记录出现 {summary['record_sightings']:,} 次；按响应内容去重后 {summary['unique_records']:,} 条，重复出现 {summary['duplicate_sightings']:,} 次。",
            f"- 去重 record 的 `item.type` 观测值：{', '.join(f'{name} {count:,}' for name, count in summary['unique_item_types'].items()) or '无'}；原始 record 总量不等于警报数。",
            f"- record.datetime 实际日期范围只有 {summary['record_datetime_span'].get('first')} 至 {summary['record_datetime_span'].get('last')}；{summary['empty_readings_records']:,} 条去重记录的 readings 为空，仅 {summary['unique_readings']:,} 个地点 reading 对象。",
            f"- alert-like 消息 {summary['alert_like_messages']:,} 条：明确 Alert {summary['corpus_explicit_alert_messages']:,}、Cancel {summary['cancel_messages']:,}、含地点但 msgType 缺失 {summary['corpus_unknown_type_messages']:,}。这是消息结构统计，不是真实洪水次数。",
            f"- 道路正文规则初筛（类别可重叠）：from-to {summary['road_pattern_counts'].get('from_to_segment', 0)}、地标邻近 {summary['road_pattern_counts'].get('near_landmark', 0)}、括号内容 {summary['road_pattern_counts'].get('parenthetical_name', 0)}、道路及关系词 {summary['road_pattern_counts'].get('road_name_with_relation', 0)}。括号规则会命中避让时长中的 `(1)` 等非道路内容；这些有限样本规则命中不是道路段匹配准确率。",
        ]
    return lines + ['']


def render_batch_html(summary):
    if summary is None:
        return '<h2>30 年逐日回填结果</h2><p>本地数据库中没有找到当前批次或批次统计。</p>'
    statuses = summary['status_counts']
    status_names = {
        'completed': '已完成', 'completed_with_gaps': '已完成但有缺口',
        'failed': '失败', 'running': '运行中', 'queued': '排队中',
    }
    details = []
    for day in summary['alert_days']:
        values = [day['messages'], day['explicit_alerts'], day['unknown_types'], day['readings']]
        rendered_values = [f'{value:,}' if value is not None else '未知' for value in values]
        details.append('<tr><th scope="row">{}</th><td>{:,}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            html.escape(day['date']), day['raw_records'], *rendered_values))
    if not details:
        details.append('<tr><td colspan="6">没有日期返回非 Cancel 警报消息。</td></tr>')
    date_span = summary['record_datetime_span']
    first = html.escape(str(date_span.get('first') or '未知'))
    last = html.escape(str(date_span.get('last') or '未知'))
    metrics = summary['batch_query_metrics']
    metric_message_count = f"{metrics['messages']:,}" if metrics is not None else '未知'
    metric_reading_count = f"{metrics['readings']:,}" if metrics is not None else '未知'
    delta_note = ''
    if summary['archive_message_delta']:
        mismatch_dates = '、'.join(
            f"{item['date']}（本地 {item['local_messages']} 条、批次 {item['batch_messages']} 条）"
            for item in summary['archive_message_delta_days'])
        delta_note = (
            f"本地消息表比批次逐日审计多 {summary['archive_message_delta']:,} 条，"
            f"来源为批次外已保存的 archive 快照；差额日期：{mismatch_dates}。"
        )
        if any(item['date'] == '2025-05-22' and item['delta'] > 0
               for item in summary['archive_message_delta_days']):
            delta_note += (
                '该日期额外条目来自未带 date 参数的旧快照；发布时间和地点描述与批次消息相同，'
                'message_key 不同，需人工复核是否重复。'
            )
    local_summary = (
        f"本地 archive 归日表为 {summary['local_archive_messages']:,} 条非 Cancel 消息 "
        f"（明确 Alert {summary['local_archive_explicit_alerts']:,}、类型不明 {summary['local_archive_unknown_types']:,}），"
        f"含 {summary['local_archive_readings']:,} 个地点条目。"
    )
    corpus_detail = ''
    if summary.get('unique_records') is not None:
        source_counts = summary['corpus_snapshots_by_source']
        archive_count = source_counts.get('archive', {}).get('snapshot_count', 0)
        live_count = source_counts.get('live', {}).get('snapshot_count', 0)
        patterns = summary['road_pattern_counts']
        item_types = '、'.join(
            f'{html.escape(str(name))} {count:,}'
            for name, count in summary['unique_item_types'].items()) or '无'
        corpus_detail = f'''<p>本地原文画像（{html.escape(str(summary['corpus_generated_at']))}）：archive {archive_count:,} 个快照、live {live_count:,} 个快照；窗口内原始 record 出现 {summary['record_sightings']:,} 次，去重后 {summary['unique_records']:,} 条（重复出现 {summary['duplicate_sightings']:,} 次）。去重 record 的 item.type：{item_types}；{summary['empty_readings_records']:,} 条去重 record 的 readings 为空，仅 {summary['unique_readings']:,} 个地点 reading 对象。alert-like {summary['alert_like_messages']:,} 条，包括 Alert {summary['corpus_explicit_alert_messages']:,}、Cancel {summary['cancel_messages']:,}、msgType 缺失 {summary['corpus_unknown_type_messages']:,}。道路文字初筛命中：from-to {patterns.get('from_to_segment', 0)}、地标邻近 {patterns.get('near_landmark', 0)}、括号内容 {patterns.get('parenthetical_name', 0)}、道路关系词 {patterns.get('road_name_with_relation', 0)}；类别可重叠，括号规则会命中避让时长里的 `(1)`，不可解释为道路段匹配。</p>'''
    return f'''<h2>30 年逐日回填结果</h2>
<div class="facts"><div><strong>{summary['complete_days']:,}/{summary['total_days']:,}</strong><span>每日查询完整</span></div><div><strong>{statuses.get('complete_nonzero', 0):,}</strong><span>含警报消息的日期</span></div><div><strong>{summary['raw_records']:,}</strong><span>API 原始 records</span></div><div><strong>{metric_message_count}</strong><span>批次审计非 Cancel 消息</span></div><div><strong>{metric_reading_count}</strong><span>批次审计地点条目</span></div></div>
<p>批次状态：<strong>{html.escape(status_names.get(summary['status'], summary['status']))}</strong>。分页 {summary['pages']:,} 页；{summary['days_with_raw_records']:,} 个请求日期有原始 records，{summary['empty_record_days']:,} 个日期的 records 为空；另有 {summary['zero_message_days_with_records']:,} 个日期有原始 records，但没有规范化非 Cancel 警报消息。原始 record.datetime 的 SGT 范围为 {first} 至 {last}。</p>
<div class="notice"><strong>历史范围限制：</strong>{summary['complete_days']:,}/{summary['total_days']:,} 天的逐日请求完成，但实际返回的 record.datetime 只覆盖 {first} 至 {last}。PUB 未公布历史保留年限；空 records 不是“现实中没有洪水”的证据，查询完整也不代表 30 年历史档案完整。</div>
<p>本批次逐日审计：非 Cancel 消息 {metric_message_count} 条、地点条目 {metric_reading_count} 个。{local_summary}{delta_note}消息和地点条目都不等同于核实后的洪水事件。</p>
<div class="table-wrap"><table><thead><tr><th scope="col">含警报消息的日期</th><th scope="col">请求原始 records</th><th scope="col">本批次非 Cancel 消息</th><th scope="col">明确 Alert</th><th scope="col">类型不明</th><th scope="col">地点条目</th></tr></thead><tbody>{''.join(details)}</tbody></table></div>{corpus_detail}'''


def render_markdown(data, profile, batch_summary):
    days = data['days']
    counts = Counter(day['status'] for day in days)
    pages = sum(day.get('pages', 0) for day in days)
    records = sum(day.get('record_count', 0) for day in days)
    readings = sum(day.get('reading_count', 0) for day in days)
    complete = sum(count for status, count in counts.items() if status.startswith('complete_'))
    batch_note = (f'- 批次卡片通过 `GET /api/backfill/{BATCH_ID}` 动态读取状态；本报告的逐日统计和语料画像由本地数据库生成。'
                  if BATCH_ID else '- 30 年批次尚未启动；启动后网页将只读查询批次进度，不会修改或续跑任务。')
    lines = [
        '# PUB 历史覆盖与 30 年回填报告', '',
        f"> 当前回填目标：{TARGET_START} 至 {TARGET_END}（新加坡时间，{TARGET_DAYS:,} 天；逐日查询）。",
        f"> 原两年试点抽样窗口：{data['window']['start']} 至 {data['window']['end']}；15 日抽样覆盖 {complete}/{len(days)} 个查询日，不代表连续历史覆盖。", '',
        '## 两年试点抽样结果', '',
        f'- 分页请求：{pages:,} 页；API 原始 `records`：{records:,} 条；地点 `readings` 条目：{readings:,} 条。',
        f'- 从已保存原始页按当前标准化规则得到：{profile["messages"]} 条去重规范消息，其中非 Cancel {profile["non_cancel_messages"]} 条、明确 Alert {profile["explicit_alert_messages"]} 条、Cancel {profile["cancel_messages"]} 条、`msgType` 缺失 {profile["missing_msg_type_messages"]} 条；规范消息内含 {profile["reading_entries"]} 个地点条目。',
        f'- 15 日中：{counts["complete_empty_response"]} 日 API 原始 records 为空，{counts["complete_records_no_readings"]} 日有原始 records 但无地点条目，{counts["complete_with_readings"]} 日有地点条目。',
        f'- 原始 records 字段画像：`msgType` 缺失 {profile["raw_message_types"]["missing"]:,} 条、Alert {profile["raw_message_types"]["alert"]:,} 条、Cancel {profile["raw_message_types"]["cancel"]:,} 条；{profile["raw_reading_lengths"][0]:,} 条的 `readings` 为空，{profile["raw_reading_lengths"][1]:,} 条各含 1 个地点条目。',
        '- 这些原始字段值不能把缺失 `msgType` 的记录解释为 Observation、警报或洪水事件；原始 records 数不能代替规范消息数。',
        '- 有地点条目的日期为 2025-05-22（1 条 record、1 个地点条目）和 2026-05-15（722 条 records、2 个地点条目）。', '',
        *render_batch_markdown(batch_summary),
        '## 日历计数口径', '',
        '- 默认显示按 `message_key` 去重的非 Cancel 警报消息数；消息类型缺失的规范消息保留在非 Cancel 口径，另有字段标出类型不明数。Cancel 不计入警报消息数。',
        '- 可切换到地点 `readings` 条目数。一个消息可以包含多个地点条目；消息数与地点条目数不能互换，更不是洪水事件数。',
        '- `not_attempted`、`running`、`failed`、`incomplete`、`unavailable` 都是未知日：主计数为 null；观察到的本地内容只作为 lower bound 单列，不能显示成完整日零值。',
        '- 只有分页完整的 `complete_zero` 或 `complete_nonzero` 才给出完整日计数。查询响应中的 `queried_undated_message_count` 单独展示，不归入任一日历日期。',
        '- 日期以唯一消息有效 `sent` 为主，必要时使用有效 `record.datetime`；不能用请求日或抓取日替代事件发布时间。', '',
        '## 数据处理路径', '',
        '`data.gov.sg 原始分页响应 → 规范化 PUB 消息并按 message_key 去重 → SQLite 保存快照、消息与逐日覆盖状态 → 从有效 circle 生成 GeoJSON Point → 日历、直方图、地图详情`', '',
        '- 原始响应按页保存并计算 SHA-256，方便复核原文；结构化消息保留来源、时间、字段质量标记与地点属性。',
        '- PUB 的 `circle` 按 `[纬度, 经度, 半径公里]` 读取，GeoJSON 坐标输出顺序为 `[经度, 纬度]`。无效坐标仍保留消息，但不生成地图点。广播半径不是积水范围。',
        '- 热力图不会把示范数据混入实测统计；完整日期的零值与失败、未采集或分页未完成日期分开编码。', '',
        '## 抽样清单', '',
        '| 日期 | 查询结果 | 页数 | API 原始 records | 去重规范消息 | 地点 reading 条目 | API 时间范围 |',
        '|---|---|---:|---:|---:|---:|---|',
    ]
    for day in days:
        lines.append('| {date} | {status} | {pages} | {records:,} | {messages:,} | {readings:,} | {minimum} – {maximum} |'.format(
            date=day['date'], status=label(day['status']), pages=day.get('pages', 0),
            records=day.get('record_count', 0), messages=profile['messages_by_date'].get(day['date'], 0),
            readings=day.get('reading_count', 0), minimum=day.get('returned_min') or '—',
            maximum=day.get('returned_max') or '—'))
    lines += [
        '', '## 限制与复现', '',
        '- 15 个日期是原两年试点的分层抽样，不是连续查询。一个日期查询成功也不能证明 PUB 历史归档永久完整，或证明当天没有发生洪水。',
        '- 官方 API 的 date 参数是单日或日期时间，没有多日范围参数，也未公布历史保留年限。成功空响应只说明接口本次没有返回可归一化警报；不能证明更长历史档案完整或现实中没有洪水。',
        f'- 原始 records 中有 {profile["raw_reading_lengths"][0]:,} 条 `readings` 为空、{profile["raw_reading_lengths"][1]:,} 条各含一个地点条目；{profile["raw_message_types"]["missing"]:,} 条缺少 `msgType`。不能给缺失类型的记录擅自赋予 Observation 标签。',
        '- 15 日抽样只有 3 条规范消息；30 年数据库画像另行覆盖目标期内本地已保存的 archive/live 原始响应，二者样本范围不同。',
        '- 当前语料画像按原始 `record.datetime` 过滤；道路规则只初筛文字形式，样本仍需人工核对，不代表道路已匹配或模式全集。',
        '- 报告生成只读取本地 SQLite 和既有抽样文件，不调用 PUB API 或 LLM，也不触发回填。',
        batch_note, '',
        '### 链接', '',
        '- [打开洪水警报工作台](/)',
        '- [中文 Markdown 报告](/artifacts/history-availability-zh.md)',
        '- [PUB 语料结构画像](/artifacts/corpus-profile-zh.md)',
        '- [15 日抽样 JSON](/artifacts/history-availability.json)',
        '- [原始分页索引；SHA-256 见 15 日抽样 JSON](/artifacts/history-probe/manifest.json)',
        '- [分页原文示例：2025-05-22 第 1 页](/artifacts/history-probe/2025-05-22/page-001.json)',
        '- [采样与报告生成脚本](/artifacts/report-generator.py)',
        '- [项目运行及回填接口说明](/artifacts/README.md)', '',
        '重生成：`python scripts/report_history_availability.py`', '',
    ]
    return '\n'.join(lines)


def render_html(data, profile, batch_summary):
    days = data['days']
    counts = Counter(day['status'] for day in days)
    pages = sum(day.get('pages', 0) for day in days)
    records = sum(day.get('record_count', 0) for day in days)
    readings = sum(day.get('reading_count', 0) for day in days)
    complete_days = sum(count for status, count in counts.items() if status.startswith('complete_'))
    rows = []
    for day in days:
        times = '—' if not day.get('returned_min') else f"{day['returned_min']} 至 {day['returned_max']}"
        rows.append('<tr><th scope="row">{}</th><td>{}</td><td>{:,}</td><td>{:,}</td><td>{:,}</td><td>{:,}</td><td>{}</td></tr>'.format(
            html.escape(day['date']), html.escape(label(day['status'])), day.get('pages', 0),
            day.get('record_count', 0), profile['messages_by_date'].get(day['date'], 0),
            day.get('reading_count', 0), html.escape(times)))
    generated = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Singapore')).strftime('%Y-%m-%d %H:%M SGT')
    corpus_snapshot = render_corpus_snapshot()
    batch_result = render_batch_html(batch_summary)
    style = '''
:root{--ink:#17364a;--text:#273f50;--muted:#5b7180;--paper:#f3f7f9;--line:#d8e2e8;--teal:#167a74;--amber:#a8631e;--blue:#176aa2}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--text);font:16px/1.7 "Segoe UI","Microsoft YaHei",sans-serif}
header{background:#e5f0f4;border-top:8px solid var(--teal);padding:32px max(20px,calc((100vw - 1080px)/2))}main{max-width:1080px;margin:auto;padding:10px 24px 60px}
h1,h2,h3{color:var(--ink);line-height:1.35}h1{font-size:clamp(28px,4vw,42px);margin:8px 0}h2{font-size:23px;margin-top:36px}h3{font-size:18px}
a{color:var(--blue)}.muted{color:var(--muted)}.facts{display:flex;gap:28px;flex-wrap:wrap;margin:20px 0}.facts div{border-left:3px solid var(--teal);padding-left:12px}.facts strong{display:block;font-size:23px;color:var(--ink)}
.notice{border-left:4px solid var(--amber);background:#fff4e5;padding:14px 18px;margin:20px 0}.pipeline{display:flex;align-items:stretch;gap:10px;flex-wrap:wrap;margin:18px 0}.pipeline span{background:#e4eef3;border-bottom:3px solid var(--teal);padding:12px;flex:1;min-width:150px}.pipeline b{display:block;color:var(--ink)}.pipeline small{color:var(--muted);display:block}
.table-wrap{overflow:auto;background:white;border:1px solid var(--line);margin:14px 0}table{border-collapse:collapse;width:100%;min-width:860px;font-size:13px}th,td{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}thead th{background:#e7eff3;color:var(--ink)}td:nth-child(3),td:nth-child(4),td:nth-child(5),td:nth-child(6){text-align:right;font-variant-numeric:tabular-nums}
.batch{background:#fff;border:1px solid var(--line);padding:18px;margin:14px 0}.batch-head{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.batch-id{font:12px Consolas,monospace;overflow-wrap:anywhere}.batch-status{color:var(--teal);font-weight:650}.batch-grid{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:12px;margin-top:12px}.batch-grid div{background:#f3f7f9;padding:10px}.batch-grid strong{display:block;font-size:19px;color:var(--ink)}progress{width:100%;height:18px;margin-top:12px;accent-color:var(--teal)}button{border:1px solid var(--teal);background:white;color:var(--teal);padding:8px 14px;border-radius:4px;cursor:pointer;font:inherit}button:focus-visible,a:focus-visible{outline:3px solid #d69736;outline-offset:3px}
code{font-family:Consolas,monospace}footer{margin-top:30px;padding-top:15px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
@media(max-width:720px){header{padding:26px 20px}main{padding:8px 16px 40px}.batch-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.pipeline span{min-width:130px}}
@media print{body{background:white}button{display:none}header{padding:20px}main{max-width:none}section{break-inside:avoid}}
'''
    script = r'''<script>
const batchId='__BATCH_ID__';
const statusNames={queued:'排队中',running:'运行中',pause_requested:'等待暂停',paused:'已暂停',completed:'已完成',completed_with_gaps:'已完成但有缺口',budget_exhausted:'本轮预算用尽',failed:'批次失败'};
const dayNames={complete_zero:'完整采集：0 条警报消息',complete_nonzero:'完整采集：含警报消息',not_attempted:'未采集（未知）',running:'采集中（未知）',unavailable:'接口不可用（未知）',failed:'采集失败（未知）',incomplete:'分页未完成（未知）'};
const number=value=>Number(value||0).toLocaleString('zh-CN');
function markBatchUnknown(message){
 document.getElementById('batch-status').textContent='读取失败（进度未知）';
 for(const id of ['batch-complete','batch-attempted','batch-running','batch-not-attempted'])document.getElementById(id).textContent='未知';
 document.getElementById('batch-other').textContent='失败／分页未完成／不可用：未知';
 const progress=document.getElementById('batch-progress');progress.removeAttribute('value');progress.setAttribute('aria-label','采集进度未知；读取失败');
 document.getElementById('batch-note').textContent=message+'；此报告不会启动或修改回填任务。';
}
async function refreshBatch(){
 const note=document.getElementById('batch-note');
 try{
  const response=await fetch('/api/backfill/'+encodeURIComponent(batchId),{method:'GET',cache:'no-store',headers:{Accept:'application/json'}});
  if(response.status===404){markBatchUnknown('工作台 API 已连接，但当前服务找不到该批次记录');return;}
  const data=await response.json();
  if(!response.ok||data.error)throw new Error(data.error||('HTTP '+response.status));
  const counts=data.day_status_counts||{};const total=Number(data.total_days)||10957;const complete=(counts.complete_zero||0)+(counts.complete_nonzero||0);
  document.getElementById('batch-status').textContent=statusNames[data.status]||data.status||'状态未知';
  document.getElementById('batch-complete').textContent=number(complete)+' / '+number(total);
  document.getElementById('batch-attempted').textContent=number(data.attempted_days);
  document.getElementById('batch-running').textContent=number(counts.running);
  document.getElementById('batch-not-attempted').textContent=number(counts.not_attempted);
  document.getElementById('batch-other').textContent=['failed','incomplete','unavailable'].map(key=>(dayNames[key]||key)+' '+number(counts[key])).join(' · ');
  const progress=document.getElementById('batch-progress');progress.max=total;progress.value=complete;progress.setAttribute('aria-label','完整采集 '+complete+' / '+total+' 天');
  const updated=data.updated_at?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Singapore',dateStyle:'medium',timeStyle:'short'}).format(new Date(data.updated_at))+' SGT':'尚无更新时间';
  note.textContent=(data.worker_running?'采集线程正在运行。':'当前没有运行中的采集线程。')+' 最近状态更新时间：'+updated+(data.error?'；说明：'+data.error:'');
 }catch(error){markBatchUnknown('无法读取批次进度：请在本地工作台服务运行时打开此报告');}
}
if(batchId){document.getElementById('refresh-batch').addEventListener('click',refreshBatch);refreshBatch();window.setInterval(refreshBatch,15000);}
else{document.getElementById('batch-status').textContent='30 年回填批次待启动';document.getElementById('batch-id').textContent='批次 ID：尚未生成';document.getElementById('batch-note').textContent='文档与日历已按目标窗口准备；启动后此卡片只读显示进度。';document.getElementById('batch-progress').removeAttribute('value');document.getElementById('batch-progress').setAttribute('aria-label','30 年回填批次尚未启动');document.getElementById('refresh-batch').disabled=true;}
</script>'''.replace('__BATCH_ID__', BATCH_ID)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PUB 历史覆盖与 30 年回填报告</title><style>{style}</style></head><body>
<header><a href="/">返回洪水警报研究工作台</a><p class="muted">生成时间：{html.escape(generated)}</p><h1>PUB 历史覆盖与 30 年回填报告</h1>
<p>本报告保留原两年试点抽样作为基线，并汇总 30 年逐日查询结果、现有原始语料画像、计数口径与来源限制。批次卡片动态读取状态；下方结果取自本地数据库。</p></header>
<main>
<div class="facts"><div><strong>{TARGET_DAYS:,} 天</strong><span>30 年目标窗口 · {TARGET_START} 至 {TARGET_END}</span></div><div><strong>{len(days)} 天</strong><span>两年试点的分层可用性抽样</span></div><div><strong>{pages:,} 页</strong><span>样本分页响应</span></div><div><strong>{records:,} 条</strong><span>样本原始 records</span></div><div><strong>{profile['messages']:,} 条</strong><span>样本去重规范消息</span></div><div><strong>{readings:,} 个</strong><span>样本地点 reading 条目</span></div></div>
<div class="notice"><strong>计数不要混用：</strong>抽样有 {records:,} 条原始 API records，其中 {profile['raw_reading_lengths'][0]:,} 条 `readings` 为空、{profile['raw_reading_lengths'][1]:,} 条各含 1 个地点条目；{profile['raw_message_types']['missing']:,} 条 `msgType` 缺失，另有 {profile['raw_message_types']['alert']} 条 Alert 和 {profile['raw_message_types']['cancel']} 条 Cancel。按当前标准化及消息键去重后为 {profile['messages']} 条规范消息；地点 reading 为 {readings} 个 API 地点条目。records、消息、地点条目均不能直接解释为真实洪水事件数。</div>
<h2>30 年回填批次状态：只读动态更新</h2><div class="batch"><div class="batch-head"><div><span id="batch-status" class="batch-status">正在读取批次状态…</span><div id="batch-id" class="batch-id">批次 ID：{BATCH_ID or '待启动'}</div></div><button id="refresh-batch" type="button">刷新状态</button></div>
<progress id="batch-progress" max="{TARGET_DAYS}" aria-label="正在读取采集进度"></progress><div class="batch-grid"><div><strong id="batch-complete">—</strong><span>完整采集天数 / 总天数</span></div><div><strong id="batch-attempted">—</strong><span>已尝试日期</span></div><div><strong id="batch-running">—</strong><span>正在采集日期</span></div><div><strong id="batch-not-attempted">—</strong><span>尚未尝试日期</span></div></div><p id="batch-other" class="muted">失败／分页未完成／不可用：等待 API 数据</p><p id="batch-note" class="muted">仅执行 GET 请求；此页面不会新建、暂停、续跑或修改回填任务。</p></div>
{batch_result}
<h2>30 年日历与未知值规则</h2><p>默认目标区间为 {TARGET_START}–{TARGET_END}（新加坡时间，首尾均包含，共 {TARGET_DAYS:,} 天）。热力图显示去重非 Cancel 警报消息数，可切换地点条目数。每日查询完整性与 PUB 实际历史数据覆盖范围分别解释。</p>
<ul><li>完整采集的日期使用完整计数；`complete_zero` 表示该查询日规范化后没有非 Cancel 警报消息，不代表 API 原始 records 一定为零。</li><li>未采集、采集中、不可用、失败、分页未完成日期标记为未知，主计数为 null；本地已观测数量只作为下限展示，不填成完整日零值。</li><li>完整响应里没有有效发布时间的消息通过 `queried_undated_message_count` 单列说明，不归入任何日历日。</li><li>按有效消息 `sent` 归日；无效或缺失时回退到有效 `record.datetime`。不能用请求日或采集日替代发布日。</li></ul>
<h2 id="sampling">旧两年试点的 15 日抽样</h2><p>抽样窗口为 {html.escape(data['window']['start'])}–{html.escape(data['window']['end'])}，累计 {pages:,} 页、{records:,} 条 API 原始 records、{readings:,} 个地点 reading。抽样日期结果：{counts['complete_empty_response']} 天原始 records 为空；{counts['complete_records_no_readings']} 天有 records 但 readings 为空；{counts['complete_with_readings']} 天有地点 reading。{readings:,} 个地点条目只出现在 2025-05-22 与 2026-05-15。</p>
<p>原始 API records 中：`msgType` 缺失 {profile['raw_message_types']['missing']:,} 条、Alert {profile['raw_message_types']['alert']} 条、Cancel {profile['raw_message_types']['cancel']} 条；`readings` 为空 {profile['raw_reading_lengths'][0]:,} 条，含一个地点条目 {profile['raw_reading_lengths'][1]:,} 条。缺失 `msgType` 不等于某个特定类型。读取保存的分页原文并按当前标准化规则重算后，得到 {profile['messages']} 条去重规范消息：明确 `Alert` {profile['explicit_alert_messages']} 条、`Cancel` {profile['cancel_messages']} 条、`msgType` 缺失 {profile['missing_msg_type_messages']} 条。默认非 Cancel 消息口径为 {profile['non_cancel_messages']} 条，其中类型不明单列；消息与 reading 是不同对象。</p>
<div class="table-wrap"><table><thead><tr><th scope="col">抽样日期</th><th scope="col">查询结果</th><th scope="col">页数</th><th scope="col">API 原始 records</th><th scope="col">去重规范消息</th><th scope="col">地点 reading 条目</th><th scope="col">API 时间范围</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>从 API 原文到日历与 GIS</h2><div class="pipeline"><span><b>1 · 原始分页响应</b><small>逐页留存请求结果、状态、原文及 SHA-256</small></span><span><b>2 · 规范消息</b><small>解析 PUB 字段、保留缺失标记、按 message_key 去重</small></span><span><b>3 · SQLite 与覆盖状态</b><small>分开存快照、消息、地点、每日查询完整性</small></span><span><b>4 · GIS 与可视化</b><small>合法 circle → GeoJSON Point；供地图、直方图及热力图读取</small></span></div>
<p>PUB circle 的原始顺序为 `[纬度, 经度, 半径公里]`；GeoJSON Point 坐标改为 `[经度, 纬度]`。无效位置仍可保留消息和质量标记，但不会生成地图点。广播半径表达广播范围，不是实测积水边界。</p>
<h2>限制与研究解释</h2><ul><li>15 个旧试点抽样日不构成连续历史覆盖；一次成功的空响应不能证明 PUB 历史档案完整，亦不能证明当日绝无洪水。官方 API 只提供单日/日期时间筛选，未公布历史保留年限；1996-10-08 边界预检返回 code=0、records 为空，仅证明该日期请求被接受。</li><li>{profile['raw_reading_lengths'][0]:,} 条旧试点原始 records 的 `readings` 为空，{profile['raw_reading_lengths'][1]:,} 条各含 1 个 reading；缺失 `msgType` 不代表某个已知类型。不能把原始 records 数直接当成“洪水次数”。</li><li>30 年 archive 原文画像按 `record.datetime` 统计，时间戳仅覆盖画像中所示的实际范围；更早的空响应不能解释为零洪水。</li><li>道路描述规则只是文字形式初筛，不等于真实道路段匹配；少量样本不能证明描述类型有限。真实事件、受影响道路和交通延误需要额外证据。</li><li>页面进度显示只读取批次 API；API 不可用或批次不存在时会显示明确状态，不会把未查询、失败或未完成的日期渲染为零。</li></ul>
<h2 id="corpus-profile">PUB 30 年窗口语料画像</h2><p>画像按本地 SQLite 中 archive/live 原始响应的 `record.datetime` 新加坡日期筛选，synthetic demo 已排除。它覆盖可获取的原始文本，不代表 API 在整个 30 年窗口内都有历史档案。</p><ul>{corpus_snapshot}</ul><p>字段缺失、值分布、道路描述规则分类与原文样例：<a href="/artifacts/corpus-profile-zh.md">下载 30 年窗口 PUB 语料画像 Markdown</a>。</p>
<h2 id="reproduction">复现与相关材料</h2><ul><li><a href="/">打开工作台与30年热力图</a></li><li><a href="/artifacts/history-availability-zh.md">下载中文 Markdown 报告</a></li><li><a href="/artifacts/history-availability.json">查看 15 日抽样 JSON</a></li><li><a href="/artifacts/history-probe/manifest.json">查看原始分页索引；SHA-256 见 15 日抽样 JSON</a>；示例：<a href="/artifacts/history-probe/2025-05-22/page-001.json">2025-05-22 第 1 页 JSON</a>。</li><li><a href="/artifacts/report-generator.py">查看报告生成脚本</a></li><li><a href="/artifacts/README.md">项目运行及回填接口说明</a></li><li>仅重生成报告：<code>python scripts/report_history_availability.py</code>。</li><li>重新发起 15 日探测：<code>python scripts/probe_history.py</code>，会向 PUB 发网络请求；本报告生成过程只读取已保存文件，不调用 LLM、不触发回填。</li></ul>
<footer>页面数据生成自 experiments/history_availability.json、已保存的采样页面和本地 SQLite；批次卡片只读调用本地状态接口。生成时间以新加坡时间记录。查询完成、历史数据覆盖和洪水事件是三个不同概念。</footer>
</main>{script}</body></html>'''


def main():
    source = json.loads(SOURCE.read_text(encoding='utf-8'))
    data = {**source, 'days': [{**day, 'status': classify(day)} for day in source.get('days', [])]}
    profile = canonical_sample_profile(data)
    batch_summary = load_backfill_summary()
    MARKDOWN.write_text(render_markdown(data, profile, batch_summary), encoding='utf-8')
    WEBPAGE.write_text(render_html(data, profile, batch_summary), encoding='utf-8')
    counts = Counter(day['status'] for day in data['days'])
    print(json.dumps({
        'backfill_target': {'start': TARGET_START, 'end': TARGET_END, 'days': TARGET_DAYS},
        'window': data['window'],
        'window_days': (datetime.fromisoformat(data['window']['end']) - datetime.fromisoformat(data['window']['start'])).days + 1,
        'sampled_days': len(data['days']),
        'pages': sum(day.get('pages', 0) for day in data['days']),
        'raw_records': sum(day.get('record_count', 0) for day in data['days']),
        'raw_msg_type_counts': dict(profile['raw_message_types']),
        'raw_reading_lengths': dict(profile['raw_reading_lengths']),
        'deduplicated_messages': profile['messages'],
        'non_cancel_messages': profile['non_cancel_messages'],
        'explicit_alert_messages': profile['explicit_alert_messages'],
        'cancel_messages': profile['cancel_messages'],
        'missing_msg_type_messages': profile['missing_msg_type_messages'],
        'reading_entries': sum(day.get('reading_count', 0) for day in data['days']),
        'normalized_reading_entries': profile['reading_entries'],
        'statuses': counts,
        'backfill': ({
            'status': batch_summary['status'],
            'complete_days': batch_summary['complete_days'],
            'total_days': batch_summary['total_days'],
            'status_counts': batch_summary['status_counts'],
            'pages': batch_summary['pages'],
            'raw_records': batch_summary['raw_records'],
            'batch_query_metrics': batch_summary['batch_query_metrics'],
            'record_datetime_span': batch_summary['record_datetime_span'],
        } if batch_summary else None),
        'live_batch_endpoint': f'/api/backfill/{BATCH_ID}' if BATCH_ID else None,
        'markdown': MARKDOWN.relative_to(ROOT).as_posix(),
        'webpage': WEBPAGE.relative_to(ROOT).as_posix(),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
