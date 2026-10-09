"""Create a reproducible, read-only profile of genuine PUB snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DB_DEFAULT = ROOT / "data" / "flood.sqlite3"
JSON_DEFAULT = ROOT / "experiments" / "corpus_profile.json"
REPORT_DEFAULT = ROOT / "experiments" / "corpus_profile_zh.md"
SGT = timezone(timedelta(hours=8))

ROAD_RULES = [
    ("from_to_segment", re.compile(r"\bfrom\s+([^.;,]{1,100}?)\s+to\s+([^.;,]{1,100}?)(?=[.;,]|$)", re.I),
     "from … to …，可能限定道路两端"),
    ("junction_or_crossing", re.compile(r"\b(junction|intersection|crossing|crossroads?)\b", re.I),
     "出现路口或交叉口词语"),
    ("near_landmark", re.compile(r"\b(near(?:by)?|beside|opposite|adjacent\s+to|in\s+front\s+of)\b", re.I),
     "出现地标邻近关系词"),
    ("parenthetical_name", re.compile(r"\([^()]{1,100}\)"),
     "出现括号内地点文字，语义需人工核对"),
    ("road_name_with_relation", re.compile(
        r"\b(?:at|along|on)\s+[^.;]{1,120}?\b(?:road|rd\.?|street|st\.?|avenue|ave\.?|drive|dr\.?|"
        r"expressway|highway|hwy\.?|way|crescent|cres\.?|close|lane|link|boulevard|blvd\.?|place|pl\.?)\b", re.I),
     "出现 at/along/on 与道路类型词组合"),
    ("multiple_place_names", re.compile(r"\b(?:and|between)\b|\s[/&]\s", re.I),
     "出现地点可能并列的连接形式"),
    ("named_road_only", re.compile(
        r"\b[A-Z0-9][\w'’-]*(?:\s+[A-Z0-9][\w'’-]*){0,5}\s+"
        r"(?:road|rd\.?|street|st\.?|avenue|ave\.?|drive|dr\.?|expressway|highway|hwy\.?|way|"
        r"crescent|cres\.?|close|lane|link|boulevard|blvd\.?|place|pl\.?)\b", re.I),
     "识别到道路类型名称，未识别到范围关系"),
]
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9_ -]{1,80}\]|\{\{?[^{}\n]{1,100}\}\}?|<[^<>\n]{1,100}>", re.I)
TEXT_FIELDS = ("description", "headline", "instruction", "areaDesc", "category", "event",
               "responseType", "severity", "urgency", "certainty", "senderName")


def as_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期需为 YYYY-MM-DD") from exc


def date_sgt(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SGT)
    return moment.astimezone(SGT).date()


def record_signature(record):
    stable = dict(record)
    stable.pop("updatedTimestamp", None)
    stable.pop("timestamp", None)
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def empty(value):
    return isinstance(value, (str, list, dict)) and not value


class Fields:
    def __init__(self):
        self.objects = 0
        self.fields = defaultdict(lambda: {
            "present": 0, "null": 0, "empty": 0, "types": Counter(),
            "array_item_types": Counter(), "array_lengths": Counter(),
        })

    def add(self, obj):
        self.objects += 1
        for key, value in obj.items():
            item = self.fields[key]
            item["present"] += 1
            item["types"][kind(value)] += 1
            item["null"] += value is None
            item["empty"] += empty(value)
            if isinstance(value, list):
                item["array_lengths"][str(len(value))] += 1
                item["array_item_types"].update(kind(element) for element in value)

    def export(self):
        return {
            "object_count": self.objects,
            "fields": {
                key: {
                    "present": stat["present"],
                    "missing": self.objects - stat["present"],
                    "null": stat["null"],
                    "empty": stat["empty"],
                    "types": dict(sorted(stat["types"].items())),
                    "array_item_types": dict(sorted(stat["array_item_types"].items())),
                    "array_lengths": dict(sorted(stat["array_lengths"].items(), key=lambda pair: int(pair[0]))),
                }
                for key, stat in sorted(self.fields.items())
            },
        }


def list_state(record):
    item = record.get("item", "__missing__")
    if item == "__missing__":
        item_state = "missing"
    elif item is None:
        item_state = "null"
    elif isinstance(item, dict):
        item_state = "empty_object" if not item else "object"
    else:
        item_state = "other_type"
    if not isinstance(item, dict):
        readings_state = "item_unavailable"
    elif "readings" not in item:
        readings_state = "missing"
    elif item["readings"] is None:
        readings_state = "null"
    elif not isinstance(item["readings"], list):
        readings_state = "other_type"
    elif not item["readings"]:
        readings_state = "empty_array"
    else:
        readings_state = "nonempty_array"
    return "item." + item_state, "item.readings." + readings_state


def text_values(reading):
    area = reading.get("area") if isinstance(reading.get("area"), dict) else {}
    values = {
        "headline": reading.get("headline"),
        "description": reading.get("description"),
        "instruction": reading.get("instruction"),
        "area.areaDesc": area.get("areaDesc", reading.get("areaDesc")),
    }
    return {key: value.strip() for key, value in values.items() if isinstance(value, str) and value.strip()}


def add_value(inventory, field, value, reference):
    if not isinstance(value, str) or not value.strip():
        return
    value = value.strip()
    stat = inventory[field].setdefault(value, {"count": 0, "references": []})
    stat["count"] += 1
    if len(stat["references"]) < 3:
        stat["references"].append(reference)


def classify_road(reading, reference, record, parent_item=None):
    fields = text_values(reading)
    if isinstance(parent_item, dict):
        for key in ("description", "headline", "instruction"):
            value = parent_item.get(key)
            if isinstance(value, str) and value.strip() and key not in fields:
                fields["item." + key] = value.strip()
    text = " | ".join(f"{key}: {value}" for key, value in fields.items())
    matches = []
    for category, pattern, explanation in ROAD_RULES:
        if category == "named_road_only" and any(
            prior["category"] in {
                "from_to_segment", "junction_or_crossing", "near_landmark",
                "parenthetical_name", "road_name_with_relation", "multiple_place_names",
            }
            for prior in matches
        ):
            continue
        found = pattern.search(text)
        if found:
            matches.append({"category": category, "rule": explanation, "evidence": found.group(0)})
    if not matches:
        matches.append({
            "category": "other_or_uncertain" if text else "no_descriptive_text",
            "rule": "规则未识别；此项不表示地理匹配失败或成功",
            "evidence": text[:240],
        })
    return {
        "categories": [item["category"] for item in matches],
        "matches": matches,
        "text_fields": fields,
        "reference": reference,
        "record_datetime": record.get("datetime"),
    }


def profile(db_path, start, end, top_limit, example_limit):
    if start and end and start > end:
        raise ValueError("--start-date 不能晚于 --end-date")
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    uri = "file:" + quote(db_path.resolve().as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        snapshots = list(conn.execute(
            "SELECT id,source,fetched_at,http_status,error,raw_text FROM snapshots "
            "WHERE source IN ('archive','live') ORDER BY id"
        ))
    finally:
        conn.close()

    snap_stats = defaultdict(lambda: {
        "snapshot_count": 0, "http_statuses": Counter(), "error_count": 0,
        "invalid_json_count": 0, "records_missing_count": 0, "records_wrong_type_count": 0,
        "records_empty_array_count": 0, "records_nonempty_array_count": 0,
        "record_sightings_in_window": 0, "pagination_token_present_count": 0,
    })
    response_fields = Fields()
    response_data_fields = Fields()
    response_code_values = Counter()
    response_error_values = Counter()
    unique_records = {}
    malformed = []
    date_counts = Counter()
    all_sightings = 0
    selected_sightings = 0
    undated_skipped = 0
    outside = 0
    for snap in snapshots:
        stat = snap_stats[snap["source"]]
        stat["snapshot_count"] += 1
        stat["http_statuses"][str(snap["http_status"])] += 1
        stat["error_count"] += bool(snap["error"])
        try:
            payload = json.loads(snap["raw_text"] or "")
        except (json.JSONDecodeError, TypeError):
            stat["invalid_json_count"] += 1
            if len(malformed) < example_limit:
                malformed.append({"snapshot_id": snap["id"], "source": snap["source"], "reason": "invalid_json"})
            continue
        if not isinstance(payload, dict):
            stat["invalid_json_count"] += 1
            continue
        response_fields.add(payload)
        if payload.get("code") is not None:
            response_code_values[str(payload["code"])] += 1
        error_message = payload.get("errorMsg")
        if error_message is not None:
            response_error_values[str(error_message)] += 1
        data = payload.get("data")
        if not isinstance(data, dict):
            stat["records_missing_count"] += 1
            continue
        response_data_fields.add(data)
        stat["pagination_token_present_count"] += bool(data.get("paginationToken"))
        if "records" not in data:
            stat["records_missing_count"] += 1
            continue
        recs = data["records"]
        if not isinstance(recs, list):
            stat["records_wrong_type_count"] += 1
            continue
        if not recs:
            stat["records_empty_array_count"] += 1
            continue
        stat["records_nonempty_array_count"] += 1
        for index, record in enumerate(recs):
            all_sightings += 1
            if not isinstance(record, dict):
                continue
            local_date = date_sgt(record.get("datetime"))
            if local_date is None:
                undated_skipped += bool(start or end)
                if start or end:
                    continue
            if start and local_date and local_date < start:
                outside += 1
                continue
            if end and local_date and local_date > end:
                outside += 1
                continue
            if local_date:
                date_counts[local_date.isoformat()] += 1
            stat["record_sightings_in_window"] += 1
            selected_sightings += 1
            signature = record_signature(record)
            reference = {"source": snap["source"], "snapshot_id": snap["id"], "record_index": index}
            item = unique_records.get(signature)
            if item is None:
                unique_records[signature] = {
                    "record": record, "signature": signature, "occurrences": 1,
                    "references": [reference], "sources": {snap["source"]},
                }
            else:
                item["occurrences"] += 1
                item["sources"].add(snap["source"])
                if len(item["references"]) < 10:
                    item["references"].append(reference)

    # Schema and state metrics use unique records so repeated API pages do not inflate field rates.
    fields = {name: Fields() for name in ("record", "item", "reading", "reading.area", "reading.eventCode")}
    structure_states = Counter()
    msg_types = Counter()
    item_types = Counter()
    missing_msg_type = 0
    messages = {}
    unique_reading_count = 0
    for saved in unique_records.values():
        record = saved["record"]
        fields["record"].add(record)
        structure_states.update(list_state(record))
        item = record.get("item")
        if not isinstance(item, dict):
            continue
        fields["item"].add(item)
        if item.get("msgType") is None or not str(item.get("msgType")).strip():
            missing_msg_type += 1
        else:
            msg_types[str(item["msgType"])] += 1
        if item.get("type") is not None:
            item_types[str(item["type"])] += 1
        readings = item.get("readings")
        if not isinstance(readings, list):
            readings = []
        for reading in readings:
            if not isinstance(reading, dict):
                continue
            fields["reading"].add(reading)
            unique_reading_count += 1
            area = reading.get("area")
            if isinstance(area, dict):
                fields["reading.area"].add(area)
            event_code = reading.get("eventCode")
            if isinstance(event_code, dict):
                fields["reading.eventCode"].add(event_code)
        meaningful = bool(
            item.get("identifier") or item.get("msgType") or readings
            or any(isinstance(item.get(key), str) and item[key].strip() for key in TEXT_FIELDS)
        )
        if meaningful:
            if item.get("identifier"):
                key = "id:" + json.dumps([
                    item.get("sender") or "", str(item["identifier"]),
                    str(item.get("sent") or record.get("datetime") or ""),
                ], ensure_ascii=False)
            else:
                key = "record:" + saved["signature"]
            messages.setdefault(key, saved)

    text_inventory = defaultdict(dict)
    placeholders = Counter()
    placeholder_examples = []
    road_counts = Counter()
    road_examples = []
    type_missing_with_readings = 0
    explicit_alerts = 0
    explicit_cancels = 0
    for message_key, saved in messages.items():
        record = saved["record"]
        item = record.get("item")
        if not isinstance(item, dict):
            continue
        reference = saved["references"][0] if saved["references"] else None
        msg_type = str(item.get("msgType") or "").casefold()
        explicit_alerts += msg_type == "alert"
        explicit_cancels += msg_type == "cancel"
        readings = item.get("readings") if isinstance(item.get("readings"), list) else []
        valid_readings = [reading for reading in readings if isinstance(reading, dict)]
        type_missing_with_readings += bool(valid_readings) and not msg_type
        for field in ("type", "msgType", "status", "scope", "category", "event", "responseType",
                      "severity", "urgency", "certainty", "description", "headline", "instruction"):
            add_value(text_inventory, "item." + field, item.get(field), reference)
        for reading_index, reading in enumerate(valid_readings):
            area = reading.get("area") if isinstance(reading.get("area"), dict) else {}
            ref = {**(reference or {}), "reading_index": reading_index,
                   "record_datetime": record.get("datetime"), "record_sha256": saved["signature"]}
            for key in TEXT_FIELDS:
                add_value(text_inventory, "reading." + key, reading.get(key), ref)
            add_value(text_inventory, "reading.area.areaDesc", area.get("areaDesc"), ref)
            code = reading.get("eventCode") if isinstance(reading.get("eventCode"), dict) else {}
            add_value(text_inventory, "reading.eventCode.value", code.get("value"), ref)
            add_value(text_inventory, "reading.eventCode.valueName", code.get("valueName"), ref)

            texts = text_values(reading)
            for key in ("description", "headline", "instruction"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    texts["item." + key] = value.strip()
            for field, text in texts.items():
                for match in PLACEHOLDER.finditer(text):
                    token = match.group(0)
                    placeholders[token] += 1
                    if len(placeholder_examples) < example_limit:
                        placeholder_examples.append({"token": token, "field": field, "text": text, "reference": ref})
            classification = classify_road(reading, ref, record, item)
            road_counts.update(classification["categories"])
            if len(road_examples) < example_limit:
                road_examples.append({
                    "message_key": message_key, "msgType": item.get("msgType"),
                    "description": reading.get("description"), "headline": reading.get("headline"),
                    "areaDesc": area.get("areaDesc"), "classification": classification,
                })

    snapshot_export = {}
    for source, stat in sorted(snap_stats.items()):
        snapshot_export[source] = {
            **{key: value for key, value in stat.items() if key != "http_statuses"},
            "http_statuses": dict(sorted(stat["http_statuses"].items())),
        }
    raw_source_counts = Counter()
    for source, stat in snap_stats.items():
        raw_source_counts[source] = stat["record_sightings_in_window"]
    unique_source_counts = Counter()
    for item in unique_records.values():
        unique_source_counts[next(iter(sorted(item["sources"])))] += 1

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": db_path.resolve().as_posix(),
        "window": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "basis": "record.datetime converted to Singapore local date; undated rows are excluded when a window is selected",
        },
        "sources_included": ["archive", "live"],
        "synthetic_demo_excluded": True,
        "deduplication": {
            "record_key": "SHA-256 of record JSON after removing top-level updatedTimestamp and timestamp",
            "message_key": "sender + identifier + sent (falling back to record.datetime); absent identifier uses deduplicated record hash",
            "profile_denominator": "deduplicated records; repeated raw sightings and snapshots are separately reported",
        },
        "snapshot_count": len(snapshots),
        "snapshots_by_source": snapshot_export,
        "record_sightings_all_snapshots": all_sightings,
        "record_sightings_in_window": selected_sightings,
        "unique_records_in_window": len(unique_records),
        "duplicate_sightings_in_window": max(0, selected_sightings - len(unique_records)),
        "unique_records_by_primary_source": dict(sorted(unique_source_counts.items())),
        "record_sightings_by_source_in_window": dict(sorted(raw_source_counts.items())),
        "record_sightings_by_date_sgt": dict(sorted(date_counts.items())),
        "undated_rows_skipped_for_window": undated_skipped,
        "rows_outside_window": outside,
        "record_date_span_sgt": {
            "first": min(date_counts) if date_counts else None,
            "last": max(date_counts) if date_counts else None,
        },
        "unique_record_empty_states": dict(sorted(structure_states.items())),
        "unique_item_type_values": dict(sorted(item_types.items())),
        "unique_item_msgType_values": dict(sorted(msg_types.items())),
        "unique_records_missing_or_empty_msgType": missing_msg_type,
        "unique_alert_like_messages": len(messages),
        "explicit_msgType_alert_messages": explicit_alerts,
        "explicit_msgType_cancel_messages": explicit_cancels,
        "readings_with_missing_msgType_messages": type_missing_with_readings,
        "unique_reading_objects": unique_reading_count,
        "response_code_values": dict(sorted(response_code_values.items())),
        "response_errorMsg_values": dict(sorted(response_error_values.items())),
        "schema_profiles": {
            "response": response_fields.export(),
            "response.data": response_data_fields.export(),
            **{name: fields[name].export() for name in fields},
        },
        "text_value_profiles": {
            field: {
                "unique_values": len(values),
                "top_values": [
                    {"value": value, "count": stat["count"], "references": stat["references"]}
                    for value, stat in sorted(values.items(), key=lambda pair: (-pair[1]["count"], pair[0]))[:top_limit]
                ],
                "omitted_values": max(0, len(values) - top_limit),
            }
            for field, values in sorted(text_inventory.items())
        },
        "placeholder_counts": dict(sorted(placeholders.items())),
        "placeholder_examples": placeholder_examples,
        "road_description_rules": [
            {"category": name, "regex": pattern.pattern, "interpretation": explanation}
            for name, pattern, explanation in ROAD_RULES
        ],
        "road_description_pattern_counts": dict(sorted(road_counts.items())),
        "road_description_examples": road_examples,
        "malformed_response_examples": malformed,
        "limitations": [
            "仅分析 SQLite 中 source=archive/live 的 PUB 原始响应；synthetic demo 已排除。",
            "快照和 record sighting 包含重复分页/重复抓取；字段分布按去重 record 统计。",
            "record 去重忽略顶层 updatedTimestamp/timestamp，但保留 record.datetime 与 item 内容。",
            "没有 identifier 的消息以 record 哈希去重；内容更新可能形成不同记录。",
            "带 readings 但 msgType 缺失的条目只能叫 alert-like，不能严格确认是 Alert 或独立洪水事件。",
            "道路模式是可审查的正则规则初筛，不是道路几何匹配；少量样本不能证明描述类型有限。",
            "本画像只覆盖所选 record.datetime 窗口；没有归档记录的日期不能据此解释为零警报。",
        ],
    }


def esc(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(data):
    snapshots = data["snapshots_by_source"]
    archive = snapshots.get("archive", {})
    live = snapshots.get("live", {})
    begin = data["window"]["start"] or "全部已归档日期"
    finish = data["window"]["end"] or "未限定"
    lines = [
        "# PUB 洪水警报语料画像", "",
        f"> 生成时间：{data['generated_at_utc']}。由 scripts/analyze_corpus.py 从本地 SQLite 只读生成。", "",
        "## 数据范围与计数口径", "",
        f"- 日期窗口：{begin} 至 {finish}，按 record.datetime 的新加坡本地日期筛选。",
        f"- 来源：archive {archive.get('snapshot_count', 0)} 个快照，live {live.get('snapshot_count', 0)} 个快照；synthetic demo 已排除。",
        f"- 窗口内 raw record sighting：{data['record_sightings_in_window']}；去重 record：{data['unique_records_in_window']}；重复 sighting：{data['duplicate_sightings_in_window']}。",
        f"- alert-like 消息：{data['unique_alert_like_messages']}；明确 msgType=Alert：{data['explicit_msgType_alert_messages']}；明确 msgType=Cancel：{data['explicit_msgType_cancel_messages']}。",
        f"- readings 对象：{data['unique_reading_objects']}；带 readings 但 msgType 缺失的消息：{data['readings_with_missing_msgType_messages']}。",
        f"- 根响应 code：{json.dumps(data['response_code_values'], ensure_ascii=False)}；errorMsg：{json.dumps(data['response_errorMsg_values'], ensure_ascii=False) or '未出现非空值'}。",
        f"- record.datetime 覆盖：{data['record_date_span_sgt']['first'] or '无'} 至 {data['record_date_span_sgt']['last'] or '无'}。",
        "", "**样本解释：**alert-like 表示消息有标识、消息类型、正文或地点等内容。若 msgType 缺失，不能严格称为 Alert；消息数也不等于洪水事件数。", "",
        "## 快照和空结构", "",
        "| 来源 | 快照 | HTTP 状态 | 错误标记 | records 空数组 | records 非空数组 | 窗口内 record sighting | 有下一页 token |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for source, stat in (("archive", archive), ("live", live)):
        statuses = ", ".join(f"{code}: {count}" for code, count in stat.get("http_statuses", {}).items()) or "无"
        lines.append(
            f"| {source} | {stat.get('snapshot_count', 0)} | {esc(statuses)} | {stat.get('error_count', 0)} | "
            f"{stat.get('records_empty_array_count', 0)} | {stat.get('records_nonempty_array_count', 0)} | "
            f"{stat.get('record_sightings_in_window', 0)} | {stat.get('pagination_token_present_count', 0)} |"
        )
    lines += ["", "去重 record 中 item/readings 的状态：", "", "| 状态 | 去重 record 数 |", "|---|---:|"]
    lines += [f"| {esc(state)} | {count} |" for state, count in data["unique_record_empty_states"].items()]
    lines += ["", "## 字段结构画像", "",
              "每个字段表的分母是对应对象数。missing 表示没有 key；null 与空字符串/数组/对象分开统计。字段分布按去重 record 计算。", ""]
    for scope, stat in data["schema_profiles"].items():
        lines += [f"### {scope}（对象数：{stat['object_count']}）", "",
                  "| 字段 | 出现 | 缺失 | null | 空值 | 值类型 | 数组元素类型 / 长度 |",
                  "|---|---:|---:|---:|---:|---|---|"]
        for field, value in stat["fields"].items():
            type_counts = ", ".join(f"{name}: {count}" for name, count in value["types"].items())
            array_types = ", ".join(f"{name}: {count}" for name, count in value["array_item_types"].items()) or "—"
            array_lengths = ", ".join(f"长度 {name}: {count}" for name, count in value["array_lengths"].items())
            array_detail = array_types + (("; " + array_lengths) if array_lengths else "")
            lines.append(
                f"| {esc(field)} | {value['present']} | {value['missing']} | {value['null']} | "
                f"{value['empty']} | {esc(type_counts)} | {esc(array_detail)} |"
            )
        lines.append("")
    lines += [
        "## 值类型、正文与占位符", "",
        f"- item.type：{json.dumps(data['unique_item_type_values'], ensure_ascii=False)}。",
        f"- item.msgType：{json.dumps(data['unique_item_msgType_values'], ensure_ascii=False)}；缺失/空值 record：{data['unique_records_missing_or_empty_msgType']}。",
        f"- 模板占位符：{json.dumps(data['placeholder_counts'], ensure_ascii=False) or '无'}。",
        "- 文本字段的频次、top 值和样本快照引用见 corpus_profile.json。只报告数据中观察到的值，不把 API 文档枚举当成真实分布。", "",
        "## 道路描述模式（规则初筛）", "",
        "规则可重叠；一个 reading 可命中多个类别。类别描述的是文字形式，不代表已经定位道路段。", "",
        "| 初筛类别 | reading 数 |", "|---|---:|",
    ]
    for category, count in data["road_description_pattern_counts"].items():
        lines.append(f"| {esc(category)} | {count} |")
    if not data["road_description_pattern_counts"]:
        lines.append("| 无真实 reading 样本 | 0 |")
    lines += ["", "### 可审查样例", ""]
    if data["road_description_examples"]:
        for sample in data["road_description_examples"]:
            cls = sample["classification"]
            ref = cls.get("reference") or {}
            lines.append(
                f"- 类别：{', '.join(cls['categories'])}；源引用：{ref.get('source')} "
                f"snapshot_id={ref.get('snapshot_id')} record_index={ref.get('record_index')}。"
            )
            for field, text in cls["text_fields"].items():
                lines.append(f"  - {field}：{json.dumps(text, ensure_ascii=False)}")
            for match in cls["matches"]:
                lines.append(f"  - 规则证据 {match['category']}：{json.dumps(match['evidence'], ensure_ascii=False)}")
    else:
        lines.append("当前窗口没有非空 reading，无法从真实文本估计道路描述类型。")
    lines += ["", "## 限制", ""]
    lines += [f"- {item}" for item in data["limitations"]]
    lines += [
        "", "HTTP 200 或非空 records 不等于有洪水警报。本地未请求日期及未完成分页也不能记作零。",
        "", "## 重跑", "",
        "    python scripts/analyze_corpus.py",
        "    python scripts/analyze_corpus.py --start-date 1996-10-08 --end-date 2026-10-07",
        "",
        "脚本通过 SQLite mode=ro 读取数据库，仅写 JSON 画像和本 Markdown；不会导出 API 请求参数或认证头。", "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help="只读 SQLite 数据库")
    parser.add_argument("--start-date", type=as_date, help="按 record.datetime 新加坡日期筛选，含起始日")
    parser.add_argument("--end-date", type=as_date, help="按 record.datetime 新加坡日期筛选，含结束日")
    parser.add_argument("--output-json", type=Path, default=JSON_DEFAULT)
    parser.add_argument("--output-report", type=Path, default=REPORT_DEFAULT)
    parser.add_argument("--top-values", type=int, default=50)
    parser.add_argument("--max-examples", type=int, default=100)
    args = parser.parse_args()
    if args.top_values < 1 or args.max_examples < 1:
        parser.error("--top-values 与 --max-examples 必须大于 0")
    data = profile(args.db, args.start_date, args.end_date, args.top_values, args.max_examples)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_report.write_text(render_report(data), encoding="utf-8")
    print(json.dumps({
        "snapshots": data["snapshot_count"],
        "record_sightings": data["record_sightings_in_window"],
        "unique_records": data["unique_records_in_window"],
        "alert_like_messages": data["unique_alert_like_messages"],
        "readings": data["unique_reading_objects"],
        "json": args.output_json.resolve().as_posix(),
        "report": args.output_report.resolve().as_posix(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
