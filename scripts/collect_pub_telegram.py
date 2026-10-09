"""Collect visible messages from PUB's public Telegram web archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
SGT = ZoneInfo("Asia/Singapore")
CHANNEL = "pubfloodalerts"
FIRST_URL = f"https://t.me/s/{CHANNEL}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def parse_page(html: str, request_url: str) -> tuple[list[dict], str | None, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    messages, errors = [], []
    for widget in soup.select(".tgme_widget_message[data-post]"):
        post = widget.get("data-post", "")
        channel, separator, identifier = post.partition("/")
        timestamp = widget.select_one("time[datetime]")
        if channel != CHANNEL or not separator or not identifier.isdigit() or timestamp is None:
            errors.append(f"invalid_message_metadata:{post}")
            continue
        try:
            published = datetime.fromisoformat(timestamp["datetime"])
            if published.tzinfo is None:
                raise ValueError("missing_timezone")
        except (ValueError, TypeError) as error:
            errors.append(f"invalid_message_time:{post}:{error}")
            continue
        text_element = widget.select_one(".tgme_widget_message_text")
        text_html = str(text_element) if text_element else None
        widget_html = str(widget)
        if text_element:
            for line_break in text_element.find_all("br"):
                line_break.replace_with("\n")
        footer = widget.select_one(".tgme_widget_message_footer")
        footer_text = footer.get_text(" ", strip=True) if footer else ""
        messages.append({
            "channel": channel,
            "message_id": int(identifier),
            "url": f"https://t.me/{post}",
            "published_at_utc": published.astimezone(timezone.utc).isoformat(),
            "published_at_sgt": published.astimezone(SGT).isoformat(),
            "text": text_element.get_text().strip() if text_element else "",
            "text_html": text_html,
            "widget_html": widget_html,
            "visible_edited_marker": "edited" in footer_text.lower(),
            "footer_text": footer_text,
            "media_links": [element.get("href") for element in widget.select("a.tgme_widget_message_photo_wrap[href], a.tgme_widget_message_video_player[href], a.tgme_widget_message_document_wrap[href]")],
            "source_page_url": request_url,
        })
    pagination = soup.select_one("a[data-before][href]")
    next_url = urljoin(FIRST_URL, pagination["href"]) if pagination else None
    if next_url:
        parsed = urlparse(next_url)
        if parsed.netloc != "t.me" or parsed.path != f"/s/{CHANNEL}":
            errors.append("unexpected_pagination_url")
            next_url = None
    return messages, next_url, errors


def fetch_page(session: requests.Session, url: str, raw_directory: Path, delay: float) -> tuple[str, dict]:
    cache_key = hashlib.sha256(url.encode()).hexdigest()[:20]
    html_path = raw_directory / f"{cache_key}.html"
    metadata_path = raw_directory / f"{cache_key}.json"
    if html_path.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        cached_html = html_path.read_bytes().decode("utf-8")
        if metadata.get("status_code") == 200 and hashlib.sha256(cached_html.encode()).hexdigest() == metadata.get("sha256"):
            return cached_html, {**metadata, "cache_reused": True}
    attempts = []
    for attempt in range(3):
        time.sleep(delay if attempt == 0 else max(delay, 2 ** attempt))
        try:
            response = session.get(url, timeout=(15, 45))
            attempts.append({"requested_at_utc": utc_now(), "status_code": response.status_code})
            response.raise_for_status()
            response.encoding = "utf-8"
            html = response.text
            metadata = {
                "request_url": url,
                "response_url": response.url,
                "fetched_at_utc": utc_now(),
                "status_code": response.status_code,
                "sha256": hashlib.sha256(html.encode()).hexdigest(),
                "raw_html_path": str(html_path.relative_to(ROOT)),
                "attempts": attempts,
                "cache_reused": False,
            }
            html_path.write_bytes(html.encode("utf-8"))
            write_json(metadata_path, metadata)
            return html, metadata
        except requests.RequestException as error:
            attempts.append({"requested_at_utc": utc_now(), "error": str(error)})
            if attempt == 2:
                raise RuntimeError(json.dumps({"url": url, "attempts": attempts}, ensure_ascii=False)) from error
    raise RuntimeError("fetch_attempts_exhausted")


def collect(start: str, end: str, output_directory: Path, delay: float, max_pages: int) -> dict:
    start_time = datetime.fromisoformat(start).replace(tzinfo=SGT)
    end_time = datetime.fromisoformat(end).replace(tzinfo=SGT)
    if start_time >= end_time:
        raise ValueError("start must precede end")
    raw_directory = output_directory / "raw"
    raw_directory.mkdir(parents=True, exist_ok=True)
    manifest_path = output_directory / "manifest.json"
    manifest = {
        "channel": CHANNEL, "source": FIRST_URL,
        "window": {"start_sgt_inclusive": start_time.isoformat(), "end_sgt_exclusive": end_time.isoformat()},
        "started_at_utc": utc_now(), "status": "running", "pages": [], "errors": [],
        "limitations": [
            "Public web messages visible at acquisition time; deletion and moderation history are unavailable.",
            "Traversing the date window does not establish a complete official archive or absence of events.",
            "Message ID gaps are diagnostics only; they cannot identify deleted messages.",
            "Publication times are Telegram timestamps, not necessarily event occurrence times.",
        ],
    }
    messages_by_id, visited, observed_ids = {}, set(), set()
    session = requests.Session()
    session.headers["User-Agent"] = "FloodEventResearchBaseline/1.0 public-channel-archive"
    url, crossed_start = FIRST_URL, False
    try:
        while url and len(manifest["pages"]) < max_pages:
            if url in visited:
                raise RuntimeError("pagination_loop")
            visited.add(url)
            html, metadata = fetch_page(session, url, raw_directory, delay)
            messages, next_url, errors = parse_page(html, url)
            metadata.update({"message_ids": [message["message_id"] for message in messages], "next_url": next_url, "parser_errors": errors})
            manifest["pages"].append(metadata)
            if errors or not messages:
                raise RuntimeError(f"page_parse_failed:{errors or 'no_visible_messages'}")
            for message in messages:
                observed_ids.add(message["message_id"])
                published = datetime.fromisoformat(message["published_at_sgt"])
                crossed_start = crossed_start or published < start_time
                if start_time <= published < end_time:
                    message["raw_page_sha256"] = metadata["sha256"]
                    message["fetched_at_utc"] = metadata["fetched_at_utc"]
                    messages_by_id[message["message_id"]] = message
            manifest["oldest_visible_at_sgt"] = min(message["published_at_sgt"] for message in messages)
            manifest["newest_visible_at_sgt"] = max(manifest.get("newest_visible_at_sgt", ""), max(message["published_at_sgt"] for message in messages))
            write_json(manifest_path, manifest)
            print(f"page={len(manifest['pages'])} collected={len(messages_by_id)} oldest={manifest['oldest_visible_at_sgt']}", flush=True)
            if crossed_start:
                manifest["status"] = "public_visible_window_traversed"
                break
            url = next_url
        if not crossed_start:
            manifest["status"] = "partial"
            manifest["errors"].append("max_pages_reached" if url else "public_pagination_ended_before_window_start")
    except Exception as error:
        manifest["status"] = "failed_partial"
        manifest["errors"].append(str(error))
    finally:
        session.close()
        ordered_messages = sorted(messages_by_id.values(), key=lambda message: (message["published_at_utc"], message["message_id"]))
        (output_directory / "messages.jsonl").write_text("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in ordered_messages), encoding="utf-8")
        manifest["finished_at_utc"] = utc_now()
        manifest["message_count"] = len(ordered_messages)
        manifest["page_count"] = len(manifest["pages"])
        manifest["crossed_start_boundary"] = crossed_start
        manifest["message_range_sgt"] = [ordered_messages[0]["published_at_sgt"], ordered_messages[-1]["published_at_sgt"]] if ordered_messages else None
        missing_ids = sorted(set(range(min(observed_ids), max(observed_ids) + 1)) - observed_ids) if observed_ids else []
        manifest["visible_id_gap_diagnostic"] = {"observed_id_min": min(observed_ids) if observed_ids else None, "observed_id_max": max(observed_ids) if observed_ids else None, "missing_id_count": len(missing_ids), "missing_ids": missing_ids}
        write_json(manifest_path, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-10-09")
    parser.add_argument("--end", default="2026-10-09")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "telegram_pub")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--max-pages", type=int, default=200)
    arguments = parser.parse_args()
    result = collect(arguments.start, arguments.end, arguments.output, arguments.delay, arguments.max_pages)
    print(json.dumps({key: result[key] for key in ("status", "message_count", "page_count", "message_range_sgt", "errors")}, ensure_ascii=False, indent=2))
    if result["status"] != "public_visible_window_traversed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
