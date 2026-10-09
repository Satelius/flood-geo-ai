"""Fixed-corpus, reading-level Responses API baseline with durable offline sealing."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .normalize import parse_circle

MANIFEST_SCHEMA_VERSION = "1"
RESULT_SCHEMA_VERSION = "1"
PROMPT_VERSION = "1"
SCHEMA_VERSION = "1"
EVIDENCE_VALIDATOR_VERSION = "1"
MODEL = "gpt-6-luna"
DEFAULT_REASONING_EFFORT = "medium"
ALLOWED_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh")
MAX_TARGETS = 16
MAX_OUTPUT_TOKENS = 8192
MAX_INPUT_BYTES = 32 * 1024
API_URL = "https://api.openai.com/v1/responses"

SYSTEM_PROMPT = """Extract only facts stated in the single original flood-warning reading supplied by the user. Do not use or infer from outer record metadata. Preserve road and place spelling exactly. Return nullable values when absent; use flood_state=unknown only when the reading explicitly says the state is unknown. Every non-null value must have evidence with a JSON path relative to the reading root and an exact quote copied from that path. Never infer a calendar date from context. Preserve time clues as raw text. Do not create coordinates, geometry, or road-closure claims. Return only the requested schema."""


def _string_schema() -> dict[str, Any]:
    return {"type": ["string", "null"], "minLength": 1}


def _evidence_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["path", "quote"],
            "additionalProperties": False,
        },
    }


def _atom_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"value": {"type": "string", "minLength": 1}, "evidence": _evidence_schema()},
        "required": ["value", "evidence"],
        "additionalProperties": False,
    }


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "road_relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "road_name": _string_schema(),
                    "start_road": _string_schema(),
                    "end_road": _string_schema(),
                    "near_landmark": _string_schema(),
                    "direction": _string_schema(),
                    "ramp": _string_schema(),
                    "evidence": _evidence_schema(),
                },
                "required": ["road_name", "start_road", "end_road", "near_landmark", "direction", "ramp", "evidence"],
                "additionalProperties": False,
            },
        },
        "flood_state": {
            "type": "object",
            "properties": {
                "value": {"type": ["string", "null"], "enum": ["occurred", "subsided", "unknown", None]},
                "evidence": _evidence_schema(),
            },
            "required": ["value", "evidence"],
            "additionalProperties": False,
        },
        "passability_hints": {"type": "array", "items": _atom_schema()},
        "time_cues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": ["clock_time", "duration", "other"]},
                    "evidence": _evidence_schema(),
                },
                "required": ["raw", "kind", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["road_relations", "flood_state", "passability_hints", "time_cues"],
    "additionalProperties": False,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def reading_sha256(reading: Any) -> str:
    return sha256_bytes(canonical_json_bytes(reading))


def record_sha256(record: dict[str, Any]) -> str:
    stable = dict(record)
    stable.pop("updatedTimestamp", None)
    stable.pop("timestamp", None)
    return sha256_bytes(canonical_json_bytes(stable))


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes({key: value for key, value in manifest.items() if key != "manifest_sha256"}))


def _readonly_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    uri = f"file:{urllib.parse.quote(path.as_posix(), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _read_profile(profile_path: str | Path) -> tuple[dict[str, Any], str]:
    profile_bytes = Path(profile_path).read_bytes()
    profile = json.loads(profile_bytes.decode("utf-8"))
    if not isinstance(profile, dict) or not isinstance(profile.get("window"), dict):
        raise ValueError("profile 缺少 window 对象")
    if not isinstance(profile.get("road_description_examples"), list):
        raise ValueError("profile 缺少 road_description_examples 数组")
    return profile, sha256_bytes(profile_bytes)


def _source_identity(reference: dict[str, Any]) -> tuple[Any, ...]:
    return (
        reference.get("source"), reference.get("snapshot_id"),
        reference.get("record_index"), reference.get("reading_index"),
        reference.get("record_sha256"),
    )


def _target_id(reference: dict[str, Any]) -> str:
    return ":".join(str(reference[key]) for key in ("source", "snapshot_id", "record_index", "reading_index", "record_sha256"))


def _iter_profile_references(profile: dict[str, Any]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for example_index, example in enumerate(profile["road_description_examples"]):
        if not isinstance(example, dict):
            continue
        classification = example.get("classification")
        reference = classification.get("reference") if isinstance(classification, dict) else None
        if reference is None:
            continue
        if not isinstance(reference, dict):
            raise ValueError(f"road_description_examples[{example_index}].classification.reference 必须是对象")
        required = ("source", "snapshot_id", "record_index", "reading_index", "record_datetime", "record_sha256")
        missing = [key for key in required if key not in reference]
        if missing:
            raise ValueError(f"画像来源引用字段缺失: index={example_index}, fields={missing}")
        references.append(dict(reference))
    return references


def _unique_reference_diagnostics(references: list[dict[str, Any]]) -> list[str]:
    diagnostics: list[str] = []
    source_seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    target_seen: dict[str, dict[str, Any]] = {}
    canonical_seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for reference in references:
        source_key = _source_identity(reference)
        target_id = _target_id(reference)
        canonical_key = (reference.get("record_sha256"), reference.get("reading_index"))
        for label, key, seen in (
            ("duplicate source reference", source_key, source_seen),
            ("duplicate target_id", target_id, target_seen),
            ("duplicate canonical_reading_key", canonical_key, canonical_seen),
        ):
            if key in seen:
                diagnostics.append(f"{label}: first={_target_id(seen[key])} duplicate={target_id}")
            else:
                seen[key] = reference
    return diagnostics


def _load_raw_record(connection: sqlite3.Connection, reference: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    row = connection.execute("SELECT source, raw_text FROM snapshots WHERE id=?", (reference["snapshot_id"],)).fetchone()
    if row is None:
        raise ValueError(f"snapshot 不存在: {_target_id(reference)}")
    if row["source"] != reference["source"]:
        raise ValueError(f"snapshot source mismatch: expected={reference['source']} actual={row['source']} target={_target_id(reference)}")
    if not row["raw_text"]:
        raise ValueError(f"snapshot 缺少 raw_text: {_target_id(reference)}")
    payload = json.loads(row["raw_text"])
    records = payload.get("data", {}).get("records") if isinstance(payload, dict) else None
    record_index = reference["record_index"]
    if not isinstance(records, list) or not isinstance(record_index, int) or isinstance(record_index, bool) or not 0 <= record_index < len(records):
        raise ValueError(f"record index 不存在: target={_target_id(reference)} records_type={type(records).__name__}")
    record = records[record_index]
    if not isinstance(record, dict):
        raise ValueError(f"record 不是对象: target={_target_id(reference)}")
    actual_record_sha = record_sha256(record)
    if actual_record_sha != reference["record_sha256"]:
        raise ValueError(f"record SHA mismatch: target={_target_id(reference)} expected={reference['record_sha256']} actual={actual_record_sha}")
    if record.get("datetime") != reference["record_datetime"]:
        raise ValueError(f"record datetime mismatch: target={_target_id(reference)} expected={reference['record_datetime']} actual={record.get('datetime')}")
    item = record.get("item")
    readings = item.get("readings") if isinstance(item, dict) else None
    reading_index = reference["reading_index"]
    if not isinstance(readings, list) or not isinstance(reading_index, int) or isinstance(reading_index, bool) or not 0 <= reading_index < len(readings):
        raise ValueError(f"reading index 不存在: target={_target_id(reference)}")
    reading = readings[reading_index]
    if not isinstance(reading, dict) or not reading:
        raise ValueError(f"reading 必须是非空对象: target={_target_id(reference)}")
    actual_reading_sha = reading_sha256(reading)
    if reference.get("reading_sha256") not in (None, actual_reading_sha):
        raise ValueError(f"reading SHA mismatch: target={_target_id(reference)} expected={reference['reading_sha256']} actual={actual_reading_sha}")
    return record, item, reading


def _manifest_target(reference: dict[str, Any], record: dict[str, Any], item: dict[str, Any], reading: dict[str, Any]) -> dict[str, Any]:
    target = {
        "target_id": _target_id(reference),
        "canonical_reading_key": [reference["record_sha256"], reference["reading_index"]],
        "source": {
            "source": reference["source"],
            "snapshot_id": reference["snapshot_id"],
            "record_index": reference["record_index"],
            "reading_index": reference["reading_index"],
            "record_datetime": reference["record_datetime"],
            "record_sha256": reference["record_sha256"],
            "reading_sha256": reading_sha256(reading),
            "msg_type_raw": item.get("msgType"),
        },
    }
    return target


def build_manifest(profile_path: str | Path, db_path: str | Path) -> dict[str, Any]:
    profile, profile_hash = _read_profile(profile_path)
    references = _iter_profile_references(profile)
    diagnostics = _unique_reference_diagnostics(references)
    if diagnostics:
        raise ValueError("画像引用存在重复，零请求阻塞:\n" + "\n".join(diagnostics))
    if len(references) != MAX_TARGETS:
        raise ValueError(f"画像必须恰有 {MAX_TARGETS} 个 classification.reference；实际 {len(references)}")
    targets: list[dict[str, Any]] = []
    connection = _readonly_connection(db_path)
    try:
        for reference in references:
            record, item, reading = _load_raw_record(connection, reference)
            targets.append(_manifest_target(reference, record, item, reading))
    finally:
        connection.close()
    manifest: dict[str, Any] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "profile_sha256": profile_hash,
        "profile_window": profile["window"],
        "max_requests": MAX_TARGETS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_input_bytes": MAX_INPUT_BYTES,
        "targets": targets,
    }
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    validate_manifest_shape(manifest)
    return manifest


def validate_manifest_shape(manifest: dict[str, Any]) -> None:
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"不支持的 manifest schema version: {manifest.get('manifest_schema_version')}")
    if manifest.get("manifest_sha256") != manifest_sha256(manifest):
        raise ValueError("manifest_sha256 校验失败")
    if manifest.get("max_requests") != MAX_TARGETS:
        raise ValueError(f"manifest max_requests 必须固定为 {MAX_TARGETS}")
    if not isinstance(manifest.get("max_output_tokens"), int) or not 1 <= manifest["max_output_tokens"] <= MAX_OUTPUT_TOKENS:
        raise ValueError("manifest max_output_tokens 超出允许范围")
    if not isinstance(manifest.get("max_input_bytes"), int) or not 1 <= manifest["max_input_bytes"] <= MAX_INPUT_BYTES:
        raise ValueError("manifest max_input_bytes 超出允许范围")
    if not isinstance(manifest.get("profile_sha256"), str) or len(manifest["profile_sha256"]) != 64:
        raise ValueError("manifest profile_sha256 无效")
    if not isinstance(manifest.get("profile_window"), dict):
        raise ValueError("manifest profile_window 必须是对象")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or len(targets) != MAX_TARGETS:
        raise ValueError(f"manifest 必须恰有 {MAX_TARGETS} 个 targets")
    source_refs: list[dict[str, Any]] = []
    target_ids: set[str] = set()
    canonical_keys: set[tuple[Any, ...]] = set()
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("source"), dict):
            raise ValueError("manifest target/source 结构无效")
        source = target["source"]
        needed = ("source", "snapshot_id", "record_index", "reading_index", "record_datetime", "record_sha256", "reading_sha256", "msg_type_raw")
        if any(key not in source for key in needed):
            raise ValueError(f"manifest target source 字段不完整: {target.get('target_id')}")
        ref = {key: source[key] for key in ("source", "snapshot_id", "record_index", "reading_index", "record_datetime", "record_sha256")}
        expected_id = _target_id(ref)
        if target.get("target_id") != expected_id:
            raise ValueError(f"target_id 与 source 不匹配: expected={expected_id} actual={target.get('target_id')}")
        key = target.get("canonical_reading_key")
        if key != [source["record_sha256"], source["reading_index"]]:
            raise ValueError(f"canonical_reading_key 与来源不匹配: {expected_id}")
        canonical_key = (source["record_sha256"], source["reading_index"])
        source_refs.append(ref)
        if expected_id in target_ids:
            raise ValueError(f"duplicate target_id: {expected_id}")
        if canonical_key in canonical_keys:
            raise ValueError(f"duplicate canonical_reading_key: {canonical_key}")
        target_ids.add(expected_id)
        canonical_keys.add(canonical_key)
    diagnostics = _unique_reference_diagnostics(source_refs)
    if diagnostics:
        raise ValueError("manifest 中存在重复来源:\n" + "\n".join(diagnostics))


def verify_manifest(manifest_path: str | Path, expected_sha256: str, profile_path: str | Path, db_path: str | Path) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    validate_manifest_shape(manifest)
    if manifest["manifest_sha256"] != expected_sha256:
        raise ValueError(f"expected manifest SHA mismatch: expected={expected_sha256} actual={manifest['manifest_sha256']}")
    profile, profile_hash = _read_profile(profile_path)
    if profile_hash != manifest.get("profile_sha256"):
        raise ValueError(f"profile SHA mismatch: expected={manifest.get('profile_sha256')} actual={profile_hash}")
    if profile["window"] != manifest.get("profile_window"):
        raise ValueError("profile window 与冻结 manifest 不匹配")
    references = _iter_profile_references(profile)
    diagnostics = _unique_reference_diagnostics(references)
    if diagnostics:
        raise ValueError("画像引用重复，零请求阻塞:\n" + "\n".join(diagnostics))
    expected_refs = [_source_identity(x) for x in references]
    actual_refs = [_source_identity({**target["source"], "record_datetime": target["source"]["record_datetime"]}) for target in manifest["targets"]]
    if expected_refs != actual_refs:
        raise ValueError("画像来源引用次序或 source/index/hash 与冻结 manifest 不一致")
    connection = _readonly_connection(db_path)
    recovered: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    try:
        for target in manifest["targets"]:
            source = target["source"]
            reference = {key: source[key] for key in ("source", "snapshot_id", "record_index", "reading_index", "record_datetime", "record_sha256")}
            record, item, reading = _load_raw_record(connection, reference)
            if reading_sha256(reading) != source["reading_sha256"]:
                raise ValueError(f"manifest reading SHA mismatch: target={target['target_id']}")
            if item.get("msgType") != source["msg_type_raw"]:
                raise ValueError(f"manifest msgType mismatch: target={target['target_id']}")
            recovered.append((record, item, reading))
    finally:
        connection.close()
    return manifest, recovered


def make_request_body(reading: dict[str, Any], reasoning_effort: str = DEFAULT_REASONING_EFFORT, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict[str, Any]:
    if reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise ValueError(f"reasoning effort 不在允许列表: {reasoning_effort}")
    if not isinstance(max_output_tokens, int) or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError(f"max_output_tokens 必须在 1..{MAX_OUTPUT_TOKENS} 范围")
    body: dict[str, Any] = {
        "model": MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps(reading, ensure_ascii=False, separators=(",", ":"))}]},
        ],
        "reasoning": {"effort": reasoning_effort},
        "text": {"format": {"type": "json_schema", "name": "reading_flood_extraction", "strict": True, "schema": OUTPUT_SCHEMA}},
        "max_output_tokens": max_output_tokens,
    }
    return body


def request_body_size(body: dict[str, Any]) -> int:
    return len(canonical_json_bytes(body))


def validate_output_shape(value: Any, schema: dict[str, Any] | None = None, path: str = "$") -> None:
    schema = schema or OUTPUT_SCHEMA
    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if expected_types and not any(_matches_type(value, type_name) for type_name in expected_types):
        raise ValueError(f"schema_error at {path}: type {type(value).__name__} not in {expected_types}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"schema_error at {path}: value not in enum")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise ValueError(f"schema_error at {path}: string is shorter than minLength")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        extras = [key for key in value if key not in props]
        if missing or extras:
            raise ValueError(f"schema_error at {path}: missing={missing} extra={extras}")
        for key, child in value.items():
            validate_output_shape(child, props[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_output_shape(child, schema["items"], f"{path}[{index}]")


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return False


def _reading_string_paths(reading: Any, path: str = "$") -> dict[str, str]:
    found: dict[str, str] = {}
    if isinstance(reading, dict):
        for key, value in reading.items():
            found.update(_reading_string_paths(value, f"{path}.{key}"))
    elif isinstance(reading, list):
        for index, value in enumerate(reading):
            found.update(_reading_string_paths(value, f"{path}[{index}]"))
    elif isinstance(reading, str):
        found[path] = reading
    return found


def validate_evidence(value: dict[str, Any], reading: dict[str, Any]) -> None:
    source_paths = _reading_string_paths(reading)

    def evidence_ok(evidence: Any, at: str) -> list[str]:
        if not isinstance(evidence, list):
            raise ValueError(f"evidence_error at {at}: evidence must be an array")
        quotes: list[str] = []
        for index, entry in enumerate(evidence):
            if not isinstance(entry, dict) or set(entry) != {"path", "quote"}:
                raise ValueError(f"evidence_error at {at}[{index}]: invalid evidence object")
            path, quote = entry["path"], entry["quote"]
            if not isinstance(path, str) or not isinstance(quote, str) or not quote:
                raise ValueError(f"evidence_error at {at}[{index}]: path/quote must be nonempty strings")
            original = source_paths.get(path)
            if original is None or quote not in original:
                raise ValueError(f"evidence_error at {at}[{index}]: quote does not exactly occur at {path}")
            quotes.append(quote)
        return quotes

    flood = value["flood_state"]
    flood_quotes = evidence_ok(flood["evidence"], "$.flood_state")
    if flood["value"] is not None and not flood_quotes:
        raise ValueError("evidence_error at $.flood_state: non-null state needs evidence")
    if flood["value"] == "unknown":
        explicit_unknown = (
            "unknown", "unclear", "unsure", "uncertain", "not known", "not sure",
            "unable to determine", "cannot determine", "cannot be determined",
            "cannot confirm", "cannot tell", "undetermined", "unconfirmed",
        )
        if not any(term in quote.casefold() for quote in flood_quotes for term in explicit_unknown):
            raise ValueError("evidence_error at $.flood_state: unknown requires explicit uncertainty wording")
    for relation_index, relation in enumerate(value["road_relations"]):
        quotes = evidence_ok(relation["evidence"], f"$.road_relations[{relation_index}]")
        for key in ("road_name", "start_road", "end_road", "near_landmark", "direction", "ramp"):
            atom = relation[key]
            if atom is not None and (not quotes or not any(atom in quote for quote in quotes)):
                raise ValueError(f"evidence_error at $.road_relations[{relation_index}].{key}: value missing from its quote")
    for index, hint in enumerate(value["passability_hints"]):
        quotes = evidence_ok(hint["evidence"], f"$.passability_hints[{index}]")
        if not quotes or not any(hint["value"] in quote for quote in quotes):
            raise ValueError(f"evidence_error at $.passability_hints[{index}].value: value missing from its quote")
    for index, cue in enumerate(value["time_cues"]):
        quotes = evidence_ok(cue["evidence"], f"$.time_cues[{index}]")
        if not quotes or not any(cue["raw"] in quote for quote in quotes):
            raise ValueError(f"evidence_error at $.time_cues[{index}].raw: raw clue missing from its quote")


def _get_response_text(response: Any) -> str:
    if not isinstance(response, dict):
        raise ValueError("response body is not a JSON object")
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    for output in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []) if isinstance(output.get("content"), list) else []:
            if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    raise ValueError("response has no output_text")


def _usage(response: Any) -> dict[str, int | None] | None:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "reasoning_tokens": reasoning,
    }


def classify_response(http_status: int | None, response_raw: str | bytes, reading: dict[str, Any]) -> tuple[str, dict[str, Any] | None, dict[str, Any], str | None]:
    raw_text = response_raw.decode("utf-8", errors="replace") if isinstance(response_raw, bytes) else response_raw
    try:
        response = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError):
        response = None
    response_id = response.get("id") if isinstance(response, dict) else None
    audit = {
        "http_status": http_status,
        "request_id": None,
        "response_id": response_id,
        "usage": _usage(response),
        "raw_response": response if response is not None else raw_text,
        "error": None,
    }
    if http_status is None or not 200 <= http_status < 300:
        return "http_error", None, audit, "HTTP response was not 2xx"
    if not isinstance(response, dict):
        return "response_error", None, audit, "response body is not valid JSON object"
    status = response.get("status")
    if response.get("refusal") or any(isinstance(c, dict) and c.get("type") == "refusal" for out in response.get("output", []) if isinstance(out, dict) for c in out.get("content", []) if isinstance(out.get("content"), list)):
        return "refusal", None, audit, "model refused the request"
    if isinstance(response.get("error"), dict) or response.get("error"):
        return "response_error", None, audit, "Responses API returned an error"
    if status == "incomplete":
        return "response_incomplete", None, audit, "response status=incomplete"
    if status == "cancelled":
        return "response_cancelled", None, audit, "response status=cancelled"
    if status == "failed":
        return "response_failed", None, audit, "response status=failed"
    if status != "completed":
        return "response_error", None, audit, f"unexpected response status={status!r}"
    try:
        semantic = json.loads(_get_response_text(response))
    except (ValueError, json.JSONDecodeError) as exc:
        return "schema_error", None, audit, f"response output is not valid JSON: {exc}"
    try:
        validate_output_shape(semantic)
    except ValueError as exc:
        return "schema_error", None, audit, str(exc)
    try:
        validate_evidence(semantic, reading)
    except ValueError as exc:
        return "evidence_error", None, audit, str(exc)
    return "success", semantic, audit, None


def make_official_result(target: dict[str, Any], record: dict[str, Any], item: dict[str, Any], reading: dict[str, Any]) -> dict[str, Any]:
    area = reading.get("area") if isinstance(reading.get("area"), dict) else {}
    point, radius, flags = parse_circle(area.get("circle"))
    geometry = {"type": "Point", "coordinates": point} if point is not None else None
    audit = {
        "status": "not_attempted", "attempts": 0, "http_status": None,
        "request_id": None, "response_id": None, "usage": None,
        "latency_ms": None, "raw_response": None, "error": None,
    }
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "target_id": target["target_id"],
        "canonical_reading_key": target["canonical_reading_key"],
        "source": target["source"],
        "official": {"item_raw": item, "reading_raw": reading, "broadcast_radius_km": radius},
        "semantic": {"road_relations": [], "flood_state": {"value": None, "evidence": []}, "passability_hints": [], "time_cues": []},
        "geometry": geometry,
        "quality_flags": flags,
        "audit": audit,
    }


def apply_response(result: dict[str, Any], response_raw: str | bytes, http_status: int | None, request_id: str | None, latency_ms: int | None) -> dict[str, Any]:
    status, semantic, audit, error = classify_response(http_status, response_raw, result["official"]["reading_raw"])
    audit.update({
        "status": status,
        "attempts": 1,
        "request_id": request_id,
        "latency_ms": latency_ms,
        "error": error,
    })
    if semantic is not None:
        result["semantic"] = semantic
    result["audit"] = audit
    return result


def append_jsonl(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(value) + b"\n"
    with path.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def read_jsonl(path: str | Path, *, ignore_incomplete_tail: bool = False) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    data = target.read_bytes()
    if ignore_incomplete_tail and data and not data.endswith(b"\n"):
        last_newline = data.rfind(b"\n")
        data = data[: last_newline + 1] if last_newline >= 0 else b""
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{target} contains invalid UTF-8") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(decoded.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{target} line {line_number} has invalid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{target} line {line_number} must be a JSON object")
        rows.append(value)
    return rows


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: str | Path, value: Any) -> bytes:
    data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(path, data)
    return data


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "Redirects are disabled for credentialed requests", headers, fp)


def open_without_redirects(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(RejectRedirectHandler())
    return opener.open(request, timeout=timeout)


def request_once(body: dict[str, Any], api_key: str, timeout_seconds: int = 60, opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Make exactly one POST attempt. The injectable opener exists for offline tests."""
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for --execute")
    request = urllib.request.Request(
        API_URL,
        data=canonical_json_bytes(body),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    opener = opener or open_without_redirects
    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw = response.read()
            status = response.status
            headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        headers = dict(exc.headers.items()) if exc.headers else {}
    return {
        "http_status": status,
        "headers": headers,
        "raw_body": raw.decode("utf-8", errors="replace"),
        "raw_body_base64": base64.b64encode(raw).decode("ascii"),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "request_id": headers.get("x-request-id") or headers.get("X-Request-Id"),
    }


def make_geojson(results: list[dict[str, Any]]) -> dict[str, Any]:
    features = []
    for result in results:
        features.append({
            "type": "Feature",
            "geometry": result.get("geometry"),
            "properties": {
                "target_id": result["target_id"],
                "source": result["source"],
                "msgType": result["source"].get("msg_type_raw"),
                "description": result["official"]["reading_raw"].get("description"),
                "areaDesc": (result["official"]["reading_raw"].get("area") or {}).get("areaDesc") if isinstance(result["official"]["reading_raw"].get("area"), dict) else None,
                "broadcast_radius_km": result["official"].get("broadcast_radius_km"),
                "semantic": result["semantic"],
                "quality_flags": result["quality_flags"],
                "audit_status": result["audit"]["status"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


def make_summary(run: dict[str, Any], manifest: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    usage_keys = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens")
    usage_values: dict[str, list[int | None]] = {key: [] for key in usage_keys}
    attempted_results: list[dict[str, Any]] = []
    for result in results:
        audit = result["audit"]
        status = audit.get("status", "not_attempted")
        statuses[status] = statuses.get(status, 0) + 1
        if audit.get("attempts", 0):
            attempted_results.append(result)
            usage = audit.get("usage")
            for key in usage_keys:
                value = usage.get(key) if isinstance(usage, dict) else None
                usage_values[key].append(value if isinstance(value, int) and not isinstance(value, bool) else None)
    attempts = sum(1 for result in results if result["audit"].get("attempts", 0))
    errors = {key: value for key, value in statuses.items() if key not in ("success", "not_attempted")}
    return {
        "run_id": run["run_id"],
        "run_status": "sealed",
        "manifest_sha256": manifest["manifest_sha256"],
        "profile_sha256": manifest["profile_sha256"],
        "profile_window": manifest["profile_window"],
        "model": run["model"],
        "reasoning_effort": run["reasoning_effort"],
        "prompt_version": run["prompt_version"],
        "schema_version": run["schema_version"],
        "evidence_validator_version": run["evidence_validator_version"],
        "target_count": len(results),
        "max_requests": manifest["max_requests"],
        "attempts": attempts,
        "status_counts": statuses,
        "success_count": statuses.get("success", 0),
        "failed_count": sum(errors.values()),
        "not_attempted_count": statuses.get("not_attempted", 0),
        "outcome_unknown_count": statuses.get("outcome_unknown", 0),
        "usage": {
            key: sum(values) if attempted_results and all(value is not None for value in values) else None
            for key, values in usage_values.items()
        },
        "started_at": run.get("started_at"),
        "ended_at": utc_now(),
        "label_scope": "record-level manual review; no field-level gold or precision/recall/F1",
    }


def _replay_raw_response(result: dict[str, Any], raw_entry: dict[str, Any]) -> dict[str, Any]:
    return apply_response(
        result,
        raw_entry.get("raw_body", ""),
        raw_entry.get("http_status"),
        raw_entry.get("request_id"),
        raw_entry.get("latency_ms"),
    )


def seal_run(run_dir: str | Path, renderer: Callable[[Path], Any] | None = None) -> dict[str, Any]:
    """Build final artifacts from local ledgers only; this function has no credentials or network path."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    validate_manifest_shape(manifest)
    if run.get("manifest_sha256") != manifest["manifest_sha256"]:
        raise ValueError("run.json manifest_sha256 does not match frozen manifest")
    if run.get("sealed"):
        checks = {
            "manifest_file_sha256": sha256_bytes((run_dir / "manifest.json").read_bytes()),
            "results_sha256": sha256_bytes((run_dir / "results.jsonl").read_bytes()),
            "geojson_sha256": sha256_bytes((run_dir / "events.geojson").read_bytes()),
            "summary_sha256": sha256_bytes((run_dir / "summary.json").read_bytes()),
            "review_sha256": sha256_bytes((run_dir / "review.html").read_bytes()),
        }
        expected = {key: run.get(key) for key in checks}
        if checks != expected:
            raise ValueError(f"sealed artifacts hash mismatch: expected={expected} actual={checks}")
        return run

    official_rows = read_jsonl(run_dir / "official_records.jsonl")
    official_by_target = {row["target_id"]: row for row in official_rows}
    if set(official_by_target) != {target["target_id"] for target in manifest["targets"]} or len(official_rows) != MAX_TARGETS:
        raise ValueError("official_records.jsonl 必须恰有 manifest 中 16 个唯一 target")
    if run.get("model") != MODEL or run.get("max_requests") != MAX_TARGETS:
        raise ValueError("run.json model/max_requests 与固定基线不匹配")
    if run.get("reasoning_effort") not in ALLOWED_REASONING_EFFORTS:
        raise ValueError("run.json reasoning_effort 不在允许范围")
    if not isinstance(run.get("max_output_tokens"), int) or not 1 <= run["max_output_tokens"] <= MAX_OUTPUT_TOKENS:
        raise ValueError("run.json max_output_tokens 超出允许范围")
    if not isinstance(run.get("max_input_bytes"), int) or not 1 <= run["max_input_bytes"] <= MAX_INPUT_BYTES:
        raise ValueError("run.json max_input_bytes 超出允许范围")
    for target in manifest["targets"]:
        target_id = target["target_id"]
        official = official_by_target[target_id]
        source = target["source"]
        official_data = official.get("official")
        if not isinstance(official_data, dict):
            raise ValueError(f"official result missing official object: {target_id}")
        item = official_data.get("item_raw")
        reading = official_data.get("reading_raw")
        if not isinstance(item, dict) or not isinstance(reading, dict):
            raise ValueError(f"official result missing raw item/reading: {target_id}")
        readings = item.get("readings")
        reading_index = source["reading_index"]
        if (not isinstance(readings, list) or not isinstance(reading_index, int)
                or isinstance(reading_index, bool) or not 0 <= reading_index < len(readings)
                or readings[reading_index] != reading):
            raise ValueError(f"official reading does not match its source index: {target_id}")
        if reading_sha256(reading) != source["reading_sha256"]:
            raise ValueError(f"official reading SHA does not match manifest: {target_id}")
        if item.get("msgType") != source["msg_type_raw"]:
            raise ValueError(f"official msgType does not match manifest: {target_id}")
        expected_official = make_official_result(target, {}, item, reading)
        if canonical_json_bytes(official) != canonical_json_bytes(expected_official):
            raise ValueError(f"official result differs from frozen source/geometry contract: {target_id}")
    partial_rows = read_jsonl(run_dir / "results.partial.jsonl", ignore_incomplete_tail=True)
    partial_by_target: dict[str, dict[str, Any]] = {}
    for row in partial_rows:
        partial_by_target[row["target_id"]] = row
    attempts = read_jsonl(run_dir / "attempts.jsonl", ignore_incomplete_tail=True)
    raw_rows = read_jsonl(run_dir / "raw_responses.jsonl", ignore_incomplete_tail=True)
    attempt_rows = [row for row in attempts if row.get("event") == "attempt_started"]
    attempts_by_target = {row["target_id"]: row for row in attempt_rows}
    raw_by_target = {row["target_id"]: row for row in raw_rows}
    final_results: list[dict[str, Any]] = []
    known_ids = {target["target_id"] for target in manifest["targets"]}
    if any(target_id not in known_ids for target_id in (*partial_by_target, *attempts_by_target, *raw_by_target)):
        raise ValueError("ledger 中出现不属于冻结 manifest 的 target_id")
    if len(partial_by_target) != len(partial_rows) or len(attempts_by_target) != len(attempt_rows) or len(raw_by_target) != len(raw_rows):
        raise ValueError("append-only ledger 中同一 target 出现重复 partial/attempt/raw response")
    orphan_results = (set(partial_by_target) | set(raw_by_target)) - set(attempts_by_target)
    if orphan_results:
        raise ValueError(f"partial/raw response 缺少唯一 attempt marker: {sorted(orphan_results)}")
    for target in manifest["targets"]:
        target_id = target["target_id"]
        official = official_by_target[target_id]
        partial = partial_by_target.get(target_id)
        raw = raw_by_target.get(target_id)
        attempt = attempts_by_target.get(target_id)
        if attempt is not None:
            expected_body = make_request_body(
                official["official"]["reading_raw"],
                run["reasoning_effort"],
                run["max_output_tokens"],
            )
            actual_body = attempt.get("request_body")
            try:
                user_text = actual_body["input"][1]["content"][0]["text"]
                submitted_reading = json.loads(user_text)
            except (TypeError, KeyError, IndexError, json.JSONDecodeError):
                raise ValueError(f"attempt request body is malformed: {target_id}") from None
            if submitted_reading != official["official"]["reading_raw"]:
                raise ValueError(f"attempt request body differs from exact original reading: {target_id}")
            expected_body["input"][1]["content"][0]["text"] = user_text
            expected_request_bytes = canonical_json_bytes(expected_body)
            if request_body_size(expected_body) > run["max_input_bytes"]:
                raise ValueError(f"stored request exceeds frozen input budget: {target_id}")
            if actual_body != expected_body:
                raise ValueError(f"attempt request parameters differ from frozen baseline: {target_id}")
            if attempt.get("request_sha256") != sha256_bytes(expected_request_bytes):
                raise ValueError(f"attempt request_sha256 mismatch: {target_id}")
        if raw is not None:
            if attempt.get("attempt") != 1 or raw.get("attempt") != 1:
                raise ValueError(f"attempt/raw response number mismatch: {target_id}")
            replayed = _replay_raw_response(official, raw)
            if partial is not None and canonical_json_bytes(partial) != canonical_json_bytes(replayed):
                raise ValueError(f"partial result does not match its durable raw response: {target_id}")
            result = replayed
        elif attempt is not None:
            if attempt.get("attempt") != 1:
                raise ValueError(f"attempt marker number mismatch: {target_id}")
            if partial is not None:
                if partial.get("audit", {}).get("status") != "outcome_unknown" or partial.get("audit", {}).get("attempts") != 1:
                    raise ValueError(f"partial result without raw response must be outcome_unknown: {target_id}")
                immutable_fields = ("schema_version", "target_id", "canonical_reading_key", "source", "official", "semantic", "geometry", "quality_flags")
                if any(canonical_json_bytes(partial.get(key)) != canonical_json_bytes(official.get(key)) for key in immutable_fields):
                    raise ValueError(f"outcome_unknown partial changed immutable source/GIS/semantic fields: {target_id}")
                result = json.loads(json.dumps(official))
                error = partial["audit"].get("error")
                result["audit"].update({
                    "status": "outcome_unknown", "attempts": 1,
                    "error": error if isinstance(error, str) else "attempt marker exists but no durable raw response",
                })
            else:
                result = json.loads(json.dumps(official))
                result["audit"].update({"status": "outcome_unknown", "attempts": 1, "error": "attempt marker exists but no durable raw response"})
        elif partial is not None:
            raise ValueError(f"partial result has no attempt marker: {target_id}")
        else:
            result = json.loads(json.dumps(official))
        final_results.append(result)

    result_data = b"".join(canonical_json_bytes(result) + b"\n" for result in final_results)
    atomic_write_bytes(run_dir / "results.jsonl", result_data)
    geojson_data = json.dumps(make_geojson(final_results), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(run_dir / "events.geojson", geojson_data)
    summary = make_summary(run, manifest, final_results)
    summary_data = json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(run_dir / "summary.json", summary_data)
    if renderer is None:
        try:
            from scripts.render_reading_baseline_review import render_run_review
        except ImportError as exc:
            raise RuntimeError("review renderer unavailable; final artifacts were written, rerun --seal-only after renderer is installed") from exc
        renderer = lambda path: render_run_review(path, allow_unsealed=True)
    rendered = renderer(run_dir)
    review_path = rendered[0] if isinstance(rendered, tuple) else rendered
    rendered_results_sha = rendered[1] if isinstance(rendered, tuple) and len(rendered) > 1 else None
    review_path = Path(review_path)
    if not review_path.is_absolute():
        review_path = run_dir / review_path
    if not review_path.exists():
        raise RuntimeError(f"review renderer did not create {review_path}")
    if rendered_results_sha is not None and rendered_results_sha != sha256_bytes(result_data):
        raise RuntimeError("review renderer results SHA does not match finalized results bytes")
    atomic_write_bytes(review_path, review_path.read_bytes())
    final = {
        **run,
        "sealed": True,
        "run_status": "sealed",
        "ended_at": summary["ended_at"],
        "result_count": len(final_results),
        "status_counts": summary["status_counts"],
        "manifest_file_sha256": sha256_bytes((run_dir / "manifest.json").read_bytes()),
        "results_sha256": sha256_bytes(result_data),
        "geojson_sha256": sha256_bytes(geojson_data),
        "summary_sha256": sha256_bytes(summary_data),
        "review_sha256": sha256_bytes(review_path.read_bytes()),
    }
    atomic_write_json(run_dir / "run.json", final)
    return final


def create_run(run_dir: str | Path, manifest: dict[str, Any], records: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]], reasoning_effort: str = DEFAULT_REASONING_EFFORT, max_output_tokens: int = MAX_OUTPUT_TOKENS, max_input_bytes: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    run_dir = Path(run_dir)
    validate_manifest_shape(manifest)
    if len(records) != MAX_TARGETS:
        raise ValueError(f"run initialization requires {MAX_TARGETS} verified records")
    run_dir.mkdir(parents=True, exist_ok=False)
    run_id = run_dir.name
    run = {
        "run_id": run_id,
        "manifest_sha256": manifest["manifest_sha256"],
        "sealed": False,
        "run_status": "running",
        "model": MODEL,
        "reasoning_effort": reasoning_effort,
        "max_requests": MAX_TARGETS,
        "max_output_tokens": max_output_tokens,
        "max_input_bytes": max_input_bytes,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "evidence_validator_version": EVIDENCE_VALIDATOR_VERSION,
        "started_at": utc_now(),
    }
    atomic_write_json(run_dir / "manifest.json", manifest)
    official = [make_official_result(target, *records[index]) for index, target in enumerate(manifest["targets"])]
    atomic_write_bytes(run_dir / "official_records.jsonl", b"".join(canonical_json_bytes(row) + b"\n" for row in official))
    for name in ("attempts.jsonl", "raw_responses.jsonl", "results.partial.jsonl"):
        (run_dir / name).touch(exist_ok=False)
        with (run_dir / name).open("ab") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    atomic_write_json(run_dir / "run.json", run)
    return run


def execute_run(run_dir: str | Path, manifest: dict[str, Any], records: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]], api_key: str, reasoning_effort: str = DEFAULT_REASONING_EFFORT, max_output_tokens: int = MAX_OUTPUT_TOKENS, max_input_bytes: int = MAX_INPUT_BYTES, timeout_seconds: int = 60, requester: Callable[..., dict[str, Any]] = request_once, renderer: Callable[[Path], Any] | None = None) -> dict[str, Any]:
    request_bodies = []
    for _, _, reading in records:
        body = make_request_body(reading, reasoning_effort, max_output_tokens)
        size = request_body_size(body)
        if size > max_input_bytes:
            raise ValueError(f"request exceeds max_input_bytes before any send: size={size} limit={max_input_bytes}")
        request_bodies.append(body)
    if len(request_bodies) != MAX_TARGETS:
        raise ValueError(f"request target count must be {MAX_TARGETS}")
    run_dir = Path(run_dir)
    run = create_run(run_dir, manifest, records, reasoning_effort, max_output_tokens, max_input_bytes)
    official_by_target = {row["target_id"]: row for row in read_jsonl(run_dir / "official_records.jsonl")}
    for index, target in enumerate(manifest["targets"]):
        body = request_bodies[index]
        request_json = canonical_json_bytes(body).decode("utf-8")
        attempt_entry = {
            "event": "attempt_started",
            "target_id": target["target_id"],
            "attempt": 1,
            "request_sha256": sha256_bytes(request_json.encode("utf-8")),
            "request_body": body,
            "started_at": utc_now(),
        }
        append_jsonl(run_dir / "attempts.jsonl", attempt_entry)
        try:
            response = requester(body, api_key=api_key, timeout_seconds=timeout_seconds)
        except Exception as exc:
            result = json.loads(json.dumps(official_by_target[target["target_id"]]))
            result["audit"].update({"status": "outcome_unknown", "attempts": 1, "error": f"{type(exc).__name__}: {exc}"})
            append_jsonl(run_dir / "results.partial.jsonl", result)
            break
        raw_entry = {
            "target_id": target["target_id"],
            "attempt": 1,
            "http_status": response.get("http_status"),
            "headers": {key: value for key, value in response.get("headers", {}).items() if key.lower() not in ("authorization", "set-cookie")},
            "request_id": response.get("request_id"),
            "response_id": None,
            "latency_ms": response.get("latency_ms"),
            "raw_body": response.get("raw_body", ""),
            "raw_body_base64": response.get("raw_body_base64") or base64.b64encode(response.get("raw_body", "").encode("utf-8")).decode("ascii"),
            "received_at": utc_now(),
        }
        append_jsonl(run_dir / "raw_responses.jsonl", raw_entry)
        result = _replay_raw_response(json.loads(json.dumps(official_by_target[target["target_id"]])), raw_entry)
        append_jsonl(run_dir / "results.partial.jsonl", result)
        http_status = response.get("http_status")
        if not isinstance(http_status, int) or not 200 <= http_status < 300:
            break
        try:
            raw_json = json.loads(raw_entry["raw_body"])
        except json.JSONDecodeError:
            raw_json = {}
        if isinstance(raw_json, dict) and isinstance(raw_json.get("error"), dict) and raw_json["error"].get("type") in ("invalid_request_error", "authentication_error", "permission_error", "rate_limit_error"):
            break
    return seal_run(run_dir, renderer=renderer)
