"""Describe the locally collected public PUB Telegram sample without model calls."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SGT = timezone(timedelta(hours=8))
LABELS = {
    "flood_occurred": "洪水发生通报", "flood_subsided": "洪水消退通报",
    "flood_risk_notice": "洪水风险通知", "heavy_rain_notice": "暴雨通知", "other": "未识别/其他",
}
TYPE_RULES = [
    ("flood_subsided", r"\b(?:flash\s+)?flood\s+(?:has\s+)?subsided\b"),
    ("flood_risk_notice", r"\brisk\s+of\s+flash\s+floods?\b"),
    ("flood_occurred", r"\bflash\s+flood\s+occurred\b|\bflash\s+flood\s+at\b"),
    ("heavy_rain_notice", r"\bheavy\s+rain\s+(?:expected|has\s+eased)\b"),
]
ROAD_RULES = {
    "from_to": r"\bfrom\b.+?\bto\b",
    "between_and": r"\bbetween\b.+?\band\b",
    "junction": r"\b(?:junction|junc|intersection)\b",
    "near": r"\b(?:near|outside|opposite|beside)\b",
    "off": r"\boff\b", "slip_road": r"\bslip\s+(?:road|rd)\b",
    "directional": r"\b(?:towards?|leading\s+to|bound|eastbound|westbound|northbound|southbound)\b",
    "parenthetical_place": r"\([^)]*[A-Za-z][^)]*\)",
    "road_name": r"\b(?:road|rd|avenue|ave|drive|dr|lane|street|st|way|link|expressway|KPE|AYE|ECP|TPE)\b",
}
TIME_PATTERN = re.compile(r"(?P<clock>\d{1,2}:\d{2}|\d{4})\s*(?:hours|hrs)\b", re.I)


def find_evidence(pattern: str, text: str, rule: str) -> list[dict]:
    return [{"rule": rule, "span": list(match.span()), "text": match.group()}
            for match in re.finditer(pattern, text, re.I | re.S)]


def classify_message(text: str) -> tuple[str, list[dict]]:
    for label, pattern in TYPE_RULES:
        evidence = find_evidence(pattern, text, label)
        if evidence:
            return label, evidence
    return "other", []


def extract_locations(text: str, label: str) -> list[dict]:
    if label not in {"flood_occurred", "flood_subsided"}:
        return []
    prefix = r"\b(?:flash\s+)?flood\s+(?:(?:has\s+)?subsided\s+)?at\s+"
    stop = r"(?=\.\s*(?:Please|Issued)|\s+Issued\b|\s*\[|$)"
    locations = []
    for match in re.finditer(prefix + r"(?P<location>.*?)" + stop, text, re.I | re.S):
        start, end = match.span("location")
        fragment = match.group("location")
        for piece in re.finditer(r"[^;\n]+", fragment):
            value = piece.group().strip().rstrip(". ")
            if not value:
                continue
            leading = len(piece.group()) - len(piece.group().lstrip())
            piece_start = start + piece.start() + leading
            evidence = [item for rule, pattern in ROAD_RULES.items()
                        for item in find_evidence(pattern, value, rule)]
            locations.append({"location_text": value, "span": [piece_start, piece_start + len(value)],
                              "extraction_rule": "flood_at_until_instruction_or_issued",
                              "road_pattern_evidence": evidence,
                              "road_pattern_labels": sorted({item["rule"] for item in evidence}),
                              "spatial_resolution": "text_only_unmatched"})
    return locations


def annotate_message(message: dict) -> dict:
    text = message.get("text", "")
    label, evidence = classify_message(text)
    clocks = [{"text": match.group(), "clock": match.group("clock"), "span": list(match.span()),
               "semantic_role": "body_clock_without_verified_date"} for match in TIME_PATTERN.finditer(text)]
    locations = extract_locations(text, label)
    return {key: message.get(key) for key in ("message_id", "url", "published_at_utc", "published_at_sgt", "text",
            "source_page_url", "raw_page_sha256", "visible_edited_marker")} | {
        "category": label, "category_zh": LABELS[label], "category_evidence": evidence,
        "locations": locations, "location_count": len(locations), "body_clock_evidence": clocks,
        "location_status": "extracted_text_only" if locations else ("unknown" if label in {"flood_occurred", "flood_subsided"} else "out_of_current_scope"),
        "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "label_method": "deterministic_rules_v1_not_gold_labels",
    }


def build_profile(rows: list[dict], manifest: dict, start: str, end: str) -> dict:
    daily = defaultdict(Counter)
    monthly = defaultdict(Counter)
    duplicates = defaultdict(list)
    locations = Counter()
    roads = Counter()
    for row in rows:
        day = row["published_at_sgt"][:10]
        daily[day][row["category"]] += 1
        monthly[day[:7]][row["category"]] += 1
        duplicates[row["body_sha256"]].append(row["message_id"])
        locations[row["category"]] += row["location_count"]
        for location in row["locations"]:
            roads.update(location["road_pattern_labels"])
    event_rows = [row for row in rows if row["category"] in {"flood_occurred", "flood_subsided"}]
    return {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "window_sgt": {"start_inclusive": start, "end_exclusive": end},
            "collector_manifest": manifest, "message_count": len(rows), "type_counts": dict(Counter(row["category"] for row in rows)),
            "event_notice_day_count": len({row["published_at_sgt"][:10] for row in event_rows}),
            "occurred_notice_day_count": len({row["published_at_sgt"][:10] for row in rows if row["category"] == "flood_occurred"}),
            "location_counts": dict(locations), "event_location_count": sum(row["location_count"] for row in event_rows),
            "missing_event_location_ids": [row["message_id"] for row in event_rows if not row["locations"]],
            "multi_location_event_ids": [row["message_id"] for row in event_rows if row["location_count"] > 1],
            "road_pattern_counts_over_event_location_entries": dict(roads),
            "exact_body_duplicate_candidates": [ids for ids in duplicates.values() if len(ids) > 1],
            "other_message_ids": [row["message_id"] for row in rows if row["category"] == "other"],
            "messages_with_body_clock_count": sum(bool(row["body_clock_evidence"]) for row in rows),
            "visible_edited_marker_count": sum(bool(row['visible_edited_marker']) for row in rows),
            "archive_comparison": compare_archive(rows,start,end),
            "observed_structure_counts_by_category": {
                label: {
                    'messages':len([row for row in rows if row['category']==label]),
                    'bracket_headline':sum(bool(re.match(r'\s*\[(?:FLASH FLOOD OCCURRED|Risk of Flash Floods?)\]',row['text'],re.I)) for row in rows if row['category']==label),
                    'please_avoid_area':sum(bool(re.search(r'Please avoid the area',row['text'],re.I)) for row in rows if row['category']==label),
                    'body_clock':sum(bool(row['body_clock_evidence']) for row in rows if row['category']==label),
                    'issued_literal':sum(bool(re.search(r'\bIssued\b',row['text'],re.I)) for row in rows if row['category']==label),
                    'nea_credit':sum(bool(re.search(r'Issued by NEA',row['text'],re.I)) for row in rows if row['category']==label),
                } for label in LABELS},
            "monthly_counts_by_publication_sgt": dict(sorted(monthly.items())), "daily_counts_by_publication_sgt": dict(sorted(daily.items())),
            "format_flags": {"issued_literal": sum(bool(re.search(r"\bIssued\b", row["text"], re.I)) for row in rows),
                             "colon_clock": sum(bool(re.search(r"\d{1,2}:\d{2}\s*(?:hours|hrs)", row["text"], re.I)) for row in rows),
                             "compact_clock": sum(bool(re.search(r"\b\d{4}\s*(?:hours|hrs)", row["text"], re.I)) for row in rows),
                             "multiline": sum("\n" in row["text"] for row in rows)},
            "permitted_ai_reuse": "not_confirmed", "classifier_accuracy": None,
            "interpretation": "Counts describe visible messages and rule-extracted textual entries, not independently verified flood events."}


def compare_archive(rows: list[dict], start: str, end: str) -> dict:
    database = ROOT/'data/flood.sqlite3'
    if not database.exists():
        return {'status':'database_unavailable'}
    with sqlite3.connect('file:'+quote(database.as_posix(),safe='/:')+'?mode=ro',uri=True) as connection:
        archived = [json.loads(item[0]) for item in connection.execute("select body from messages where source = 'archive'")]
    daily = defaultdict(lambda: {'telegram_occurred':0,'archive_explicit_alert':0,'telegram_urls':[],'archive_locations':[]})
    for row in rows:
        if row['category'] == 'flood_occurred':
            value = daily[row['published_at_sgt'][:10]]
            value['telegram_occurred'] += 1
            value['telegram_urls'].append(row['url'])
    for message in archived:
        sent = message.get('sent')
        if message.get('msg_type') != 'Alert' or not sent:
            continue
        day = datetime.fromisoformat(sent.replace('Z','+00:00')).astimezone(SGT).date().isoformat()
        if not start <= day < end:
            continue
        value = daily[day]
        value['archive_explicit_alert'] += 1
        value['archive_locations'].extend(reading.get('area_description','') for reading in message.get('readings',[]))
    return {'status':'read_only_local_archive', 'grouping':'Telegram published_at_sgt vs archive sent converted to SGT; no event merge',
            'daily_counts':dict(sorted(daily.items())), 'telegram_occurred_count':sum(item['telegram_occurred'] for item in daily.values()),
            'archive_explicit_alert_count':sum(item['archive_explicit_alert'] for item in daily.values())}


def render_markdown(profile: dict, rows: list[dict]) -> str:
    counts = profile["type_counts"]
    lines = ["# PUB Telegram 最近一年公开样本模式分析", "",
             f"窗口（SGT）：{profile['window_sgt']['start_inclusive']} ≤ 发布时间 < {profile['window_sgt']['end_exclusive']}。共采集 **{len(rows)} 条可见消息**。",
             "", "## 消息分类与当前研究范围", "", "|规则类型|消息数|提取地点条目数|", "|---|---:|---:|"]
    for label, name in LABELS.items():
        lines.append(f"|{name}|{counts.get(label, 0)}|{profile['location_counts'].get(label, 0)}|")
    lines += ["", f"当前洪水事件通报分析采用发生与消退两类，共 {counts.get('flood_occurred',0)+counts.get('flood_subsided',0)} 条消息、{profile['event_location_count']} 个地点文字条目；发生通报分布在 {profile['occurred_notice_day_count']} 个日期。",
              "风险及暴雨通知保留用于说明频道组成，未构建风险预测方法。每条消息可以带一个或多个地点；这里不把消息数等同洪水事件数。",
              "", "## 月度分布（按Telegram发布时间，SGT）", "", "|月份|发生|消退|风险|暴雨|其他|", "|---|---:|---:|---:|---:|---:|"]
    month = datetime.fromisoformat(profile['window_sgt']['start_inclusive'])
    while month.strftime('%Y-%m') <= profile['window_sgt']['end_exclusive'][:7]:
        key = month.strftime('%Y-%m')
        item = profile['monthly_counts_by_publication_sgt'].get(key, {})
        lines.append('|'+key+'|'+ '|'.join(str(item.get(label,0)) for label in LABELS)+'|')
        month = month.replace(year=month.year + (month.month == 12), month=1 if month.month == 12 else month.month + 1)
    lines += ["", "月份边界为部分月份（2025-10-09起、2026-10-09止）；零表示本次已遍历页面中未见该类消息，不证明真实零洪水。", "", "## 道路描述模式", "",
              "只对发生/消退的地点文字运行规则，不对避让指令或时间括号运行。模式可重叠；没有完成真实道路匹配。", "", "|模式|地点条目命中数|", "|---|---:|"]
    lines.extend(f"|{name}|{profile['road_pattern_counts_over_event_location_entries'].get(name,0)}|" for name in ROAD_RULES)
    lines += ["", f"发生/消退地点提取缺失 ID：{profile['missing_event_location_ids']}；多地点消息 ID：{profile['multi_location_event_ids']}。",
              "未识别为多地点不保证每条确为一个事件；junction 中的 and 属于道路关系，不应拆成两个独立地点。风险通知中可见分号/换行列举多个地点，这类地点未纳入当前事件条目统计。",
              "", "## 格式变体与证据", "", "观察到结构化标题与正文并存、标题缺失的消退正文、Issued可有可无、HH:MM与HHMM、hours与hrs、道路缩写与完整词形。下列全文来源可逐条核查。"]
    examples = {}
    for row in rows:
        for key in [row['category']] + [pattern for loc in row['locations'] for pattern in loc['road_pattern_labels']]:
            examples.setdefault(key,row)
    for key,row in examples.items():
        lines += ["", f"**{key}** — [{row['message_id']}]({row['url']})", "", "> " + row['text'].replace('\n','\n> ')]
    lines += ["", "|类型|消息|方括号类型标题|Please avoid the area|正文时间|Issued文字|NEA署名|", "|---|---:|---:|---:|---:|---:|---:|"]
    for label,item in profile['observed_structure_counts_by_category'].items():
        lines.append('|'+LABELS[label]+'|'+'|'.join(str(item[key]) for key in ['messages','bracket_headline','please_avoid_area','body_clock','issued_literal','nea_credit'])+'|')
    lines += ["", "这些是本样本观察到的结构，不是频道未来消息的必填字段契约；5类是分析规则分类，不证明来源只可能发布5类信息。", "", f"时间形式计数（可重叠）：{json.dumps(profile['format_flags'],ensure_ascii=False)}。",
              "正文时间只保留原文证据，未推断日期；Telegram发布时间不等于正文Issued时间，也不能自动作为洪水开始或结束时刻。",
              "", "## 重复与未知", "", f"完全相同正文候选组：{profile['exact_body_duplicate_candidates']}（未自动删除）。规则未识别 ID：{profile['other_message_ids']}。",
              "相同正文可在不同时间再次发布，地点相同或正文近似也可能是后续更新；不能仅按正文或地点自动合并。Boon Lay Avenue与Boon Lay Ave的消退通知还体现近似重复/更新候选，需要人工核对。",
              "", "## 来源、完整性与研究复用限制", "",
              "本次只遍历当前 [pubfloodalerts公开网页](https://t.me/s/pubfloodalerts)，没有合并旧sgflood别名。采集清单与页面哈希见JSON输出及manifest。公开页面成功回溯至窗口之前，不能保证被删除、被编辑、不可见或未呈现的历史消息完整。",
              f"可见edited标记消息 {profile['visible_edited_marker_count']} 条；没有恢复其历史版本。公开正文有可能是已编辑后的版本或网页截断呈现（如1332反诈骗通知末尾为please），下表的完整正文指完整可见文本。",
              "来源与访问方法详见本地文档 docs/PUB_TELEGRAM_SOURCE_RESEARCH.md。PUB渠道发布、NEA署名暴雨通知、JOC传感器与现场数据的关联应按官方证据解读；频道没有提供每条消息对应的传感器原始记录。",
              "[Telegram内容许可条款](https://telegram.org/tos/content-licensing)对用于AI开发、验证和benchmarking的抓取/聚合设有限制。当前完成本地规则描述统计，没有取得相关AI研究复用许可；只用规则或不训练模型不能自动证明合规。后续应向PUB申请直接历史通报数据及明确研究使用许可。",
              "分类由确定性规则产生，未建立人工金标，不能报告分类准确率。地点仍是文本，没有真实道路段、坐标、通报间事件配对或持续时间推断。", ""]
    comparison = profile['archive_comparison']
    if comparison['status'] == 'read_only_local_archive':
        lines += ['## 与本地data.gov.sg历史API通报对照', '',
                  f"同一时间窗口，Telegram规则识别发生通报 {comparison['telegram_occurred_count']} 条，本地archive明确Alert消息 {comparison['archive_explicit_alert_count']} 条。这里只读比较消息日期与地点，未合并事件。",
                  'Telegram按发布时间SGT聚合，archive按sent转换SGT聚合；两者的日期定义不同。来源同属PUB相关发布链路，不能当作独立ground truth，也不能仅凭数量差异认定API漏报。', '',
                  '|日期|Telegram发生|archive明确Alert|archive地点文字|', '|---|---:|---:|---|']
        for day,item in comparison['daily_counts'].items():
            lines.append(f"|{day}|{item['telegram_occurred']}|{item['archive_explicit_alert']}|{'；'.join(item['archive_locations'])}|")
        lines += ['', 'Telegram原文链接见JSON profile archive_comparison.daily_counts.telegram_urls。0表示当前本地样本未见相应消息；各来源覆盖完整性仍需向PUB核实。', '']
    return '\n'.join(lines)


def markdown_to_html(markdown: str) -> str:
    def inline(text: str) -> str:
        escaped = html.escape(text)
        escaped = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', escaped)
        return re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', escaped)
    output = []
    in_table = False
    for line in markdown.splitlines():
        if in_table and not line.startswith('|'):
            output.append('</table>')
            in_table = False
        if line.startswith('|'):
            if re.fullmatch(r'[|:\- ]+',line):
                continue
            if not in_table:
                output.append('<table>')
                in_table = True
            output.append('<tr>' + ''.join('<td>'+inline(cell.strip())+'</td>' for cell in line.strip('|').split('|'))+'</tr>')
        elif line.startswith('# '):
            output.append('<h1>'+inline(line[2:])+'</h1>')
        elif line.startswith('## '):
            output.append('<h2>'+inline(line[3:])+'</h2>')
        elif line.startswith('> '):
            output.append('<blockquote>'+inline(line[2:])+'</blockquote>')
        elif line.strip():
            output.append('<p>'+inline(line)+'</p>')
    if in_table:
        output.append('</table>')
    return ''.join(output)


def render_html(markdown: str, rows: list[dict], profile: dict) -> str:
    body = markdown_to_html(markdown)
    table = ''.join(f'<tr data-type="{row["category"]}"><td><a href="{html.escape(row["url"])}">{row["message_id"]}</a></td><td>{html.escape(row["published_at_sgt"])}</td><td>{row["category_zh"]}</td><td>{html.escape("; ".join(loc["location_text"] for loc in row["locations"]))}</td><td><pre>{html.escape(row["text"])}</pre></td></tr>' for row in rows)
    options = ''.join(f'<option value="{label}">{name}</option>' for label,name in LABELS.items())
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PUB Telegram 事件通报样本分析</title><style>body{max-width:1200px;margin:36px auto;padding:0 22px;font:16px/1.65 system-ui;color:#18302b;background:#f7faf8}h1,h2{color:#126b56}table{border-collapse:collapse;width:100%;margin:20px 0;background:white}td,th{padding:9px;border:1px solid #d9e4df;text-align:left;vertical-align:top}pre{white-space:pre-wrap;font:14px/1.5 system-ui;margin:0}a{color:#076b91}blockquote{border-left:3px solid #67a891;padding:8px 16px;background:#fff}input,select{padding:10px;margin:5px}</style><main>' + body + '<h2>全部正文样本（本地报告）</h2><p>可按类型筛选并搜索正文、道路或日期。分类证据span、地点规则证据及原始页面哈希保存在标注JSONL中。风险通知地点列留空表示未纳入当前事件研究范围。</p><input id="search" placeholder="搜索道路、日期、正文" aria-label="搜索"><select id="category" aria-label="类型"><option value="">全部类型</option>'+options+'</select><span id="count"></span><table><thead><tr><th>原始消息</th><th>发布时间SGT</th><th>规则分类</th><th>事件地点文字</th><th>完整正文</th></tr></thead><tbody id="samples">'+table+'</tbody></table></main><script>const rows=[...document.querySelectorAll("#samples tr")];function filter(){const query=document.getElementById("search").value.toLowerCase(),type=document.getElementById("category").value;let count=0;rows.forEach(row=>{const visible=(!type||row.dataset.type===type)&&row.textContent.toLowerCase().includes(query);row.hidden=!visible;count+=visible});document.getElementById("count").textContent=`显示 ${count} / ${rows.length} 条`;}document.getElementById("search").addEventListener("input",filter);document.getElementById("category").addEventListener("change",filter);filter();</script></html>'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, default=ROOT/'data/telegram_pub/messages.jsonl')
    parser.add_argument('--start-date', default='2025-10-09')
    parser.add_argument('--end-date', default='2026-10-09')
    args = parser.parse_args()
    raw = [json.loads(line) for line in args.input.read_text(encoding='utf-8').splitlines() if line.strip()]
    rows = [annotate_message(item) for item in raw if args.start_date <= item['published_at_sgt'][:10] < args.end_date]
    if len({row['message_id'] for row in rows}) != len(rows):
        raise ValueError('Input contains repeated message IDs; collector must resolve identity first.')
    manifest_path = args.input.parent/'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'status':'missing_manifest'}
    profile = build_profile(rows,manifest,args.start_date,args.end_date)
    experiments = ROOT/'experiments'
    experiments.mkdir(exist_ok=True)
    (experiments/'pub_telegram_profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
    (experiments/'pub_telegram_labeled.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')
    with (experiments/'pub_telegram_labeled.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=['message_id','url','published_at_sgt','category','location_count','location_text','text'])
        writer.writeheader()
        for row in rows:
            writer.writerow({key:row[key] for key in ['message_id','url','published_at_sgt','category','location_count','text']} | {'location_text':'; '.join(loc['location_text'] for loc in row['locations'])})
    markdown = render_markdown(profile,rows)
    (experiments/'pub_telegram_profile_zh.md').write_text(markdown,encoding='utf-8')
    (ROOT/'reports/telegram_pub.html').write_text(render_html(markdown,rows,profile),encoding='utf-8')
    print(json.dumps({key:profile[key] for key in ['message_count','type_counts','event_location_count','road_pattern_counts_over_event_location_entries','missing_event_location_ids','other_message_ids']},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
