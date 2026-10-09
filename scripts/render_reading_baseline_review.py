#!/usr/bin/env python3
"""Render an offline, record-level review page for a finalized reading run."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


EXPECTED_RECORD_COUNT = 16
LABELS = frozenset({"correct", "omitted", "wrong", "unjudgeable"})
MAX_REMARKS_LENGTH = 2_000
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
OUTPUT_NAMES = frozenset({"review.html", "implementation-report.html"})


class ReviewArtifactError(ValueError):
    """Raised when a finalized run artifact cannot safely be reviewed."""


def parse_final_results(results_bytes: bytes) -> tuple[list[dict[str, Any]], str]:
    """Parse the finalized JSONL bytes and return rows plus their exact digest."""
    digest = hashlib.sha256(results_bytes).hexdigest()
    try:
        decoded = results_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewArtifactError("results.jsonl is not valid UTF-8") from exc

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(decoded.splitlines(), start=1):
        if not line.strip():
            raise ReviewArtifactError(f"results.jsonl contains a blank line at {line_number}")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewArtifactError(
                f"results.jsonl has invalid JSON on line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(record, dict):
            raise ReviewArtifactError(f"results.jsonl line {line_number} must be a JSON object")
        for field in ("target_id", "source", "official", "semantic", "geometry", "audit"):
            if field not in record:
                raise ReviewArtifactError(
                    f"results.jsonl line {line_number} is missing required field {field!r}"
                )
        if not isinstance(record["target_id"], str) or not record["target_id"]:
            raise ReviewArtifactError(f"results.jsonl line {line_number} has an invalid target_id")
        for field in ("source", "official", "semantic", "audit"):
            if not isinstance(record[field], dict):
                raise ReviewArtifactError(
                    f"results.jsonl line {line_number} field {field!r} must be an object"
                )
        records.append(record)

    if len(records) != EXPECTED_RECORD_COUNT:
        raise ReviewArtifactError(
            f"expected {EXPECTED_RECORD_COUNT} finalized records; found {len(records)}"
        )
    target_ids = [record["target_id"] for record in records]
    if len(set(target_ids)) != len(target_ids):
        raise ReviewArtifactError("results.jsonl contains duplicate target_id values")
    return records, digest


def validate_review_import(
    payload: Any,
    *,
    run_id: str,
    manifest_sha256: str,
    results_sha256: str,
    target_ids: set[str],
) -> list[dict[str, str]]:
    """Validate an exported review payload against this finalized run."""
    if not isinstance(payload, dict):
        raise ReviewArtifactError("review import must be a JSON object")
    if payload.get("run_id") != run_id:
        raise ReviewArtifactError("review import run_id does not match this run")
    if payload.get("manifest_sha256") != manifest_sha256:
        raise ReviewArtifactError("review import manifest_sha256 does not match this run")
    if payload.get("results_sha256") != results_sha256:
        raise ReviewArtifactError("review import results_sha256 does not match finalized results")
    labels = payload.get("labels")
    if not isinstance(labels, list):
        raise ReviewArtifactError("review import labels must be an array")

    validated: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, label_item in enumerate(labels):
        if not isinstance(label_item, dict):
            raise ReviewArtifactError(f"review label {index + 1} must be an object")
        target_id = label_item.get("target_id")
        label = label_item.get("label")
        remarks = label_item.get("remarks")
        if not isinstance(target_id, str) or target_id not in target_ids:
            raise ReviewArtifactError(f"review label {index + 1} has an unknown target_id")
        if target_id in seen:
            raise ReviewArtifactError(f"review import contains duplicate target_id {target_id!r}")
        if not isinstance(label, str) or label not in LABELS:
            raise ReviewArtifactError(f"review label {index + 1} has an invalid label")
        if not isinstance(remarks, str):
            raise ReviewArtifactError(f"review label {index + 1} remarks must be text")
        if len(remarks) > MAX_REMARKS_LENGTH:
            raise ReviewArtifactError(
                f"review label {index + 1} remarks exceed {MAX_REMARKS_LENGTH} characters"
            )
        seen.add(target_id)
        validated.append({"target_id": target_id, "label": label, "remarks": remarks})
    return validated


def _json_for_html(value: Any) -> str:
    """Serialize JSON as inert script data, including script-closing text."""
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        rendered.replace("<", r"\u003c")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )


def _validate_run_metadata(run_id: Any, manifest_sha256: Any) -> tuple[str, str]:
    if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
        raise ReviewArtifactError("run.json must contain a non-empty run_id of at most 256 characters")
    if not isinstance(manifest_sha256, str) or not SHA256_PATTERN.fullmatch(manifest_sha256):
        raise ReviewArtifactError("run.json must contain a lowercase 64-character manifest_sha256")
    return run_id, manifest_sha256


def _manifest_digest(manifest: dict[str, Any]) -> str:
    stable_manifest = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    canonical = json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_review_html(
    results_path: str | Path,
    output_path: str | Path,
    *,
    run_id: str,
    manifest_sha256: str,
    run_summary: dict[str, Any] | None = None,
) -> str:
    """Render a self-contained review page and return finalized results SHA-256."""
    run_id, manifest_sha256 = _validate_run_metadata(run_id, manifest_sha256)
    results_bytes = Path(results_path).read_bytes()
    records, results_sha256 = parse_final_results(results_bytes)
    payload = {
        "run_id": run_id,
        "manifest_sha256": manifest_sha256,
        "results_sha256": results_sha256,
        "records": records,
        "run_summary": run_summary or {},
        "labels": sorted(LABELS),
        "max_remarks_length": MAX_REMARKS_LENGTH,
    }
    html = _HTML_TEMPLATE.replace("__REVIEW_DATA__", _json_for_html(payload))
    target_path = Path(output_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(html, encoding="utf-8", newline="\n")
    return results_sha256


def render_run_review(
    run_dir: str | Path,
    *,
    allow_unsealed: bool = False,
    output_name: str = "review.html",
) -> tuple[Path, str]:
    """Render a review page, normally requiring the sealed results digest."""
    if output_name not in OUTPUT_NAMES:
        raise ReviewArtifactError("output_name must be review.html or implementation-report.html")
    if allow_unsealed and output_name != "review.html":
        raise ReviewArtifactError("unsealed artifacts may only render the internal review.html")
    run_path = Path(run_dir)
    try:
        run_metadata = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReviewArtifactError(f"cannot read run.json: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReviewArtifactError("run.json is not valid JSON") from exc
    if not isinstance(run_metadata, dict):
        raise ReviewArtifactError("run.json must contain a JSON object")
    run_id, expected_manifest_sha256 = _validate_run_metadata(
        run_metadata.get("run_id"), run_metadata.get("manifest_sha256")
    )
    manifest_path = run_path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReviewArtifactError(f"cannot read manifest.json: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReviewArtifactError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ReviewArtifactError("manifest.json must contain a JSON object")
    if (
        manifest.get("manifest_sha256") != expected_manifest_sha256
        or _manifest_digest(manifest) != expected_manifest_sha256
    ):
        raise ReviewArtifactError("manifest.json SHA does not match run.json manifest_sha256")
    results_path = run_path / "results.jsonl"
    try:
        results_sha256 = hashlib.sha256(results_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReviewArtifactError(f"cannot read finalized results: {exc}") from exc
    if not allow_unsealed:
        if run_metadata.get("sealed") is not True:
            raise ReviewArtifactError("run is not sealed; review rendering requires finalized results")
        if run_metadata.get("results_sha256") != results_sha256:
            raise ReviewArtifactError("run.json results_sha256 does not match finalized results")
    run_summary: dict[str, Any] = {}
    summary_path = run_path / "summary.json"
    if not summary_path.exists() and not allow_unsealed:
        raise ReviewArtifactError("sealed run is missing summary.json")
    if summary_path.exists():
        summary_bytes = summary_path.read_bytes()
        if not allow_unsealed and run_metadata.get("summary_sha256") != hashlib.sha256(summary_bytes).hexdigest():
            raise ReviewArtifactError("run.json summary_sha256 does not match summary.json")
        try:
            parsed_summary = json.loads(summary_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewArtifactError("summary.json is not valid UTF-8 JSON") from exc
        if not isinstance(parsed_summary, dict):
            raise ReviewArtifactError("summary.json must be a JSON object")
        run_summary = parsed_summary
    output_path = run_path / output_name
    results_sha256 = render_review_html(
        results_path,
        output_path,
        run_id=run_id,
        manifest_sha256=expected_manifest_sha256,
        run_summary=run_summary,
    )
    return output_path, results_sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="run directory with run.json and results.jsonl")
    parser.add_argument("--output-name", choices=sorted(OUTPUT_NAMES), default="review.html")
    args = parser.parse_args(argv)
    try:
        output_path, results_sha256 = render_run_review(args.run_dir, output_name=args.output_name)
    except (OSError, ReviewArtifactError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"output_html={output_path}")
    print(f"results_sha256={results_sha256}")
    return 0


_HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'none'; img-src 'self' data: https://www.onemap.gov.sg; object-src 'none'; base-uri 'none'; form-action 'none'">
  <link rel="stylesheet" href="/vendor/leaflet.css">
  <title>Flood Event Agent：逐条 LLM→GIS 基线实现报告</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; color: #18212b; background: #f3f6f8; }
    body { max-width: 1180px; margin: 0 auto; padding: 24px; }
    h1, h2, h3 { line-height: 1.2; }
    h1 { margin-bottom: 8px; }
    .muted { color: #526170; }
    .notice { border-left: 4px solid #b36b00; background: #fff3d7; padding: 12px 16px; margin: 16px 0; }
    .summary, .card, .tools, .map-panel { background: white; border: 1px solid #d4dce3; border-radius: 8px; padding: 16px; margin: 16px 0; }
    .summary-grid { display: grid; gap: 8px 24px; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
    .summary-grid p { margin: 4px 0; }
    .map-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
    #review-map { height: 360px; border: 1px solid #c7d1d9; border-radius: 6px; background: #e8edf0; }
    .map-caption { margin: 8px 0 0; min-height: 1.5em; }
    .card { border-top: 4px solid #35708f; }
    .card h2 { margin-top: 0; overflow-wrap: anywhere; }
    .detail-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
    .detail { min-width: 0; }
    .detail h3 { margin-bottom: 6px; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto; background: #f5f7f9; padding: 12px; border-radius: 5px; }
    label { display: block; margin: 10px 0 5px; font-weight: 650; }
    select, textarea, button { font: inherit; }
    select, textarea { width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #8695a3; border-radius: 4px; background: white; }
    textarea { min-height: 76px; resize: vertical; }
    button { cursor: pointer; padding: 8px 12px; margin: 5px 8px 5px 0; border: 1px solid #526170; border-radius: 4px; background: #fff; }
    button.primary { color: white; background: #174f70; border-color: #174f70; }
    .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .status { min-height: 1.5em; color: #344c5b; }
    .storage-warning { display: none; }
    .leaflet-popup-content { overflow-wrap: anywhere; }
    @media (max-width: 640px) { body { padding: 12px; } }
  </style>
</head>
<body>
  <main>
    <h1>Flood Event Agent：逐条 LLM→GIS 基线实现报告</h1>
    <p class="muted">运行标识：<code id="run-id"></code>。每次请求只含一条原始 reading；地图点来自官方广播中心。人工标签针对整条 reading，不代表逐字段精确率或召回率。</p>
    <div id="storage-warning" class="notice storage-warning" role="status"></div>
    <section class="summary" aria-labelledby="summary-title">
      <h2 id="summary-title">运行与人工审阅摘要</h2>
      <div id="summary" class="summary-grid"></div>
      <div id="evidence-diagnostic" class="notice" style="display:none"></div>
      <p class="muted">“模型成功提取”表示通过结构和证据自动校验，不等于人工确认语义正确。“模型非成功”记录保留原始模型输出，可逐条检查。没有预置金标准；人工标签未完成前不报告准确率，本页不计算 precision、recall 或 F1。点表示官方警报广播中心，不表示实际淹水范围。</p>
    </section>
    <section class="map-panel" aria-labelledby="map-title">
      <div class="map-heading">
        <div><h2 id="map-title">官方广播中心点地图</h2><p class="muted">选择记录卡片中的“地图定位”可查看对应点；地图使用 OneMap 底图。</p></div>
        <button id="basemap-toggle" type="button">切换离线点位图</button>
      </div>
      <div id="review-map" role="img" aria-label="本次 16 条 reading 的官方广播中心点地图"></div>
      <p id="map-caption" class="map-caption muted" role="status">正在加载 OneMap；如果底图不可用，点位和下方官方坐标仍可查看。</p>
    </section>
    <section class="tools" aria-labelledby="transfer-title">
      <h2 id="transfer-title">导入或导出人工标签</h2>
      <p class="muted">导入文件必须匹配当前运行、manifest 和最终结果文件；导入会替换本页现有标签。</p>
      <label for="import-json">审阅 JSON</label>
      <textarea id="import-json" spellcheck="false" aria-describedby="import-status" placeholder="在此粘贴导出的审阅 JSON"></textarea>
      <div class="actions">
        <button id="import-button" type="button">导入标签</button>
        <button id="export-button" class="primary" type="button">导出审阅 JSON</button>
      </div>
      <p id="import-status" class="status" role="status" aria-live="polite"></p>
    </section>
    <section id="records" aria-label="逐条警报阅读记录"></section>
  </main>
  <script id="review-data" type="application/json">__REVIEW_DATA__</script>
  <script src="/vendor/leaflet.js"></script>
  <script>
  (() => {
    "use strict";
    const review = JSON.parse(document.getElementById("review-data").textContent);
    const labelNames = new Set(["correct", "omitted", "wrong", "unjudgeable"]);
    const labelsByTarget = new Map();
    const controlsByTarget = new Map();
    const markersByTarget = new Map();
    const targetIds = new Set(review.records.map((record) => record.target_id));
    const storageKey = "reading-baseline-review:v1:" + JSON.stringify([
      review.run_id, review.manifest_sha256, review.results_sha256
    ]);
    const warning = document.getElementById("storage-warning");
    const importStatus = document.getElementById("import-status");
    let localStorageHandle = null;
    let storageAvailable = false;
    let mapInstance = null;
    let tileLayer = null;

    function setStorageWarning(message) {
      warning.textContent = message;
      warning.style.display = message ? "block" : "none";
    }

    function countCharacters(value) {
      return Array.from(value).length;
    }

    function validateImport(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("导入内容必须是 JSON 对象。");
      }
      if (value.run_id !== review.run_id ||
          value.manifest_sha256 !== review.manifest_sha256 ||
          value.results_sha256 !== review.results_sha256) {
        throw new Error("导入文件与当前运行、manifest 或最终结果不匹配。");
      }
      if (!Array.isArray(value.labels)) {
        throw new Error("labels 必须是数组。");
      }
      const validated = new Map();
      for (const item of value.labels) {
        if (!item || typeof item !== "object" || Array.isArray(item) ||
            typeof item.target_id !== "string" || !targetIds.has(item.target_id)) {
          throw new Error("导入文件包含未知或无效的 target_id。");
        }
        if (validated.has(item.target_id)) {
          throw new Error("导入文件包含重复的 target_id。");
        }
        if (typeof item.label !== "string" || !labelNames.has(item.label)) {
          throw new Error("导入文件包含无效标签。");
        }
        if (typeof item.remarks !== "string" || countCharacters(item.remarks) > review.max_remarks_length) {
          throw new Error("备注必须为文本且不超过 2,000 字符。");
        }
        validated.set(item.target_id, { label: item.label, remarks: item.remarks });
      }
      return validated;
    }

    function makeExportPayload() {
      const labels = [];
      for (const record of review.records) {
        const entry = labelsByTarget.get(record.target_id);
        if (entry) {
          labels.push({ target_id: record.target_id, label: entry.label, remarks: entry.remarks });
        }
      }
      return {
        run_id: review.run_id,
        manifest_sha256: review.manifest_sha256,
        results_sha256: review.results_sha256,
        labels
      };
    }

    function persistLabels() {
      if (!storageAvailable) return;
      try {
        localStorageHandle.setItem(storageKey, JSON.stringify(makeExportPayload()));
      } catch (_) {
        storageAvailable = false;
        setStorageWarning("浏览器存储不可用或空间已满。标签仍保留在当前页面内存中，请关闭页面前导出审阅 JSON。");
      }
    }

    function loadLabels() {
      try {
        localStorageHandle = window.localStorage;
        storageAvailable = true;
        const stored = localStorageHandle.getItem(storageKey);
        if (stored === null) return;
        labelsByTarget.clear();
        for (const [targetId, entry] of validateImport(JSON.parse(stored))) {
          labelsByTarget.set(targetId, entry);
        }
      } catch (error) {
        labelsByTarget.clear();
        storageAvailable = false;
        setStorageWarning("无法读取浏览器存储。标签仍保留在当前页面内存中并可导出。" + error.message);
      }
    }

    function appendText(parent, tagName, value, className) {
      const element = document.createElement(tagName);
      if (className) element.className = className;
      element.textContent = value;
      parent.appendChild(element);
      return element;
    }

    function formatJson(value) {
      return JSON.stringify(value, null, 2);
    }

    function appendJsonDetail(parent, title, value) {
      const section = document.createElement("section");
      section.className = "detail";
      appendText(section, "h3", title);
      appendText(section, "pre", formatJson(value));
      parent.appendChild(section);
    }

    function officialPoint(geometry) {
      if (geometry && geometry.type === "Point" && Array.isArray(geometry.coordinates) && geometry.coordinates.length >= 2) {
        const longitude = geometry.coordinates[0];
        const latitude = geometry.coordinates[1];
        if (typeof longitude === "number" && typeof latitude === "number") {
          return longitude + ", " + latitude + "（经度，纬度）";
        }
      }
      return "没有有效的官方点位。";
    }

    function initializeMap() {
      const caption = document.getElementById("map-caption");
      const mapElement = document.getElementById("review-map");
      if (!window.L) {
        caption.textContent = "地图组件未加载；官方坐标和 GeoJSON 仍可在每条记录中查看。";
        return;
      }
      try {
        mapInstance = window.L.map(mapElement, { scrollWheelZoom: false, minZoom: 11, maxZoom: 19 });
        mapInstance.setView([1.3521, 103.8198], 11);
        let loadedTiles = 0;
        let failedTiles = 0;
        tileLayer = window.L.tileLayer(
          "https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png",
          {
            minZoom: 11,
            maxZoom: 19,
            attribution: '<a href="https://www.onemap.gov.sg/" target="_blank" rel="noopener noreferrer"><img src="/vendor/onemap-logo.svg" alt="OneMap" style="height:18px;vertical-align:middle"></a> © contributors | <a href="https://www.sla.gov.sg/" target="_blank" rel="noopener noreferrer">Singapore Land Authority</a>'
          }
        );
        tileLayer.on("tileload", () => {
          loadedTiles += 1;
          caption.textContent = "OneMap 在线底图 · 点表示官方广播中心，不表示淹水范围。";
        });
        tileLayer.on("tileerror", () => {
          failedTiles += 1;
          if (loadedTiles === 0 && failedTiles >= 2) {
            caption.textContent = "OneMap 底图加载失败；点位、原文和官方坐标仍可查看。";
          }
        });
        tileLayer.addTo(mapInstance);
        const bounds = [];
        review.records.forEach((record, index) => {
          const geometry = record.geometry;
          const coordinates = geometry && geometry.type === "Point" ? geometry.coordinates : null;
          if (!Array.isArray(coordinates) || coordinates.length !== 2 ||
              !Number.isFinite(coordinates[0]) || !Number.isFinite(coordinates[1])) return;
          const longitude = coordinates[0];
          const latitude = coordinates[1];
          const marker = window.L.circleMarker([latitude, longitude], {
            radius: 7, color: "#174f70", weight: 2, fillColor: "#5bb5d3", fillOpacity: 0.92
          }).addTo(mapInstance);
          const popup = document.createElement("div");
          appendText(popup, "strong", "记录 " + (index + 1));
          appendText(popup, "p", record.target_id);
          appendText(popup, "p", record.official.reading_raw.description ?? "未提供地点描述");
          appendText(popup, "p", officialPoint(geometry));
          marker.bindPopup(popup);
          marker.on("click", () => {
            const card = document.getElementById("record-card-" + index);
            if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
          });
          markersByTarget.set(record.target_id, marker);
          bounds.push([latitude, longitude]);
        });
        if (bounds.length > 1) {
          mapInstance.fitBounds(bounds, { padding: [24, 24], maxZoom: 13 });
        } else if (bounds.length === 1) {
          mapInstance.setView(bounds[0], 15);
        }
        document.getElementById("basemap-toggle").addEventListener("click", () => {
          if (mapInstance.hasLayer(tileLayer)) {
            mapInstance.removeLayer(tileLayer);
            caption.textContent = "离线点位图 · 官方点位和每条记录中的经纬度仍可查看。";
            document.getElementById("basemap-toggle").textContent = "启用 OneMap 底图";
          } else {
            tileLayer.addTo(mapInstance);
            caption.textContent = "正在加载 OneMap；如果底图不可用，点位和官方坐标仍可查看。";
            document.getElementById("basemap-toggle").textContent = "切换离线点位图";
          }
        });
        window.setTimeout(() => mapInstance.invalidateSize(), 0);
      } catch (error) {
        caption.textContent = "地图初始化失败；官方坐标仍可在每条记录中查看。";
      }
    }

    function refreshSummary() {
      const counts = { correct: 0, omitted: 0, wrong: 0, unjudgeable: 0 };
      for (const entry of labelsByTarget.values()) counts[entry.label] += 1;
      const labeled = labelsByTarget.size;
      const correctShare = labeled ? (counts.correct / labeled * 100).toFixed(1) + "%" : "暂无标签";
      const summaryData = review.run_summary || {};
      const statusCounts = summaryData.status_counts || {};
      const usage = summaryData.usage || {};
      const showUnknown = (value) => Number.isInteger(value) ? String(value) : "未知";
      const rows = [
        ["模型 / 思考强度", (summaryData.model || "未知") + " / " + (summaryData.reasoning_effort || "未知")],
        ["封账记录", String(review.records.length)],
        ["模型成功提取", showUnknown(statusCounts.success)],
        ["模型非成功", String((summaryData.failed_count ?? 0))],
        ["未尝试 / 结果未知", showUnknown(summaryData.not_attempted_count) + " / " + showUnknown(summaryData.outcome_unknown_count)],
        ["已发请求", showUnknown(summaryData.attempts)],
        ["输入 / 输出 token", showUnknown(usage.input_tokens) + " / " + showUnknown(usage.output_tokens)],
        ["已标注记录", String(labeled)],
        ["未标注记录", String(review.records.length - labeled)],
        ["正确标签 / 已标注", correctShare],
        ["正确 / 遗漏 / 错误 / 无法判断", [counts.correct, counts.omitted, counts.wrong, counts.unjudgeable].join(" / ")]
      ];
      const summary = document.getElementById("summary");
      summary.replaceChildren();
      for (const [name, value] of rows) {
        const entry = document.createElement("p");
        appendText(entry, "strong", name + ": ");
        entry.appendChild(document.createTextNode(value));
        summary.appendChild(entry);
      }
    }

    function collectEvidencePaths(value, paths = []) {
      if (!value || typeof value !== "object") return paths;
      if (Array.isArray(value)) {
        for (const item of value) collectEvidencePaths(item, paths);
        return paths;
      }
      for (const [key, item] of Object.entries(value)) {
        if (key === "path" && typeof item === "string") paths.push(item);
        else collectEvidencePaths(item, paths);
      }
      return paths;
    }

    function rawModelOutput(record) {
      const response = record.audit.raw_response;
      if (!response || typeof response !== "object") return null;
      if (typeof response.output_text === "string") return response.output_text;
      const parts = [];
      for (const output of Array.isArray(response.output) ? response.output : []) {
        for (const item of Array.isArray(output?.content) ? output.content : []) {
          if (item?.type === "output_text" && typeof item.text === "string") parts.push(item.text);
        }
      }
      return parts.length ? parts.join("\n") : null;
    }

    function refreshEvidenceDiagnostic() {
      const diagnostic = document.getElementById("evidence-diagnostic");
      const mismatches = review.records.filter((record) => {
        if (record.audit.status !== "evidence_error" || typeof record.audit.error !== "string") return false;
        const text = rawModelOutput(record);
        if (!text) return false;
        try {
          const paths = collectEvidencePaths(JSON.parse(text));
          return paths.some((path) => path.startsWith("/") && record.audit.error.includes(path));
        } catch (_) {
          return false;
        }
      });
      if (!mismatches.length) return;
      diagnostic.style.display = "block";
      diagnostic.textContent = `路径格式诊断：${mismatches.length} 条证据校验失败的原始输出使用了斜线形式的 JSON Pointer（如 /description），而当前校验器按计划中的 $.description 形式查找。此项说明路径格式不一致；不能据此判断语义正确或错误。原始模型输出已保留，请结合原文逐条复核。`;
    }

    function makeRecordCard(record, index) {
      const card = document.createElement("article");
      card.className = "card";
      card.id = "record-card-" + index;
      appendText(card, "h2", "记录 " + (index + 1) + " — " + record.target_id);
      const source = record.source;
      const sourceLine = [
        "来源：" + (source.source ?? "未知"),
        "快照 " + (source.snapshot_id ?? "未知"),
        "记录 " + (source.record_index ?? "未知"),
        "reading " + (source.reading_index ?? "未知"),
        "类型 " + (source.msg_type_raw ?? "未知"),
        "时间 " + (source.record_datetime ?? "未知")
      ].join(" · ");
      appendText(card, "p", sourceLine, "muted");

      const details = document.createElement("div");
      details.className = "detail-grid";
      appendJsonDetail(details, "来源身份", source);
      appendJsonDetail(details, "原始 reading", record.official.reading_raw);
      appendJsonDetail(details, "模型提取与证据", record.semantic);
      appendJsonDetail(details, "官方几何", record.geometry);
      appendText(details, "p", "官方坐标：" + officialPoint(record.geometry), "muted detail");
      appendJsonDetail(details, "调用状态", {
        status: record.audit.status ?? null,
        error: record.audit.error ?? null,
        attempts: record.audit.attempts ?? null
      });
      if (record.audit.status !== "success" && record.audit.raw_response) {
        const modelOutput = rawModelOutput(record);
        appendJsonDetail(details, "未通过自动校验的原始模型输出", modelOutput ?? record.audit.raw_response);
      }
      card.appendChild(details);

      const locateButton = document.createElement("button");
      locateButton.type = "button";
      locateButton.textContent = "地图定位";
      locateButton.disabled = !markersByTarget.has(record.target_id);
      locateButton.addEventListener("click", () => {
        const marker = markersByTarget.get(record.target_id);
        if (!mapInstance || !marker) return;
        mapInstance.flyTo(marker.getLatLng(), Math.max(15, mapInstance.getZoom()));
        marker.openPopup();
        document.getElementById("review-map").scrollIntoView({ behavior: "smooth", block: "center" });
      });
      card.appendChild(locateButton);

      const labelField = document.createElement("label");
      labelField.htmlFor = "label-" + index;
      labelField.textContent = "记录标签";
      card.appendChild(labelField);
      const select = document.createElement("select");
      select.id = "label-" + index;
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "未标注";
      select.appendChild(blank);
      for (const label of ["correct", "omitted", "wrong", "unjudgeable"]) {
        const option = document.createElement("option");
        option.value = label;
        option.textContent = ({ correct: "正确", omitted: "遗漏", wrong: "错误", unjudgeable: "无法判断" })[label];
        select.appendChild(option);
      }
      const notesLabel = document.createElement("label");
      notesLabel.htmlFor = "notes-" + index;
      notesLabel.textContent = "备注（最多 2,000 字符）";
      const notes = document.createElement("textarea");
      notes.id = "notes-" + index;
      notes.maxLength = 2000;
      notes.disabled = true;
      const prior = labelsByTarget.get(record.target_id);
      if (prior) {
        select.value = prior.label;
        notes.value = prior.remarks;
        notes.disabled = false;
      }
      select.addEventListener("change", () => {
        if (!select.value) {
          labelsByTarget.delete(record.target_id);
          notes.value = "";
          notes.disabled = true;
        } else {
          labelsByTarget.set(record.target_id, { label: select.value, remarks: notes.value });
          notes.disabled = false;
        }
        refreshSummary();
        persistLabels();
      });
      notes.addEventListener("input", () => {
        if (labelsByTarget.has(record.target_id)) {
          labelsByTarget.set(record.target_id, {
            label: select.value,
            remarks: notes.value.slice(0, 2000)
          });
          persistLabels();
        }
      });
      card.appendChild(select);
      card.appendChild(notesLabel);
      card.appendChild(notes);
      controlsByTarget.set(record.target_id, { select, notes });
      return card;
    }

    function refreshCards() {
      for (const [targetId, controls] of controlsByTarget) {
        const entry = labelsByTarget.get(targetId);
        controls.select.value = entry ? entry.label : "";
        controls.notes.value = entry ? entry.remarks : "";
        controls.notes.disabled = !entry;
      }
      refreshSummary();
    }

    function exportLabels() {
      const contents = JSON.stringify(makeExportPayload(), null, 2) + "\n";
      const url = URL.createObjectURL(new Blob([contents], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "reading-review.json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        importStatus.textContent = "审阅 JSON 已导出。";
    }

    function importLabels() {
      try {
        const parsed = JSON.parse(document.getElementById("import-json").value);
        const validated = validateImport(parsed);
        labelsByTarget.clear();
        for (const [targetId, entry] of validated) labelsByTarget.set(targetId, entry);
        refreshCards();
        persistLabels();
        importStatus.textContent = "已导入 " + validated.size + " 条记录标签。";
      } catch (error) {
        importStatus.textContent = "导入被拒绝：" + error.message;
      }
    }

    loadLabels();
    document.getElementById("run-id").textContent = review.run_id;
    initializeMap();
    const recordsContainer = document.getElementById("records");
    review.records.forEach((record, index) => recordsContainer.appendChild(makeRecordCard(record, index)));
    refreshSummary();
    refreshEvidenceDiagnostic();
    document.getElementById("import-button").addEventListener("click", importLabels);
    document.getElementById("export-button").addEventListener("click", exportLabels);
  })();
  </script>
</body>
</html>
'''


if __name__ == "__main__":
    raise SystemExit(main())
