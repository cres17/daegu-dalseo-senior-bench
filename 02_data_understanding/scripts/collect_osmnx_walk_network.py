#!/usr/bin/env python3
"""달서구 행정동 경계를 이용해 OSMnx 보행 가능 도로망을 수집한다.

이 스크립트는 원천 OpenStreetMap 자료를 수정하지 않고 다음을 저장한다.

- WGS84 및 EPSG:5179 GraphML
- QGIS 등에서 확인할 수 있는 EPSG:5179 GeoPackage
- 달서구 원경계와 500m 다운로드 경계 GeoJSON
- 수집 조건·연결성·태그 완성도를 기록한 JSON/CSV

주의: OSMnx의 ``walk`` 네트워크는 '실제 보도 위치의 완전한 목록'이 아니라
OSM 태그상 보행 가능한 길의 중심선 네트워크다. 보도 누락과 통행 가능 여부는
주요 구간을 표본 검수해야 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data_external" / "osmnx_dalseo_walk"
DEFAULT_ADMIN_PREFIX = "22070"
DEFAULT_EXPECTED_DONGS = 23
DEFAULT_BUFFER_M = 500
ANALYSIS_CRS = "EPSG:5179"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="달서구 센서스 행정동 경계로 OSMnx walk 네트워크를 수집합니다."
    )
    parser.add_argument(
        "--boundary",
        type=Path,
        default=None,
        help=(
            "행정동 경계 SHP 경로. 생략하면 프로젝트에서 "
            "BND_ADM_DONG_PG.shp를 자동 탐색합니다."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"출력 폴더(기본값: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--admin-prefix",
        default=DEFAULT_ADMIN_PREFIX,
        help="센서스 달서구 행정동 코드 접두어(기본값: 22070)",
    )
    parser.add_argument(
        "--expected-dongs",
        type=int,
        default=DEFAULT_EXPECTED_DONGS,
        help="기대하는 달서구 행정동 수(기본값: 23)",
    )
    parser.add_argument(
        "--buffer-m",
        type=int,
        default=DEFAULT_BUFFER_M,
        help="경계부 연결 보존용 외곽 버퍼(m, 기본값: 500)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Overpass 요청 제한시간(초, 기본값: 300)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Overpass 다운로드 최대 시도 횟수(기본값: 3)",
    )
    parser.add_argument(
        "--skip-plot",
        action="store_true",
        help="보행망 미리보기 PNG 생성을 생략합니다.",
    )
    return parser.parse_args()


def import_dependencies() -> tuple[Any, Any, Any, Any]:
    try:
        import geopandas as gpd
        import networkx as nx
        import osmnx as ox
        import pandas as pd
    except ImportError as exc:
        raise SystemExit(
            "필수 패키지가 없습니다. 먼저 다음을 실행하세요:\n"
            "  python -m pip install -r "
            "02_data_understanding/requirements-osmnx.txt\n"
            f"원래 오류: {exc}"
        ) from exc
    return gpd, nx, ox, pd


def discover_boundary(explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        path = explicit_path.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"경계 파일을 찾을 수 없습니다: {path}")
        return path

    candidates = sorted(PROJECT_ROOT.rglob("BND_ADM_DONG_PG.shp"))
    if len(candidates) != 1:
        raise FileNotFoundError(
            "BND_ADM_DONG_PG.shp를 하나로 확정하지 못했습니다. "
            f"발견 수={len(candidates)}. --boundary로 경로를 지정하세요."
        )
    return candidates[0].resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    """프로젝트 내부 경로는 상대경로로, 외부 경로는 절대경로로 기록한다."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def load_dalseo_boundary(
    boundary_path: Path,
    admin_prefix: str,
    expected_dongs: int,
    gpd: Any,
) -> tuple[Any, Any, list[dict[str, str]]]:
    all_dongs = gpd.read_file(boundary_path)
    required_columns = {"ADM_CD", "ADM_NM", "geometry"}
    missing = required_columns.difference(all_dongs.columns)
    if missing:
        raise ValueError(f"행정동 경계 필수 컬럼이 없습니다: {sorted(missing)}")
    if all_dongs.crs is None:
        raise ValueError("행정동 경계에 CRS가 없습니다. PRJ 파일을 확인하세요.")

    admin_codes = all_dongs["ADM_CD"].astype(str).str.strip().str.replace(".0", "", regex=False)
    dalseo = all_dongs.loc[admin_codes.str.startswith(admin_prefix)].copy()
    if len(dalseo) != expected_dongs:
        raise ValueError(
            f"달서구 행정동 수가 기대값과 다릅니다: {len(dalseo)} != {expected_dongs}. "
            "코드 체계와 기준연도를 확인하세요."
        )
    if dalseo["ADM_CD"].astype(str).duplicated().any():
        raise ValueError("달서구 행정동 코드에 중복이 있습니다.")

    if hasattr(dalseo.geometry, "make_valid"):
        dalseo.geometry = dalseo.geometry.make_valid()
    elif not bool(dalseo.geometry.is_valid.all()):
        dalseo.geometry = dalseo.geometry.buffer(0)

    dalseo_metric = dalseo.to_crs(ANALYSIS_CRS)
    core_metric = dalseo_metric.dissolve().geometry.iloc[0]
    core_wgs84 = gpd.GeoSeries([core_metric], crs=ANALYSIS_CRS).to_crs("EPSG:4326").iloc[0]
    dong_rows = (
        dalseo[["ADM_CD", "ADM_NM"]]
        .astype(str)
        .sort_values("ADM_CD")
        .rename(columns={"ADM_CD": "admin_code", "ADM_NM": "admin_name"})
        .to_dict("records")
    )
    return core_metric, core_wgs84, dong_rows


def configure_osmnx(ox: Any, output_dir: Path, timeout: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / "cache"
    logs_dir = output_dir / "logs"
    cache_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    ox.settings.use_cache = True
    ox.settings.cache_folder = cache_dir
    ox.settings.log_console = True
    ox.settings.log_file = True
    ox.settings.logs_folder = logs_dir
    ox.settings.requests_timeout = timeout
    ox.settings.overpass_rate_limit = True

    node_tags = {
        "barrier",
        "crossing",
        "highway",
        "kerb",
        "tactile_paving",
        "wheelchair",
    }
    way_tags = {
        "access",
        "bridge",
        "covered",
        "crossing",
        "foot",
        "highway",
        "incline",
        "layer",
        "lit",
        "name",
        "service",
        "sidewalk",
        "sidewalk:left",
        "sidewalk:right",
        "smoothness",
        "surface",
        "tunnel",
        "wheelchair",
        "width",
    }
    ox.settings.useful_tags_node = sorted(set(ox.settings.useful_tags_node) | node_tags)
    ox.settings.useful_tags_way = sorted(set(ox.settings.useful_tags_way) | way_tags)


def download_walk_graph(
    download_polygon_wgs84: Any,
    ox: Any,
    max_retries: int,
) -> Any:
    if max_retries < 1:
        raise ValueError("--max-retries는 1 이상이어야 합니다.")

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            logging.info("OSM 보행망 다운로드 시도 %s/%s", attempt, max_retries)
            return ox.graph.graph_from_polygon(
                download_polygon_wgs84,
                network_type="walk",
                simplify=True,
                retain_all=True,
                truncate_by_edge=False,
            )
        except Exception as exc:  # 네트워크·Overpass 오류 유형이 다양함
            last_error = exc
            if attempt == max_retries:
                break
            wait_seconds = min(60, 15 * (2 ** (attempt - 1)))
            logging.warning("다운로드 실패: %s", exc)
            logging.info("%s초 후 다시 시도합니다.", wait_seconds)
            time.sleep(wait_seconds)
    raise RuntimeError(f"OSM 보행망 다운로드가 최종 실패했습니다: {last_error}") from last_error


def normalize_osm_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return " | ".join(sorted(str(item) for item in value))
    if value is None:
        return "(missing)"
    try:
        if value != value:  # NaN
            return "(missing)"
    except Exception:
        pass
    return str(value)


def annotate_graph(graph: Any, core_wgs84: Any, ox: Any, nx: Any) -> tuple[Any, Any]:
    nodes, edges = ox.convert.graph_to_gdfs(graph)
    nodes["in_dalseo"] = nodes.geometry.intersects(core_wgs84)
    edges["intersects_dalseo"] = edges.geometry.intersects(core_wgs84)

    crossing_nodes = {
        node_id
        for node_id, node_data in graph.nodes(data=True)
        if normalize_osm_value(node_data.get("crossing")) != "(missing)"
        or normalize_osm_value(node_data.get("highway")) == "crossing"
    }

    for node_id, is_inside in nodes["in_dalseo"].items():
        graph.nodes[node_id]["in_dalseo"] = bool(is_inside)
    for (u, v, key), intersects in edges["intersects_dalseo"].items():
        graph.edges[u, v, key]["intersects_dalseo"] = bool(intersects)

    for u, v, key, edge_data in graph.edges(keys=True, data=True):
        highway = normalize_osm_value(edge_data.get("highway"))
        sidewalk = normalize_osm_value(edge_data.get("sidewalk"))
        crossing = normalize_osm_value(edge_data.get("crossing"))
        edge_data["is_steps"] = "steps" in highway.split(" | ")
        edge_data["has_sidewalk_tag"] = sidewalk != "(missing)"
        edge_data["is_crossing"] = (
            crossing != "(missing)"
            or "crossing" in highway.split(" | ")
            or u in crossing_nodes
            or v in crossing_nodes
        )

    # 전체 컴포넌트를 보존한다. 이후 경로 분석 때 출발·도착점의 연결 여부를 별도 검사한다.
    component_id_by_node: dict[Any, int] = {}
    components = sorted(nx.weakly_connected_components(graph), key=len, reverse=True)
    for component_id, component_nodes in enumerate(components, start=1):
        for node_id in component_nodes:
            component_id_by_node[node_id] = component_id
    nx.set_node_attributes(graph, component_id_by_node, "weak_component_id")
    return nodes, edges


def build_summary(
    graph: Any,
    nodes_wgs84: Any,
    edges_wgs84: Any,
    core_metric: Any,
    boundary_path: Path,
    boundary_hash: str,
    dong_rows: list[dict[str, str]],
    admin_prefix: str,
    buffer_m: int,
    ox: Any,
    nx: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    components = sorted((len(c) for c in nx.weakly_connected_components(graph)), reverse=True)
    edge_lengths = edges_wgs84["length"].fillna(0).astype(float)
    edges_metric = edges_wgs84.to_crs(ANALYSIS_CRS)
    clipped_lengths = edges_metric.geometry.intersection(core_metric).length

    highway_counts = Counter(normalize_osm_value(v) for v in edges_wgs84["highway"])
    highway_rows = [
        {"highway": highway, "directed_edge_count": count}
        for highway, count in highway_counts.most_common()
    ]

    sidewalk_tagged = sum(
        normalize_osm_value(value) != "(missing)"
        for value in edges_wgs84.get("sidewalk", [None] * len(edges_wgs84))
    )
    steps_count = sum(
        "steps" in normalize_osm_value(value).split(" | ")
        for value in edges_wgs84["highway"]
    )
    in_core_nodes = int(nodes_wgs84["in_dalseo"].sum())
    intersecting_core_edges = int(edges_wgs84["intersects_dalseo"].sum())

    summary = {
        "status": "collected",
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "OpenStreetMap via Overpass API and OSMnx",
        "source_license": "Open Database License (ODbL)",
        "region": "대구광역시 달서구",
        "network_type": "walk",
        "interpretation": (
            "OSM 태그상 보행 가능한 길의 중심선 네트워크이며, "
            "실제 보도의 완전한 위치 목록 또는 실제 고령자 이동경로가 아님"
        ),
        "analysis_crs": ANALYSIS_CRS,
        "download_buffer_m": buffer_m,
        "boundary": {
            "path": display_path(boundary_path),
            "sha256": boundary_hash,
            "admin_prefix": admin_prefix,
            "dong_count": len(dong_rows),
            "dongs": dong_rows,
        },
        "software": {
            "osmnx": ox.__version__,
            "python": sys.version.split()[0],
        },
        "network": {
            "nodes_download_area": int(len(nodes_wgs84)),
            "nodes_in_dalseo": in_core_nodes,
            "directed_edges_download_area": int(len(edges_wgs84)),
            "directed_edges_intersecting_dalseo": intersecting_core_edges,
            "directed_edge_length_download_area_m": round(float(edge_lengths.sum()), 3),
            "directed_edge_length_clipped_to_dalseo_m": round(float(clipped_lengths.sum()), 3),
            "weak_component_count": len(components),
            "largest_weak_component_nodes": components[0] if components else 0,
            "largest_weak_component_node_share": (
                round(components[0] / len(nodes_wgs84), 6) if components else 0
            ),
            "directed_steps_edges": int(steps_count),
            "directed_edges_with_sidewalk_tag": int(sidewalk_tagged),
            "sidewalk_tag_coverage": (
                round(sidewalk_tagged / len(edges_wgs84), 6) if len(edges_wgs84) else 0
            ),
        },
        "quality_warnings": [
            "달서구 경계 밖 500m 네트워크가 경계 연결성 보존을 위해 포함됨",
            "sidewalk 태그 부재는 보도가 없다는 뜻이 아니라 OSM에 속성이 없을 수 있음을 의미",
            "계단(highway=steps)은 고령자 경로에서 제외 또는 페널티 부여를 민감도 분석해야 함",
            "하천·철도·고속도로 횡단과 주요 시설 주변의 누락·단절을 표본 검수해야 함",
            "OpenStreetMap은 수시 갱신되므로 collected_at_utc를 결과 기준시점으로 보존해야 함",
        ],
    }
    return summary, highway_rows


def save_outputs(
    graph: Any,
    core_metric: Any,
    download_metric: Any,
    summary: dict[str, Any],
    highway_rows: list[dict[str, Any]],
    output_dir: Path,
    ox: Any,
    gpd: Any,
    pd: Any,
    skip_plot: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    core_gdf = gpd.GeoDataFrame(
        [{"region": "대구광역시 달서구", "geometry": core_metric}], crs=ANALYSIS_CRS
    ).to_crs("EPSG:4326")
    download_gdf = gpd.GeoDataFrame(
        [{"region": "달서구+다운로드 버퍼", "geometry": download_metric}], crs=ANALYSIS_CRS
    ).to_crs("EPSG:4326")
    core_gdf.to_file(output_dir / "dalseo_boundary_core_wgs84.geojson", driver="GeoJSON")
    download_gdf.to_file(
        output_dir / "dalseo_boundary_download_buffer_wgs84.geojson", driver="GeoJSON"
    )

    graph.graph.update(
        {
            "region": "대구광역시 달서구",
            "network_type": "walk",
            "download_buffer_m": summary["download_buffer_m"],
            "collected_at_utc": summary["collected_at_utc"],
            "source_license": "Open Database License (ODbL)",
        }
    )
    ox.io.save_graphml(graph, output_dir / "dalseo_walk_buffered_wgs84.graphml")

    projected_graph = ox.projection.project_graph(graph, to_crs=ANALYSIS_CRS)
    ox.io.save_graphml(projected_graph, output_dir / "dalseo_walk_buffered_epsg5179.graphml")
    ox.io.save_graph_geopackage(
        projected_graph,
        output_dir / "dalseo_walk_buffered_epsg5179.gpkg",
        directed=True,
    )

    pd.DataFrame(highway_rows).to_csv(
        output_dir / "highway_type_distribution.csv", index=False, encoding="utf-8-sig"
    )
    (output_dir / "collection_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not skip_plot:
        try:
            ox.plot.plot_graph(
                projected_graph,
                node_size=0,
                edge_color="#365F91",
                edge_linewidth=0.45,
                bgcolor="white",
                show=False,
                close=True,
                save=True,
                filepath=output_dir / "dalseo_walk_network_preview.png",
                dpi=200,
            )
        except Exception as exc:
            logging.warning("미리보기 PNG 생성만 실패했습니다: %s", exc)


def main() -> int:
    args = parse_args()
    gpd, nx, ox, pd = import_dependencies()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.buffer_m < 0:
        raise ValueError("--buffer-m은 0 이상이어야 합니다.")

    boundary_path = discover_boundary(args.boundary)
    output_dir = args.output_dir.expanduser().resolve()
    logging.info("행정동 경계: %s", boundary_path)
    logging.info("출력 폴더: %s", output_dir)

    core_metric, core_wgs84, dong_rows = load_dalseo_boundary(
        boundary_path,
        args.admin_prefix,
        args.expected_dongs,
        gpd,
    )
    download_metric = core_metric.buffer(args.buffer_m)
    download_wgs84 = gpd.GeoSeries([download_metric], crs=ANALYSIS_CRS).to_crs("EPSG:4326").iloc[0]

    configure_osmnx(ox, output_dir, args.timeout)
    graph = download_walk_graph(download_wgs84, ox, args.max_retries)
    if len(graph) == 0 or graph.number_of_edges() == 0:
        raise RuntimeError("다운로드 결과가 비어 있습니다.")

    nodes_wgs84, edges_wgs84 = annotate_graph(graph, core_wgs84, ox, nx)
    summary, highway_rows = build_summary(
        graph,
        nodes_wgs84,
        edges_wgs84,
        core_metric,
        boundary_path,
        sha256_file(boundary_path),
        dong_rows,
        args.admin_prefix,
        args.buffer_m,
        ox,
        nx,
    )
    save_outputs(
        graph,
        core_metric,
        download_metric,
        summary,
        highway_rows,
        output_dir,
        ox,
        gpd,
        pd,
        args.skip_plot,
    )

    logging.info("수집 완료: 노드 %s, 방향성 링크 %s", len(nodes_wgs84), len(edges_wgs84))
    logging.info("결과 요약: %s", output_dir / "collection_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
