"""Prepare, execute once, or offline-seal the fixed 16-reading baseline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flood_agent.reading_baseline import (
    ALLOWED_REASONING_EFFORTS,
    DEFAULT_REASONING_EFFORT,
    MAX_INPUT_BYTES,
    MAX_OUTPUT_TOKENS,
    MAX_TARGETS,
    atomic_write_json,
    build_manifest,
    execute_run,
    seal_run,
    sha256_bytes,
    validate_manifest_shape,
    verify_manifest,
)

DEFAULT_DB = ROOT / "data" / "flood.sqlite3"
DEFAULT_PROFILE = ROOT / "experiments" / "corpus_profile.json"
DEFAULT_OUTPUT_ROOT = ROOT / "reports" / "readings-llm-baseline"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fixed-corpus, reading-level LLM → GIS baseline")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare-only", action="store_true", help="只读验证并冻结 16 条来源 manifest，不联网")
    modes.add_argument("--execute", action="store_true", help="执行一次最多 16 条的 Responses API 批次")
    modes.add_argument("--seal-only", action="store_true", help="只从既有 run artifacts 离线封账，不读凭据、不联网")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--max-requests", type=int, default=MAX_TARGETS)
    parser.add_argument("--max-output-tokens", type=int, default=MAX_OUTPUT_TOKENS)
    parser.add_argument("--max-input-bytes", type=int, default=MAX_INPUT_BYTES)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--reasoning-effort", choices=ALLOWED_REASONING_EFFORTS, default=DEFAULT_REASONING_EFFORT)
    return parser


def _load_api_key() -> str:
    """Read the execution credential only at execution time; never display or persist it."""
    for name in ("OPENAI_API_KEY", "LLM_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, raw_value = line.partition("=")
            if separator and key.strip() in ("OPENAI_API_KEY", "LLM_API_KEY"):
                parsed = raw_value.strip()
                if len(parsed) >= 2 and parsed[0] == parsed[-1] and parsed[0] in "\"'":
                    parsed = parsed[1:-1]
                if parsed:
                    return parsed
    raise ValueError("OPENAI_API_KEY or LLM_API_KEY is not configured")


def _validate_caps(args: argparse.Namespace) -> None:
    if args.max_requests != MAX_TARGETS:
        raise ValueError(f"max-requests 固定为 {MAX_TARGETS}")
    if not 1 <= args.max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError(f"max-output-tokens 必须在 1..{MAX_OUTPUT_TOKENS} 范围")
    if not 1 <= args.max_input_bytes <= MAX_INPUT_BYTES:
        raise ValueError(f"max-input-bytes 必须在 1..{MAX_INPUT_BYTES} 范围")
    if args.timeout_seconds <= 0:
        raise ValueError("timeout-seconds 必须为正数")


def _prepare(args: argparse.Namespace) -> int:
    _validate_caps(args)
    manifest = build_manifest(args.profile, args.db)
    manifest["max_output_tokens"] = args.max_output_tokens
    manifest["max_input_bytes"] = args.max_input_bytes
    manifest["manifest_sha256"] = ""
    from flood_agent.reading_baseline import manifest_sha256
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    validate_manifest_shape(manifest)
    preflight_dir = args.output_root / "preflight" / manifest["manifest_sha256"]
    preflight_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = preflight_dir / "manifest.json"
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if manifest_path.exists():
        if manifest_path.read_bytes() != encoded:
            raise ValueError(f"冻结 manifest 路径已存在且内容不同: {manifest_path}")
    else:
        atomic_write_json(manifest_path, manifest)
    reloaded, _ = verify_manifest(manifest_path, manifest["manifest_sha256"], args.profile, args.db)
    print(json.dumps({"manifest_path": str(manifest_path), "manifest_sha256": reloaded["manifest_sha256"], "target_count": len(reloaded["targets"])}, ensure_ascii=False))
    return 0


def _execute(args: argparse.Namespace) -> int:
    _validate_caps(args)
    if args.manifest is None or not args.expected_manifest_sha256:
        raise ValueError("--execute 必须同时提供 --manifest 与 --expected-manifest-sha256")
    manifest, records = verify_manifest(args.manifest, args.expected_manifest_sha256, args.profile, args.db)
    if args.max_output_tokens != manifest["max_output_tokens"] or args.max_input_bytes != manifest["max_input_bytes"]:
        raise ValueError("execute 预算必须与冻结 manifest 一致")
    api_key = _load_api_key()
    args.output_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = args.output_root / run_id
    run = execute_run(
        run_dir,
        manifest,
        records,
        api_key,
        reasoning_effort=args.reasoning_effort,
        max_output_tokens=args.max_output_tokens,
        max_input_bytes=args.max_input_bytes,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps({"run_dir": str(run_dir), "run_id": run["run_id"], "sealed": run["sealed"], "status_counts": run.get("status_counts", {})}, ensure_ascii=False))
    return 0


def _seal(args: argparse.Namespace) -> int:
    if args.run_dir is None or not args.run_dir.is_dir():
        raise ValueError("--seal-only 必须提供存在的 --run-dir")
    run = seal_run(args.run_dir)
    print(json.dumps({"run_dir": str(args.run_dir), "run_id": run["run_id"], "sealed": run["sealed"], "results_sha256": run["results_sha256"]}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.prepare_only:
            return _prepare(args)
        if args.execute:
            return _execute(args)
        return _seal(args)
    except Exception as exc:
        print(f"reading baseline blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
