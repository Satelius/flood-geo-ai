from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts.render_reading_baseline_review import (
    EXPECTED_RECORD_COUNT,
    ReviewArtifactError,
    parse_final_results,
    render_run_review,
    validate_review_import,
)


RUN_ID = "20261009T010203Z-a1b2c3"
MANIFEST_DATA = {"manifest_schema_version": "1", "targets": []}
MANIFEST_SHA = hashlib.sha256(
    json.dumps(MANIFEST_DATA, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def make_record(index: int, *, malicious: bool = False, failed: bool = False) -> dict[str, object]:
    description = "Water across Parbury Avenue"
    if malicious and index == 0:
        description = 'Flood near </script><img src=x onerror="alert(1)"> & stay safe'
    target_id = f"archive:{index}:0:0:{index:064x}"
    quote = description
    semantic = {
        "road_relations": [
            {
                "road_name": {
                    "value": "Parbury Avenue" if not malicious else "Flood",
                    "evidence": [{"path": "$.description", "quote": quote}],
                }
            }
        ],
        "flood_state": {"value": "occurred", "evidence": [{"path": "$.description", "quote": quote}]},
        "passability_hints": [],
        "time_cues": [],
    }
    audit = {"status": "success", "attempts": 1, "error": None}
    if failed and index == 0:
        semantic = {"road_relations": [], "flood_state": {"value": None, "evidence": []}, "passability_hints": [], "time_cues": []}
        audit = {
            "status": "evidence_error",
            "attempts": 1,
            "error": "evidence_error at $.flood_state[0]: quote does not exactly occur at /description",
            "raw_response": {
                "output": [{"content": [{"type": "output_text", "text": '{"path":"/description","quote":"Flash flood"}'}]}]
            },
        }
    return {
        "schema_version": "1",
        "target_id": target_id,
        "source": {
            "source": "archive",
            "snapshot_id": index + 1,
            "record_index": 0,
            "reading_index": 0,
            "record_datetime": "2026-10-07T04:00:00Z",
            "record_sha256": f"{index:064x}",
            "reading_sha256": f"{index:064x}",
            "msg_type_raw": "Alert",
        },
        "official": {
            "item_raw": {"headline": "outer metadata"},
            "reading_raw": {"description": description},
        },
        "semantic": semantic,
        "geometry": {"type": "Point", "coordinates": [151.2 + index / 100, -33.8]},
        "quality_flags": [],
        "audit": audit,
    }


def write_run(run_dir: Path, *, malicious: bool = False, failed: bool = False) -> tuple[bytes, list[dict[str, object]]]:
    run_dir.mkdir(parents=True)
    records = [make_record(index, malicious=malicious, failed=failed) for index in range(EXPECTED_RECORD_COUNT)]
    results_bytes = b"\n".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for record in records
    ) + b"\n"
    (run_dir / "results.jsonl").write_bytes(results_bytes)
    summary_bytes = json.dumps({
        "run_id": RUN_ID,
        "model": "gpt-6-luna",
        "reasoning_effort": "medium",
        "attempts": EXPECTED_RECORD_COUNT,
        "success_count": EXPECTED_RECORD_COUNT,
        "failed_count": 0,
        "not_attempted_count": 0,
        "outcome_unknown_count": 0,
        "status_counts": {"success": EXPECTED_RECORD_COUNT},
        "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None, "reasoning_tokens": None},
    }, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    (run_dir / "summary.json").write_bytes(summary_bytes)
    manifest = {**MANIFEST_DATA, "manifest_sha256": MANIFEST_SHA}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({
            "run_id": RUN_ID,
            "manifest_sha256": MANIFEST_SHA,
            "sealed": True,
            "results_sha256": hashlib.sha256(results_bytes).hexdigest(),
            "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        }, indent=2),
        encoding="utf-8",
    )
    return results_bytes, records


class ReadingReviewTests(unittest.TestCase):
    def test_renders_bound_offline_page_and_escapes_script_closer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            results_bytes, _ = write_run(run_dir, malicious=True)
            output_path, actual_sha = render_run_review(run_dir)
            html = output_path.read_text(encoding="utf-8")

            expected_sha = hashlib.sha256(results_bytes).hexdigest()
            self.assertEqual(actual_sha, expected_sha)
            self.assertIn(expected_sha, html)
            self.assertIn(MANIFEST_SHA, html)
            self.assertIn(RUN_ID, html)
            self.assertNotIn('</script><img src=x', html)
            self.assertIn(r"\u003c/script>", html)
            self.assertIn("模型提取与证据", html)
            self.assertIn("官方坐标", html)
            self.assertIn("来源身份", html)
            self.assertIn('["正确标签 / 已标注", correctShare]', html)
            self.assertIn("不计算 precision", html)
            self.assertIn("https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png", html)
            self.assertIn("官方广播中心点地图", html)

            data_match = re.search(
                r'<script id="review-data" type="application/json">(.*?)</script>',
                html,
                re.DOTALL,
            )
            self.assertIsNotNone(data_match)
            embedded = json.loads(data_match.group(1))
            self.assertEqual(embedded["results_sha256"], expected_sha)
            self.assertEqual(len(embedded["records"]), EXPECTED_RECORD_COUNT)
            self.assertEqual(
                embedded["records"][0]["official"]["reading_raw"]["description"],
                'Flood near </script><img src=x onerror="alert(1)"> & stay safe',
            )

    def test_review_import_validation_accepts_matching_payload(self) -> None:
        target_ids = {make_record(index)["target_id"] for index in range(EXPECTED_RECORD_COUNT)}
        payload = {
            "run_id": RUN_ID,
            "manifest_sha256": MANIFEST_SHA,
            "results_sha256": "b" * 64,
            "labels": [
                {"target_id": next(iter(target_ids)), "label": "correct", "remarks": "Looks right."}
            ],
        }
        validated = validate_review_import(
            payload,
            run_id=RUN_ID,
            manifest_sha256=MANIFEST_SHA,
            results_sha256="b" * 64,
            target_ids=target_ids,
        )
        self.assertEqual(validated, payload["labels"])

    def test_review_import_rejects_mismatch_unknown_duplicate_bad_label_and_long_note(self) -> None:
        target_ids = {make_record(index)["target_id"] for index in range(EXPECTED_RECORD_COUNT)}
        known_target = next(iter(target_ids))
        base = {
            "run_id": RUN_ID,
            "manifest_sha256": MANIFEST_SHA,
            "results_sha256": "b" * 64,
            "labels": [{"target_id": known_target, "label": "correct", "remarks": "ok"}],
        }
        cases = [
            ({**base, "run_id": "another-run"}, "run_id"),
            ({**base, "manifest_sha256": "c" * 64}, "manifest_sha256"),
            ({**base, "results_sha256": "d" * 64}, "results_sha256"),
            ({**base, "labels": [{**base["labels"][0], "target_id": "not-in-run"}]}, "unknown target_id"),
            ({**base, "labels": [{**base["labels"][0], "label": "maybe"}]}, "invalid label"),
            (
                {**base, "labels": [base["labels"][0], dict(base["labels"][0])]},
                "duplicate target_id",
            ),
            (
                {**base, "labels": [{**base["labels"][0], "remarks": "x" * 2001}]},
                "exceed 2000",
            ),
        ]
        for payload, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                with self.assertRaisesRegex(ReviewArtifactError, expected_message):
                    validate_review_import(
                        payload,
                        run_id=RUN_ID,
                        manifest_sha256=MANIFEST_SHA,
                        results_sha256="b" * 64,
                        target_ids=target_ids,
                    )

    def test_result_contract_rejects_wrong_count_and_duplicate_targets(self) -> None:
        one_record = json.dumps(make_record(0)).encode("utf-8") + b"\n"
        with self.assertRaisesRegex(ReviewArtifactError, "expected 16 finalized records"):
            parse_final_results(one_record)

        duplicate_records = [make_record(index) for index in range(EXPECTED_RECORD_COUNT)]
        duplicate_records[1]["target_id"] = duplicate_records[0]["target_id"]
        duplicate_bytes = b"\n".join(
            json.dumps(record).encode("utf-8") for record in duplicate_records
        ) + b"\n"
        with self.assertRaisesRegex(ReviewArtifactError, "duplicate target_id"):
            parse_final_results(duplicate_bytes)

    def test_run_renderer_requires_sealed_results_and_matching_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            unsealed_dir = Path(temp_dir) / "unsealed"
            write_run(unsealed_dir)
            run_path = unsealed_dir / "run.json"
            metadata = json.loads(run_path.read_text(encoding="utf-8"))
            metadata["sealed"] = False
            run_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(ReviewArtifactError, "not sealed"):
                render_run_review(unsealed_dir)
            self.assertFalse((unsealed_dir / "review.html").exists())
            with self.assertRaisesRegex(ReviewArtifactError, "unsealed artifacts"):
                render_run_review(unsealed_dir, allow_unsealed=True, output_name="implementation-report.html")
            self.assertFalse((unsealed_dir / "implementation-report.html").exists())

            mismatched_dir = Path(temp_dir) / "mismatched"
            results_bytes, _ = write_run(mismatched_dir)
            run_path = mismatched_dir / "run.json"
            metadata = json.loads(run_path.read_text(encoding="utf-8"))
            metadata["results_sha256"] = "0" * 64
            run_path.write_text(json.dumps(metadata), encoding="utf-8")
            self.assertNotEqual(hashlib.sha256(results_bytes).hexdigest(), metadata["results_sha256"])
            with self.assertRaisesRegex(ReviewArtifactError, "does not match finalized results"):
                render_run_review(mismatched_dir)
            self.assertFalse((mismatched_dir / "review.html").exists())

            manifest_mismatch_dir = Path(temp_dir) / "manifest-mismatch"
            write_run(manifest_mismatch_dir)
            (manifest_mismatch_dir / "manifest.json").write_text(
                json.dumps({"manifest_schema_version": "1", "targets": ["tampered"], "manifest_sha256": MANIFEST_SHA}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReviewArtifactError, "manifest.json SHA"):
                render_run_review(manifest_mismatch_dir)
            self.assertFalse((manifest_mismatch_dir / "review.html").exists())

    def test_page_uses_text_nodes_and_survives_local_storage_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            write_run(run_dir)
            output_path, _ = render_run_review(run_dir)
            html = output_path.read_text(encoding="utf-8")

            self.assertNotIn("innerHTML", html)
            self.assertIn("element.textContent = value", html)
            self.assertIn("localStorageHandle = window.localStorage", html)
            self.assertIn("catch (error)", html)
            self.assertIn("标签仍保留在当前页面内存中", html)
            self.assertIn("关闭页面前导出审阅 JSON", html)
            persist_path = re.search(r"function persistLabels\(\) \{(.*?)\n    \}", html, re.DOTALL)
            self.assertIsNotNone(persist_path)
            self.assertIn("storageAvailable = false", persist_path.group(1))
            self.assertIn("setStorageWarning", persist_path.group(1))
            self.assertNotIn("labelsByTarget.clear()", persist_path.group(1))
            export_path = re.search(r"function exportLabels\(\) \{(.*?)\n    \}", html, re.DOTALL)
            self.assertIsNotNone(export_path)
            self.assertIn("makeExportPayload()", export_path.group(1))
            self.assertNotIn("storageAvailable", export_path.group(1))
            self.assertIn("connect-src 'none'", html)
            self.assertIn("img-src 'self' data: https://www.onemap.gov.sg", html)
            self.assertIn("/vendor/leaflet.js", html)
            self.assertNotRegex(html, r"<script\s+src=\"https?://|\bfetch\s*\(|XMLHttpRequest|sendBeacon")

    def test_implementation_report_includes_unvalidated_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            write_run(run_dir, failed=True)
            output_path, _ = render_run_review(run_dir, output_name="implementation-report.html")
            html = output_path.read_text(encoding="utf-8")

            self.assertEqual(output_path.name, "implementation-report.html")
            self.assertIn("逐条 LLM→GIS 基线实现报告", html)
            self.assertIn("未通过自动校验的原始模型输出", html)
            self.assertIn("/description", html)
            self.assertIn("JSON Pointer", html)
            self.assertIn("refreshEvidenceDiagnostic()", html)
            self.assertFalse((run_dir / "review.html").exists())

            with self.assertRaisesRegex(ReviewArtifactError, "output_name"):
                render_run_review(run_dir, output_name="../unsafe.html")


if __name__ == "__main__":
    unittest.main()
