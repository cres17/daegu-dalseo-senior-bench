#!/usr/bin/env python3
"""달서구 보행망 수동검수 CSV를 QA GeoPackage에 안전하게 반영한다.

자동검수 GeoPackage는 수정하지 않고 별도 반영본을 생성한다. 자동 판정은
보존하고, 사람이 입력한 판정과 분석에 사용할 최종 경로 처리값을 새 컬럼에
기록한다.
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
QA_DIR = PROJECT_ROOT / "data_external" / "osmnx_dalseo_walk" / "qa"
DEFAULT_DECISIONS = PROJECT_ROOT / "dalseo_manual_review_decisions.csv"
DEFAULT_MASTER = QA_DIR / "manual_review_segments.csv"
DEFAULT_INPUT_GPKG = QA_DIR / "dalseo_walk_qa_epsg5179.gpkg"
DEFAULT_OUTPUT_GPKG = QA_DIR / "dalseo_walk_reviewed_epsg5179.gpkg"
DEFAULT_SUMMARY = QA_DIR / "manual_review_apply_summary.json"
DEFAULT_VALIDATION = QA_DIR / "manual_review_validation.csv"
DEFAULT_COUNTS = QA_DIR / "manual_review_decision_counts.csv"

REQUIRED_DECISION_COLUMNS = {
    "segment_id",
    "index",
    "name",
    "highway",
    "length_m",
    "manual_decision",
    "review_source",
    "review_note",
    "reviewed_at",
    "recommended_default",
    "review_reason",
}
REQUIRED_MASTER_COLUMNS = {
    "segment_id",
    "u",
    "v",
    "osmid",
    "directed_edge_count",
}
ALLOWED_DECISIONS = {"KEEP", "EXCLUDE", "UNCERTAIN", "FIELD_CHECK"}
DECISION_POLICY = {
    "KEEP": ("KEEP", "MANUAL_KEEP"),
    "EXCLUDE": ("EXCLUDE", "MANUAL_EXCLUDE"),
    "UNCERTAIN": ("EXCLUDE_CONSERVATIVE", "MANUAL_UNCERTAIN"),
    "FIELD_CHECK": ("EXCLUDE_CONSERVATIVE", "MANUAL_FIELD_CHECK"),
}
MANUAL_COLUMNS = {
    "qa_auto_review_status": "TEXT",
    "qa_auto_route_action": "TEXT",
    "qa_manual_decision": "TEXT",
    "qa_manual_review_source": "TEXT",
    "qa_manual_review_note": "TEXT",
    "qa_manual_reviewed_at": "TEXT",
    "qa_effective_route_action": "TEXT",
    "qa_effective_review_status": "TEXT",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="수동검수 CSV를 달서구 보행망 QA GeoPackage에 반영합니다."
    )
    parser.add_argument("--decisions-csv", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--master-csv", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--input-gpkg", type=Path, default=DEFAULT_INPUT_GPKG)
    parser.add_argument("--output-gpkg", type=Path, default=DEFAULT_OUTPUT_GPKG)
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--validation-csv", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--counts-csv", type=Path, default=DEFAULT_COUNTS)
    return parser.parse_args()


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_osmid_key(value: Any) -> str:
    numeric_ids = re.findall(r"\d+", clean(value))
    if numeric_ids:
        return "|".join(sorted(set(numeric_ids), key=int))
    return clean(value)


def segment_id(u: Any, v: Any, osmid: Any) -> str:
    node_u = int(u)
    node_v = int(v)
    return (
        f"{min(node_u, node_v)}-{max(node_u, node_v)}-"
        f"{normalize_osmid_key(osmid)}"
    )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


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


def parse_reviewed_at(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except (TypeError, ValueError):
        return False


def validate_inputs(
    decision_fields: list[str],
    decisions: list[dict[str, str]],
    master_fields: list[str],
    master_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def add(
        check_id: str,
        severity: str,
        passed: bool,
        evidence: str,
        action: str,
    ) -> None:
        findings.append(
            {
                "check_id": check_id,
                "severity": severity,
                "status": "PASS" if passed else "FAIL",
                "evidence": evidence,
                "action": action,
            }
        )

    missing_decision_columns = REQUIRED_DECISION_COLUMNS - set(decision_fields)
    missing_master_columns = REQUIRED_MASTER_COLUMNS - set(master_fields)
    add(
        "MR-01",
        "Critical",
        not missing_decision_columns and not missing_master_columns,
        (
            f"decision_missing={sorted(missing_decision_columns)}, "
            f"master_missing={sorted(missing_master_columns)}"
        ),
        "필수 컬럼을 복구한 뒤 재실행",
    )
    if missing_decision_columns or missing_master_columns:
        return findings

    decision_ids = [clean(row["segment_id"]) for row in decisions]
    master_ids = [clean(row["segment_id"]) for row in master_rows]
    duplicate_decisions = len(decision_ids) - len(set(decision_ids))
    duplicate_master = len(master_ids) - len(set(master_ids))
    add(
        "MR-02",
        "Critical",
        duplicate_decisions == 0 and duplicate_master == 0,
        (
            f"decision_duplicate_ids={duplicate_decisions}, "
            f"master_duplicate_ids={duplicate_master}"
        ),
        "중복 segment_id를 하나로 확정",
    )

    missing_ids = sorted(set(master_ids) - set(decision_ids))
    extra_ids = sorted(set(decision_ids) - set(master_ids))
    add(
        "MR-03",
        "Critical",
        not missing_ids and not extra_ids,
        (
            f"decision_rows={len(decisions)}, master_rows={len(master_rows)}, "
            f"missing={len(missing_ids)}, extra={len(extra_ids)}"
        ),
        "수동검수 후보와 결과 CSV의 ID 집합을 일치시킴",
    )

    invalid_decisions = [
        clean(row["segment_id"])
        for row in decisions
        if clean(row["manual_decision"]) not in ALLOWED_DECISIONS
    ]
    add(
        "MR-04",
        "Critical",
        not invalid_decisions,
        f"invalid_decision_rows={len(invalid_decisions)}",
        f"허용값 사용: {sorted(ALLOWED_DECISIONS)}",
    )

    invalid_times = [
        clean(row["segment_id"])
        for row in decisions
        if not parse_reviewed_at(clean(row["reviewed_at"]))
    ]
    add(
        "MR-05",
        "High",
        not invalid_times,
        f"valid_reviewed_at={len(decisions)-len(invalid_times)}/{len(decisions)}",
        "ISO 8601 검수시각을 저장",
    )

    missing_source = sum(not clean(row["review_source"]) for row in decisions)
    add(
        "MR-06",
        "Low",
        missing_source == 0,
        f"missing_review_source={missing_source}/{len(decisions)}",
        "판정 추적성이 필요하면 확인 출처를 보완",
    )

    missing_note = sum(not clean(row["review_note"]) for row in decisions)
    add(
        "MR-07",
        "Low",
        True,
        f"blank_review_note={missing_note}/{len(decisions)} (선택 입력)",
        "예외적 판단만 메모 보완",
    )
    return findings


def drop_edge_triggers(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type='trigger' AND tbl_name='edges' AND sql IS NOT NULL
        ORDER BY name
        """
    ).fetchall()
    for trigger_name, _ in rows:
        quoted_name = '"' + str(trigger_name).replace('"', '""') + '"'
        connection.execute(f"DROP TRIGGER {quoted_name}")
    return [(str(name), str(sql)) for name, sql in rows]


def restore_triggers(
    connection: sqlite3.Connection, trigger_rows: list[tuple[str, str]]
) -> None:
    for _, trigger_sql in trigger_rows:
        connection.execute(trigger_sql)


def apply_to_geopackage(
    output_gpkg: Path,
    decisions: list[dict[str, str]],
    master_rows: list[dict[str, str]],
    validation_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    decisions_by_id = {clean(row["segment_id"]): row for row in decisions}
    master_by_id = {clean(row["segment_id"]): row for row in master_rows}
    connection = sqlite3.connect(output_gpkg)
    connection.row_factory = sqlite3.Row
    try:
        columns = table_columns(connection, "edges")
        required = {
            "fid",
            "u",
            "v",
            "osmid",
            "qa_manual_review",
            "qa_review_status",
            "qa_route_action",
        }
        missing = required - columns
        if missing:
            raise ValueError(f"QA GeoPackage edges 필수 컬럼 누락: {sorted(missing)}")
        conflicts = set(MANUAL_COLUMNS) & columns
        if conflicts:
            raise ValueError(f"출력본에 수동검수 컬럼이 이미 존재함: {sorted(conflicts)}")

        edge_rows = connection.execute(
            """
            SELECT fid,u,v,osmid,qa_manual_review,qa_review_status,qa_route_action
            FROM edges
            ORDER BY fid
            """
        ).fetchall()
        matching_edges: dict[str, list[int]] = {}
        for row in edge_rows:
            if int(row["qa_manual_review"] or 0) != 1:
                continue
            sid = segment_id(row["u"], row["v"], row["osmid"])
            matching_edges.setdefault(sid, []).append(int(row["fid"]))

        missing_edge_ids = sorted(set(decisions_by_id) - set(matching_edges))
        unexpected_edge_ids = sorted(set(matching_edges) - set(decisions_by_id))
        directed_count_mismatches = []
        for sid, fids in matching_edges.items():
            expected = int(float(master_by_id[sid]["directed_edge_count"]))
            if len(fids) != expected:
                directed_count_mismatches.append(
                    {"segment_id": sid, "expected": expected, "actual": len(fids)}
                )
        if missing_edge_ids or unexpected_edge_ids or directed_count_mismatches:
            raise ValueError(
                "GeoPackage 링크 매핑 불일치: "
                f"missing={len(missing_edge_ids)}, "
                f"unexpected={len(unexpected_edge_ids)}, "
                f"count_mismatch={len(directed_count_mismatches)}"
            )

        for column, data_type in MANUAL_COLUMNS.items():
            connection.execute(f"ALTER TABLE edges ADD COLUMN {column} {data_type}")

        trigger_rows = drop_edge_triggers(connection)
        try:
            connection.execute(
                """
                UPDATE edges SET
                    qa_auto_review_status=qa_review_status,
                    qa_auto_route_action=qa_route_action,
                    qa_effective_review_status=qa_review_status,
                    qa_effective_route_action=qa_route_action
                """
            )
            update_rows = []
            for sid, decision_row in decisions_by_id.items():
                decision = clean(decision_row["manual_decision"])
                effective_action, effective_status = DECISION_POLICY[decision]
                for fid in matching_edges[sid]:
                    update_rows.append(
                        (
                            decision,
                            clean(decision_row["review_source"]),
                            clean(decision_row["review_note"]),
                            clean(decision_row["reviewed_at"]),
                            effective_action,
                            effective_status,
                            fid,
                        )
                    )
            connection.executemany(
                """
                UPDATE edges SET
                    qa_manual_decision=?,
                    qa_manual_review_source=?,
                    qa_manual_review_note=?,
                    qa_manual_reviewed_at=?,
                    qa_effective_route_action=?,
                    qa_effective_review_status=?
                WHERE fid=?
                """,
                update_rows,
            )
        finally:
            restore_triggers(connection, trigger_rows)

        connection.execute(
            "CREATE INDEX idx_edges_qa_manual_decision ON edges(qa_manual_decision)"
        )
        connection.execute(
            "CREATE INDEX idx_edges_qa_effective_action ON edges(qa_effective_route_action)"
        )
        connection.execute(
            "CREATE INDEX idx_edges_qa_effective_status ON edges(qa_effective_review_status)"
        )

        connection.execute(
            """
            CREATE TABLE manual_review_decisions (
                segment_id TEXT PRIMARY KEY,
                review_index INTEGER,
                name TEXT,
                highway TEXT,
                length_m REAL,
                latitude REAL,
                longitude REAL,
                manual_decision TEXT NOT NULL,
                review_source TEXT,
                review_note TEXT,
                reviewed_at TEXT NOT NULL,
                recommended_default TEXT,
                review_reason TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO manual_review_decisions VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    clean(row["segment_id"]),
                    int(row["index"]),
                    clean(row["name"]),
                    clean(row["highway"]),
                    float(row["length_m"]),
                    float(row["lat"]) if clean(row.get("lat")) else None,
                    float(row["lng"]) if clean(row.get("lng")) else None,
                    clean(row["manual_decision"]),
                    clean(row["review_source"]),
                    clean(row["review_note"]),
                    clean(row["reviewed_at"]),
                    clean(row["recommended_default"]),
                    clean(row["review_reason"]),
                )
                for row in decisions
            ],
        )
        connection.execute(
            """
            CREATE TABLE manual_review_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        metadata = {
            "applied_at_utc": datetime.now(timezone.utc).isoformat(),
            "decision_policy_version": "1.0",
            "decision_segment_count": len(decisions),
            "matched_directed_edge_count": sum(len(v) for v in matching_edges.values()),
            "unresolved_segment_count": sum(
                clean(row["manual_decision"]) in {"UNCERTAIN", "FIELD_CHECK"}
                for row in decisions
            ),
        }
        connection.executemany(
            "INSERT INTO manual_review_metadata(key,value) VALUES (?,?)",
            [(key, str(value)) for key, value in metadata.items()],
        )
        connection.execute(
            """
            CREATE TABLE manual_review_validation (
                check_id TEXT PRIMARY KEY,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence TEXT NOT NULL,
                action TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO manual_review_validation
            (check_id,severity,status,evidence,action)
            VALUES (:check_id,:severity,:status,:evidence,:action)
            """,
            validation_findings,
        )
        connection.commit()

        physical_counts = Counter(clean(row["manual_decision"]) for row in decisions)
        directed_counts = Counter()
        for sid, fids in matching_edges.items():
            directed_counts[clean(decisions_by_id[sid]["manual_decision"])] += len(fids)
        effective_counts = Counter(
            clean(DECISION_POLICY[clean(row["manual_decision"])][0])
            for row in decisions
        )
        output_edge_count = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        applied_edge_count = connection.execute(
            "SELECT COUNT(*) FROM edges WHERE qa_manual_decision IS NOT NULL"
        ).fetchone()[0]
        return {
            "output_edge_count": int(output_edge_count),
            "matched_directed_edge_count": int(applied_edge_count),
            "physical_decision_counts": dict(sorted(physical_counts.items())),
            "directed_edge_decision_counts": dict(sorted(directed_counts.items())),
            "effective_physical_action_counts": dict(sorted(effective_counts.items())),
        }
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    decisions_path = args.decisions_csv.expanduser().resolve()
    master_path = args.master_csv.expanduser().resolve()
    input_gpkg = args.input_gpkg.expanduser().resolve()
    output_gpkg = args.output_gpkg.expanduser().resolve()
    summary_path = args.summary_json.expanduser().resolve()
    validation_path = args.validation_csv.expanduser().resolve()
    counts_path = args.counts_csv.expanduser().resolve()

    for path in (decisions_path, master_path, input_gpkg):
        if not path.exists():
            raise FileNotFoundError(f"필수 입력 파일이 없습니다: {path}")

    decision_fields, decisions = read_csv(decisions_path)
    master_fields, master_rows = read_csv(master_path)
    findings = validate_inputs(decision_fields, decisions, master_fields, master_rows)
    failed_blocking = [
        row
        for row in findings
        if row["status"] == "FAIL" and row["severity"] in {"Critical", "High"}
    ]
    write_csv(
        validation_path,
        findings,
        ["check_id", "severity", "status", "evidence", "action"],
    )
    if failed_blocking:
        raise ValueError(
            "수동검수 결과에 반영 차단 오류가 있습니다: "
            + ", ".join(row["check_id"] for row in failed_blocking)
        )

    atomic_copy(input_gpkg, output_gpkg)
    applied = apply_to_geopackage(output_gpkg, decisions, master_rows, findings)

    count_rows = []
    for decision in sorted(ALLOWED_DECISIONS):
        count_rows.append(
            {
                "manual_decision": decision,
                "physical_segment_count": applied["physical_decision_counts"].get(
                    decision, 0
                ),
                "directed_edge_count": applied["directed_edge_decision_counts"].get(
                    decision, 0
                ),
                "effective_route_action": DECISION_POLICY[decision][0],
            }
        )
    write_csv(
        counts_path,
        count_rows,
        [
            "manual_decision",
            "physical_segment_count",
            "directed_edge_count",
            "effective_route_action",
        ],
    )

    summary = {
        "status": "manual_review_applied",
        "applied_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy_version": "1.0",
        "source_qa_gpkg": str(input_gpkg),
        "source_qa_sha256": sha256_file(input_gpkg),
        "decisions_csv": str(decisions_path),
        "decisions_csv_sha256": sha256_file(decisions_path),
        "master_segments_csv": str(master_path),
        "output_gpkg": str(output_gpkg),
        "output_gpkg_sha256": sha256_file(output_gpkg),
        "validation_csv": str(validation_path),
        "counts_csv": str(counts_path),
        "validation": {
            "check_count": len(findings),
            "pass_count": sum(row["status"] == "PASS" for row in findings),
            "fail_count": sum(row["status"] == "FAIL" for row in findings),
            "blocking_fail_count": len(failed_blocking),
        },
        **applied,
        "unresolved_segment_count": sum(
            clean(row["manual_decision"]) in {"UNCERTAIN", "FIELD_CHECK"}
            for row in decisions
        ),
        "interpretation": (
            "qa_effective_route_action을 경로 분석에 사용한다. UNCERTAIN과 "
            "FIELD_CHECK는 확인 전까지 EXCLUDE_CONSERVATIVE로 처리한다."
        ),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
