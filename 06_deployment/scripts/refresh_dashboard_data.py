#!/usr/bin/env python3
"""Evaluation 결과를 웹 대시보드 데이터로 갱신하고 구조를 검증한다."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVALUATION_SCRIPT = (
    ROOT / "05_evaluation" / "scripts" / "integrate_field_review_results.py"
)
EVALUATION_OUT = ROOT / "05_evaluation" / "outputs"
PUBLIC = ROOT / "06_deployment" / "dashboard_site" / "public"
QA = ROOT / "06_deployment" / "qa"

FILES = {
    "field_adjusted_priority_candidates_wgs84.geojson": "candidates.geojson",
    "field_adjusted_priority_top10.csv": "priority_top10.csv",
    "field_adjusted_priority_candidates.csv": "all_candidates.csv",
}


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    QA.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(EVALUATION_SCRIPT)],
        cwd=ROOT,
        check=True,
    )
    for source_name, target_name in FILES.items():
        shutil.copy2(EVALUATION_OUT / source_name, PUBLIC / target_name)

    payload = json.loads(
        (PUBLIC / "candidates.geojson").read_text(encoding="utf-8")
    )
    features = payload.get("features", [])
    candidate_ids = [
        feature.get("properties", {}).get("field_review_unique_id")
        for feature in features
    ]
    actionable = [
        feature
        for feature in features
        if feature.get("properties", {}).get("field_adjusted_review_rank")
        is not None
    ]
    ranks = sorted(
        int(feature["properties"]["field_adjusted_review_rank"])
        for feature in actionable
    )
    checks = {
        "feature_collection": payload.get("type") == "FeatureCollection",
        "candidate_rows_29": len(features) == 29,
        "candidate_ids_unique": len(set(candidate_ids)) == 29,
        "actionable_rows_10": len(actionable) == 10,
        "actionable_rank_sequence": ranks == list(range(1, 11)),
        "all_line_geometry": all(
            feature.get("geometry", {}).get("type") == "LineString"
            for feature in features
        ),
    }
    if not all(checks.values()):
        raise ValueError(f"대시보드 데이터 검증 실패: {checks}")

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": "05_evaluation 현장조사 통합 Evaluation",
        "dashboard_grain": "physical_id 기준 고유 OSM 보행 도로구간",
        "checks": checks,
        "candidate_rows": len(features),
        "actionable_rows": len(actionable),
        "status_counts": {},
        "decision_band_counts": {},
    }
    for feature in features:
        properties = feature["properties"]
        for key, output_key in [
            ("review_status", "status_counts"),
            ("decision_band", "decision_band_counts"),
        ]:
            value = properties[key]
            summary[output_key][value] = (
                summary[output_key].get(value, 0) + 1
            )
    (QA / "deployment_data_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
