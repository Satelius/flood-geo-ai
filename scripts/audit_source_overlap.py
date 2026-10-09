"""Read-only audit for live/archive separation and repeated PUB history pulls."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DB_DEFAULT = ROOT / "data" / "flood.sqlite3"


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def record_fingerprint(record: dict) -> str:
    stable_record = dict(record)
    stable_record.pop("updatedTimestamp", None)
    stable_record.pop("timestamp", None)
    canonical = json.dumps(stable_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_meaningful_record(record: dict) -> bool:
    item = record.get("item")
    if not isinstance(item, dict):
        return False
    readings = item.get("readings") if isinstance(item.get("readings"), list) else []
    return bool(
        item.get("identifier") or item.get("msgType") or readings
        or any(isinstance(item.get(key), str) and item[key].strip()
               for key in ("description", "headline", "instruction"))
    )


def semantic_message_fingerprint(message: dict) -> str:
    readings = []
    for reading in message.get("readings") or []:
        if isinstance(reading, dict):
            readings.append({key: value for key, value in reading.items()
                             if key not in ("reading_key", "quality_flags")})
    semantic = {
        key: message.get(key)
        for key in ("identifier", "sender", "msg_type", "sent", "expires", "references", "description")
    }
    semantic["sent"] = canonical_instant(semantic["sent"])
    semantic["expires"] = canonical_instant(semantic["expires"])
    semantic["readings"] = sorted(
        readings,
        key=lambda reading: json.dumps(reading, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    canonical = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def local_date_sgt(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(timedelta(hours=8)))
    return moment.astimezone(timezone(timedelta(hours=8))).date().isoformat()


def canonical_instant(value):
    if not isinstance(value, str) or not value:
        return value
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(timedelta(hours=8)))
    return moment.astimezone(timezone.utc).isoformat()


def count_date_overlap(left_start: str, left_end: str, right_start: str, right_end: str) -> int:
    start = max(date.fromisoformat(left_start), date.fromisoformat(right_start))
    end = min(date.fromisoformat(left_end), date.fromisoformat(right_end))
    return max(0, (end - start).days + 1)


def audit_database(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = connect_readonly(path)
    try:
        snapshots = list(connection.execute("""
            SELECT id, source, fetched_at, request_json, http_status, error, raw_text
            FROM snapshots WHERE source IN ('live', 'archive') ORDER BY id
        """))
        message_rows = list(connection.execute("""
            SELECT source, message_key, body FROM messages WHERE source IN ('live', 'archive')
        """))
        batches = list(connection.execute("""
            SELECT batch_id, source, start_date, end_date, status, budget, created_at, updated_at
            FROM history_batches WHERE source='archive' ORDER BY start_date, created_at
        """))
        coverage_rows = list(connection.execute("""
            SELECT source, date, status, batch_id, pages, record_count, snapshot_ids,
                   queried_message_count, queried_reading_count,
                   queried_explicit_alert_message_count, queried_unknown_msg_type_count,
                   queried_undated_message_count, query_metrics_version
            FROM coverage_days WHERE source IN ('live', 'archive') ORDER BY source, date
        """))
    finally:
        connection.close()

    snapshot_stats = defaultdict(lambda: {
        "snapshots": 0, "http_statuses": Counter(), "errors": 0,
        "requested_dates": Counter(), "valid_json_pages": 0,
    })
    page_fetches = Counter()
    record_occurrences = defaultdict(list)
    meaningful_record_occurrences = defaultdict(list)
    malformed_snapshots = []
    archive_undated_snapshots = []
    raw_record_sightings = Counter()
    meaningful_sightings = Counter()

    for snapshot in snapshots:
        source = snapshot["source"]
        stats = snapshot_stats[source]
        stats["snapshots"] += 1
        stats["http_statuses"][str(snapshot["http_status"])] += 1
        stats["errors"] += bool(snapshot["error"])
        try:
            request = json.loads(snapshot["request_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            request = {}
        requested_date = request.get("date") if isinstance(request, dict) else None
        if source == "archive" and not requested_date:
            archive_undated_snapshots.append(snapshot["id"])
        if requested_date:
            stats["requested_dates"][requested_date] += 1
            page_token = request.get("paginationToken") or "<first-page>"
            if source == "archive":
                page_fetches[(requested_date, str(page_token))] += 1
        try:
            payload = json.loads(snapshot["raw_text"] or "")
        except (json.JSONDecodeError, TypeError):
            if len(malformed_snapshots) < 20:
                malformed_snapshots.append(snapshot["id"])
            continue
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        records = data.get("records") if isinstance(data, dict) else None
        if (snapshot["http_status"] != 200 or snapshot["error"]
                or payload.get("code", 0) != 0
                or not isinstance(records, list)):
            continue
        stats["valid_json_pages"] += 1
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            raw_record_sightings[source] += 1
            fingerprint = record_fingerprint(record)
            reference = {
                "source": source,
                "snapshot_id": snapshot["id"],
                "fetched_at": snapshot["fetched_at"],
                "requested_date": requested_date,
                "record_datetime": record.get("datetime"),
                "identifier": (record.get("item") or {}).get("identifier")
                    if isinstance(record.get("item"), dict) else None,
                "record_index": index,
            }
            record_occurrences[fingerprint].append(reference)
            if is_meaningful_record(record):
                meaningful_sightings[source] += 1
                meaningful_record_occurrences[fingerprint].append(reference)

    def duplicate_summary(occurrences: dict[str, list[dict]]) -> dict:
        repeated_within_archive = []
        cross_source = []
        archive_extra = 0
        live_extra = 0
        cross_source_duplicate_sightings = 0
        for fingerprint, references in occurrences.items():
            sources = Counter(reference["source"] for reference in references)
            archive_count, live_count = sources["archive"], sources["live"]
            archive_extra += max(0, archive_count - 1)
            live_extra += max(0, live_count - 1)
            if archive_count > 1 and len(repeated_within_archive) < 20:
                repeated_within_archive.append({
                    "fingerprint": fingerprint,
                    "occurrences": len(references),
                    "references": references[:8],
                })
            if archive_count and live_count:
                cross_source_duplicate_sightings += min(archive_count, live_count)
                if len(cross_source) < 20:
                    cross_source.append({
                        "fingerprint": fingerprint,
                        "archive_occurrences": archive_count,
                        "live_occurrences": live_count,
                        "references": references[:8],
                    })
        return {
            "unique_records": len(occurrences),
            "archive_repeated_sightings_beyond_first": archive_extra,
            "live_repeated_sightings_beyond_first": live_extra,
            "cross_source_matching_record_count": sum(
                1 for references in occurrences.values()
                if {reference["source"] for reference in references} == {"archive", "live"}
                or (any(reference["source"] == "archive" for reference in references)
                    and any(reference["source"] == "live" for reference in references))
            ),
            "cross_source_duplicate_sightings_lower_bound": cross_source_duplicate_sightings,
            "archive_duplicate_examples": repeated_within_archive,
            "cross_source_examples": cross_source,
        }

    message_source_keys = defaultdict(list)
    message_counts = Counter()
    semantic_messages = defaultdict(list)
    local_counts_by_source_date = defaultdict(lambda: {
        "message_count": 0, "reading_count": 0,
        "explicit_alert_message_count": 0, "unknown_msg_type_count": 0,
    })
    for row in message_rows:
        message_counts[row["source"]] += 1
        message_source_keys[row["message_key"]].append(row["source"])
        try:
            message = json.loads(row["body"])
        except (json.JSONDecodeError, TypeError):
            continue
        fingerprint = semantic_message_fingerprint(message)
        semantic_messages[fingerprint].append({
            "source": row["source"], "message_key": row["message_key"],
            "sent": message.get("sent"), "msg_type": message.get("msg_type"),
            "identifier": message.get("identifier"), "sender": message.get("sender"),
            "description": message.get("description"),
            "reading_count": len(message.get("readings") or []),
            "area_descriptions": [
                reading.get("area_description") for reading in message.get("readings") or []
                if isinstance(reading, dict)
            ],
            "raw_snapshot_id": message.get("raw_snapshot_id"),
            "fetched_at": message.get("fetched_at"),
        })
        msg_type = str(message.get("msg_type") or "").lower()
        message_date = local_date_sgt(message.get("sent"))
        if message_date and msg_type != "cancel":
            bucket = local_counts_by_source_date[(row["source"], message_date)]
            bucket["message_count"] += 1
            bucket["reading_count"] += len(message.get("readings") or [])
            bucket["explicit_alert_message_count"] += msg_type == "alert"
            bucket["unknown_msg_type_count"] += msg_type not in ("alert", "cancel")
    cross_source_message_keys = [
        key for key, sources in message_source_keys.items()
        if "archive" in sources and "live" in sources
    ]

    semantic_duplicate_groups = []
    semantic_cross_source_groups = []
    for fingerprint, occurrences in semantic_messages.items():
        by_source = Counter(item["source"] for item in occurrences)
        if by_source["archive"] > 1:
            semantic_duplicate_groups.append({
                "semantic_fingerprint": fingerprint,
                "archive_occurrences": by_source["archive"],
                "examples": [item for item in occurrences if item["source"] == "archive"][:8],
            })
        if by_source["archive"] and by_source["live"]:
            semantic_cross_source_groups.append({
                "semantic_fingerprint": fingerprint,
                "archive_occurrences": by_source["archive"],
                "live_occurrences": by_source["live"],
                "examples": occurrences[:8],
            })

    coverage_count_mismatches = []
    for row in coverage_rows:
        if row["status"] not in ("complete_zero", "complete_nonzero"):
            continue
        if row["query_metrics_version"] != 1 or row["queried_message_count"] is None:
            continue
        local = local_counts_by_source_date.get((row["source"], row["date"]), {})
        undated_count = row["queried_undated_message_count"]
        comparisons = {
            "message_count": (
                local.get("message_count", 0),
                row["queried_message_count"] - undated_count if undated_count is not None else None,
            ),
        }
        not_comparable_metrics = []
        if undated_count == 0:
            comparisons.update({
                "reading_count": (local.get("reading_count", 0), row["queried_reading_count"]),
                "explicit_alert_message_count": (
                    local.get("explicit_alert_message_count", 0),
                    row["queried_explicit_alert_message_count"],
                ),
                "unknown_msg_type_count": (
                    local.get("unknown_msg_type_count", 0), row["queried_unknown_msg_type_count"],
                ),
            })
        else:
            not_comparable_metrics = [
                "reading_count", "explicit_alert_message_count", "unknown_msg_type_count"
            ]
        differing_metrics = {
            metric: {"cached_local": local_value, "query_audit": query_value,
                     "delta": local_value - query_value}
            for metric, (local_value, query_value) in comparisons.items()
            if query_value is not None and local_value != query_value
        }
        if differing_metrics:
            coverage_count_mismatches.append({
                "source": row["source"], "date": row["date"], "status": row["status"],
                "current_batch_id": row["batch_id"],
                "differing_metrics": differing_metrics,
                "not_comparable_metrics": not_comparable_metrics,
                "queried_undated_message_count": undated_count,
                "snapshot_ids": json.loads(row["snapshot_ids"] or "[]"),
            })

    archive_page_repeats = [
        {"date": requested_date, "page_token": token, "fetch_count": fetch_count}
        for (requested_date, token), fetch_count in sorted(page_fetches.items())
        if fetch_count > 1
    ]
    repeated_archive_dates = [
        {"date": requested_date, "snapshot_count": count}
        for requested_date, count in sorted(snapshot_stats["archive"]["requested_dates"].items())
        if count > 1
    ]
    archive_page_count_by_date = Counter(requested_date for requested_date, _ in page_fetches)
    archive_dates_with_multiple_pages = [
        {"date": requested_date, "distinct_page_count": count}
        for requested_date, count in sorted(archive_page_count_by_date.items())
        if count > 1
    ]

    batch_summaries = []
    for batch in batches:
        linked = [row for row in coverage_rows if row["batch_id"] == batch["batch_id"]]
        batch_summaries.append({
            "batch_id": batch["batch_id"], "status": batch["status"],
            "start_date": batch["start_date"], "end_date": batch["end_date"],
            "declared_days": (date.fromisoformat(batch["end_date"])
                              - date.fromisoformat(batch["start_date"])).days + 1,
            "currently_linked_coverage_days": len(linked),
            "linked_status_counts": dict(Counter(row["status"] for row in linked)),
        })
    batch_overlaps = []
    for index, left in enumerate(batches):
        for right in batches[index + 1:]:
            overlap_days = count_date_overlap(
                left["start_date"], left["end_date"], right["start_date"], right["end_date"])
            if overlap_days:
                batch_overlaps.append({
                    "left_batch_id": left["batch_id"], "left_range": [left["start_date"], left["end_date"]],
                    "right_batch_id": right["batch_id"], "right_range": [right["start_date"], right["end_date"]],
                    "overlap_days": overlap_days,
                })

    coverage_by_source = defaultdict(Counter)
    coverage_dates_by_source = defaultdict(list)
    for row in coverage_rows:
        coverage_by_source[row["source"]][row["status"]] += 1
        coverage_dates_by_source[row["source"]].append(row["date"])

    def export_snapshot_stats(source: str) -> dict:
        stats = snapshot_stats[source]
        dates = list(stats["requested_dates"])
        return {
            "snapshot_count": stats["snapshots"],
            "http_status_counts": dict(sorted(stats["http_statuses"].items())),
            "error_count": stats["errors"],
            "valid_json_page_count": stats["valid_json_pages"],
            "requested_date_count": len(dates),
            "requested_date_range": [min(dates), max(dates)] if dates else None,
            "requested_date_snapshot_count": sum(stats["requested_dates"].values()),
        }

    return {
        "database": str(path.resolve()),
        "read_only": True,
        "snapshot_counts_by_source": {
            source: export_snapshot_stats(source) for source in ("live", "archive")
        },
        "normalized_message_counts_by_source": {
            source: message_counts[source] for source in ("live", "archive")
        },
        "cross_source_matching_message_key_count": len(cross_source_message_keys),
        "cross_source_message_key_examples": cross_source_message_keys[:20],
        "archive_semantically_duplicate_message_groups": len(semantic_duplicate_groups),
        "archive_semantically_duplicate_message_examples": semantic_duplicate_groups,
        "cross_source_semantically_duplicate_message_groups": len(semantic_cross_source_groups),
        "cross_source_semantically_duplicate_message_examples": semantic_cross_source_groups,
        "coverage_cache_vs_query_count_mismatch_count": len(coverage_count_mismatches),
        "coverage_cache_vs_query_count_mismatch_examples": coverage_count_mismatches[:100],
        "raw_record_sightings_by_source": {
            source: raw_record_sightings[source] for source in ("live", "archive")
        },
        "meaningful_record_sightings_by_source": {
            source: meaningful_sightings[source] for source in ("live", "archive")
        },
        "all_record_duplicate_audit": duplicate_summary(record_occurrences),
        "meaningful_record_duplicate_audit": duplicate_summary(meaningful_record_occurrences),
        "repeated_archive_page_request_group_count": len(archive_page_repeats),
        "repeated_archive_page_examples": archive_page_repeats[:30],
        "archive_dates_with_multiple_pages_count": len(archive_dates_with_multiple_pages),
        "archive_dates_with_multiple_pages_examples": archive_dates_with_multiple_pages[:30],
        "archive_dates_with_multiple_snapshots_count": len(repeated_archive_dates),
        "archive_dates_with_multiple_snapshots_examples": repeated_archive_dates[:30],
        "archive_snapshots_without_requested_date_count": len(archive_undated_snapshots),
        "archive_snapshots_without_requested_date_examples": archive_undated_snapshots[:30],
        "coverage_day_counts_by_source": {
            source: dict(sorted(coverage_by_source[source].items()))
            for source in ("live", "archive")
        },
        "coverage_date_ranges_by_source": {
            source: ([min(values), max(values)] if values else None)
            for source, values in coverage_dates_by_source.items()
        },
        "coverage_days_have_source_date_primary_key": True,
        "archive_batches": batch_summaries,
        "overlapping_archive_batch_ranges": batch_overlaps,
        "malformed_snapshot_ids_sample": malformed_snapshots,
    }


def render_report(report: dict) -> str:
    lines = [
        f"数据库（只读）：{report['database']}",
        "",
        "快照与规范消息：",
    ]
    for source in ("live", "archive"):
        snapshot = report["snapshot_counts_by_source"][source]
        lines.append(
            f"- {source}: {snapshot['snapshot_count']:,} 个快照，"
            f"{report['normalized_message_counts_by_source'][source]:,} 条规范消息，"
            f"原始 records {report['raw_record_sightings_by_source'][source]:,} 条。"
        )
    lines.extend([
        f"- 两个 source 都出现的规范 message_key：{report['cross_source_matching_message_key_count']:,} 个。",
        f"- archive 内语义相同但 message_key 不同的消息组：{report['archive_semantically_duplicate_message_groups']:,} 组。",
        f"- 按 source 的本地缓存归日计数与 queried 审计指标不一致："
        f"{report['coverage_cache_vs_query_count_mismatch_count']:,} 天。",
        "",
        "重复与重叠：",
    ])
    for label, key in (("所有 records", "all_record_duplicate_audit"),
                       ("有意义的消息 records", "meaningful_record_duplicate_audit")):
        audit = report[key]
        lines.append(
            f"- {label}: archive 源内重复 sighting {audit['archive_repeated_sightings_beyond_first']:,} 次；"
            f"live/archive 完全相同的 record {audit['cross_source_matching_record_count']:,} 条。"
        )
    lines.append(
        f"- 相同 archive 日期和分页 token 出现多次请求："
        f"{report['repeated_archive_page_request_group_count']:,} 组；"
        f"有多个不同分页 token 的日期：{report['archive_dates_with_multiple_pages_count']:,} 天。"
    )
    lines.append(
        f"- archive 无 date 参数的旧快照：{report['archive_snapshots_without_requested_date_count']:,} 个；"
        "这些快照无法按请求日直接归入某次历史回填。"
    )
    lines.append(f"- archive 回填批次日期区间重叠：{len(report['overlapping_archive_batch_ranges']):,} 对。")
    for overlap in report["overlapping_archive_batch_ranges"]:
        lines.append(
            f"  - {overlap['left_range'][0]}–{overlap['left_range'][1]} 与 "
            f"{overlap['right_range'][0]}–{overlap['right_range'][1]}："
            f"重叠 {overlap['overlap_days']:,} 天。"
        )
    lines.extend([
        "",
        "逐日覆盖状态：",
        f"- live: {report['coverage_day_counts_by_source'].get('live', {})}",
        f"- archive: {report['coverage_day_counts_by_source'].get('archive', {})}",
        "- coverage_days 以 (source, date) 为主键；同一个日期只有一行，batch_id 记录当前关联的批次。",
        "- 语义重复消息和计数偏差的具体日期、message_key、snapshot_id 见 JSON 输出。",
        "",
        "重复样例见 JSON 输出；相同 raw record 的重复出现不会自动表示多次洪水事件。",
        "重复 date/page-token 只说明多次请求尝试，不代表已比较并确认响应正文相同。",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help="SQLite 数据库路径")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON，包括重复样例")
    args = parser.parse_args()
    report = audit_database(args.db)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_report(report))


if __name__ == "__main__":
    main()
