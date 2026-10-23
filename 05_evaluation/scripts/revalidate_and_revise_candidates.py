#!/usr/bin/env python3
"""현장검수 피드백을 반영한 후보 모델 타당성 감사 및 v2 후보 재산정.

핵심 변경:
- 고령인구 총수 대신 행정동 면적당 만 65세 이상 인구밀도를 함께 사용한다.
- 고밀도(상위 25%) 지역은 300m, 그 외 지역은 500m 공백 기준을 적용한다.
- 의료·노인복지 목적지 또는 경로당·노인복지 출발 근거가 없는
  '일반 버스 수요 + 행정동 중심점' 후보는 1차 우선목록에서 제외한다.
- 기존 54개 현장검수 결과를 physical_id로 자동 승계한다.
- 실제 기준 초과 부분만 잘라 GeoJSON으로 저장한다.

결과는 최종 설치 위치가 아니라 재검토 우선 구간이다.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
MODEL_SCRIPT = ROOT / "04_modeling" / "scripts" / "model_expected_walk_and_bench_gaps.py"
PREP_SCRIPT = ROOT / "03_data_preparation" / "scripts" / "prepare_analysis_layers.py"
MODEL_OUT = ROOT / "04_modeling" / "outputs"
PREP_OUT = ROOT / "03_data_preparation" / "outputs"
OUT = ROOT / "05_evaluation" / "outputs"
QA = ROOT / "05_evaluation" / "qa"
REPORT = ROOT / "05_evaluation" / "07_모델_타당성_재검증_보고서.md"
REVIEW = ROOT / "dalseo_field_review_completed_54.csv"

DENSITY_QUANTILE = 0.75
TOP_N_PER_TIER = 20
SCORE_MIXES = {
    "SENIOR_EVIDENCE_FOCUSED": (0.50, 0.30, 0.20),
    "BALANCED": (1 / 3, 1 / 3, 1 / 3),
    "DENSITY_FOCUSED": (0.30, 0.50, 0.20),
}
SENIOR_ROUTE_COLUMNS = [
    "destination_medical_routes_800m",
    "destination_senior_welfare_routes_800m",
    "origin_senior_center_routes_800m",
    "origin_senior_welfare_routes_800m",
]
SUPPORT_ROUTE_COLUMNS = [
    "destination_park_routes_800m",
    "destination_bus_stop_routes_800m",
]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"모듈을 불러올 수 없음: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODEL = load_module("dalseo_model", MODEL_SCRIPT)
PREP = load_module("dalseo_prep", PREP_SCRIPT)


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def ring_area(ring: list[list[float]]) -> float:
    return abs(
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])
        )
    ) / 2


def geometry_area(geometry: dict) -> float:
    polygons = (
        geometry["coordinates"]
        if geometry["type"] == "MultiPolygon"
        else [geometry["coordinates"]]
    )
    return sum(
        max(0.0, ring_area(polygon[0]) - sum(ring_area(hole) for hole in polygon[1:]))
        for polygon in polygons
    )


def point_in_geometry(x: float, y: float, geometry: dict) -> bool:
    polygons = (
        geometry["coordinates"]
        if geometry["type"] == "MultiPolygon"
        else [geometry["coordinates"]]
    )
    return any(
        PREP.point_in_ring(x, y, polygon[0])
        and not any(PREP.point_in_ring(x, y, hole) for hole in polygon[1:])
        for polygon in polygons
    )


def load_admin_density() -> tuple[pd.DataFrame, list[dict], float]:
    payload = json.loads(
        (PREP_OUT / "admin_dong_population_epsg5179.geojson").read_text(encoding="utf-8")
    )
    rows = []
    for feature in payload["features"]:
        props = feature["properties"]
        area_km2 = geometry_area(feature["geometry"]) / 1_000_000
        rows.append(
            {
                "admin_dong": props["admin_dong"],
                "senior_65plus_total": int(props["senior_65plus_total"]),
                "admin_area_km2": area_km2,
                "senior_density_km2": float(props["senior_65plus_total"]) / area_km2,
                "geometry": feature["geometry"],
            }
        )
    frame = pd.DataFrame(rows)
    threshold = float(frame["senior_density_km2"].quantile(DENSITY_QUANTILE))
    return frame.drop(columns="geometry"), rows, threshold


def assign_admin_density(
    physical: pd.DataFrame,
    density_rows: list[dict],
    density_threshold: float,
) -> pd.DataFrame:
    result = physical.copy()
    midpoints = [MODEL.point_along(coords) for coords in result["coords"]]
    result["midpoint_x_5179"] = [point[0] for point in midpoints]
    result["midpoint_y_5179"] = [point[1] for point in midpoints]
    assigned = []
    for x, y in zip(result["midpoint_x_5179"], result["midpoint_y_5179"]):
        match = next(
            (
                row
                for row in density_rows
                if point_in_geometry(float(x), float(y), row["geometry"])
            ),
            None,
        )
        assigned.append(match)
    result["admin_dong"] = [row["admin_dong"] if row else "" for row in assigned]
    result["senior_65plus_total"] = [
        row["senior_65plus_total"] if row else np.nan for row in assigned
    ]
    result["admin_area_km2"] = [
        row["admin_area_km2"] if row else np.nan for row in assigned
    ]
    result["senior_density_km2"] = [
        row["senior_density_km2"] if row else np.nan for row in assigned
    ]
    result["density_tier"] = np.where(
        result["senior_density_km2"].ge(density_threshold),
        "DENSE_300M",
        "STANDARD_500M",
    )
    result.loc[result["admin_dong"].eq(""), "density_tier"] = "OUTSIDE_OR_UNASSIGNED"
    return result


def add_network_context(physical: pd.DataFrame, edges: pd.DataFrame) -> pd.DataFrame:
    """원 모델 적재 범위 밖의 서비스도로 세부 태그를 physical_id에 보강한다."""
    with sqlite3.connect(MODEL.NETWORK) as connection:
        tags = pd.read_sql_query("SELECT fid, service FROM edges", connection)
    tagged = edges[["fid", "physical_id"]].merge(tags, on="fid", how="left")
    parking_ids = set(
        tagged.loc[tagged["service"].eq("parking_aisle"), "physical_id"].astype(str)
    )
    result = physical.copy()
    result["has_parking_aisle"] = result["physical_id"].astype(str).isin(parking_ids)
    return result


def nearest_euclidean(
    candidate_lon: np.ndarray,
    candidate_lat: np.ndarray,
    reference: pd.DataFrame,
) -> np.ndarray:
    lat0 = math.radians(float(np.nanmean(candidate_lat)))
    points = np.column_stack(
        [candidate_lon * 111_320 * math.cos(lat0), candidate_lat * 110_540]
    )
    refs = np.column_stack(
        [
            reference["lon"].to_numpy(float) * 111_320 * math.cos(lat0),
            reference["lat"].to_numpy(float) * 110_540,
        ]
    )
    distances = np.sqrt(((points[:, None, :] - refs[None, :, :]) ** 2).sum(axis=2))
    return distances.min(axis=1)


def percentile_component(series: pd.Series) -> pd.Series:
    return np.log1p(series.clip(lower=0)).rank(method="average", pct=True)


def build_audit(
    review: pd.DataFrame,
    overlap: pd.DataFrame,
    physical_density: pd.DataFrame,
) -> pd.DataFrame:
    evidence_columns = [
        "physical_id",
        "route_weight_800m",
        *SENIOR_ROUTE_COLUMNS,
        *SUPPORT_ROUTE_COLUMNS,
        "origin_admin_population_routes_800m",
    ]
    evidence = overlap[evidence_columns].groupby("physical_id", as_index=False).max()
    density = physical_density[
        [
            "physical_id",
            "admin_dong",
            "senior_65plus_total",
            "admin_area_km2",
            "senior_density_km2",
            "density_tier",
        ]
    ]
    audit = review.merge(evidence, on="physical_id", how="left").merge(
        density, on="physical_id", how="left"
    )
    route_columns = SENIOR_ROUTE_COLUMNS + SUPPORT_ROUTE_COLUMNS + [
        "origin_admin_population_routes_800m"
    ]
    audit[route_columns] = audit[route_columns].fillna(0)
    audit["senior_specific_route_count"] = audit[SENIOR_ROUTE_COLUMNS].sum(axis=1)
    audit["general_support_route_count"] = audit[SUPPORT_ROUTE_COLUMNS].sum(axis=1)
    audit["weak_senior_demand_evidence"] = audit["senior_specific_route_count"].eq(0)
    audit["bus_admin_only_pattern"] = (
        audit["destination_bus_stop_routes_800m"].gt(0)
        & audit["origin_admin_population_routes_800m"].gt(0)
        & audit["senior_specific_route_count"].eq(0)
    )

    seats = read_csv(PREP_OUT / "resting_seats.csv")
    seats = seats[seats["active_supply"].eq(1)].copy()
    audit["euclidean_nearest_park_bench_m"] = nearest_euclidean(
        audit["midpoint_lon"].to_numpy(float),
        audit["midpoint_lat"].to_numpy(float),
        seats[seats["source_type"].eq("park_bench")],
    )
    audit["euclidean_nearest_bus_stop_seat_m"] = nearest_euclidean(
        audit["midpoint_lon"].to_numpy(float),
        audit["midpoint_lat"].to_numpy(float),
        seats[seats["source_type"].eq("bus_stop_seat")],
    )
    audit["supply_display_warning"] = np.where(
        audit[
            ["euclidean_nearest_park_bench_m", "euclidean_nearest_bus_stop_seat_m"]
        ].min(axis=1).le(300),
        "SEAT_WITHIN_300M_OF_EDGE_MIDPOINT_CHECK_EXACT_GAP_SEGMENT",
        "",
    )
    audit["v2_demand_gate"] = np.where(
        audit["senior_specific_route_count"].gt(0),
        "PASS_SENIOR_SPECIFIC_ROUTE",
        "EXCLUDE_FROM_PRIMARY_WEAK_SENIOR_EVIDENCE",
    )
    return audit


def score_candidates(
    overlap: pd.DataFrame,
    physical_density: pd.DataFrame,
    field_review: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    density_columns = [
        "physical_id",
        "midpoint_x_5179",
        "midpoint_y_5179",
        "admin_dong",
        "senior_65plus_total",
        "admin_area_km2",
        "senior_density_km2",
        "density_tier",
        "has_parking_aisle",
    ]
    candidates = overlap.merge(
        physical_density[density_columns], on="physical_id", how="left"
    )
    candidates["senior_specific_route_count"] = candidates[SENIOR_ROUTE_COLUMNS].sum(
        axis=1
    )
    candidates["general_support_route_count"] = candidates[SUPPORT_ROUTE_COLUMNS].sum(
        axis=1
    )
    candidates["senior_specific_destination_route_count"] = candidates[
        [
            "destination_medical_routes_800m",
            "destination_senior_welfare_routes_800m",
        ]
    ].sum(axis=1)
    candidates["adaptive_threshold_m"] = np.where(
        candidates["density_tier"].eq("DENSE_300M"), 300, 500
    )
    candidates["adaptive_gap_length_m"] = np.where(
        candidates["adaptive_threshold_m"].eq(300),
        candidates["gap_length_300m"],
        candidates["gap_length_500m"],
    )
    candidates["adaptive_gap_severity"] = np.where(
        candidates["adaptive_threshold_m"].eq(300),
        candidates["gap_severity_300m"],
        candidates["gap_severity_500m"],
    )
    eligible = candidates[
        candidates["intersects_dalseo"].eq(1)
        & candidates["route_weight_800m"].gt(0)
        & candidates["adaptive_gap_length_m"].gt(0)
        & candidates["density_tier"].isin(["DENSE_300M", "STANDARD_500M"])
        & candidates["senior_specific_route_count"].gt(0)
        & (
            candidates["density_tier"].eq("DENSE_300M")
            | candidates["senior_specific_destination_route_count"].gt(0)
        )
        & ~candidates["has_parking_aisle"].fillna(False)
    ].copy()

    review_lookup = (
        field_review.sort_values("field_review_unique_id")
        .drop_duplicates("physical_id")
        .set_index("physical_id")
    )
    eligible["field_review_status"] = eligible["physical_id"].map(
        review_lookup["review_status"]
    )
    eligible["field_review_status"] = eligible["field_review_status"].fillna(
        "NEEDS_REVIEW"
    )
    eligible = eligible[~eligible["field_review_status"].eq("NOT_INSTALLABLE")].copy()

    ranking_parts = []
    for (scenario, density_tier), group in eligible.groupby(
        ["scenario", "density_tier"]
    ):
        for mix_name, (senior_weight, density_weight, support_weight) in SCORE_MIXES.items():
            ranked = group.copy()
            senior_component = percentile_component(
                ranked["senior_specific_route_count"]
            )
            density_component = ranked["senior_density_km2"].rank(
                method="average", pct=True
            )
            support_component = percentile_component(
                ranked["general_support_route_count"]
            )
            demand_component = (
                senior_component.pow(senior_weight)
                * density_component.pow(density_weight)
                * support_component.pow(support_weight)
            )
            gap_component = percentile_component(ranked["adaptive_gap_severity"])
            ranked["score_mix"] = mix_name
            ranked["senior_weight"] = senior_weight
            ranked["density_weight"] = density_weight
            ranked["support_weight"] = support_weight
            ranked["v2_demand_component"] = demand_component
            ranked["v2_gap_component"] = gap_component
            ranked["v2_priority_score"] = np.sqrt(demand_component * gap_component)
            ranked["v2_priority_rank"] = ranked["v2_priority_score"].rank(
                method="min", ascending=False
            )
            ranked["selected_top_n"] = ranked["v2_priority_rank"].le(TOP_N_PER_TIER)
            ranking_parts.append(ranked)

    rankings = pd.concat(ranking_parts, ignore_index=True)
    selected = rankings[rankings["selected_top_n"]].copy()
    consensus = (
        selected.groupby(["density_tier", "physical_id"], as_index=False)
        .agg(
            sensitivity_selection_count=("selected_top_n", "size"),
            best_v2_rank=("v2_priority_rank", "min"),
            median_v2_rank=("v2_priority_rank", "median"),
            mean_v2_score=("v2_priority_score", "mean"),
            scenario_count=("scenario", "nunique"),
            score_mix_count=("score_mix", "nunique"),
            name=("name", "first"),
            highway=("highway", "first"),
            admin_dong=("admin_dong", "first"),
            senior_65plus_total=("senior_65plus_total", "first"),
            senior_density_km2=("senior_density_km2", "first"),
            adaptive_threshold_m=("adaptive_threshold_m", "first"),
            adaptive_gap_length_m=("adaptive_gap_length_m", "max"),
            nearest_seat_max_m=("nearest_seat_max_m", "max"),
            route_count_800m=("route_count_800m", "max"),
            route_weight_800m=("route_weight_800m", "max"),
            senior_specific_route_count=("senior_specific_route_count", "max"),
            senior_specific_destination_route_count=(
                "senior_specific_destination_route_count",
                "max",
            ),
            general_support_route_count=("general_support_route_count", "max"),
            destination_medical_routes_800m=(
                "destination_medical_routes_800m",
                "max",
            ),
            destination_senior_welfare_routes_800m=(
                "destination_senior_welfare_routes_800m",
                "max",
            ),
            destination_park_routes_800m=("destination_park_routes_800m", "max"),
            destination_bus_stop_routes_800m=(
                "destination_bus_stop_routes_800m",
                "max",
            ),
            origin_senior_center_routes_800m=(
                "origin_senior_center_routes_800m",
                "max",
            ),
            origin_senior_welfare_routes_800m=(
                "origin_senior_welfare_routes_800m",
                "max",
            ),
            field_review_status=("field_review_status", "first"),
            midpoint_x_5179=("midpoint_x_5179", "first"),
            midpoint_y_5179=("midpoint_y_5179", "first"),
        )
        .sort_values(
            ["density_tier", "sensitivity_selection_count", "mean_v2_score"],
            ascending=[True, False, False],
        )
    )
    consensus["v2_tier_rank"] = consensus.groupby("density_tier").cumcount() + 1
    lon, lat = MODEL.inverse_epsg5179(
        consensus["midpoint_x_5179"], consensus["midpoint_y_5179"]
    )
    consensus["midpoint_lon"] = lon
    consensus["midpoint_lat"] = lat
    consensus["kakao_map_url"] = [
        f"https://map.kakao.com/link/map/벤치후보v2,{latitude:.7f},{longitude:.7f}"
        for longitude, latitude in zip(lon, lat)
    ]
    return rankings, consensus


def interpolate(coords: list[tuple[float, float]], measure: float) -> tuple[float, float]:
    remaining = max(0.0, measure)
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        if remaining <= length or length == 0:
            fraction = 0.0 if length == 0 else remaining / length
            return x1 + fraction * (x2 - x1), y1 + fraction * (y2 - y1)
        remaining -= length
    return coords[-1]


def line_substring(
    coords: list[tuple[float, float]], start: float, end: float
) -> list[tuple[float, float]]:
    total = MODEL.polyline_length(coords)
    start = min(max(0.0, start), total)
    end = min(max(start, end), total)
    output = [interpolate(coords, start)]
    cumulative = 0.0
    for point_a, point_b in zip(coords, coords[1:]):
        segment = math.hypot(point_b[0] - point_a[0], point_b[1] - point_a[1])
        cumulative += segment
        if start < cumulative < end:
            output.append(point_b)
    output.append(interpolate(coords, end))
    deduped = [output[0]]
    for point in output[1:]:
        if point != deduped[-1]:
            deduped.append(point)
    return deduped


def exact_gap_ranges(
    physical_id: str,
    physical_row: pd.Series,
    threshold: int,
    seat_snaps: pd.DataFrame,
    node_seat_distance: dict[int, float],
) -> list[tuple[float, float]]:
    length = float(physical_row["length_m"])
    interval_count = max(1, int(math.ceil(length / MODEL.EDGE_SAMPLE_INTERVAL_M)))
    interval_length = length / interval_count
    measures = [(index + 0.5) * interval_length for index in range(interval_count)]
    direct = seat_snaps[seat_snaps["physical_id"].eq(physical_id)]
    direct_seats = list(
        zip(
            direct["measure_from_node_a_m"].astype(float),
            direct["snap_distance_m"].astype(float),
        )
    )
    node_a_distance = node_seat_distance.get(int(physical_row["node_a"]), math.inf)
    node_b_distance = node_seat_distance.get(int(physical_row["node_b"]), math.inf)
    flags = []
    for measure in measures:
        distances = [
            node_a_distance + measure,
            node_b_distance + length - measure,
        ]
        distances.extend(abs(measure - seat_measure) + offset for seat_measure, offset in direct_seats)
        flags.append(min(distances) > threshold)
    ranges = []
    start_index = None
    for index, flag in enumerate(flags + [False]):
        if flag and start_index is None:
            start_index = index
        elif not flag and start_index is not None:
            ranges.append((start_index * interval_length, index * interval_length))
            start_index = None
    return ranges


def write_exact_gap_geojson(
    shortlist: pd.DataFrame,
    physical: pd.DataFrame,
    edges: pd.DataFrame,
    path: Path,
) -> None:
    physical_lookup = physical.set_index("physical_id")
    snaps = read_csv(MODEL_OUT / "point_to_walk_edge_snaps.csv")
    snaps = snaps[
        snaps["scenario"].eq("steps_excluded")
        & snaps["role"].eq("resting_seat")
    ].copy()
    allowed = MODEL.allowed_physical_ids(edges, "steps_excluded")
    adjacency = MODEL.build_undirected_adjacency(physical, allowed)
    node_distances = MODEL.multi_source_seat_distances(snaps, adjacency)
    features = []
    for _, row in shortlist.iterrows():
        physical_row = physical_lookup.loc[row["physical_id"]]
        ranges = exact_gap_ranges(
            row["physical_id"],
            physical_row,
            int(row["adaptive_threshold_m"]),
            snaps,
            node_distances,
        )
        segments = [
            line_substring(physical_row["coords"], start, end)
            for start, end in ranges
            if end > start
        ]
        wgs84_segments = []
        for segment in segments:
            lon, lat = MODEL.inverse_epsg5179(
                [point[0] for point in segment], [point[1] for point in segment]
            )
            wgs84_segments.append(
                [[float(longitude), float(latitude)] for longitude, latitude in zip(lon, lat)]
            )
        properties = {}
        for column, value in row.items():
            if isinstance(value, np.generic):
                value = value.item()
            if pd.isna(value):
                value = None
            properties[column] = value
        properties["exact_gap_part_count"] = len(wgs84_segments)
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": wgs84_segments,
                },
            }
        )
    payload = {
        "type": "FeatureCollection",
        "name": path.stem,
        "features": features,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_report(
    audit: pd.DataFrame,
    consensus: pd.DataFrame,
    density_threshold: float,
    seat_counts: dict[str, int],
) -> None:
    status_counts = audit["review_status"].value_counts()
    weak_count = int(audit["weak_senior_demand_evidence"].sum())
    bus_admin_only = int(audit["bus_admin_only_pattern"].sum())
    near_park = int(audit["euclidean_nearest_park_bench_m"].le(300).sum())
    near_bus = int(audit["euclidean_nearest_bus_stop_seat_m"].le(300).sum())
    old_ids = set(audit["physical_id"])
    top = consensus[consensus["v2_tier_rank"].le(TOP_N_PER_TIER)]
    inherited = int(top["physical_id"].isin(old_ids).sum())
    dense_count = int(top["density_tier"].eq("DENSE_300M").sum())
    standard_count = int(top["density_tier"].eq("STANDARD_500M").sum())
    text = f"""# 모델 타당성 재검증 보고서

## 결론

기존 54개 결과는 현장조사 초안으로는 사용할 수 있지만 최종 우선순위로 바로
사용하기에는 수정이 필요하다. 고령자 수요의 공간 표현과 후보 구간 표시 방식에서
사용자가 지적한 편향이 실제로 확인됐다.

## 54개 현장검수 결과

- 설치 가능: {int(status_counts.get("INSTALLABLE", 0))}개
- 조건부 가능: {int(status_counts.get("CONDITIONAL", 0))}개
- 설치 부적합: {int(status_counts.get("NOT_INSTALLABLE", 0))}개
- 고령자 특화 경로 근거가 전혀 없는 후보: {weak_count}개
- 일반 버스 목적지와 행정동 인구 중심점만으로 설명되는 후보: {bus_admin_only}개

## 질문별 검증

### 1. 고령자가 자주 갈 가능성이 높은 지역을 제대로 파악했는가?

부분적으로만 그렇다. 의료기관, 노인복지시설, 경로당은 사용했지만 행정동별
고령인구를 동 중심점 하나에 배치했고 면적당 밀도를 사용하지 않았다. 또한 의료,
노인복지, 공원, 전체 연령 버스 이용량 네 목적지 묶음에 동일한 총가중치를 주어
일반 버스 수요의 영향이 상대적으로 커질 수 있었다.

### 2. 공원 벤치와 버스정류장 의자를 제외했는가?

거리 계산에는 포함했다. 활성 공원 벤치 {seat_counts.get("park_bench", 0):,}개와
버스정류장 의자 {seat_counts.get("bus_stop_seat", 0):,}개가 네트워크 좌석 공급으로
사용됐다. 다만 도로의 일부만 기준거리를 초과해도 OSM 도로 전체를 후보로
표시하여, 좌석이 가까운 쪽까지 공백처럼 보였다. 실제로 후보 중점 300m 이내에
공원 벤치가 있는 후보는 {near_park}개, 정류장 의자가 있는 후보는 {near_bus}개였다.
v2에서는 기준을 초과한 실제 부분구간만 잘라 표시한다.

공원 벤치 위치 자료의 기준일은 2022년이므로 이후 철거·신설은 현장확인이
필요하다. 달서구 벤치 현황의 공원별 집계값은 대표좌표뿐이라 거리 공급으로
사용하지 않고 참고값으로 유지한다.

### 3. 고령자 밀집지역을 더 촘촘하게 볼 수 있는가?

가능하다. 행정동 면적당 만 65세 이상 인구밀도 상위 25% 경계는
{density_threshold:,.1f}명/㎢이며, 이 지역에는 300m 기준을 적용한다. 나머지
지역에는 500m 기준을 적용한다.

## v2 재산정 규칙

1. `DENSE_300M`: 고령인구 밀도 상위 25%, 300m 공백
2. `STANDARD_500M`: 그 외 지역, 500m 공백
3. 의료·노인복지 목적지 또는 경로당·노인복지 출발 경로가 최소 1개 있어야 함
4. 일반지역 500m 후보는 의료·노인복지 목적지 경로가 최소 1개 있어야 함
5. 일반 버스 수요만으로는 1차 후보가 될 수 없음
6. 주차장 내부 통로(`service=parking_aisle`)는 자동 제외
7. 수요 점수는 고령자 특화 경로, 고령인구 밀도, 공원·버스 보조근거의 세
   가중치 조합으로 민감도 검증
8. 기존 현장검수에서 `NOT_INSTALLABLE`인 동일 도로는 자동 제외
9. 지도에는 전체 OSM 도로가 아니라 기준 초과 부분구간만 표시

## 재산정 산출물

- 고밀도 300m 검토목록: {dense_count}개
- 일반지역 500m 검토목록: {standard_count}개
- 이 중 기존 54개 현장검수와 일치: {inherited}개
- 나머지는 새 모델에서 처음 나타난 구간이므로 현장검수 전에는 최종후보로
  해석하지 않는다.
"""
    REPORT.write_text(text, encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    QA.mkdir(parents=True, exist_ok=True)
    if not REVIEW.exists():
        raise FileNotFoundError(f"54개 검수 파일이 없음: {REVIEW}")

    review = read_csv(REVIEW)
    overlap = read_csv(MODEL_OUT / "route_overlap_and_bench_gap_edges.csv")
    _, edges, _, physical = MODEL.load_network()
    physical = add_network_context(physical, edges)
    density_frame, density_rows, density_threshold = load_admin_density()
    physical_density = assign_admin_density(physical, density_rows, density_threshold)

    audit = build_audit(review, overlap, physical_density)
    rankings, consensus = score_candidates(overlap, physical_density, review)
    shortlist = consensus[consensus["v2_tier_rank"].le(TOP_N_PER_TIER)].copy()

    audit.to_csv(
        OUT / "candidate_model_validity_audit_54.csv",
        index=False,
        encoding="utf-8-sig",
    )
    rankings.to_csv(
        OUT / "revised_priority_rankings_v2_all.csv",
        index=False,
        encoding="utf-8-sig",
    )
    consensus.to_csv(
        OUT / "revised_priority_candidates_v2.csv",
        index=False,
        encoding="utf-8-sig",
    )
    shortlist.to_csv(
        OUT / "revised_priority_top20_per_tier_v2.csv",
        index=False,
        encoding="utf-8-sig",
    )
    density_frame.assign(
        density_tier=np.where(
            density_frame["senior_density_km2"].ge(density_threshold),
            "DENSE_300M",
            "STANDARD_500M",
        )
    ).to_csv(
        OUT / "senior_density_thresholds_v2.csv",
        index=False,
        encoding="utf-8-sig",
    )
    write_exact_gap_geojson(
        shortlist,
        physical,
        edges,
        OUT / "revised_priority_top20_per_tier_v2_wgs84.geojson",
    )

    seats = read_csv(PREP_OUT / "resting_seats.csv")
    active_seats = seats[seats["active_supply"].eq(1)]
    seat_counts = active_seats["source_type"].value_counts().astype(int).to_dict()
    write_report(audit, consensus, density_threshold, seat_counts)

    checks = pd.DataFrame(
        [
            {
                "check_id": "V2-01",
                "status": "PASS" if len(review) == 54 else "FAIL",
                "detail": f"현장검수 {len(review)}개",
            },
            {
                "check_id": "V2-02",
                "status": "PASS"
                if not rankings["senior_specific_route_count"].eq(0).any()
                else "FAIL",
                "detail": "모든 v2 후보에 고령자 특화 경로 근거 존재",
            },
            {
                "check_id": "V2-03",
                "status": "PASS"
                if (
                    rankings.loc[
                        rankings["density_tier"].eq("DENSE_300M"),
                        "adaptive_threshold_m",
                    ].eq(300).all()
                    and rankings.loc[
                        rankings["density_tier"].eq("STANDARD_500M"),
                        "adaptive_threshold_m",
                    ].eq(500).all()
                )
                else "FAIL",
                "detail": "고밀도 300m·일반지역 500m",
            },
            {
                "check_id": "V2-04",
                "status": "PASS"
                if not rankings["field_review_status"].eq("NOT_INSTALLABLE").any()
                else "FAIL",
                "detail": "기존 현장 부적합 후보 자동 제외",
            },
            {
                "check_id": "V2-05",
                "status": "PASS"
                if shortlist.groupby("density_tier").size().le(TOP_N_PER_TIER).all()
                else "FAIL",
                "detail": "유형별 TOP N 상한",
            },
            {
                "check_id": "V2-06",
                "status": "PASS"
                if not rankings["has_parking_aisle"].fillna(False).any()
                else "FAIL",
                "detail": "주차장 내부 통로 자동 제외",
            },
            {
                "check_id": "V2-07",
                "status": "PASS"
                if rankings.loc[
                    rankings["density_tier"].eq("STANDARD_500M"),
                    "senior_specific_destination_route_count",
                ].gt(0).all()
                else "FAIL",
                "detail": "일반지역 후보는 의료·노인복지 목적지 근거 필수",
            },
        ]
    )
    checks.to_csv(QA / "revised_candidate_model_v2_checks.csv", index=False, encoding="utf-8-sig")
    if checks["status"].eq("FAIL").any():
        raise RuntimeError(checks.to_string(index=False))

    print(f"audit={len(audit)}")
    print(
        "shortlist="
        + ", ".join(
            f"{tier}:{count}"
            for tier, count in shortlist.groupby("density_tier").size().items()
        )
    )
    print(f"density_threshold={density_threshold:.3f}")
    print(f"report={REPORT}")


if __name__ == "__main__":
    main()
