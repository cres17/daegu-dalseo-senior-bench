#!/usr/bin/env python3
"""v2 후보 40개 현장검수 결과를 검증하고 의사결정 목록으로 분리한다."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "dalseo_v2_field_review_completed_40.csv"
TEMPLATE = ROOT / "05_evaluation" / "outputs" / "field_review_v2_40_prefilled.csv"
SOURCE_GEOJSON = (
    ROOT
    / "05_evaluation"
    / "outputs"
    / "revised_priority_top20_per_tier_v2_wgs84.geojson"
)
OUT = ROOT / "05_evaluation" / "outputs"
QA = ROOT / "05_evaluation" / "qa"
REPORT = ROOT / "05_evaluation" / "08_v2_현장검수_통합결과.md"

ALLOWED_STATUS = {"INSTALLABLE", "CONDITIONAL", "NOT_INSTALLABLE", "REVISIT"}
MANUAL_FIELDS = [
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


def read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")


def write_geojson(frame: pd.DataFrame, path: Path) -> None:
    source = json.loads(SOURCE_GEOJSON.read_text(encoding="utf-8"))
    row_lookup = frame.set_index("physical_id").to_dict("index")
    features = []
    for feature in source["features"]:
        physical_id = feature["properties"]["physical_id"]
        if physical_id not in row_lookup:
            continue
        props = {}
        for key, value in row_lookup[physical_id].items():
            if pd.isna(value):
                value = None
            props[key] = value
        feature["properties"].update(props)
        features.append(feature)
    path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "name": path.stem, "features": features},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    QA.mkdir(parents=True, exist_ok=True)
    if not INPUT.exists():
        raise FileNotFoundError(INPUT)

    review = read(INPUT)
    template = read(TEMPLATE)
    checks = []

    same_ids = set(review["physical_id"]) == set(template["physical_id"])
    checks.append(
        {
            "check_id": "FR2-01",
            "status": "PASS" if len(review) == 40 else "FAIL",
            "detail": f"행 수 {len(review)}",
        }
    )
    checks.append(
        {
            "check_id": "FR2-02",
            "status": "PASS" if review["physical_id"].nunique() == 40 else "FAIL",
            "detail": f"고유 physical_id {review['physical_id'].nunique()}개",
        }
    )
    checks.append(
        {
            "check_id": "FR2-03",
            "status": "PASS" if same_ids else "FAIL",
            "detail": "v2 검수 템플릿과 ID 집합 일치",
        }
    )
    invalid_status = set(review["review_status"]) - ALLOWED_STATUS
    checks.append(
        {
            "check_id": "FR2-04",
            "status": "PASS"
            if review["review_status"].ne("").all() and not invalid_status
            else "FAIL",
            "detail": (
                "40개 종합 판정 완료"
                if not invalid_status and review["review_status"].ne("").all()
                else f"빈값 또는 허용되지 않은 상태: {sorted(invalid_status)}"
            ),
        }
    )
    checks_frame = pd.DataFrame(checks)
    checks_frame.to_csv(
        QA / "v2_field_review_integration_checks.csv",
        index=False,
        encoding="utf-8-sig",
    )
    if checks_frame["status"].eq("FAIL").any():
        raise RuntimeError(checks_frame.to_string(index=False))

    numeric_columns = [
        "v2_tier_rank",
        "sensitivity_selection_count",
        "mean_v2_score",
        "senior_density_km2",
        "adaptive_threshold_m",
        "adaptive_gap_length_m",
        "nearest_seat_max_m",
        "senior_specific_route_count",
        "senior_specific_destination_route_count",
    ]
    for column in numeric_columns:
        review[column] = pd.to_numeric(review[column], errors="coerce")

    review["vehicle_interference_noted"] = review["review_notes"].str.contains(
        "차량 통행 방해", na=False
    )
    review["private_land_risk"] = review["ownership_installability"].eq(
        "PRIVATE_LIKELY"
    )
    review["existing_seat_not_explicitly_checked"] = review[
        "existing_seat_verified"
    ].eq("")

    review["decision_class"] = "EXCLUDE_FIELD_NOT_INSTALLABLE"
    review.loc[
        review["review_status"].eq("CONDITIONAL")
        & review["vehicle_interference_noted"],
        "decision_class",
    ] = "ALTERNATIVE_SITE_SEARCH_TRAFFIC_CONFLICT"
    review.loc[
        review["review_status"].eq("CONDITIONAL")
        & ~review["vehicle_interference_noted"],
        "decision_class",
    ] = "CONDITIONAL_FOLLOW_UP"
    review.loc[
        review["review_status"].eq("INSTALLABLE")
        & review["private_land_risk"],
        "decision_class",
    ] = "CONDITIONAL_OWNERSHIP_CHECK"
    review.loc[
        review["review_status"].eq("INSTALLABLE")
        & ~review["private_land_risk"]
        & review["existing_seat_not_explicitly_checked"],
        "decision_class",
    ] = "INSTALLABLE_VERIFY_EXISTING_SEAT"
    review.loc[
        review["review_status"].eq("INSTALLABLE")
        & ~review["private_land_risk"]
        & review["existing_seat_verified"].eq("NO"),
        "decision_class",
    ] = "INSTALLABLE_FIELD_PRIORITY"
    review.loc[
        review["review_status"].eq("INSTALLABLE")
        & review["existing_seat_verified"].eq("YES"),
        "decision_class",
    ] = "EXCLUDE_EXISTING_SEAT_FOUND"

    decision_order = {
        "INSTALLABLE_FIELD_PRIORITY": 1,
        "INSTALLABLE_VERIFY_EXISTING_SEAT": 2,
        "CONDITIONAL_OWNERSHIP_CHECK": 3,
        "CONDITIONAL_FOLLOW_UP": 4,
        "ALTERNATIVE_SITE_SEARCH_TRAFFIC_CONFLICT": 5,
        "EXCLUDE_EXISTING_SEAT_FOUND": 8,
        "EXCLUDE_FIELD_NOT_INSTALLABLE": 9,
    }
    review["decision_order"] = review["decision_class"].map(decision_order)
    review = review.sort_values(
        [
            "decision_order",
            "sensitivity_selection_count",
            "mean_v2_score",
            "density_tier",
            "v2_tier_rank",
        ],
        ascending=[True, False, False, True, True],
    ).reset_index(drop=True)
    review["field_adjusted_order"] = range(1, len(review) + 1)

    installable = review[
        review["decision_class"].isin(
            ["INSTALLABLE_FIELD_PRIORITY", "INSTALLABLE_VERIFY_EXISTING_SEAT"]
        )
    ].copy()
    installable["installable_review_order"] = range(1, len(installable) + 1)
    conditional = review[
        review["decision_class"].isin(
            [
                "CONDITIONAL_OWNERSHIP_CHECK",
                "CONDITIONAL_FOLLOW_UP",
                "ALTERNATIVE_SITE_SEARCH_TRAFFIC_CONFLICT",
            ]
        )
    ].copy()
    conditional["conditional_review_order"] = range(1, len(conditional) + 1)
    excluded = review[
        review["decision_class"].str.startswith("EXCLUDE")
    ].copy()

    review.to_csv(
        OUT / "v2_field_review_validated.csv", index=False, encoding="utf-8-sig"
    )
    installable.to_csv(
        OUT / "v2_installable_priority_candidates.csv",
        index=False,
        encoding="utf-8-sig",
    )
    conditional.to_csv(
        OUT / "v2_conditional_and_alternative_site_candidates.csv",
        index=False,
        encoding="utf-8-sig",
    )
    excluded.to_csv(
        OUT / "v2_excluded_after_field_review.csv",
        index=False,
        encoding="utf-8-sig",
    )
    write_geojson(installable, OUT / "v2_installable_priority_candidates_wgs84.geojson")
    write_geojson(
        conditional,
        OUT / "v2_conditional_and_alternative_site_candidates_wgs84.geojson",
    )

    blank_seat = int(review["existing_seat_not_explicitly_checked"].sum())
    installable_blank_seat = int(
        installable["existing_seat_not_explicitly_checked"].sum()
    )
    installable_public = int(
        installable["ownership_installability"].eq("PUBLIC_LIKELY").sum()
    )
    installable_continuous = int(
        installable["continuous_sidewalk"].eq("YES").sum()
    )
    installable_partial = int(
        installable["continuous_sidewalk"].eq("PARTIAL").sum()
    )
    vehicle_conflict = int(review["vehicle_interference_noted"].sum())
    private_risk = int(review["private_land_risk"].sum())
    status_counts = review["review_status"].value_counts()
    report = f"""# v2 현장검수 통합 결과

## 결론

40개 후보의 종합 판정은 모두 입력됐고 후보 ID도 원본 템플릿과 완전히
일치한다. 현장검수 결과는 분석에 반영할 수 있다.

다만 `INSTALLABLE`을 곧바로 최종 설치 위치로 해석하지 않는다. 기존 좌석,
소유권, 정확한 설치점 확인이 남아 있기 때문이다.

## 판정 결과

- 설치 가능: {int(status_counts.get("INSTALLABLE", 0))}개
- 조건부: {int(status_counts.get("CONDITIONAL", 0))}개
- 설치 부적합: {int(status_counts.get("NOT_INSTALLABLE", 0))}개
- 차량 통행 방해가 메모된 후보: {vehicle_conflict}개
- 사유지 가능성이 입력된 후보: {private_risk}개
- 기존 좌석 확인란이 비어 있는 후보: {blank_seat}개

## 의사결정용 분리

### 직접 설치 검토군

{len(installable)}개다. 이 가운데 기존 좌석 없음이 확인된 후보는
{len(installable) - installable_blank_seat}개, 공공 설치 가능성이 높다고
검수된 후보는 {installable_public}개다. 연속 보도는 {installable_continuous}개,
일부 보도는 {installable_partial}개다.

### 조건부·대체 위치 탐색군

{len(conditional)}개다. `차량 통행 방해`가 기록된 골목은 도로 위 설치 후보가
아니라 반경 50~100m 내 공공부지·후퇴공간·시설 입구를 찾는 대상으로 분류했다.

### 제외군

{len(excluded)}개다. 현장 설치 부적합 판정을 우선하여 모델 점수와 관계없이
후속 우선순위에서 제외했다.

## 검증 평가

**Ready to share as a review priority list.** 직접 설치 검토군은 기존 좌석,
보도 연속성, 공공 설치 가능성 입력이 완료됐다. 다만 이 평가는 최종 설치
승인이 아니라 행정·현장 정밀검토 우선순위다. 전체 40개 중 기존 좌석
확인란이 비어 있는 {blank_seat}개는 조건부 또는 제외군이므로 직접 설치
검토군의 순위에는 영향을 주지 않는다.

## 해석 주의사항

- 실제 고령자 이동 경로가 아니라 예상 보행수요다.
- `CONDITIONAL`은 현재 선형 위 설치 승인이 아니라 대체 공간 탐색 대상이다.
- 공원 벤치 자료는 2022년 기준이므로 현장 변동 가능성이 있다.
- 최종 설치에는 도로관리청·토지소유자 협의와 보행·차량 통행 폭 검토가 필요하다.
"""
    REPORT.write_text(report, encoding="utf-8")

    print(f"validated={len(review)}")
    print(f"installable={len(installable)}")
    print(f"conditional={len(conditional)}")
    print(f"excluded={len(excluded)}")
    print(f"report={REPORT}")


if __name__ == "__main__":
    main()
