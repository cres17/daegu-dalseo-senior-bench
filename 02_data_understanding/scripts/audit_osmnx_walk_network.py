#!/usr/bin/env python3
"""달서구 OSMnx 보행망을 자동 검수하고 분석용 QA 속성을 생성한다.

원본 GeoPackage는 수정하지 않는다. 복사본에 QA 컬럼을 추가하고,
자동 처리 결과·수동 검토 후보·품질 요약을 CSV/JSON으로 저장한다.

외부 Python 패키지 없이 표준 라이브러리만 사용한다.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data_external" / "osmnx_dalseo_walk"
DEFAULT_INPUT_GPKG = DEFAULT_SOURCE_DIR / "dalseo_walk_buffered_epsg5179.gpkg"
DEFAULT_SUMMARY = DEFAULT_SOURCE_DIR / "collection_summary.json"
DEFAULT_OUTPUT_DIR = DEFAULT_SOURCE_DIR / "qa"

QA_COLUMNS = {
    "qa_component_id": "INTEGER",
    "qa_in_main_component": "BOOLEAN",
    "qa_crossing_tag": "BOOLEAN",
    "qa_crossing_node_adjacent": "BOOLEAN",
    "qa_is_crossing_candidate": "BOOLEAN",
    "qa_manual_review": "BOOLEAN",
    "qa_review_priority": "INTEGER",
    "qa_review_status": "TEXT",
    "qa_route_action": "TEXT",
    "qa_review_reason": "TEXT",
}

TOKEN_PATTERN = re.compile(r"[a-zA-Z_]+")
FOOT_ALLOWED = {"yes", "designated", "permissive", "official"}
ACCESS_BLOCKED = {"no", "private"}
SIDEWALK_EVIDENCE = {"yes", "both", "left", "right", "separate"}
HARD_BLOCK_HIGHWAYS = {"motorway", "motorway_link"}
TRUNK_HIGHWAYS = {"trunk", "trunk_link"}
PRIMARY_HIGHWAYS = {"primary", "primary_link"}
TRAIL_HIGHWAYS = {"track", "bridleway"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="달서구 OSMnx GeoPackage를 자동 검수하고 QA 복사본을 생성합니다."
    )
    parser.add_argument("--input-gpkg", type=Path, default=DEFAULT_INPUT_GPKG)
    parser.add_argument("--collection-summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def tokens(value: Any) -> set[str]:
    return {token.lower() for token in TOKEN_PATTERN.findall(clean(value))}


def normalize_osmid_key(value: Any) -> str:
    """OSM way ID 목록의 순서 차이를 제거해 양방향 링크를 같은 구간으로 묶는다."""
    numeric_ids = re.findall(r"\d+", clean(value))
    if numeric_ids:
        return "|".join(sorted(set(numeric_ids), key=int))
    return clean(value)


def truthy(value: Any) -> bool:
    return value in (1, True, "1", "true", "True")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.stem}_", suffix=target.suffix, dir=target.parent, delete=False
    ) as temp_file:
        temp_path = Path(temp_file.name)
    try:
        shutil.copy2(source, temp_path)
        temp_path.replace(target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def validate_schema(connection: sqlite3.Connection) -> None:
    node_required = {
        "osmid",
        "in_dalseo",
        "weak_component_id",
        "crossing",
        "highway",
        "geom",
    }
    edge_required = {
        "fid",
        "u",
        "v",
        "key",
        "osmid",
        "highway",
        "length",
        "intersects_dalseo",
        "is_steps",
        "sidewalk",
        "foot",
        "access",
        "service",
        "crossing",
        "geom",
    }
    node_missing = node_required - table_columns(connection, "nodes")
    edge_missing = edge_required - table_columns(connection, "edges")
    if node_missing or edge_missing:
        raise ValueError(
            f"입력 GeoPackage 스키마가 예상과 다릅니다. "
            f"nodes 누락={sorted(node_missing)}, edges 누락={sorted(edge_missing)}"
        )


def profile_network(connection: sqlite3.Connection) -> dict[str, Any]:
    node_row = connection.execute(
        """
        SELECT
            COUNT(*) AS node_count,
            SUM(CASE WHEN in_dalseo=1 THEN 1 ELSE 0 END) AS nodes_in_dalseo,
            COUNT(DISTINCT weak_component_id) AS component_count,
            SUM(CASE WHEN geom IS NULL THEN 1 ELSE 0 END) AS nodes_missing_geometry,
            COUNT(*)-COUNT(DISTINCT osmid) AS duplicate_node_ids
        FROM nodes
        """
    ).fetchone()
    edge_row = connection.execute(
        """
        SELECT
            COUNT(*) AS edge_count,
            SUM(CASE WHEN intersects_dalseo=1 THEN 1 ELSE 0 END) AS edges_in_dalseo,
            SUM(CASE WHEN length IS NULL THEN 1 ELSE 0 END) AS null_length,
            SUM(CASE WHEN length<=0 THEN 1 ELSE 0 END) AS nonpositive_length,
            SUM(CASE WHEN geom IS NULL THEN 1 ELSE 0 END) AS edges_missing_geometry,
            COUNT(*)-COUNT(DISTINCT CAST(u AS TEXT)||'|'||CAST(v AS TEXT)||'|'||CAST(key AS TEXT))
                AS duplicate_edge_keys,
            ROUND(SUM(length),3) AS directed_length_m
        FROM edges
        """
    ).fetchone()
    orphan_edges = connection.execute(
        """
        SELECT COUNT(*)
        FROM edges e
        LEFT JOIN nodes u ON e.u=u.osmid
        LEFT JOIN nodes v ON e.v=v.osmid
        WHERE u.osmid IS NULL OR v.osmid IS NULL
        """
    ).fetchone()[0]
    component_rows = connection.execute(
        """
        SELECT
            weak_component_id,
            COUNT(*) AS nodes,
            SUM(CASE WHEN in_dalseo=1 THEN 1 ELSE 0 END) AS nodes_in_dalseo
        FROM nodes
        GROUP BY weak_component_id
        ORDER BY nodes DESC, weak_component_id
        """
    ).fetchall()
    if not component_rows:
        raise ValueError("노드 테이블이 비어 있습니다.")
    main_component_id = int(component_rows[0][0])
    main_component_nodes = int(component_rows[0][1])
    dalseo_nodes_off_main = sum(
        int(row[2]) for row in component_rows if int(row[0]) != main_component_id
    )

    return {
        "node_count": int(node_row[0]),
        "nodes_in_dalseo": int(node_row[1]),
        "component_count": int(node_row[2]),
        "nodes_missing_geometry": int(node_row[3]),
        "duplicate_node_ids": int(node_row[4]),
        "edge_count": int(edge_row[0]),
        "edges_in_dalseo": int(edge_row[1]),
        "null_length": int(edge_row[2]),
        "nonpositive_length": int(edge_row[3]),
        "edges_missing_geometry": int(edge_row[4]),
        "duplicate_edge_keys": int(edge_row[5]),
        "directed_length_m": float(edge_row[6]),
        "orphan_edges": int(orphan_edges),
        "main_component_id": main_component_id,
        "main_component_nodes": main_component_nodes,
        "main_component_node_share": round(
            main_component_nodes / int(node_row[0]), 6
        ),
        "dalseo_nodes_off_main": dalseo_nodes_off_main,
        "dalseo_nodes_off_main_share": round(
            dalseo_nodes_off_main / int(node_row[1]), 6
        ),
        "components": [
            {
                "component_id": int(row[0]),
                "nodes": int(row[1]),
                "nodes_in_dalseo": int(row[2]),
            }
            for row in component_rows
        ],
    }


def load_nodes(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = connection.execute(
        "SELECT osmid, weak_component_id, in_dalseo, crossing, highway FROM nodes"
    )
    return {
        int(osmid): {
            "component_id": int(component_id),
            "in_dalseo": truthy(in_dalseo),
            "is_crossing": bool(clean(crossing))
            or "crossing" in tokens(highway),
        }
        for osmid, component_id, in_dalseo, crossing, highway in rows
    }


def classify_edge(
    row: sqlite3.Row,
    nodes: dict[int, dict[str, Any]],
    main_component_id: int,
) -> dict[str, Any]:
    u = int(row["u"])
    v = int(row["v"])
    u_node = nodes[u]
    v_node = nodes[v]
    component_id = int(u_node["component_id"])
    highway_tokens = tokens(row["highway"])
    foot_tokens = tokens(row["foot"])
    access_tokens = tokens(row["access"])
    sidewalk_tokens = tokens(row["sidewalk"])
    service_tokens = tokens(row["service"])

    foot_allowed = bool(foot_tokens & FOOT_ALLOWED)
    access_blocked = bool(access_tokens & ACCESS_BLOCKED) and not foot_allowed
    sidewalk_evidence = bool(sidewalk_tokens & SIDEWALK_EVIDENCE)
    hard_block = bool(highway_tokens & HARD_BLOCK_HIGHWAYS) and not (
        foot_allowed or sidewalk_evidence
    )
    trunk_without_evidence = bool(highway_tokens & TRUNK_HIGHWAYS) and not (
        foot_allowed or sidewalk_evidence
    )
    primary_without_evidence = bool(highway_tokens & PRIMARY_HIGHWAYS) and not (
        foot_allowed or sidewalk_evidence
    )
    is_steps = truthy(row["is_steps"]) or "steps" in highway_tokens
    is_trail = bool(highway_tokens & TRAIL_HIGHWAYS)
    is_service = "service" in highway_tokens
    disconnected = component_id != main_component_id
    invalid_length = row["length"] is None or float(row["length"]) <= 0
    buffer_only = not truthy(row["intersects_dalseo"])

    crossing_tag = bool(clean(row["crossing"])) or "crossing" in highway_tokens
    crossing_node_adjacent = bool(u_node["is_crossing"] or v_node["is_crossing"])
    crossing_candidate = crossing_tag or crossing_node_adjacent

    reasons: list[str] = []
    if invalid_length:
        reasons.append("길이 결측 또는 0 이하")
    if access_blocked:
        reasons.append("보행 허용 근거 없는 access=no/private")
    if hard_block:
        reasons.append("보행 허용 근거 없는 motorway 계열")
    if trunk_without_evidence:
        reasons.append("보행 허용·보도 태그 없는 trunk 계열")
    if disconnected:
        reasons.append("주 연결망 밖 연결요소")
    if is_steps:
        reasons.append("계단 링크")
    if is_trail:
        reasons.append("track/bridleway 링크")
    if primary_without_evidence:
        reasons.append("보행 근거 태그 없는 primary 계열")
    if is_service:
        service_detail = "/".join(sorted(service_tokens)) if service_tokens else "미분류"
        reasons.append(f"service 도로({service_detail})")
    if buffer_only:
        reasons.append("달서구 경계 밖 버퍼 지원 링크")

    if invalid_length or access_blocked or hard_block:
        review_status = "AUTO_EXCLUDE"
        route_action = "EXCLUDE"
        review_priority = 3
        manual_review = False
    elif disconnected:
        review_status = "AUTO_NO_ROUTE"
        route_action = "NO_ROUTE_COMPONENT"
        review_priority = 2
        manual_review = False
    elif trunk_without_evidence and buffer_only:
        review_status = "AUTO_EXCLUDE"
        route_action = "EXCLUDE_CONSERVATIVE"
        review_priority = 2
        manual_review = False
    elif trunk_without_evidence:
        review_status = "REVIEW_REQUIRED"
        route_action = "EXCLUDE_CONSERVATIVE"
        review_priority = 3
        manual_review = True
    elif is_steps:
        review_status = "AUTO_PENALIZE"
        route_action = "PENALIZE_STEPS"
        review_priority = 2
        manual_review = False
    elif is_trail:
        review_status = "AUTO_PENALIZE"
        route_action = "PENALIZE_TRAIL"
        review_priority = 1
        manual_review = False
    elif primary_without_evidence or is_service:
        review_status = "REVIEW_IF_SELECTED"
        route_action = "KEEP_REVIEW_IF_SELECTED"
        review_priority = 1
        manual_review = False
    elif buffer_only:
        review_status = "AUTO_KEEP"
        route_action = "BUFFER_SUPPORT"
        review_priority = 0
        manual_review = False
    else:
        review_status = "AUTO_KEEP"
        route_action = "KEEP"
        review_priority = 0
        manual_review = False

    return {
        "fid": int(row["fid"]),
        "u": u,
        "v": v,
        "key": int(row["key"]),
        "osmid": clean(row["osmid"]),
        "name": clean(row["name"]),
        "highway": clean(row["highway"]),
        "length": float(row["length"]) if row["length"] is not None else None,
        "component_id": component_id,
        "in_main_component": component_id == main_component_id,
        "crossing_tag": crossing_tag,
        "crossing_node_adjacent": crossing_node_adjacent,
        "crossing_candidate": crossing_candidate,
        "manual_review": manual_review,
        "review_priority": review_priority,
        "review_status": review_status,
        "route_action": route_action,
        "review_reason": " | ".join(reasons) if reasons else "자동검수 이상 없음",
    }


def add_qa_columns(connection: sqlite3.Connection) -> None:
    existing = table_columns(connection, "edges")
    conflicts = set(QA_COLUMNS) & existing
    if conflicts:
        raise ValueError(f"입력 GeoPackage에 QA 컬럼이 이미 있습니다: {sorted(conflicts)}")
    for column, data_type in QA_COLUMNS.items():
        connection.execute(f"ALTER TABLE edges ADD COLUMN {column} {data_type}")


def update_qa_columns(
    connection: sqlite3.Connection, classifications: list[dict[str, Any]]
) -> None:
    values = [
        (
            row["component_id"],
            int(row["in_main_component"]),
            int(row["crossing_tag"]),
            int(row["crossing_node_adjacent"]),
            int(row["crossing_candidate"]),
            int(row["manual_review"]),
            row["review_priority"],
            row["review_status"],
            row["route_action"],
            row["review_reason"],
            row["fid"],
        )
        for row in classifications
    ]
    # GeoPackage의 RTree UPDATE 트리거는 일반 sqlite3에 없는 ST_IsEmpty 등
    # 공간 함수를 호출한다. 기하를 변경하지 않는 비공간 속성 UPDATE 동안만
    # 트리거 정의를 보존해 제거한 뒤 동일 SQL로 복구한다.
    trigger_rows = connection.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type='trigger' AND tbl_name='edges' AND sql IS NOT NULL
        ORDER BY name
        """
    ).fetchall()
    for trigger_name, _ in trigger_rows:
        quoted_name = '"' + str(trigger_name).replace('"', '""') + '"'
        connection.execute(f"DROP TRIGGER {quoted_name}")
    try:
        connection.executemany(
            """
            UPDATE edges SET
                qa_component_id=?,
                qa_in_main_component=?,
                qa_crossing_tag=?,
                qa_crossing_node_adjacent=?,
                qa_is_crossing_candidate=?,
                qa_manual_review=?,
                qa_review_priority=?,
                qa_review_status=?,
                qa_route_action=?,
                qa_review_reason=?
            WHERE fid=?
            """,
            values,
        )
    finally:
        for _, trigger_sql in trigger_rows:
            connection.execute(trigger_sql)
    connection.execute(
        "CREATE INDEX idx_edges_qa_review_status ON edges(qa_review_status)"
    )
    connection.execute(
        "CREATE INDEX idx_edges_qa_route_action ON edges(qa_route_action)"
    )
    connection.execute(
        "CREATE INDEX idx_edges_qa_manual_review ON edges(qa_manual_review)"
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def build_manual_review_rows(
    classifications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[int, int, str], dict[str, Any]] = {}
    for row in classifications:
        if not row["manual_review"]:
            continue
        normalized_osmid = normalize_osmid_key(row["osmid"])
        segment_key = (min(row["u"], row["v"]), max(row["u"], row["v"]), normalized_osmid)
        current = deduplicated.get(segment_key)
        if current is None:
            deduplicated[segment_key] = {
                "segment_id": f"{segment_key[0]}-{segment_key[1]}-{segment_key[2]}",
                "u": segment_key[0],
                "v": segment_key[1],
                "osmid": row["osmid"],
                "name": row["name"],
                "highway": row["highway"],
                "length_m": round(row["length"] or 0, 3),
                "component_id": row["component_id"],
                "review_reason": row["review_reason"],
                "recommended_default": row["route_action"],
                "directed_edge_count": 1,
            }
        else:
            current["directed_edge_count"] += 1
            current["length_m"] = max(current["length_m"], round(row["length"] or 0, 3))
    return sorted(
        deduplicated.values(),
        key=lambda row: (row["name"] or "", row["u"], row["v"]),
    )


def build_findings(
    profile: dict[str, Any],
    classifications: list[dict[str, Any]],
    collection_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    actions = Counter(row["route_action"] for row in classifications)
    manual_edges = sum(row["manual_review"] for row in classifications)
    crossing_candidates = sum(row["crossing_candidate"] for row in classifications)
    collection_network = collection_summary.get("network", {})
    count_consistent = (
        collection_network.get("nodes_download_area") == profile["node_count"]
        and collection_network.get("directed_edges_download_area") == profile["edge_count"]
    )
    return [
        {
            "finding_id": "OSM-QA-01",
            "severity": "Low" if count_consistent else "High",
            "finding": "수집 요약과 GeoPackage 노드·링크 수 일치",
            "evidence": (
                f"nodes={profile['node_count']}, edges={profile['edge_count']}, "
                f"consistent={count_consistent}"
            ),
            "impact": "산출물 간 개수 불일치가 없으면 동일 수집본으로 판단 가능",
            "action": "재수집 시 동일 자동검사 반복",
        },
        {
            "finding_id": "OSM-QA-02",
            "severity": "Medium",
            "finding": "달서구 내부 노드 일부가 주 연결망 밖에 존재",
            "evidence": (
                f"{profile['dalseo_nodes_off_main']}/{profile['nodes_in_dalseo']} "
                f"({profile['dalseo_nodes_off_main_share']:.3%})"
            ),
            "impact": "서로 다른 연결요소 간 최단경로는 존재하지 않음",
            "action": "qa_route_action=NO_ROUTE_COMPONENT 자동 적용",
        },
        {
            "finding_id": "OSM-QA-03",
            "severity": "Medium",
            "finding": "기존 is_crossing=0 오류를 원본 태그와 인접 노드로 보정",
            "evidence": f"교차 후보 방향성 링크 {crossing_candidates}개",
            "impact": "횡단시설 존재 지표의 과소계상 방지",
            "action": "qa_is_crossing_candidate 사용",
        },
        {
            "finding_id": "OSM-QA-04",
            "severity": "High" if manual_edges else "Low",
            "finding": "보행 근거 태그 없는 trunk 계열을 수동검수 후보로 분리",
            "evidence": f"방향성 링크 {manual_edges}개",
            "impact": "자동차전용 또는 보행불가 간선도로가 경로에 포함될 위험",
            "action": "기본 EXCLUDE_CONSERVATIVE, 확인 후 예외 승인",
        },
        {
            "finding_id": "OSM-QA-05",
            "severity": "Low",
            "finding": "길이·키·참조 무결성 검사",
            "evidence": (
                f"null_length={profile['null_length']}, "
                f"nonpositive_length={profile['nonpositive_length']}, "
                f"duplicate_edge_keys={profile['duplicate_edge_keys']}, "
                f"orphan_edges={profile['orphan_edges']}"
            ),
            "impact": "거리비용과 그래프 참조 무결성 확인",
            "action": "값이 0이 아니면 분석 중단",
        },
        {
            "finding_id": "OSM-QA-06",
            "severity": "Medium",
            "finding": "자동 경로정책 분류 완료",
            "evidence": json.dumps(actions, ensure_ascii=False, sort_keys=True),
            "impact": "사람이 전체 링크를 검수할 필요 없이 예외 후보만 확인 가능",
            "action": "분석 코드에서 qa_route_action을 비용·제외 규칙으로 사용",
        },
    ]


def add_metadata_tables(
    connection: sqlite3.Connection,
    summary: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    connection.execute(
        "CREATE TABLE qa_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    metadata_rows = [
        ("generated_at_utc", summary["generated_at_utc"]),
        ("source_sha256", summary["source_sha256"]),
        ("main_component_id", str(summary["profile"]["main_component_id"])),
        ("manual_review_segments", str(summary["manual_review_segment_count"])),
        ("policy_version", summary["policy_version"]),
    ]
    connection.executemany("INSERT INTO qa_metadata(key,value) VALUES (?,?)", metadata_rows)
    connection.execute(
        """
        CREATE TABLE qa_findings (
            finding_id TEXT PRIMARY KEY,
            severity TEXT,
            finding TEXT,
            evidence TEXT,
            impact TEXT,
            action TEXT
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO qa_findings
        (finding_id,severity,finding,evidence,impact,action)
        VALUES (:finding_id,:severity,:finding,:evidence,:impact,:action)
        """,
        findings,
    )


def main() -> int:
    args = parse_args()
    input_gpkg = args.input_gpkg.expanduser().resolve()
    summary_path = args.collection_summary.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not input_gpkg.exists():
        raise FileNotFoundError(f"입력 GeoPackage가 없습니다: {input_gpkg}")
    if not summary_path.exists():
        raise FileNotFoundError(f"수집 요약 JSON이 없습니다: {summary_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    source_hash = sha256_file(input_gpkg)
    collection_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    output_gpkg = output_dir / "dalseo_walk_qa_epsg5179.gpkg"
    atomic_copy(input_gpkg, output_gpkg)

    connection = sqlite3.connect(output_gpkg)
    connection.row_factory = sqlite3.Row
    try:
        validate_schema(connection)
        profile = profile_network(connection)
        nodes = load_nodes(connection)
        edge_rows = connection.execute(
            """
            SELECT
                fid,u,v,key,osmid,highway,name,length,intersects_dalseo,
                is_steps,sidewalk,foot,access,service,crossing
            FROM edges
            ORDER BY fid
            """
        ).fetchall()
        classifications = [
            classify_edge(row, nodes, profile["main_component_id"]) for row in edge_rows
        ]
        add_qa_columns(connection)
        update_qa_columns(connection, classifications)

        manual_rows = build_manual_review_rows(classifications)
        findings = build_findings(profile, classifications, collection_summary)
        action_counts = Counter(row["route_action"] for row in classifications)
        status_counts = Counter(row["review_status"] for row in classifications)
        summary = {
            "status": "qa_completed",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "policy_version": "1.0",
            "source_gpkg": str(input_gpkg),
            "source_sha256": source_hash,
            "output_gpkg": str(output_gpkg),
            "profile": profile,
            "route_action_counts": dict(sorted(action_counts.items())),
            "review_status_counts": dict(sorted(status_counts.items())),
            "manual_review_directed_edge_count": sum(
                row["manual_review"] for row in classifications
            ),
            "manual_review_segment_count": len(manual_rows),
            "crossing_candidate_directed_edge_count": sum(
                row["crossing_candidate"] for row in classifications
            ),
            "interpretation": (
                "원본 OSMnx 보행망의 자동검수 결과다. AUTO_KEEP은 실제 보도 완전성을 "
                "보증하지 않으며, 최종 후보구간은 현장 또는 지도 확인이 필요하다."
            ),
        }
        add_metadata_tables(connection, summary, findings)
        connection.commit()
    finally:
        connection.close()

    write_csv(
        output_dir / "manual_review_segments.csv",
        manual_rows,
        [
            "segment_id",
            "u",
            "v",
            "osmid",
            "name",
            "highway",
            "length_m",
            "component_id",
            "review_reason",
            "recommended_default",
            "directed_edge_count",
        ],
    )
    write_csv(
        output_dir / "qa_findings.csv",
        findings,
        ["finding_id", "severity", "finding", "evidence", "impact", "action"],
    )
    action_rows = [
        {"route_action": action, "directed_edge_count": count}
        for action, count in sorted(action_counts.items())
    ]
    write_csv(
        output_dir / "route_action_counts.csv",
        action_rows,
        ["route_action", "directed_edge_count"],
    )
    (output_dir / "qa_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "status": summary["status"],
                "nodes": profile["node_count"],
                "directed_edges": profile["edge_count"],
                "manual_review_segments": len(manual_rows),
                "output_gpkg": str(output_gpkg),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
