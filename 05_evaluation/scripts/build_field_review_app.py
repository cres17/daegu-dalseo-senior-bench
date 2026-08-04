#!/usr/bin/env python3
"""v2 재산정 후보 40개의 현장조사 입력용 전체화면 지도를 생성한다."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "05_evaluation" / "outputs" / "revised_priority_top20_per_tier_v2.csv"
REVIEW_54 = ROOT / "dalseo_field_review_completed_54.csv"
GAP_GEOJSON = (
    ROOT
    / "05_evaluation"
    / "outputs"
    / "revised_priority_top20_per_tier_v2_wgs84.geojson"
)
RESTING_SEATS = ROOT / "03_data_preparation" / "outputs" / "resting_seats.csv"
PARKS = ROOT / "03_data_preparation" / "outputs" / "parks.csv"
MODEL_SCRIPT = (
    ROOT
    / "04_modeling"
    / "scripts"
    / "model_expected_walk_and_bench_gaps.py"
)
APP_DIR = ROOT / "05_evaluation" / "field_review_app"
APP_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT = APP_DIR / "index.html"


spec = importlib.util.spec_from_file_location("dalseo_modeling", MODEL_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError("Modeling 모듈을 불러올 수 없습니다.")
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)

review = pd.read_csv(INPUT, encoding="utf-8-sig").fillna("")
old_review = pd.read_csv(REVIEW_54, encoding="utf-8-sig").fillna("")
manual_fields = [
    "review_status",
    "existing_seat_verified",
    "continuous_sidewalk",
    "effective_width_m",
    "slope_condition",
    "crossing_risk",
    "shade_weather_protection",
    "ownership_installability",
    "recommended_segment_adjustment",
    "review_notes",
    "reviewer",
    "review_date",
]
old_lookup = old_review.drop_duplicates("physical_id").set_index("physical_id")
review["field_review_unique_id"] = [
    f"V2_{'300' if tier == 'DENSE_300M' else '500'}_{int(rank):03d}"
    for tier, rank in zip(review["density_tier"], review["v2_tier_rank"])
]
review["prior_review_included"] = review["field_review_status"].ne("NEEDS_REVIEW").map(
    {True: "YES", False: "NO"}
)
review["candidate_thresholds_m"] = review["adaptive_threshold_m"].astype(int).astype(str)
review["gap_length_300m"] = review["adaptive_gap_length_m"].where(
    review["adaptive_threshold_m"].eq(300), 0
)
review["gap_length_500m"] = review["adaptive_gap_length_m"].where(
    review["adaptive_threshold_m"].eq(500), 0
)
review["selection_drivers"] = "V2_ADAPTIVE_DENSITY_AND_SENIOR_EVIDENCE"
review["destination_evidence_type_count"] = (
    review[
        [
            "destination_medical_routes_800m",
            "destination_senior_welfare_routes_800m",
            "destination_park_routes_800m",
            "destination_bus_stop_routes_800m",
        ]
    ]
    .gt(0)
    .sum(axis=1)
)
for field in manual_fields:
    review[field] = [
        old_lookup.at[physical_id, field]
        if physical_id in old_lookup.index and field in old_lookup.columns
        else ""
        for physical_id in review["physical_id"]
    ]
review["name"] = review["name"].replace("", "도로명 없음")
review.to_csv(
    ROOT / "05_evaluation" / "outputs" / "field_review_v2_40_prefilled.csv",
    index=False,
    encoding="utf-8-sig",
)

gap_payload = json.loads(GAP_GEOJSON.read_text(encoding="utf-8"))
gap_geometry = {
    feature["properties"]["physical_id"]: feature["geometry"]
    for feature in gap_payload["features"]
}

features = []
for _, row in review.iterrows():
    physical_id = row["physical_id"]
    properties = {}
    for column, value in row.items():
        if hasattr(value, "item"):
            value = value.item()
        properties[column] = value
    features.append(
        {
            "type": "Feature",
            "properties": properties,
            "geometry": gap_geometry[physical_id],
        }
    )

candidate_xy = review[["midpoint_lon", "midpoint_lat"]].to_numpy(float)


def near_any_candidate(frame: pd.DataFrame, radius_m: float = 700.0) -> pd.Series:
    lat0 = float(review["midpoint_lat"].mean())
    lon_scale = 111_320 * __import__("math").cos(__import__("math").radians(lat0))
    ref_x = candidate_xy[:, 0] * lon_scale
    ref_y = candidate_xy[:, 1] * 110_540
    point_x = frame["lon"].to_numpy(float) * lon_scale
    point_y = frame["lat"].to_numpy(float) * 110_540
    minimum = (
        ((point_x[:, None] - ref_x[None, :]) ** 2 + (point_y[:, None] - ref_y[None, :]) ** 2)
        ** 0.5
    ).min(axis=1)
    return pd.Series(minimum <= radius_m, index=frame.index)


seats = pd.read_csv(RESTING_SEATS, encoding="utf-8-sig")
seats = seats[seats["active_supply"].eq(1)].copy()
seats = seats[near_any_candidate(seats)].copy()
parks = pd.read_csv(PARKS, encoding="utf-8-sig")
parks = parks[near_any_candidate(parks)].copy()
seat_points = [
    {
        "type": "Feature",
        "properties": {
            "source_type": row["source_type"],
            "name": row["name"],
            "place_name": row["place_name"],
            "seat_count": int(row["seat_count"]),
        },
        "geometry": {
            "type": "Point",
            "coordinates": [float(row["lon"]), float(row["lat"])],
        },
    }
    for _, row in seats.iterrows()
]
park_points = [
    {
        "type": "Feature",
        "properties": {"name": row["name"]},
        "geometry": {
            "type": "Point",
            "coordinates": [float(row["lon"]), float(row["lat"])],
        },
    }
    for _, row in parks.iterrows()
]

payload = {
    "type": "FeatureCollection",
    "generated_at": datetime.now().astimezone().isoformat(),
    "features": features,
}

html = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>달서구 벤치 후보 v2 재검수</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {
      color-scheme: light;
      --ink: #16202a;
      --muted: #66717c;
      --line: #d8dee5;
      --panel: #ffffff;
      --soft: #f5f7f9;
      --accent: #155eef;
      --ok: #1b7f4b;
      --warn: #b86400;
      --bad: #c92a2a;
      --revisit: #7356bf;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; color: var(--ink); background: var(--soft); }
    body { overflow: hidden; }
    button, input, select, textarea { font: inherit; }
    button { cursor: pointer; }
    .app { height: 100vh; display: grid; grid-template-rows: auto 1fr; }
    header {
      display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
      padding: 10px 14px; background: var(--panel); border-bottom: 1px solid var(--line);
      z-index: 1001;
    }
    header h1 { margin: 0; font-size: 18px; font-weight: 650; }
    .progress { color: var(--muted); font-size: 13px; }
    .toolbar { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }
    .btn {
      border: 1px solid var(--line); border-radius: 7px; background: var(--panel);
      color: var(--ink); padding: 7px 10px;
    }
    .btn:hover { background: #eef3ff; }
    .btn-primary { background: var(--accent); color: white; border-color: var(--accent); }
    .btn-danger { color: var(--bad); }
    .layout { min-height: 0; display: grid; grid-template-columns: 330px minmax(420px, 1fr) 390px; }
    aside { min-height: 0; background: var(--panel); }
    .list-panel { border-right: 1px solid var(--line); display: grid; grid-template-rows: auto 1fr; }
    .filters { padding: 10px; border-bottom: 1px solid var(--line); display: grid; gap: 8px; }
    .filter-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    input, select, textarea {
      width: 100%; border: 1px solid #bac3cd; border-radius: 6px; padding: 7px 8px;
      background: white; color: var(--ink);
    }
    textarea { min-height: 62px; resize: vertical; }
    .candidate-list { overflow: auto; padding: 8px; }
    .candidate {
      width: 100%; text-align: left; border: 1px solid var(--line); border-radius: 8px;
      background: white; padding: 9px; margin-bottom: 7px;
    }
    .candidate:hover, .candidate.selected { border-color: var(--accent); background: #f2f6ff; }
    .candidate-top { display: flex; gap: 7px; align-items: center; }
    .candidate-name { font-weight: 650; flex: 1; }
    .badge {
      display: inline-flex; align-items: center; border-radius: 999px; padding: 2px 7px;
      background: #edf1f5; font-size: 11px; color: #47515b;
    }
    .meta { color: var(--muted); font-size: 12px; line-height: 1.45; margin-top: 5px; }
    .status-dot { width: 9px; height: 9px; border-radius: 50%; background: #8b96a1; flex: 0 0 auto; }
    .status-INSTALLABLE { background: var(--ok); }
    .status-CONDITIONAL { background: var(--warn); }
    .status-NOT_INSTALLABLE { background: var(--bad); }
    .status-REVISIT { background: var(--revisit); }
    #map { min-width: 0; height: 100%; }
    .form-panel { border-left: 1px solid var(--line); overflow: auto; padding: 12px; }
    .empty { color: var(--muted); padding: 24px 8px; text-align: center; }
    .detail-head { border-bottom: 1px solid var(--line); padding-bottom: 10px; margin-bottom: 10px; }
    .detail-head h2 { font-size: 18px; margin: 0 0 7px; }
    .detail-links { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .detail-links a { color: var(--accent); font-size: 13px; }
    .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; margin: 10px 0; }
    .metric { background: var(--soft); padding: 8px; border-radius: 7px; }
    .metric-label { color: var(--muted); font-size: 11px; }
    .metric-value { font-size: 15px; margin-top: 2px; }
    .field { margin: 10px 0; }
    .field label { display: block; font-size: 12px; font-weight: 650; margin-bottom: 4px; }
    .required::after { content: " *"; color: var(--bad); }
    .hint { color: var(--muted); font-size: 11px; margin-top: 3px; line-height: 1.4; }
    .leaflet-popup-content { line-height: 1.5; }
    .leaflet-popup-content b { font-size: 14px; }
    .import-input { display: none; }
    .save-note {
      position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%);
      background: #14213d; color: white; padding: 7px 12px; border-radius: 7px;
      z-index: 2000; opacity: 0; pointer-events: none; transition: opacity .2s;
    }
    .save-note.show { opacity: .95; }
    @media (max-width: 1050px) {
      body { overflow: auto; }
      .app { height: auto; min-height: 100vh; }
      .layout { grid-template-columns: 300px 1fr; grid-template-rows: 64vh auto; }
      .form-panel { grid-column: 1 / -1; border-left: 0; border-top: 1px solid var(--line); }
      #map { min-height: 64vh; }
    }
    @media (max-width: 680px) {
      .layout { display: block; }
      .list-panel { height: 48vh; border-right: 0; }
      #map { height: 58vh; }
      .form-panel { min-height: 70vh; }
      header { align-items: flex-start; }
      .toolbar { margin-left: 0; width: 100%; }
    }
  </style>
</head>
<body>
<div class="app">
  <header>
    <div>
      <h1>달서구 벤치 후보 v2 · 실제 공백 부분구간</h1>
      <div class="progress" id="progressText">5 / 40 판정 승계 · 새 후보 35개 검토</div>
    </div>
    <div class="toolbar">
      <button class="btn" id="nextIncomplete">다음 미검토</button>
      <button class="btn" id="importButton">CSV 불러오기</button>
      <input class="import-input" id="importInput" type="file" accept=".csv,text/csv">
      <button class="btn btn-primary" id="exportCsv">완료 CSV 내보내기</button>
      <button class="btn" id="exportJson">백업 JSON</button>
      <button class="btn btn-danger" id="resetAll">입력 초기화</button>
    </div>
  </header>
  <div class="layout">
    <aside class="list-panel">
      <div class="filters">
        <input id="searchInput" type="search" placeholder="도로명 또는 검토 ID 검색">
        <div class="filter-row">
          <select id="thresholdFilter" aria-label="벤치 공백 기준">
            <option value="all">300m·500m 전체</option>
            <option value="300">고밀도 300m</option>
            <option value="500">일반지역 500m</option>
          </select>
          <select id="statusFilter" aria-label="검토 상태">
            <option value="new_only" selected>새로 검토할 35개</option>
            <option value="all">상태 전체</option>
            <option value="unreviewed">미검토</option>
            <option value="reviewed">판정 완료</option>
            <option value="INSTALLABLE">설치 가능</option>
            <option value="CONDITIONAL">조건부</option>
            <option value="NOT_INSTALLABLE">설치 부적합</option>
            <option value="REVISIT">재확인</option>
          </select>
        </div>
      </div>
      <div class="candidate-list" id="candidateList"></div>
    </aside>
    <main id="map" aria-label="현장조사 후보 지도"></main>
    <aside class="form-panel" id="formPanel">
      <div class="empty">왼쪽 후보를 선택하면 조사 입력란이 표시됩니다.</div>
    </aside>
  </div>
</div>
<div class="save-note" id="saveNote">자동 저장됨</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const featureCollection = __FEATURE_COLLECTION__;
const features = featureCollection.features;
const restingSeatCollection = __RESTING_SEATS__;
const parkCollection = __PARKS__;
const manualFields = [
  'review_status','existing_seat_verified','continuous_sidewalk','effective_width_m',
  'slope_condition','crossing_risk','shade_weather_protection',
  'ownership_installability','recommended_segment_adjustment','review_notes',
  'reviewer','review_date'
];
const storageKey = 'dalseo-field-review-v2-40-v1';
let saved = {};
try { saved = JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch (_) { saved = {}; }
let selectedId = null;
const layerById = new Map();
const cardById = new Map();

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'
  })[char]);
}
function current(feature) {
  const p = feature.properties;
  return {...p, ...(saved[p.physical_id] || {})};
}
function statusColor(status) {
  return ({INSTALLABLE:'#1b7f4b',CONDITIONAL:'#b86400',NOT_INSTALLABLE:'#c92a2a',REVISIT:'#7356bf'})[status] || '#7b8794';
}
function statusLabel(status) {
  return ({INSTALLABLE:'설치 가능',CONDITIONAL:'조건부',NOT_INSTALLABLE:'설치 부적합',REVISIT:'재확인'})[status] || '미검토';
}
function formatNumber(value, digits=0) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('ko-KR',{maximumFractionDigits:digits}) : '-';
}
function thresholds(p) { return String(p.candidate_thresholds_m).split('|').map(x => x.trim()); }
function saveState(showToast=true) {
  localStorage.setItem(storageKey, JSON.stringify(saved));
  if (showToast) {
    const note = document.getElementById('saveNote');
    note.classList.add('show');
    clearTimeout(note.timer);
    note.timer = setTimeout(() => note.classList.remove('show'), 900);
  }
  updateProgress();
}
function updateProgress() {
  const completed = features.filter(f => current(f).review_status).length;
  document.getElementById('progressText').textContent = `${completed} / ${features.length} 판정 완료 · 브라우저 자동저장`;
}

const map = L.map('map', {zoomControl:true}).setView([35.83,128.53],13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom:20, attribution:'© OpenStreetMap'
}).addTo(map);

const parkSeatLayer = L.geoJSON(restingSeatCollection, {
  filter: f => f.properties.source_type === 'park_bench',
  pointToLayer: (f, latlng) => L.circleMarker(latlng, {
    radius:4, color:'#b86400', weight:1, fillColor:'#f59f00', fillOpacity:.8
  }),
  onEachFeature: (f, layer) => layer.bindPopup(
    `<b>공원 벤치</b><br>${escapeHtml(f.properties.place_name || f.properties.name)}`
  )
}).addTo(map);
const busSeatLayer = L.geoJSON(restingSeatCollection, {
  filter: f => f.properties.source_type === 'bus_stop_seat',
  pointToLayer: (f, latlng) => L.circleMarker(latlng, {
    radius:4, color:'#155eef', weight:1, fillColor:'#4c83ff', fillOpacity:.8
  }),
  onEachFeature: (f, layer) => layer.bindPopup(
    `<b>정류장 의자</b><br>${escapeHtml(f.properties.name)} · ${formatNumber(f.properties.seat_count)}석`
  )
}).addTo(map);
const parkLayer = L.geoJSON(parkCollection, {
  pointToLayer: (f, latlng) => L.circleMarker(latlng, {
    radius:6, color:'#1b7f4b', weight:2, fillColor:'#56b881', fillOpacity:.25
  }),
  onEachFeature: (f, layer) => layer.bindPopup(`<b>공원</b><br>${escapeHtml(f.properties.name)}`)
}).addTo(map);
L.control.layers(null, {
  '공원 벤치': parkSeatLayer,
  '정류장 의자': busSeatLayer,
  '공원 중심': parkLayer
}, {collapsed:false}).addTo(map);

features.forEach(feature => {
  const p = feature.properties;
  const layer = L.geoJSON(feature, {
    style: () => ({color:statusColor(current(feature).review_status), weight:7, opacity:.88})
  }).addTo(map);
  layer.bindPopup(() => {
    const c = current(feature);
    return `<b>${escapeHtml(c.name)}</b><br>후보 기준: ${escapeHtml(c.candidate_thresholds_m)}m` +
      `<br>판정: ${escapeHtml(statusLabel(c.review_status))}` +
      `<br>행정동: ${escapeHtml(c.admin_dong)} · 고령밀도 ${formatNumber(c.senior_density_km2,1)}명/㎢` +
      `<br>고령자 특화 경로: ${formatNumber(c.senior_specific_route_count)}회` +
      `<br>실제 공백 길이: ${formatNumber(c.adaptive_gap_length_m,1)}m`;
  });
  layer.on('click', () => selectFeature(p.physical_id, false));
  layerById.set(p.physical_id, layer);
});
const allLayers = L.featureGroup([...layerById.values()]);
if (features.length) map.fitBounds(allLayers.getBounds(), {padding:[24,24]});

function matchesFilters(feature) {
  const p = current(feature);
  const query = document.getElementById('searchInput').value.trim().toLowerCase();
  const threshold = document.getElementById('thresholdFilter').value;
  const status = document.getElementById('statusFilter').value;
  if (query && !`${p.name} ${p.field_review_unique_id}`.toLowerCase().includes(query)) return false;
  if (threshold !== 'all' && !thresholds(p).includes(threshold)) return false;
  if (status === 'new_only' && p.prior_review_included !== 'NO') return false;
  if (status === 'unreviewed' && p.review_status) return false;
  if (status === 'reviewed' && !p.review_status) return false;
  if (!['all','new_only','unreviewed','reviewed'].includes(status) && p.review_status !== status) return false;
  return true;
}

function renderList() {
  const list = document.getElementById('candidateList');
  list.innerHTML = '';
  cardById.clear();
  const visible = features.filter(matchesFilters);
  for (const feature of visible) {
    const p = current(feature);
    const button = document.createElement('button');
    button.className = `candidate${p.physical_id === selectedId ? ' selected' : ''}`;
    button.type = 'button';
    button.innerHTML = `
      <div class="candidate-top">
        <span class="status-dot status-${escapeHtml(p.review_status)}"></span>
        <span class="candidate-name">${escapeHtml(p.name)}</span>
        <span class="badge">${escapeHtml(p.candidate_thresholds_m)}m</span>
      </div>
      <div class="meta">${escapeHtml(p.field_review_unique_id)} · ${escapeHtml(p.admin_dong)} · ${p.prior_review_included === 'NO' ? '새 검토' : '기존 판정 승계'} · ${escapeHtml(statusLabel(p.review_status))}
        <br>특화 경로 ${formatNumber(p.senior_specific_route_count)}회 · 실제 공백 ${formatNumber(p.adaptive_gap_length_m,1)}m
      </div>`;
    button.onclick = () => selectFeature(p.physical_id, true);
    list.appendChild(button);
    cardById.set(p.physical_id, button);
  }
  if (!visible.length) list.innerHTML = '<div class="empty">조건에 맞는 후보가 없습니다.</div>';
}

function selectFeature(id, zoom=true) {
  selectedId = id;
  renderList();
  renderForm();
  const layer = layerById.get(id);
  if (zoom && layer) {
    map.fitBounds(layer.getBounds(), {maxZoom:18, padding:[40,40]});
    layer.openPopup();
  }
  cardById.get(id)?.scrollIntoView({block:'nearest'});
}

function option(value, label) { return `<option value="${value}">${label}</option>`; }
function selectField(id, label, values, required=false) {
  return `<div class="field"><label for="${id}" class="${required?'required':''}">${label}</label>` +
    `<select id="${id}" data-field="${id}"><option value="">선택</option>` +
    values.map(v => option(v[0],v[1])).join('') + '</select></div>';
}
function renderForm() {
  const panel = document.getElementById('formPanel');
  const feature = features.find(f => f.properties.physical_id === selectedId);
  if (!feature) { panel.innerHTML = '<div class="empty">후보를 선택하세요.</div>'; return; }
  const p = current(feature);
  panel.innerHTML = `
    <div class="detail-head">
      <h2>${escapeHtml(p.name)}</h2>
      <div class="detail-links">
        <span class="badge">${escapeHtml(p.field_review_unique_id)}</span>
        <span class="badge">${escapeHtml(p.candidate_thresholds_m)}m 후보</span>
        <a href="${escapeHtml(p.kakao_map_url)}" target="_blank" rel="noopener">카카오맵에서 보기</a>
      </div>
      <div class="meta">${Number(p.midpoint_lat).toFixed(6)}, ${Number(p.midpoint_lon).toFixed(6)} · ${escapeHtml(p.highway)}</div>
    </div>
    <div class="metrics">
      <div class="metric"><div class="metric-label">고령인구 밀도</div><div class="metric-value">${formatNumber(p.senior_density_km2,1)}명/㎢</div></div>
      <div class="metric"><div class="metric-label">고령자 특화 경로</div><div class="metric-value">${formatNumber(p.senior_specific_route_count)}회</div></div>
      <div class="metric"><div class="metric-label">의료·복지 목적지 경로</div><div class="metric-value">${formatNumber(p.senior_specific_destination_route_count)}회</div></div>
      <div class="metric"><div class="metric-label">최대 좌석거리</div><div class="metric-value">${formatNumber(p.nearest_seat_max_m,1)}m</div></div>
      <div class="metric"><div class="metric-label">${escapeHtml(p.adaptive_threshold_m)}m 실제 공백 길이</div><div class="metric-value">${formatNumber(p.adaptive_gap_length_m,1)}m</div></div>
      <div class="metric"><div class="metric-label">민감도 선정</div><div class="metric-value">${formatNumber(p.sensitivity_selection_count)} / 6</div></div>
    </div>
    ${selectField('review_status','종합 판정',[
      ['INSTALLABLE','설치 가능'],['CONDITIONAL','조건부'],
      ['NOT_INSTALLABLE','설치 부적합'],['REVISIT','재확인']
    ],true)}
    ${selectField('existing_seat_verified','기존 좌석 현장 확인',[
      ['YES','있음'],['NO','없음'],['UNCERTAIN','불확실']
    ])}
    ${selectField('continuous_sidewalk','연속 보도',[
      ['YES','연속'],['PARTIAL','일부 단절'],['NO','없음']
    ])}
    <div class="field"><label for="effective_width_m">유효 보행 폭(m)</label>
      <input id="effective_width_m" data-field="effective_width_m" type="number" min="0" step="0.1" placeholder="예: 2.4">
    </div>
    ${selectField('slope_condition','경사',[
      ['FLAT','평탄'],['MODERATE','보통'],['STEEP','가파름'],['UNCERTAIN','불확실']
    ])}
    ${selectField('crossing_risk','횡단·차량 위험',[
      ['LOW','낮음'],['MEDIUM','보통'],['HIGH','높음'],['UNCERTAIN','불확실']
    ])}
    ${selectField('shade_weather_protection','그늘·기후 보호',[
      ['GOOD','양호'],['PARTIAL','일부'],['NONE','없음']
    ])}
    ${selectField('ownership_installability','소유권·설치 가능성',[
      ['PUBLIC_LIKELY','공공 설치 가능성 높음'],['PRIVATE_LIKELY','사유지 가능성 높음'],['UNCERTAIN','불확실']
    ])}
    <div class="field"><label for="recommended_segment_adjustment">후보 위치 조정</label>
      <textarea id="recommended_segment_adjustment" data-field="recommended_segment_adjustment" placeholder="예: 북쪽으로 약 40m 이동"></textarea>
    </div>
    <div class="field"><label for="review_notes">현장 메모</label>
      <textarea id="review_notes" data-field="review_notes" placeholder="좌석 상태, 불법주정차, 통행 방해, 사진 번호 등"></textarea>
    </div>
    <div class="filter-row">
      <div class="field"><label for="reviewer">검토자</label><input id="reviewer" data-field="reviewer"></div>
      <div class="field"><label for="review_date">검토일</label><input id="review_date" data-field="review_date" type="date"></div>
    </div>
    <div class="hint">입력 즉시 이 브라우저에 자동 저장됩니다. 작업 후 상단의 ‘완료 CSV 내보내기’를 눌러 파일로 보관하세요.</div>`;
  for (const field of manualFields) {
    const element = panel.querySelector(`[data-field="${field}"]`);
    if (!element) continue;
    element.value = p[field] ?? '';
    element.addEventListener('input', event => {
      saved[selectedId] = saved[selectedId] || {};
      saved[selectedId][field] = event.target.value;
      saveState();
      const layer = layerById.get(selectedId);
      layer?.setStyle({color:statusColor(current(feature).review_status)});
      renderList();
    });
  }
}

function csvEscape(value) {
  const text = String(value ?? '');
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g,'""')}"` : text;
}
function download(name, text, type) {
  const blob = new Blob([text], {type});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name; a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function exportRows() {
  return features.map(feature => {
    const p = current(feature);
    const row = {...feature.properties};
    manualFields.forEach(field => row[field] = p[field] ?? '');
    return row;
  });
}
function parseCsv(text) {
  const rows=[]; let row=[]; let field=''; let quoted=false;
  for (let i=0;i<text.length;i++) {
    const ch=text[i];
    if (quoted) {
      if (ch === '"' && text[i+1] === '"') { field+='"'; i++; }
      else if (ch === '"') quoted=false;
      else field+=ch;
    } else if (ch === '"') quoted=true;
    else if (ch === ',') { row.push(field); field=''; }
    else if (ch === '\n') { row.push(field.replace(/\r$/,'')); rows.push(row); row=[]; field=''; }
    else field+=ch;
  }
  if (field.length || row.length) { row.push(field.replace(/\r$/,'')); rows.push(row); }
  const headers=rows.shift() || [];
  return rows.filter(r => r.some(v => v !== '')).map(r => Object.fromEntries(headers.map((h,i)=>[h,r[i]??''])));
}

document.getElementById('exportCsv').onclick = () => {
  const rows=exportRows(); const headers=Object.keys(rows[0]);
  const csv='\ufeff' + [headers.join(','), ...rows.map(r=>headers.map(h=>csvEscape(r[h])).join(','))].join('\r\n');
  download('dalseo_v2_field_review_completed_40.csv',csv,'text/csv;charset=utf-8');
};
document.getElementById('exportJson').onclick = () => {
  download('dalseo_v2_field_review_backup.json',JSON.stringify(exportRows(),null,2),'application/json');
};
document.getElementById('importButton').onclick = () => document.getElementById('importInput').click();
document.getElementById('importInput').onchange = async event => {
  const file=event.target.files[0]; if (!file) return;
  const rows=parseCsv(await file.text()); let applied=0;
  for (const row of rows) {
    const id=row.physical_id;
    if (!id || !features.some(f=>f.properties.physical_id===id)) continue;
    saved[id]=saved[id] || {};
    manualFields.forEach(field => { if (field in row) saved[id][field]=row[field]; });
    applied++;
  }
  saveState(false); renderList(); renderForm();
  features.forEach(f => layerById.get(f.properties.physical_id)?.setStyle({color:statusColor(current(f).review_status)}));
  alert(`${applied}개 후보의 입력값을 불러왔습니다.`);
  event.target.value='';
};
document.getElementById('resetAll').onclick = () => {
  if (!confirm('이 브라우저에 저장된 현장조사 입력을 모두 지울까요? 내보낸 CSV는 삭제되지 않습니다.')) return;
  saved={}; localStorage.removeItem(storageKey); selectedId=null;
  features.forEach(f => layerById.get(f.properties.physical_id)?.setStyle({color:statusColor('')}));
  renderList(); renderForm(); updateProgress();
};
document.getElementById('nextIncomplete').onclick = () => {
  const next=features.find(f=>!current(f).review_status);
  if (next) selectFeature(next.properties.physical_id,true);
  else alert('모든 후보에 종합 판정이 입력됐습니다.');
};
['searchInput','thresholdFilter','statusFilter'].forEach(id => {
  document.getElementById(id).addEventListener('input',renderList);
  document.getElementById(id).addEventListener('change',renderList);
});

renderList();
updateProgress();
const initialFeature = features.find(matchesFilters) || features[0];
if (initialFeature) selectFeature(initialFeature.properties.physical_id,false);
</script>
</body>
</html>
"""
html = (
    html.replace("__FEATURE_COLLECTION__", json.dumps(payload, ensure_ascii=False))
    .replace(
        "__RESTING_SEATS__",
        json.dumps({"type": "FeatureCollection", "features": seat_points}, ensure_ascii=False),
    )
    .replace(
        "__PARKS__",
        json.dumps({"type": "FeatureCollection", "features": park_points}, ensure_ascii=False),
    )
)

OUTPUT.write_text(html, encoding="utf-8")
print(OUTPUT)
