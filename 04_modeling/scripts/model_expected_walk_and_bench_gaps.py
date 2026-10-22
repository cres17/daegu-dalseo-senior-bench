#!/usr/bin/env python3
"""달서구 고령자 예상 보행경로·벤치 공백 Modeling.

주의:
- 실제 고령자 GPS 경로가 아닌 시설·인구·정류소 기반 예상 경로이다.
- 버스 이용량은 전체 연령 대리변수다.
- 최종 설치 위치가 아니라 설치 우선 검토 구간을 만든다.
- 안전성·경사도·설치 가능성은 아직 최종 점수에 포함하지 않는다.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import sqlite3
import struct
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
PREP = ROOT / "03_data_preparation" / "outputs"
NETWORK = ROOT / "data_external" / "osmnx_dalseo_walk" / "qa" / "dalseo_walk_reviewed_epsg5179.gpkg"
OUT = ROOT / "04_modeling" / "outputs"
QA = ROOT / "04_modeling" / "qa"
OUT.mkdir(parents=True, exist_ok=True)
QA.mkdir(parents=True, exist_ok=True)

OD_RADII_M = (500, 800, 1200)
PRIMARY_OD_RADIUS_M = 800
BENCH_THRESHOLDS_M = (300, 500)
EDGE_SAMPLE_INTERVAL_M = 25.0
TOP_N = 20
HARD_EXCLUDE = {"EXCLUDE", "EXCLUDE_CONSERVATIVE", "NO_ROUTE_COMPONENT"}
SCENARIOS = {
    "steps_included": lambda action: action not in HARD_EXCLUDE,
    "steps_excluded": lambda action: action not in HARD_EXCLUDE and action != "PENALIZE_STEPS",
}


def nfc(value: object) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", str(value)).strip()


def read_csv(name: str, **kwargs: object) -> pd.DataFrame:
    return pd.read_csv(PREP / name, encoding="utf-8-sig", **kwargs)


def short_join(values: Iterable[object], limit: int = 8) -> str:
    unique = []
    for value in values:
        text = nfc(value)
        if text and text.lower() != "nan" and text not in unique:
            unique.append(text)
    result = " | ".join(unique[:limit])
    if len(unique) > limit:
        result += f" | …(+{len(unique) - limit})"
    return result


def gpkg_wkb_offset(blob: bytes) -> int:
    if blob[:2] != b"GP":
        return 0
    envelope = (blob[3] >> 1) & 0b111
    envelope_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(envelope)
    if envelope_bytes is None:
        raise ValueError(f"지원하지 않는 GeoPackage envelope indicator: {envelope}")
    return 8 + envelope_bytes


def decode_linestring(blob: bytes) -> list[tuple[float, float]]:
    offset = gpkg_wkb_offset(blob)
    endian = "<" if blob[offset] == 1 else ">"
    geometry_type = struct.unpack_from(f"{endian}I", blob, offset + 1)[0] % 1000
    if geometry_type != 2:
        raise ValueError(f"LINESTRING이 아닌 WKB type: {geometry_type}")
    count = struct.unpack_from(f"{endian}I", blob, offset + 5)[0]
    coordinates = []
    position = offset + 9
    for _ in range(count):
        x, y = struct.unpack_from(f"{endian}dd", blob, position)
        coordinates.append((float(x), float(y)))
        position += 16
    return coordinates


def polyline_length(coords: list[tuple[float, float]]) -> float:
    return sum(math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(coords, coords[1:]))


def meridional_arc(phi: np.ndarray | float, a: float, e2: float) -> np.ndarray | float:
    return a * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * phi
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * np.sin(2 * phi)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * np.sin(4 * phi)
        - (35 * e2**3 / 3072) * np.sin(6 * phi)
    )


def inverse_epsg5179(
    x: Iterable[float],
    y: Iterable[float],
) -> tuple[np.ndarray, np.ndarray]:
    """EPSG:5179를 WGS84 경위도로 변환한다."""
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lon0 = math.radians(127.5)
    lat0 = math.radians(38.0)
    k0 = 0.9996
    xx = np.asarray(list(x), dtype=float) - 1_000_000.0
    yy = np.asarray(list(y), dtype=float) - 2_000_000.0
    m0 = float(meridional_arc(lat0, a, e2))
    m = m0 + yy / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    fp = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * np.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * np.sin(4 * mu)
        + (151 * e1**3 / 96) * np.sin(6 * mu)
        + (1097 * e1**4 / 512) * np.sin(8 * mu)
    )
    c1 = ep2 * np.cos(fp) ** 2
    t1 = np.tan(fp) ** 2
    n1 = a / np.sqrt(1 - e2 * np.sin(fp) ** 2)
    r1 = a * (1 - e2) / (1 - e2 * np.sin(fp) ** 2) ** 1.5
    d = xx / (n1 * k0)
    lat = fp - (n1 * np.tan(fp) / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720
    )
    lon = lon0 + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120
    ) / np.cos(fp)
    return np.degrees(lon), np.degrees(lat)


def point_along(
    coords: list[tuple[float, float]],
    fraction: float = 0.5,
) -> tuple[float, float]:
    target = polyline_length(coords) * fraction
    cumulative = 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        segment_length = math.hypot(x2 - x1, y2 - y1)
        if cumulative + segment_length >= target:
            t = (target - cumulative) / segment_length if segment_length else 0
            return x1 + t * (x2 - x1), y1 + t * (y2 - y1)
        cumulative += segment_length
    return coords[-1]


def orient_to_u(
    coords: list[tuple[float, float]],
    u_xy: tuple[float, float],
) -> list[tuple[float, float]]:
    if math.hypot(coords[0][0] - u_xy[0], coords[0][1] - u_xy[1]) <= math.hypot(
        coords[-1][0] - u_xy[0], coords[-1][1] - u_xy[1]
    ):
        return coords
    return list(reversed(coords))


def physical_key(row: pd.Series, geom_length: float) -> str:
    u, v = int(row["u"]), int(row["v"])
    raw = f"{min(u,v)}|{max(u,v)}|{row['osmid']}|{round(geom_length,1)}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"PE_{min(u,v)}_{max(u,v)}_{digest}"


def load_network() -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    dict[int, str],
    pd.DataFrame,
]:
    with sqlite3.connect(NETWORK) as connection:
        nodes = pd.read_sql_query("SELECT osmid,x,y FROM nodes", connection)
        edges = pd.read_sql_query(
            """
            SELECT fid,geom,u,v,key,osmid,highway,name,length,intersects_dalseo,is_steps,
                   qa_effective_route_action,qa_effective_review_status
            FROM edges
            """,
            connection,
        )
    node_lookup = {
        int(osmid): (float(x), float(y))
        for osmid, x, y in zip(nodes["osmid"], nodes["x"], nodes["y"])
    }
    geometries = []
    geom_lengths = []
    physical_ids = []
    for _, row in edges.iterrows():
        coords = orient_to_u(decode_linestring(row["geom"]), node_lookup[int(row["u"])])
        length = polyline_length(coords)
        geometries.append(coords)
        geom_lengths.append(length)
        physical_ids.append(physical_key(row, length))
    edges["coords_u_to_v"] = geometries
    edges["geometry_length_m"] = geom_lengths
    edges["physical_id"] = physical_ids

    fid_to_physical = dict(zip(edges["fid"].astype(int), edges["physical_id"]))
    physical_rows = []
    for physical_id, group in edges.groupby("physical_id", sort=False):
        representative = group.iloc[0]
        u, v = int(representative["u"]), int(representative["v"])
        coords = representative["coords_u_to_v"]
        if u > v:
            coords = list(reversed(coords))
        physical_rows.append(
            {
                "physical_id": physical_id,
                "node_a": min(u, v),
                "node_b": max(u, v),
                "osmid": representative["osmid"],
                "highway": representative["highway"],
                "name": representative["name"],
                "length_m": float(representative["geometry_length_m"]),
                "intersects_dalseo": int(group["intersects_dalseo"].max()),
                "is_steps": int(group["is_steps"].max()),
                "actions": short_join(group["qa_effective_route_action"], limit=20),
                "coords": coords,
                "directed_fids": "|".join(map(str, group["fid"].astype(int).tolist())),
            }
        )
    physical = pd.DataFrame(physical_rows)
    return nodes, edges, fid_to_physical, physical


def make_origin_locations() -> pd.DataFrame:
    population = read_csv("senior_population_origin_points.csv")
    centers = read_csv("senior_centers.csv")
    welfare = read_csv("welfare.csv")
    pop_total = float(population["senior_65plus_total"].sum())
    rows = []
    for _, row in population.iterrows():
        rows.append(
            {
                "feature_id": row["feature_id"],
                "origin_type": "admin_population",
                "name": row["name"],
                "x": row["x_5179"],
                "y": row["y_5179"],
                # 고령인구 근거군 전체 질량=1
                "origin_weight_proxy": float(row["senior_65plus_total"]) / pop_total,
                "senior_65plus_total": row["senior_65plus_total"],
            }
        )
    for origin_type, frame in [("senior_center", centers), ("senior_welfare", welfare)]:
        for _, row in frame.iterrows():
            rows.append(
                {
                    "feature_id": row["feature_id"],
                    "origin_type": origin_type,
                    "name": row["name"],
                    "x": row["x_5179"],
                    "y": row["y_5179"],
                    # 경로당·노인복지시설 근거군도 각각 전체 질량=1
                    "origin_weight_proxy": 1.0 / len(frame),
                    "senior_65plus_total": 0,
                }
            )
    raw = pd.DataFrame(rows)
    raw["x_key"] = raw["x"].round(3)
    raw["y_key"] = raw["y"].round(3)
    grouped = (
        raw.groupby(["x_key", "y_key"], as_index=False)
        .agg(
            x=("x", "mean"),
            y=("y", "mean"),
            origin_types=("origin_type", short_join),
            names=("name", short_join),
            source_feature_ids=("feature_id", short_join),
            source_record_count=("feature_id", "size"),
            origin_weight_proxy=("origin_weight_proxy", "sum"),
            senior_65plus_total=("senior_65plus_total", "sum"),
        )
        .drop(columns=["x_key", "y_key"])
    )
    grouped.insert(0, "origin_id", [f"OR_{i+1:04d}" for i in range(len(grouped))])
    return grouped


def make_destination_locations() -> pd.DataFrame:
    frames = []
    for destination_type, filename in [
        ("senior_welfare", "welfare.csv"),
        ("medical", "medical.csv"),
        ("park", "parks.csv"),
        ("bus_stop", "bus_stops.csv"),
    ]:
        frame = read_csv(filename, dtype={"bs_id": str} if filename == "bus_stops.csv" else None)
        temp = pd.DataFrame(
            {
                "feature_id": frame["feature_id"],
                "destination_type": destination_type,
                "name": frame["name"],
                "x": frame["x_5179"],
                "y": frame["y_5179"],
                "annual_bus_users": frame["annual_total_users_proxy"]
                if "annual_total_users_proxy" in frame
                else 0,
                "demand_coverage_status": frame["demand_coverage_status"]
                if "demand_coverage_status" in frame
                else "NOT_APPLICABLE",
            }
        )
        frames.append(temp)
    raw = pd.concat(frames, ignore_index=True)
    raw["x_key"] = raw["x"].round(3)
    raw["y_key"] = raw["y"].round(3)
    grouped = (
        raw.groupby(["x_key", "y_key", "destination_type"], as_index=False)
        .agg(
            x=("x", "mean"),
            y=("y", "mean"),
            names=("name", short_join),
            source_feature_ids=("feature_id", short_join),
            source_record_count=("feature_id", "size"),
            annual_bus_users=("annual_bus_users", "max"),
            demand_coverage_status=("demand_coverage_status", short_join),
        )
        .drop(columns=["x_key", "y_key"])
    )
    grouped["destination_weight_proxy"] = 0.0
    bus_mask = grouped["destination_type"].eq("bus_stop")
    bus_logs = np.log1p(
        pd.to_numeric(grouped.loc[bus_mask, "annual_bus_users"], errors="coerce").fillna(0)
    )
    positive_mean = float(bus_logs[bus_logs > 0].mean())
    bus_mass = bus_logs.where(bus_logs > 0, positive_mean)
    grouped.loc[bus_mask, "destination_weight_proxy"] = bus_mass / float(bus_mass.sum())
    for destination_type in ["medical", "senior_welfare", "park"]:
        mask = grouped["destination_type"].eq(destination_type)
        grouped.loc[mask, "destination_weight_proxy"] = 1.0 / int(mask.sum())
    grouped["destination_weight_note"] = np.where(
        bus_mask,
        "bus_type_total_mass_1; log1p_2025_all_age_users; missing_id_uses_positive_mean",
        "destination_type_total_mass_1; equal_per_physical_location",
    )
    grouped.insert(0, "destination_id", [f"DE_{i+1:05d}" for i in range(len(grouped))])
    return grouped


class SegmentIndex:
    def __init__(self, physical: pd.DataFrame, allowed: set[str], cell_size: float = 200.0):
        self.cell_size = cell_size
        self.physical = physical.set_index("physical_id", drop=False)
        self.segments: list[tuple[str, float, float, float, float, float, float]] = []
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for _, edge in physical[physical["physical_id"].isin(allowed)].iterrows():
            cumulative = 0.0
            coords = edge["coords"]
            for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
                length = math.hypot(x2 - x1, y2 - y1)
                if length <= 0:
                    continue
                segment_id = len(self.segments)
                self.segments.append((edge["physical_id"], x1, y1, x2, y2, cumulative, length))
                min_cx = math.floor(min(x1, x2) / cell_size)
                max_cx = math.floor(max(x1, x2) / cell_size)
                min_cy = math.floor(min(y1, y2) / cell_size)
                max_cy = math.floor(max(y1, y2) / cell_size)
                for cx in range(min_cx, max_cx + 1):
                    for cy in range(min_cy, max_cy + 1):
                        self.grid[(cx, cy)].append(segment_id)
                cumulative += length

    @staticmethod
    def project(
        x: float,
        y: float,
        segment: tuple[str, float, float, float, float, float, float],
    ) -> tuple[float, float, float, float]:
        _, x1, y1, x2, y2, cumulative, length = segment
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (length * length)))
        px, py = x1 + t * dx, y1 + t * dy
        return math.hypot(x - px, y - py), px, py, cumulative + t * length

    def snap(self, x: float, y: float) -> dict[str, object]:
        base_x = math.floor(x / self.cell_size)
        base_y = math.floor(y / self.cell_size)
        seen: set[int] = set()
        best: tuple[float, float, float, float, str] | None = None
        for radius in range(0, 12):
            candidates = []
            for cx in range(base_x - radius, base_x + radius + 1):
                for cy in range(base_y - radius, base_y + radius + 1):
                    if radius > 0 and abs(cx - base_x) < radius and abs(cy - base_y) < radius:
                        continue
                    candidates.extend(self.grid.get((cx, cy), []))
            for segment_id in candidates:
                if segment_id in seen:
                    continue
                seen.add(segment_id)
                distance, px, py, measure = self.project(x, y, self.segments[segment_id])
                physical_id = self.segments[segment_id][0]
                if best is None or distance < best[0]:
                    best = (distance, px, py, measure, physical_id)
            if best is not None and radius >= 2 and (radius - 1) * self.cell_size > best[0]:
                break
        if best is None:
            raise RuntimeError(f"보행 엣지 스냅 실패: ({x}, {y})")
        distance, px, py, measure, physical_id = best
        edge = self.physical.loc[physical_id]
        return {
            "physical_id": physical_id,
            "snap_x": px,
            "snap_y": py,
            "snap_distance_m": distance,
            "measure_from_node_a_m": max(0.0, min(float(edge["length_m"]), measure)),
            "node_a": int(edge["node_a"]),
            "node_b": int(edge["node_b"]),
            "edge_length_m": float(edge["length_m"]),
        }


def allowed_physical_ids(edges: pd.DataFrame, scenario: str) -> set[str]:
    predicate = SCENARIOS[scenario]
    allowed_directed = edges[edges["qa_effective_route_action"].map(predicate)]
    return set(allowed_directed["physical_id"])


def snap_frame(
    frame: pd.DataFrame,
    id_column: str,
    role: str,
    scenario: str,
    index: SegmentIndex,
) -> pd.DataFrame:
    rows = []
    for _, row in frame.iterrows():
        snapped = index.snap(float(row["x"]), float(row["y"]))
        rows.append(
            {
                "scenario": scenario,
                "role": role,
                "point_id": row[id_column],
                "point_x": row["x"],
                "point_y": row["y"],
                **snapped,
                "snap_status": "PASS_50M"
                if snapped["snap_distance_m"] <= 50
                else ("PASS_100M" if snapped["snap_distance_m"] <= 100 else "REVIEW_OVER_100M"),
            }
        )
    return pd.DataFrame(rows)


def build_directed_adjacency(
    edges: pd.DataFrame,
    scenario: str,
) -> dict[int, list[tuple[int, float, int]]]:
    predicate = SCENARIOS[scenario]
    best: dict[tuple[int, int], tuple[float, int]] = {}
    for _, row in edges[edges["qa_effective_route_action"].map(predicate)].iterrows():
        u, v = int(row["u"]), int(row["v"])
        length = float(row["geometry_length_m"])
        key = (u, v)
        if key not in best or length < best[key][0]:
            best[key] = (length, int(row["fid"]))
    adjacency: dict[int, list[tuple[int, float, int]]] = defaultdict(list)
    for (u, v), (length, fid) in best.items():
        adjacency[u].append((v, length, fid))
    return adjacency


def build_undirected_adjacency(
    physical: pd.DataFrame,
    allowed: set[str],
) -> dict[int, list[tuple[int, float, str]]]:
    adjacency: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
    for _, edge in physical[physical["physical_id"].isin(allowed)].iterrows():
        a, b, length = int(edge["node_a"]), int(edge["node_b"]), float(edge["length_m"])
        adjacency[a].append((b, length, edge["physical_id"]))
        adjacency[b].append((a, length, edge["physical_id"]))
    return adjacency


def dijkstra_from_point(
    snap: pd.Series,
    adjacency: dict[int, list[tuple[int, float, int]]],
    max_cost: float,
) -> tuple[dict[int, float], dict[int, tuple[int, int] | None]]:
    a, b = int(snap["node_a"]), int(snap["node_b"])
    measure, length, off = (
        float(snap["measure_from_node_a_m"]),
        float(snap["edge_length_m"]),
        float(snap["snap_distance_m"]),
    )
    distances = {a: off + measure, b: off + length - measure}
    parents: dict[int, tuple[int, int] | None] = {a: None, b: None}
    heap = [(distances[a], a), (distances[b], b)]
    while heap:
        cost, node = heapq.heappop(heap)
        if cost != distances.get(node) or cost > max_cost:
            continue
        for neighbor, edge_length, fid in adjacency.get(node, []):
            new_cost = cost + edge_length
            if new_cost <= max_cost and new_cost < distances.get(neighbor, math.inf):
                distances[neighbor] = new_cost
                parents[neighbor] = (node, fid)
                heapq.heappush(heap, (new_cost, neighbor))
    return distances, parents


def reconstruct_path(
    endpoint: int,
    parents: dict[int, tuple[int, int] | None],
    fid_to_physical: dict[int, str],
) -> list[str]:
    path = []
    node = endpoint
    while parents.get(node) is not None:
        previous, fid = parents[node]  # type: ignore[misc]
        path.append(fid_to_physical[int(fid)])
        node = int(previous)
    path.reverse()
    return path


def route_to_destination(
    origin_snap: pd.Series,
    destination_snap: pd.Series,
    distances: dict[int, float],
    parents: dict[int, tuple[int, int] | None],
    fid_to_physical: dict[int, str],
) -> tuple[float, list[str]] | None:
    candidates: list[tuple[float, int | None, str]] = []
    dest_measure = float(destination_snap["measure_from_node_a_m"])
    dest_length = float(destination_snap["edge_length_m"])
    dest_off = float(destination_snap["snap_distance_m"])
    for endpoint, access in [
        (int(destination_snap["node_a"]), dest_measure),
        (int(destination_snap["node_b"]), dest_length - dest_measure),
    ]:
        if endpoint in distances:
            candidates.append((distances[endpoint] + access + dest_off, endpoint, "network"))
    if origin_snap["physical_id"] == destination_snap["physical_id"]:
        direct = (
            float(origin_snap["snap_distance_m"])
            + abs(
                float(origin_snap["measure_from_node_a_m"])
                - float(destination_snap["measure_from_node_a_m"])
            )
            + dest_off
        )
        candidates.append((direct, None, "same_edge"))
    if not candidates:
        return None
    cost, endpoint, mode = min(candidates, key=lambda item: item[0])
    if mode == "same_edge":
        path = [str(origin_snap["physical_id"])]
    else:
        path = reconstruct_path(int(endpoint), parents, fid_to_physical)
        path = [str(origin_snap["physical_id"])] + path + [str(destination_snap["physical_id"])]
    deduplicated = []
    for physical_id in path:
        if not deduplicated or deduplicated[-1] != physical_id:
            deduplicated.append(physical_id)
    return cost, deduplicated


def route_scenario(
    scenario: str,
    origins: pd.DataFrame,
    destinations: pd.DataFrame,
    origin_snaps: pd.DataFrame,
    destination_snaps: pd.DataFrame,
    adjacency: dict[int, list[tuple[int, float, int]]],
    fid_to_physical: dict[int, str],
) -> tuple[pd.DataFrame, dict[str, dict[str, float]]]:
    destination_snap_lookup = destination_snaps.set_index("point_id")
    overlap: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    routes = []
    dest_xy = destinations[["x", "y"]].to_numpy(float)
    for origin_number, (_, origin) in enumerate(origins.iterrows(), start=1):
        origin_snap = origin_snaps[origin_snaps["point_id"].eq(origin["origin_id"])].iloc[0]
        distances, parents = dijkstra_from_point(
            origin_snap,
            adjacency,
            max_cost=max(OD_RADII_M) + 500,
        )
        euclidean = np.hypot(dest_xy[:, 0] - float(origin["x"]), dest_xy[:, 1] - float(origin["y"]))
        candidate_indices = np.flatnonzero((euclidean <= max(OD_RADII_M)) & (euclidean >= 5))
        for destination_index in candidate_indices:
            destination = destinations.iloc[int(destination_index)]
            destination_snap = destination_snap_lookup.loc[destination["destination_id"]]
            result = route_to_destination(
                origin_snap,
                destination_snap,
                distances,
                parents,
                fid_to_physical,
            )
            if result is None:
                continue
            route_length, physical_path = result
            if route_length > max(OD_RADII_M):
                continue
            route_weight = float(origin["origin_weight_proxy"]) * float(
                destination["destination_weight_proxy"]
            )
            route_id = f"{scenario[:2].upper()}_{len(routes)+1:07d}"
            row = {
                "route_id": route_id,
                "scenario": scenario,
                "origin_id": origin["origin_id"],
                "origin_types": origin["origin_types"],
                "origin_names": origin["names"],
                "destination_id": destination["destination_id"],
                "destination_type": destination["destination_type"],
                "destination_names": destination["names"],
                "route_length_m": round(route_length, 3),
                "origin_snap_distance_m": round(float(origin_snap["snap_distance_m"]), 3),
                "destination_snap_distance_m": round(float(destination_snap["snap_distance_m"]), 3),
                "origin_weight_proxy": origin["origin_weight_proxy"],
                "destination_weight_proxy": destination["destination_weight_proxy"],
                "route_weight_proxy": route_weight,
                "path_physical_ids": "|".join(physical_path),
            }
            for radius in OD_RADII_M:
                row[f"within_{radius}m"] = int(route_length <= radius)
            routes.append(row)
            for physical_id in set(physical_path):
                metrics = overlap[physical_id]
                metrics["route_count_1200m"] += 1
                metrics["route_weight_1200m"] += route_weight
                if route_length <= 800:
                    metrics["route_count_800m"] += 1
                    metrics["route_weight_800m"] += route_weight
                    metrics[f"destination_{destination['destination_type']}_routes_800m"] += 1
                    for origin_type in str(origin["origin_types"]).split(" | "):
                        metrics[f"origin_{origin_type}_routes_800m"] += 1
                if route_length <= 500:
                    metrics["route_count_500m"] += 1
                    metrics["route_weight_500m"] += route_weight
        if origin_number % 100 == 0:
            print(f"[{scenario}] routed origins {origin_number}/{len(origins)}")
    return pd.DataFrame(routes), overlap


def multi_source_seat_distances(
    seat_snaps: pd.DataFrame,
    adjacency: dict[int, list[tuple[int, float, str]]],
) -> dict[int, float]:
    distances: dict[int, float] = {}
    heap: list[tuple[float, int]] = []
    for _, snap in seat_snaps.iterrows():
        measure = float(snap["measure_from_node_a_m"])
        length = float(snap["edge_length_m"])
        off = float(snap["snap_distance_m"])
        for node, cost in [
            (int(snap["node_a"]), off + measure),
            (int(snap["node_b"]), off + length - measure),
        ]:
            if cost < distances.get(node, math.inf):
                distances[node] = cost
                heapq.heappush(heap, (cost, node))
    while heap:
        cost, node = heapq.heappop(heap)
        if cost != distances.get(node):
            continue
        for neighbor, edge_length, _ in adjacency.get(node, []):
            new_cost = cost + edge_length
            if new_cost < distances.get(neighbor, math.inf):
                distances[neighbor] = new_cost
                heapq.heappush(heap, (new_cost, neighbor))
    return distances


def calculate_bench_gaps(
    scenario: str,
    physical: pd.DataFrame,
    overlap: dict[str, dict[str, float]],
    seat_snaps: pd.DataFrame,
    allowed: set[str],
) -> pd.DataFrame:
    undirected = build_undirected_adjacency(physical, allowed)
    node_seat_distance = multi_source_seat_distances(seat_snaps, undirected)
    direct_seats: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for _, snap in seat_snaps.iterrows():
        direct_seats[str(snap["physical_id"])].append(
            (float(snap["measure_from_node_a_m"]), float(snap["snap_distance_m"]))
        )
    rows = []
    for _, edge in physical.iterrows():
        physical_id = edge["physical_id"]
        if physical_id not in allowed or physical_id not in overlap:
            continue
        length = float(edge["length_m"])
        interval_count = max(1, int(math.ceil(length / EDGE_SAMPLE_INTERVAL_M)))
        interval_length = length / interval_count
        measures = [(i + 0.5) * interval_length for i in range(interval_count)]
        a_distance = node_seat_distance.get(int(edge["node_a"]), math.inf)
        b_distance = node_seat_distance.get(int(edge["node_b"]), math.inf)
        seat_distances = []
        for measure in measures:
            candidates = [a_distance + measure, b_distance + length - measure]
            candidates.extend(abs(measure - seat_measure) + off for seat_measure, off in direct_seats.get(physical_id, []))
            seat_distances.append(min(candidates))
        row: dict[str, object] = {
            "scenario": scenario,
            "physical_id": physical_id,
            "node_a": edge["node_a"],
            "node_b": edge["node_b"],
            "osmid": edge["osmid"],
            "highway": edge["highway"],
            "name": edge["name"],
            "length_m": length,
            "intersects_dalseo": edge["intersects_dalseo"],
            "is_steps": edge["is_steps"],
            "actions": edge["actions"],
            "sample_count": interval_count,
            "nearest_seat_mean_m": float(np.mean(seat_distances)),
            "nearest_seat_max_m": float(np.max(seat_distances)),
            **overlap[physical_id],
        }
        for threshold in BENCH_THRESHOLDS_M:
            flags = np.asarray(seat_distances) > threshold
            row[f"gap_length_{threshold}m"] = float(flags.sum() * interval_length)
            row[f"gap_share_{threshold}m"] = float(flags.mean())
            row[f"gap_severity_{threshold}m"] = float(
                flags.mean() * max(0.0, np.max(seat_distances) / threshold - 1)
            )
        rows.append(row)
    result = pd.DataFrame(rows).fillna(0)
    for destination_type in ["medical", "senior_welfare", "park", "bus_stop"]:
        column = f"destination_{destination_type}_routes_800m"
        if column not in result:
            result[column] = 0
    for origin_type in ["admin_population", "senior_center", "senior_welfare"]:
        column = f"origin_{origin_type}_routes_800m"
        if column not in result:
            result[column] = 0
    return result


def percentile_component(series: pd.Series) -> pd.Series:
    return np.log1p(series.clip(lower=0)).rank(method="average", pct=True)


def make_candidates(overlap_all: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    ranked_parts = []
    for scenario in SCENARIOS:
        scenario_rows = overlap_all[
            overlap_all["scenario"].eq(scenario)
            & overlap_all["intersects_dalseo"].eq(1)
            & overlap_all["route_weight_800m"].gt(0)
        ].copy()
        for threshold in BENCH_THRESHOLDS_M:
            candidate = scenario_rows[scenario_rows[f"gap_length_{threshold}m"].gt(0)].copy()
            if candidate.empty:
                continue
            candidate["bench_threshold_m"] = threshold
            candidate["demand_component"] = percentile_component(candidate["route_weight_800m"])
            candidate["gap_component"] = percentile_component(candidate[f"gap_severity_{threshold}m"])
            candidate["priority_core_score"] = np.sqrt(
                candidate["demand_component"] * candidate["gap_component"]
            )
            candidate["priority_rank"] = candidate["priority_core_score"].rank(
                method="min", ascending=False
            ).astype(int)
            candidate["evidence_facility_type_count"] = (
                candidate[
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
            ranked_parts.append(candidate)
    ranked = pd.concat(ranked_parts, ignore_index=True)
    top_sets = (
        ranked[ranked["priority_rank"].le(TOP_N)]
        .groupby("physical_id")
        .size()
        .rename("top20_scenario_stability_count")
    )
    ranked = ranked.merge(top_sets, on="physical_id", how="left")
    ranked["top20_scenario_stability_count"] = ranked["top20_scenario_stability_count"].fillna(0).astype(int)
    top = ranked[ranked["priority_rank"].le(TOP_N)].copy()
    top = top.sort_values(
        ["top20_scenario_stability_count", "priority_core_score"],
        ascending=[False, False],
    )
    return ranked, top


def geojson_lines(
    frame: pd.DataFrame,
    physical_lookup: dict[str, dict[str, object]],
    path: Path,
    property_columns: list[str],
) -> None:
    features = []
    for _, row in frame.iterrows():
        physical_id = row["physical_id"]
        coords = physical_lookup[physical_id]["coords"]
        properties = {}
        for column in property_columns:
            value = row.get(column)
            if isinstance(value, np.generic):
                value = value.item()
            if pd.isna(value):
                value = None
            properties[column] = value
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[round(x, 3), round(y, 3)] for x, y in coords],
                },
            }
        )
    payload = {
        "type": "FeatureCollection",
        "name": path.stem,
        "crs": {"type": "name", "properties": {"name": "EPSG:5179"}},
        "features": features,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def add_candidate_coordinates(
    frame: pd.DataFrame,
    physical_lookup: dict[str, dict[str, object]],
) -> pd.DataFrame:
    result = frame.copy()
    midpoints = [point_along(physical_lookup[pid]["coords"]) for pid in result["physical_id"]]
    result["midpoint_x_5179"] = [p[0] for p in midpoints]
    result["midpoint_y_5179"] = [p[1] for p in midpoints]
    lon, lat = inverse_epsg5179(result["midpoint_x_5179"], result["midpoint_y_5179"])
    result["midpoint_lon"] = lon
    result["midpoint_lat"] = lat
    result["kakao_map_url"] = [
        f"https://map.kakao.com/link/map/벤치후보,{latitude:.7f},{longitude:.7f}"
        for longitude, latitude in zip(lon, lat)
    ]
    return result


def unique_candidate_summary(top: pd.DataFrame) -> pd.DataFrame:
    best = (
        top.sort_values("priority_core_score", ascending=False)
        .drop_duplicates("physical_id")
        .copy()
    )
    contexts = (
        top.groupby("physical_id", as_index=False)
        .agg(
            candidate_scenarios=("scenario", short_join),
            candidate_thresholds_m=("bench_threshold_m", lambda x: short_join(map(int, x), limit=10)),
            best_priority_rank=("priority_rank", "min"),
            best_priority_core_score=("priority_core_score", "max"),
            top20_scenario_stability_count=("top20_scenario_stability_count", "max"),
        )
    )
    drop_columns = [
        "scenario",
        "bench_threshold_m",
        "priority_rank",
        "priority_core_score",
        "top20_scenario_stability_count",
    ]
    best = best.drop(columns=[c for c in drop_columns if c in best.columns])
    result = contexts.merge(best, on="physical_id", how="left", validate="one_to_one")
    return result.sort_values(
        ["top20_scenario_stability_count", "best_priority_core_score"],
        ascending=[False, False],
    )


def write_review_html(
    candidates: pd.DataFrame,
    physical_lookup: dict[str, dict[str, object]],
    path: Path,
) -> None:
    features = []
    for _, row in candidates.iterrows():
        coords_5179 = physical_lookup[row["physical_id"]]["coords"]
        lon, lat = inverse_epsg5179(
            [coord[0] for coord in coords_5179],
            [coord[1] for coord in coords_5179],
        )
        props = {}
        for column in [
            "physical_id",
            "name",
            "highway",
            "candidate_thresholds_m",
            "candidate_scenarios",
            "best_priority_rank",
            "best_priority_core_score",
            "top20_scenario_stability_count",
            "route_count_800m",
            "route_weight_800m",
            "nearest_seat_max_m",
            "gap_length_300m",
            "gap_length_500m",
            "evidence_facility_type_count",
            "midpoint_lat",
            "midpoint_lon",
            "kakao_map_url",
        ]:
            value = row.get(column)
            if isinstance(value, np.generic):
                value = value.item()
            if pd.isna(value):
                value = None
            props[column] = value
        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[float(x), float(y)] for x, y in zip(lon, lat)],
                },
            }
        )
    payload = json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False)
    html = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>달서구 벤치 설치 우선 검토 구간</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html,body{height:100%;margin:0;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans KR",sans-serif;color:#182230}
#app{height:100%;display:grid;grid-template-columns:390px 1fr}.panel{overflow:auto;padding:18px;background:#fff;border-right:1px solid #d7dee8}
#map{height:100%;min-height:600px}.title{font-size:20px;font-weight:750;margin:0 0 7px}.sub{font-size:13px;line-height:1.45;color:#536174;margin-bottom:14px}
.filters{position:sticky;top:-18px;background:#fff;padding:12px 0;border-bottom:1px solid #e5e9f0;z-index:2}
button{border:1px solid #b9c4d3;background:#fff;padding:7px 10px;border-radius:8px;margin-right:5px;cursor:pointer}
button.active{background:#174ea6;color:#fff;border-color:#174ea6}.card{padding:12px 8px;border-bottom:1px solid #e7ebf1;cursor:pointer}
.card:hover{background:#f4f7fb}.rank{font-weight:750;color:#174ea6}.road{font-weight:650}.meta{font-size:12px;color:#5c6878;line-height:1.55;margin-top:5px}
.badge{display:inline-block;font-size:11px;background:#e8f0fe;color:#174ea6;border-radius:10px;padding:2px 7px;margin-right:4px}
@media(max-width:780px){#app{grid-template-columns:1fr;grid-template-rows:44% 56%}.panel{border-right:0;border-bottom:1px solid #d7dee8}}
</style></head><body><div id="app"><aside class="panel">
<h1 class="title">벤치 설치 우선 검토 구간</h1>
<div class="sub">예상 보행경로 중첩과 300m·500m 휴식시설 공백을 결합한 검토용 결과입니다. 실제 이동 경로나 최종 설치 위치가 아닙니다.</div>
<div class="filters"><button class="active" data-filter="all">전체</button><button data-filter="300">300m</button><button data-filter="500">500m</button></div>
<div id="list"></div></aside><main id="map"></main></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const data=__DATA__;
const map=L.map('map',{zoomControl:true}).setView([35.83,128.53],13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:20,attribution:'© OpenStreetMap'}).addTo(map);
const layers=[]; const list=document.getElementById('list');
function color(stability){return stability>=4?'#c62828':stability>=3?'#ef6c00':'#1565c0'}
function popup(p){return `<b>${p.name||'도로명 없음'}</b><br>기준: ${p.candidate_thresholds_m}m<br>800m 경로 중첩: ${Math.round(p.route_count_800m)}회<br>최대 벤치거리: ${Math.round(p.nearest_seat_max_m)}m<br>300m 공백: ${Math.round(p.gap_length_300m)}m / 500m 공백: ${Math.round(p.gap_length_500m)}m<br><a href="${p.kakao_map_url}" target="_blank">카카오맵에서 보기</a>`}
data.features.forEach((f,i)=>{const p=f.properties;const layer=L.geoJSON(f,{style:{color:color(p.top20_scenario_stability_count),weight:7,opacity:.85}}).bindPopup(popup(p)).addTo(map);layers.push({layer,f});
const card=document.createElement('div');card.className='card';card.dataset.thresholds=p.candidate_thresholds_m;card.innerHTML=`<div><span class="rank">#${p.best_priority_rank}</span> <span class="road">${p.name||'도로명 없음'}</span></div><div><span class="badge">${p.candidate_thresholds_m}m</span><span class="badge">안정성 ${p.top20_scenario_stability_count}/4</span></div><div class="meta">경로 ${Math.round(p.route_count_800m)}회 · 최대 벤치거리 ${Math.round(p.nearest_seat_max_m)}m<br>${Number(p.midpoint_lat).toFixed(6)}, ${Number(p.midpoint_lon).toFixed(6)}</div>`;card.onclick=()=>{map.fitBounds(layer.getBounds(),{maxZoom:18,padding:[30,30]});layer.openPopup()};list.appendChild(card);});
if(layers.length)map.fitBounds(L.featureGroup(layers.map(x=>x.layer)).getBounds(),{padding:[25,25]});
function applyFilter(value){layers.forEach(({layer,f},i)=>{const show=value==='all'||String(f.properties.candidate_thresholds_m).split(' | ').includes(value);if(show){layer.addTo(map)}else{map.removeLayer(layer)};list.children[i].style.display=show?'block':'none'});}
document.querySelectorAll('button[data-filter]').forEach(b=>b.onclick=()=>{document.querySelectorAll('button').forEach(x=>x.classList.remove('active'));b.classList.add('active');applyFilter(b.dataset.filter)});
</script></body></html>""".replace("__DATA__", payload)
    path.write_text(html, encoding="utf-8")


def validate_results(
    origins: pd.DataFrame,
    destinations: pd.DataFrame,
    snap_audit: pd.DataFrame,
    routes: pd.DataFrame,
    overlap: pd.DataFrame,
    candidates: pd.DataFrame,
) -> pd.DataFrame:
    checks = [
        ("M-01", len(origins) == 488, f"통합 출발 위치 {len(origins)}개"),
        (
            "M-02",
            destinations["destination_type"].isin(["medical", "senior_welfare", "park", "bus_stop"]).all(),
            f"통합 도착 위치 {len(destinations)}개, 의료기관 동일좌표 집계 적용",
        ),
        (
            "M-03",
            snap_audit["physical_id"].notna().all(),
            f"점-보행 엣지 스냅 {len(snap_audit):,}건",
        ),
        (
            "M-04",
            set(routes["scenario"]) == set(SCENARIOS),
            f"계단 포함·제외 경로 {len(routes):,}건",
        ),
        (
            "M-05",
            routes["route_length_m"].between(0, max(OD_RADII_M)).all(),
            "저장 경로 길이 0~1,200m",
        ),
        (
            "M-06",
            (routes["within_500m"] <= routes["within_800m"]).all()
            and (routes["within_800m"] <= routes["within_1200m"]).all(),
            "OD 반경 포함관계 500⊆800⊆1200",
        ),
        (
            "M-07",
            overlap[[f"gap_share_{t}m" for t in BENCH_THRESHOLDS_M]]
            .apply(lambda c: c.between(0, 1).all())
            .all(),
            "벤치 공백 비율 0~1",
        ),
        (
            "M-08",
            (
                overlap["gap_length_500m"].le(overlap["gap_length_300m"] + 1e-6)
            ).all(),
            "500m 공백 길이는 300m 공백 길이 이하",
        ),
        (
            "M-09",
            candidates["priority_core_score"].between(0, 1).all(),
            f"후보 점수 0~1, TOP N={TOP_N}",
        ),
        (
            "M-10",
            abs(float(origins["origin_weight_proxy"].sum()) - 3.0) < 1e-9
            and destinations.groupby("destination_type")["destination_weight_proxy"]
            .sum()
            .sub(1.0)
            .abs()
            .lt(1e-9)
            .all(),
            "출발 근거군별 질량 1(합계 3), 도착 유형별 질량 1",
        ),
    ]
    return pd.DataFrame(
        [
            {"check_id": check_id, "status": "PASS" if passed else "FAIL", "detail": detail}
            for check_id, passed, detail in checks
        ]
    )


def main() -> None:
    started = datetime.now().astimezone()
    print("Loading network and prepared inputs...")
    nodes, edges, fid_to_physical, physical = load_network()
    origins = make_origin_locations()
    destinations = make_destination_locations()
    seats = read_csv("resting_seats.csv")
    active_seats = seats[seats["active_supply"].eq(1)].copy()
    active_seats = pd.DataFrame(
        {
            "seat_id": active_seats["feature_id"],
            "x": active_seats["x_5179"],
            "y": active_seats["y_5179"],
            "source_type": active_seats["source_type"],
        }
    )
    origins.to_csv(OUT / "origin_locations.csv", index=False, encoding="utf-8-sig")
    destinations.to_csv(OUT / "destination_locations.csv", index=False, encoding="utf-8-sig")

    all_routes = []
    all_overlap = []
    all_snaps = []
    physical_lookup = physical.set_index("physical_id").to_dict("index")
    for scenario in SCENARIOS:
        print(f"Scenario: {scenario}")
        allowed = allowed_physical_ids(edges, scenario)
        segment_index = SegmentIndex(physical, allowed)
        origin_snaps = snap_frame(origins, "origin_id", "origin", scenario, segment_index)
        destination_snaps = snap_frame(
            destinations, "destination_id", "destination", scenario, segment_index
        )
        seat_snaps = snap_frame(active_seats, "seat_id", "resting_seat", scenario, segment_index)
        all_snaps.extend([origin_snaps, destination_snaps, seat_snaps])
        directed = build_directed_adjacency(edges, scenario)
        routes, overlap = route_scenario(
            scenario,
            origins,
            destinations,
            origin_snaps,
            destination_snaps,
            directed,
            fid_to_physical,
        )
        all_routes.append(routes)
        gap_edges = calculate_bench_gaps(
            scenario,
            physical,
            overlap,
            seat_snaps,
            allowed,
        )
        all_overlap.append(gap_edges)

    snap_audit = pd.concat(all_snaps, ignore_index=True)
    routes_all = pd.concat(all_routes, ignore_index=True)
    overlap_all = pd.concat(all_overlap, ignore_index=True).fillna(0)
    ranked_candidates, top_candidates = make_candidates(overlap_all)
    ranked_candidates = add_candidate_coordinates(ranked_candidates, physical_lookup)
    top_candidates = add_candidate_coordinates(top_candidates, physical_lookup)
    unique_candidates = unique_candidate_summary(top_candidates)

    snap_audit.to_csv(OUT / "point_to_walk_edge_snaps.csv", index=False, encoding="utf-8-sig")
    snap_audit.loc[
        snap_audit["snap_status"].eq("REVIEW_OVER_100M")
    ].to_csv(
        QA / "review_edge_snap_over_100m.csv",
        index=False,
        encoding="utf-8-sig",
    )
    routes_all.to_csv(OUT / "expected_od_routes.csv", index=False, encoding="utf-8-sig")
    overlap_all.to_csv(OUT / "route_overlap_and_bench_gap_edges.csv", index=False, encoding="utf-8-sig")
    ranked_candidates.to_csv(OUT / "priority_edge_candidates_all.csv", index=False, encoding="utf-8-sig")
    top_candidates.to_csv(OUT / "priority_edge_candidates_top20.csv", index=False, encoding="utf-8-sig")
    unique_candidates.to_csv(
        OUT / "priority_edge_candidates_unique_review.csv",
        index=False,
        encoding="utf-8-sig",
    )

    overlap_properties = [
        "scenario",
        "physical_id",
        "name",
        "highway",
        "length_m",
        "route_count_500m",
        "route_count_800m",
        "route_count_1200m",
        "route_weight_800m",
        "nearest_seat_mean_m",
        "nearest_seat_max_m",
        "gap_length_300m",
        "gap_share_300m",
        "gap_length_500m",
        "gap_share_500m",
    ]
    for scenario in SCENARIOS:
        geojson_lines(
            overlap_all[overlap_all["scenario"].eq(scenario)],
            physical_lookup,
            OUT / f"route_overlap_bench_gap_{scenario}_epsg5179.geojson",
            overlap_properties,
        )
    candidate_properties = [
        "scenario",
        "bench_threshold_m",
        "physical_id",
        "name",
        "highway",
        "length_m",
        "route_count_800m",
        "route_weight_800m",
        "nearest_seat_max_m",
        "gap_length_300m",
        "gap_length_500m",
        "priority_core_score",
        "priority_rank",
        "top20_scenario_stability_count",
        "evidence_facility_type_count",
    ]
    geojson_lines(
        top_candidates,
        physical_lookup,
        OUT / "priority_edge_candidates_top20_epsg5179.geojson",
        candidate_properties,
    )
    write_review_html(
        unique_candidates,
        physical_lookup,
        OUT / "priority_candidates_review.html",
    )

    checks = validate_results(
        origins,
        destinations,
        snap_audit,
        routes_all,
        overlap_all,
        ranked_candidates,
    )
    checks.to_csv(QA / "modeling_validation_checks.csv", index=False, encoding="utf-8-sig")

    scenario_metrics = []
    for scenario in SCENARIOS:
        route_part = routes_all[routes_all["scenario"].eq(scenario)]
        edge_part = overlap_all[overlap_all["scenario"].eq(scenario)]
        snap_part = snap_audit[snap_audit["scenario"].eq(scenario)]
        scenario_metrics.append(
            {
                "scenario": scenario,
                "routes_500m": int(route_part["within_500m"].sum()),
                "routes_800m": int(route_part["within_800m"].sum()),
                "routes_1200m": int(route_part["within_1200m"].sum()),
                "overlap_edges": len(edge_part),
                "gap_edges_300m": int(edge_part["gap_length_300m"].gt(0).sum()),
                "gap_edges_500m": int(edge_part["gap_length_500m"].gt(0).sum()),
                "snap_over_100m": int(snap_part["snap_status"].eq("REVIEW_OVER_100M").sum()),
                "median_snap_distance_m": round(float(snap_part["snap_distance_m"].median()), 3),
                "p95_snap_distance_m": round(float(snap_part["snap_distance_m"].quantile(0.95)), 3),
            }
        )
    scenario_metrics_frame = pd.DataFrame(scenario_metrics)
    scenario_metrics_frame.to_csv(QA / "scenario_metrics.csv", index=False, encoding="utf-8-sig")

    summary = {
        "pipeline": "dalseo_expected_walk_and_bench_gap_modeling",
        "started_at": started.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "status": "PASS" if checks["status"].eq("PASS").all() else "FAIL",
        "interpretation": "expected_walking_routes_not_observed_gps",
        "analysis_crs": "EPSG:5179",
        "parameters": {
            "od_radii_m": list(OD_RADII_M),
            "primary_od_radius_m": PRIMARY_OD_RADIUS_M,
            "bench_gap_thresholds_m": list(BENCH_THRESHOLDS_M),
            "edge_sample_interval_m": EDGE_SAMPLE_INTERVAL_M,
            "scenarios": list(SCENARIOS),
            "top_n_per_scenario_threshold": TOP_N,
        },
        "counts": {
            "origin_locations": len(origins),
            "destination_locations": len(destinations),
            "active_resting_seat_records": len(active_seats),
            "expected_routes": len(routes_all),
            "overlap_edge_scenario_rows": len(overlap_all),
            "ranked_candidate_rows": len(ranked_candidates),
            "top_candidate_rows": len(top_candidates),
            "unique_top_candidate_edges": len(unique_candidates),
        },
        "scenario_metrics": scenario_metrics,
        "validation": checks.to_dict("records"),
        "not_in_priority_score": [
            "slope",
            "pedestrian_accident_risk",
            "crossing_safety",
            "installation_feasibility",
        ],
    }
    (QA / "modeling_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
