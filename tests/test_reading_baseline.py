from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from flood_agent import reading_baseline as baseline
from scripts import run_reading_baseline


class ReadingBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.sqlite3"
        self.profile_path = self.root / "profile.json"
        self.readings: list[dict] = []
        self.records: list[dict] = []
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, source TEXT NOT NULL, raw_text TEXT)")
        for index in range(16):
            reading = {
                "description": f"Flash flood at Jalan Example {index}; issued 1510 hours.",
                "area": {"circle": [1.33 + index / 10000, 103.87, 1], "areaDesc": f"Jalan Example {index}"},
            }
            item = {
                "msgType": "Alert" if index % 2 == 0 else "Cancel",
                "description": f"OUTER_ONLY_MARKER_{index}",
                "headline": "Outer headline must not be prompt content",
                "readings": [reading],
            }
            record = {"datetime": f"2025-05-{22 + index:02d}T20:16:06+08:00", "item": item, "timestamp": "unstable", "updatedTimestamp": index}
            self.readings.append(reading)
            self.records.append(record)
        raw = json.dumps({"data": {"records": self.records}}, ensure_ascii=False)
        connection.execute("INSERT INTO snapshots(id,source,raw_text) VALUES(1,'archive',?)", (raw,))
        connection.commit()
        connection.close()
        examples = []
        for index, record in enumerate(self.records):
            examples.append({
                "classification": {"reference": {
                    "source": "archive",
                    "snapshot_id": 1,
                    "record_index": index,
                    "reading_index": 0,
                    "record_datetime": record["datetime"],
                    "record_sha256": baseline.record_sha256(record),
                }}
            })
        self.profile = {"window": {"start": "2025-05-22", "end": "2025-06-06", "basis": "test"}, "road_description_examples": examples}
        self.profile_path.write_text(json.dumps(self.profile, ensure_ascii=False), encoding="utf-8")
        self.manifest = baseline.build_manifest(self.profile_path, self.db_path)
        self.manifest_path = self.root / "manifest.json"
        baseline.atomic_write_json(self.manifest_path, self.manifest)
        self.manifest, self.recovered = baseline.verify_manifest(
            self.manifest_path, self.manifest["manifest_sha256"], self.profile_path, self.db_path
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_credential_loader_accepts_existing_llm_api_key_name(self) -> None:
        (self.root / ".env").write_text("LLM_API_KEY=offline-test-value\n", encoding="utf-8")
        with patch.object(run_reading_baseline, "ROOT", self.root), patch.dict("os.environ", {}, clear=True):
            self.assertEqual(run_reading_baseline._load_api_key(), "offline-test-value")

    def _completed_response(self) -> dict:
        semantic = {"road_relations": [], "flood_state": {"value": None, "evidence": []}, "passability_hints": [], "time_cues": []}
        return {
            "id": "resp_mock",
            "status": "completed",
            "output_text": json.dumps(semantic),
            "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42, "output_tokens_details": {"reasoning_tokens": 5}},
        }

    def _attempt_started(self, target: dict, reading: dict) -> dict:
        body = baseline.make_request_body(reading)
        return {
            "event": "attempt_started",
            "target_id": target["target_id"],
            "attempt": 1,
            "request_body": body,
            "request_sha256": baseline.sha256_bytes(baseline.canonical_json_bytes(body)),
        }

    def _mock_requester(self, captured: list[dict] | None = None):
        def requester(body, *, api_key, timeout_seconds):
            if captured is not None:
                captured.append(body)
            return {
                "http_status": 200,
                "headers": {"x-request-id": "req_mock"},
                "request_id": "req_mock",
                "raw_body": json.dumps(self._completed_response()),
                "latency_ms": 1,
            }
        return requester

    def test_manifest_rechecks_profile_and_raw_sqlite_identity(self) -> None:
        self.assertEqual(len(self.manifest["targets"]), 16)
        self.assertEqual(len({target["target_id"] for target in self.manifest["targets"]}), 16)
        self.assertEqual(len({tuple(target["canonical_reading_key"]) for target in self.manifest["targets"]}), 16)
        self.assertEqual(self.manifest["targets"][0]["source"]["reading_sha256"], baseline.reading_sha256(self.readings[0]))
        self.assertEqual(self.recovered[0][2], self.readings[0])

        self.records[0]["item"]["readings"][0]["description"] = "changed after manifest"
        connection = sqlite3.connect(self.db_path)
        connection.execute("UPDATE snapshots SET raw_text=? WHERE id=1", (json.dumps({"data": {"records": self.records}}, ensure_ascii=False),))
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(ValueError, "record SHA mismatch"):
            baseline.verify_manifest(self.manifest_path, self.manifest["manifest_sha256"], self.profile_path, self.db_path)

    def test_duplicate_source_reference_blocks_manifest_without_calling_any_requester(self) -> None:
        self.profile["road_description_examples"][-1] = self.profile["road_description_examples"][0]
        self.profile_path.write_text(json.dumps(self.profile), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate source reference"):
            baseline.build_manifest(self.profile_path, self.db_path)

    def test_request_contains_exact_original_reading_and_reasoning_has_no_sampling_fields(self) -> None:
        reading = self.readings[0]
        body = baseline.make_request_body(reading, "medium", 8192)
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertEqual(body["input"][1]["role"], "user")
        self.assertEqual(json.loads(body["input"][1]["content"][0]["text"]), reading)
        self.assertNotIn("OUTER_ONLY_MARKER_0", json.dumps(body, ensure_ascii=False))
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertEqual(body["max_output_tokens"], 8192)
        for key in ("temperature", "top_p", "top_logprobs"):
            self.assertNotIn(key, body)

    def test_schema_and_evidence_validate_path_and_exact_quote(self) -> None:
        semantic = {
            "road_relations": [{
                "road_name": "Jalan Example 0", "start_road": None, "end_road": None,
                "near_landmark": None, "direction": None, "ramp": None,
                "evidence": [{"path": "$.description", "quote": "Flash flood at Jalan Example 0"}],
            }],
            "flood_state": {"value": "occurred", "evidence": [{"path": "$.description", "quote": "Flash flood"}]},
            "passability_hints": [], "time_cues": [],
        }
        baseline.validate_output_shape(semantic)
        baseline.validate_evidence(semantic, self.readings[0])
        bad = json.loads(json.dumps(semantic))
        bad["road_relations"][0]["evidence"][0]["path"] = "$.outer_description"
        with self.assertRaisesRegex(ValueError, "evidence_error"):
            baseline.validate_evidence(bad, self.readings[0])
        bad = json.loads(json.dumps(semantic))
        bad["extra"] = True
        with self.assertRaisesRegex(ValueError, "schema_error"):
            baseline.validate_output_shape(bad)
        for field, replacement in (
            ("road_relations", [{**semantic["road_relations"][0], "road_name": ""}]),
            ("passability_hints", [{"value": "", "evidence": [{"path": "$.description", "quote": "Flash flood"}]}]),
            ("time_cues", [{"raw": "", "kind": "other", "evidence": [{"path": "$.description", "quote": "Flash flood"}]}]),
        ):
            invalid = json.loads(json.dumps(semantic))
            invalid[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "minLength"):
                baseline.validate_output_shape(invalid)

        unknown = {"road_relations": [], "flood_state": {"value": "unknown", "evidence": [
            {"path": "$.description", "quote": "Flash flood at Jalan Example 0"},
        ]}, "passability_hints": [], "time_cues": []}
        baseline.validate_output_shape(unknown)
        with self.assertRaisesRegex(ValueError, "explicit uncertainty"):
            baseline.validate_evidence(unknown, self.readings[0])
        unknown["flood_state"]["evidence"][0]["quote"] = "Flood status unknown"
        baseline.validate_evidence(unknown, {"description": "Flood status unknown"})

    def test_http_status_responses_status_refusal_and_usage_are_audited(self) -> None:
        valid = self._completed_response()
        status, semantic, audit, error = baseline.classify_response(200, json.dumps(valid), self.readings[0])
        self.assertEqual((status, error), ("success", None))
        self.assertIsNotNone(semantic)
        self.assertEqual(audit["usage"]["reasoning_tokens"], 5)

        incomplete = {**valid, "status": "incomplete"}
        status, _, audit, _ = baseline.classify_response(200, json.dumps(incomplete), self.readings[0])
        self.assertEqual(status, "response_incomplete")
        self.assertEqual(audit["usage"]["input_tokens"], 30)

        refusal = {"status": "completed", "output": [{"content": [{"type": "refusal", "refusal": "cannot help"}]}], "usage": valid["usage"]}
        self.assertEqual(baseline.classify_response(200, json.dumps(refusal), self.readings[0])[0], "refusal")
        self.assertEqual(baseline.classify_response(200, json.dumps({**valid, "status": "failed"}), self.readings[0])[0], "response_failed")
        status, _, audit, _ = baseline.classify_response(429, json.dumps(valid), self.readings[0])
        self.assertEqual(status, "http_error")
        self.assertEqual(audit["usage"]["total_tokens"], 42)

    def test_usage_summary_is_unknown_when_any_attempt_usage_is_missing(self) -> None:
        run = {
            "run_id": "mock-run", "model": "gpt-6-luna", "reasoning_effort": "medium",
            "prompt_version": "1", "schema_version": "1", "evidence_validator_version": "1",
            "started_at": "2026-10-09T00:00:00+00:00",
        }
        manifest = {
            "manifest_sha256": "a" * 64,
            "profile_sha256": "b" * 64,
            "profile_window": {"start": "2025-05-22", "end": "2025-06-06"},
            "max_requests": 16,
        }
        results = [
            {"audit": {"status": "success", "attempts": 1, "usage": {
                "input_tokens": 30, "output_tokens": 12, "total_tokens": 42, "reasoning_tokens": 5,
            }}},
            {"audit": {"status": "response_error", "attempts": 1, "usage": None}},
        ]
        summary = baseline.make_summary(run, manifest, results)
        self.assertTrue(all(value is None for value in summary["usage"].values()))

    def test_single_attempt_requester_calls_transport_once(self) -> None:
        calls = []

        class Response:
            status = 200
            headers = {"x-request-id": "req_once"}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return b'{"status":"completed"}'

        def opener(request, timeout):
            calls.append((request, timeout))
            return Response()

        response = baseline.request_once(baseline.make_request_body(self.readings[0]), "offline-key", opener=opener)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 60)
        self.assertEqual(response["request_id"], "req_once")
        self.assertEqual(response["http_status"], 200)

    def test_default_requester_rejects_redirect_without_forwarding_credentials(self) -> None:
        transport_calls = []
        destination_calls = []
        redirect_headers = Message()
        redirect_headers["Location"] = "https://outside.example/capture"

        class FakeOpener:
            def __init__(self, handler):
                self.handler = handler

            def open(self, request, timeout):
                transport_calls.append((request, timeout))
                try:
                    self.handler.redirect_request(
                        request,
                        io.BytesIO(b"redirect body"),
                        302,
                        "Found",
                        redirect_headers,
                        redirect_headers["Location"],
                    )
                except urllib.error.HTTPError:
                    raise
                destination_calls.append(redirect_headers["Location"])
                raise AssertionError("redirect was not rejected")

        def build_fake_opener(*handlers):
            redirect_handler = next(handler for handler in handlers if isinstance(handler, baseline.RejectRedirectHandler))
            return FakeOpener(redirect_handler)

        with patch.object(baseline, "API_URL", "https://api.openai.com/v1/responses"), \
             patch.object(baseline.urllib.request, "build_opener", side_effect=build_fake_opener), \
             patch.object(baseline.urllib.request, "urlopen", side_effect=AssertionError("default urlopen used")):
            response = baseline.request_once(baseline.make_request_body(self.readings[0]), "offline-secret")

        self.assertEqual(response["http_status"], 302)
        self.assertEqual(response["raw_body"], "redirect body")
        self.assertEqual(len(transport_calls), 1)
        self.assertEqual(transport_calls[0][0].get_header("Authorization"), "Bearer offline-secret")
        self.assertEqual(destination_calls, [])

    def test_execute_once_per_target_persists_request_response_and_gis(self) -> None:
        captured = []
        run_dir = self.root / "mock-run"
        run = baseline.execute_run(
            run_dir, self.manifest, self.recovered, "mock-only", requester=self._mock_requester(captured)
        )
        self.assertTrue(run["sealed"])
        self.assertEqual(len(captured), 16)
        attempts = baseline.read_jsonl(run_dir / "attempts.jsonl")
        responses = baseline.read_jsonl(run_dir / "raw_responses.jsonl")
        results = baseline.read_jsonl(run_dir / "results.partial.jsonl")
        self.assertEqual(len(attempts), 16)
        self.assertEqual(len(responses), 16)
        self.assertEqual(len(results), 16)
        self.assertEqual(attempts[0]["attempt"], 1)
        self.assertEqual(json.loads(captured[0]["input"][1]["content"][0]["text"]), self.readings[0])
        self.assertEqual(results[0]["audit"]["request_id"], "req_mock")
        self.assertEqual(results[0]["geometry"], {"type": "Point", "coordinates": [103.87, 1.33]})
        self.assertEqual(results[0]["audit"]["status"], "success")
        final_rows = baseline.read_jsonl(run_dir / "results.jsonl")
        self.assertEqual(len(final_rows), 16)
        before = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
        baseline.seal_run(run_dir)
        after = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
        self.assertEqual(before, after)

    def test_offline_seal_never_loads_credentials_or_calls_requester(self) -> None:
        run_dir = self.root / "interrupted-run"
        baseline.create_run(run_dir, self.manifest, self.recovered)
        target = self.manifest["targets"][0]
        baseline.append_jsonl(run_dir / "attempts.jsonl", self._attempt_started(target, self.readings[0]))
        with patch.object(run_reading_baseline, "_load_api_key", side_effect=AssertionError("credential loader used")), \
             patch.object(baseline, "request_once", side_effect=AssertionError("requester used")):
            self.assertEqual(run_reading_baseline.main(["--seal-only", "--run-dir", str(run_dir)]), 0)
        results = baseline.read_jsonl(run_dir / "results.jsonl")
        self.assertEqual(len(results), 16)
        self.assertEqual(results[0]["audit"]["status"], "outcome_unknown")
        self.assertTrue(all(result["audit"]["status"] == "not_attempted" for result in results[1:]))
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["not_attempted_count"], 15)
        self.assertEqual(summary["failed_count"], 1)

    def test_seal_rejects_partial_result_without_attempt_marker(self) -> None:
        run_dir = self.root / "orphan-partial-run"
        baseline.create_run(run_dir, self.manifest, self.recovered)
        official = baseline.read_jsonl(run_dir / "official_records.jsonl")[0]
        official["audit"].update({"status": "success", "attempts": 1})
        baseline.append_jsonl(run_dir / "results.partial.jsonl", official)

        with self.assertRaisesRegex(ValueError, "缺少唯一 attempt marker"):
            baseline.seal_run(run_dir)
        self.assertFalse((run_dir / "results.jsonl").exists())

    def test_seal_rejects_partial_result_that_disagrees_with_durable_response(self) -> None:
        run_dir = self.root / "mismatched-partial-run"
        baseline.create_run(run_dir, self.manifest, self.recovered)
        target = self.manifest["targets"][0]
        body = baseline.make_request_body(self.readings[0])
        baseline.append_jsonl(run_dir / "attempts.jsonl", self._attempt_started(target, self.readings[0]))
        response = self._mock_requester()(
            body, api_key="mock-only", timeout_seconds=60
        )
        raw_entry = {
            "target_id": target["target_id"], "attempt": 1,
            "http_status": response["http_status"], "headers": response["headers"],
            "request_id": response["request_id"], "latency_ms": response["latency_ms"],
            "raw_body": response["raw_body"],
        }
        baseline.append_jsonl(run_dir / "raw_responses.jsonl", raw_entry)
        partial = baseline.make_official_result(target, *self.recovered[0])
        partial["audit"].update({"status": "success", "attempts": 1})
        baseline.append_jsonl(run_dir / "results.partial.jsonl", partial)

        with self.assertRaisesRegex(ValueError, "does not match its durable raw response"):
            baseline.seal_run(run_dir)

    def test_seal_ignores_only_unterminated_ledger_tails(self) -> None:
        run_dir = self.root / "truncated-ledger-run"
        baseline.create_run(run_dir, self.manifest, self.recovered)
        first, second = self.manifest["targets"][:2]
        baseline.append_jsonl(run_dir / "attempts.jsonl", self._attempt_started(first, self.readings[0]))
        with (run_dir / "attempts.jsonl").open("ab") as stream:
            stream.write(b'{"event":"attempt_started","target_id":"truncated')
        with (run_dir / "raw_responses.jsonl").open("ab") as stream:
            stream.write(b'{"target_id":"truncated response')
        with (run_dir / "results.partial.jsonl").open("ab") as stream:
            stream.write(b'{"target_id":"truncated result')

        baseline.seal_run(run_dir)
        results = baseline.read_jsonl(run_dir / "results.jsonl")
        self.assertEqual(results[0]["audit"]["status"], "outcome_unknown")
        self.assertEqual(results[1]["audit"]["status"], "not_attempted")

    def test_seal_does_not_trust_modified_official_fields_in_unknown_partial(self) -> None:
        run_dir = self.root / "modified-unknown-run"
        baseline.create_run(run_dir, self.manifest, self.recovered)
        target = self.manifest["targets"][0]
        baseline.append_jsonl(run_dir / "attempts.jsonl", self._attempt_started(target, self.readings[0]))
        partial = baseline.read_jsonl(run_dir / "official_records.jsonl")[0]
        partial["audit"].update({"status": "outcome_unknown", "attempts": 1})
        partial["official"]["item_raw"]["headline"] = "tampered"
        baseline.append_jsonl(run_dir / "results.partial.jsonl", partial)

        with self.assertRaisesRegex(ValueError, "changed immutable source/GIS/semantic fields"):
            baseline.seal_run(run_dir)

    def test_seal_rejects_attempt_body_outside_exact_reading_contract(self) -> None:
        run_dir = self.root / "tampered-request-run"
        baseline.create_run(run_dir, self.manifest, self.recovered)
        target = self.manifest["targets"][0]
        marker = self._attempt_started(target, self.readings[0])
        marker["request_body"]["input"][1]["content"][0]["text"] = json.dumps({"description": "outer context"})
        marker["request_sha256"] = baseline.sha256_bytes(baseline.canonical_json_bytes(marker["request_body"]))
        baseline.append_jsonl(run_dir / "attempts.jsonl", marker)

        with self.assertRaisesRegex(ValueError, "differs from exact original reading"):
            baseline.seal_run(run_dir)
        self.assertFalse((run_dir / "results.jsonl").exists())

    def test_oversize_input_fails_before_run_creation_or_request(self) -> None:
        run_dir = self.root / "must-not-exist"
        calls = []
        with self.assertRaisesRegex(ValueError, "before any send"):
            baseline.execute_run(run_dir, self.manifest, self.recovered, "mock-only", max_input_bytes=1,
                                 requester=lambda *args, **kwargs: calls.append(args))
        self.assertFalse(run_dir.exists())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
