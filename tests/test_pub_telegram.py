import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.collect_pub_telegram import FIRST_URL, ROOT, collect, fetch_page, parse_page


def message_html(identifier, timestamp, text="Flood<br>Location: A Road"):
    return (
        f'<div class="tgme_widget_message" data-post="pubfloodalerts/{identifier}">'
        f'<div class="tgme_widget_message_text">{text}</div>'
        f'<div class="tgme_widget_message_footer">edited <time datetime="{timestamp}"></time></div>'
        '</div>'
    )


class PubTelegramCollectionTests(unittest.TestCase):
    def test_raw_html_byte_hash_and_cache_are_consistent(self):
        response = Mock(status_code=200, url=FIRST_URL, text="<html>\nexample\n</html>")
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary_directory:
            raw_directory = Path(temporary_directory)
            html, metadata = fetch_page(session, FIRST_URL, raw_directory, 0)
            raw_path = ROOT / metadata["raw_html_path"]
            self.assertEqual(raw_path.read_bytes(), html.encode("utf-8"))
            self.assertEqual(hashlib.sha256(raw_path.read_bytes()).hexdigest(), metadata["sha256"])
            cached_html, cached_metadata = fetch_page(session, FIRST_URL, raw_directory, 0)
            self.assertEqual(cached_html, html)
            self.assertTrue(cached_metadata["cache_reused"])
            session.get.assert_called_once()

    def test_parse_preserves_text_and_html_evidence(self):
        html = message_html(8, "2025-10-09T01:00:00+00:00") + '<a data-before="8" href="/s/pubfloodalerts?before=8">Previous</a>'
        messages, next_url, errors = parse_page(html, FIRST_URL)
        self.assertEqual(errors, [])
        self.assertEqual(messages[0]["text"], "Flood\nLocation: A Road")
        self.assertIn("<br/>", messages[0]["text_html"])
        self.assertIn("<br/>", messages[0]["widget_html"])
        self.assertEqual(messages[0]["published_at_sgt"], "2025-10-09T09:00:00+08:00")
        self.assertTrue(messages[0]["visible_edited_marker"])
        self.assertEqual(next_url, FIRST_URL + "?before=8")

    def test_window_uses_sgt_and_excludes_end_date(self):
        html = "".join([
            message_html(1, "2025-10-08T15:59:59+00:00"),
            message_html(2, "2025-10-08T16:00:00+00:00"),
            message_html(3, "2026-10-08T15:59:59+00:00"),
            message_html(4, "2026-10-08T16:00:00+00:00"),
        ])
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            with patch("scripts.collect_pub_telegram.fetch_page", return_value=(html, {"sha256": "test", "fetched_at_utc": "test"})):
                result = collect("2025-10-09", "2026-10-09", output, 0, 2)
            messages = [json.loads(line) for line in (output / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([message["message_id"] for message in messages], [2, 3])
            self.assertEqual(result["status"], "public_visible_window_traversed")

    def test_early_pagination_end_does_not_claim_complete(self):
        html = message_html(1, "2026-01-01T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch("scripts.collect_pub_telegram.fetch_page", return_value=(html, {"sha256": "test", "fetched_at_utc": "test"})):
                result = collect("2025-10-09", "2026-10-09", Path(temporary_directory), 0, 2)
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["crossed_start_boundary"])
            self.assertIn("public_pagination_ended_before_window_start", result["errors"])

    def test_invalid_page_is_recorded_as_failed_partial(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch("scripts.collect_pub_telegram.fetch_page", return_value=("<html>unavailable</html>", {"sha256": "test", "fetched_at_utc": "test"})):
                result = collect("2025-10-09", "2026-10-09", Path(temporary_directory), 0, 2)
            self.assertEqual(result["status"], "failed_partial")
            self.assertEqual(result["message_count"], 0)
            self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()
