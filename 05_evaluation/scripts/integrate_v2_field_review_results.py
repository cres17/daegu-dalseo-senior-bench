#!/usr/bin/env python3
"""v2 후보 40개 현장검수 결과를 검증하고 의사결정 목록으로 분리한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

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

STANDARD_MIN_EFFECTIVE_WIDTH_M = 2.0
EXCEPTION_MIN_EFFECTIVE_WIDTH_M = 1.2
PASSING_ZONE_TRIGGER_WIDTH_M = 1.5
ALLOWED_WIDTH_EXCEPTION_BASES = {
    "TOPOGRAPHY_IMPOSSIBLE",
    "EXISTING_ROAD_UNAVOIDABLE",
}
WIDTH_STANDARD_SOURCE = (
    "교통약자의 이동편의 증진법 시행규칙 별표 1 제3호가목1)"
)
WIDTH_STANDARD_URL = "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=283221"
WIDTH_DEFINITION_URL = (
    "https://law.go.kr/lsLinkCommonInfo.do?"
    "chrClsCd=010202&lsJoLnkSeq=1024181483"
)

ALLOWED_STATUS = {"INSTALLABLE", "CONDITIONAL", "NOT_INSTALLABLE", "REVISIT"}
ALLOWED_MANUAL_VALUES = {
    "existing_seat_verified": {"", "YES", "NO", "UNCERTAIN"},
    "continuous_sidewalk": {"", "YES", "PARTIAL", "NO"},
    "slope_condition": {"", "FLAT", "MODERATE", "STEEP", "UNCERTAIN"},
    "crossing_risk": {"", "LOW", "MEDIUM", "HIGH", "UNCERTAIN"},
    "shade_weather_protection": {"", "GOOD", "PARTIAL", "NONE"},
    "ownership_installability": {
        "",
        "PUBLIC_LIKELY",
        "PRIVATE_LIKELY",
        "UNCERTAIN",
    },
    "width_standard_exception": {"", "NO", "APPROVED", "PENDING"},
    "width_exception_basis": {"", *ALLOWED_WIDTH_EXCEPTION_BASES},
    "passing_zone_compliance": {"", "YES", "NO", "NOT_REQUIRED", "UNCERTAIN"},
    "sloped_rest_landing_compliance": {
        "",
        "YES",
        "NO",
        "NOT_REQUIRED",
        "UNCERTAIN",
    },
}
INSTALLABLE_REQUIRED_FIELDS = [
    "existing_seat_verified",
    "continuous_sidewalk",
    "effective_width_m",
    "width_measurement_method",
    "width_evidence_ref",
    "slope_condition",
    "crossing_risk",
    "ownership_installability",
    "reviewer",
    "review_date",
]
OPTIONAL_WIDTH_EVIDENCE_FIELDS = [
    "width_measurement_method",
    "width_evidence_ref",
    "width_standard_exception",
    "width_exception_authority",
    "width_exception_basis",
    "width_exception_reason",
    "passing_zone_compliance",
    "sloped_rest_landing_compliance",
]
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
    *OPTIONAL_WIDTH_EVIDENCE_FIELDS,
]


def read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")


def evaluate_installable_safety(row: Mapping[str, object]) -> dict[str, object]:
    """INSTALLABLE 판정이 현장 안전 원칙과 일치하는지 보수적으로 검사한다.

    현장 입력 원본은 수정하지 않는다. 직접 설치 검토군으로 내보낼 수
    있는지 별도의 safety gate로 판정해 누락과 모순을 추적한다.
    """

    status = str(row.get("review_status", "")).strip()
    if status != "INSTALLABLE":
        return {
            "safety_gate_status": "NOT_APPLICABLE",
            "safety_gate_reasons": "",
            "safety_gate_missing_fields": "",
            "safety_gate_conflicts": "",
            "effective_width_m_numeric": None,
            "required_effective_width_m": STANDARD_MIN_EFFECTIVE_WIDTH_M,
            "width_compliance_status": "NOT_APPLICABLE",
            "width_exception_path_used": False,
            "width_standard_source": WIDTH_STANDARD_SOURCE,
            "width_standard_url": WIDTH_STANDARD_URL,
            "safety_gate_overrode_status": False,
        }

    values = {
        field: str(row.get(field, "")).strip()
        for field in INSTALLABLE_REQUIRED_FIELDS + OPTIONAL_WIDTH_EVIDENCE_FIELDS
    }
    missing = [
        field for field in INSTALLABLE_REQUIRED_FIELDS if not values[field]
    ]
    conflicts: list[str] = []

    width = pd.to_numeric(values["effective_width_m"], errors="coerce")
    width_number = None if pd.isna(width) else float(width)
    width_compliance_status = "MISSING"
    width_exception_path_used = False
    if values["effective_width_m"] and (width_number is None or width_number <= 0):
        conflicts.append("INVALID_EFFECTIVE_WIDTH")
        width_compliance_status = "INVALID"
    elif width_number is not None:
        if width_number < EXCEPTION_MIN_EFFECTIVE_WIDTH_M:
            conflicts.append(
                f"EFFECTIVE_WIDTH_BELOW_ABSOLUTE_MIN:{width_number:g}"
            )
            width_compliance_status = "BELOW_ABSOLUTE_MIN"
        elif width_number < STANDARD_MIN_EFFECTIVE_WIDTH_M:
            width_exception_path_used = True
            width_compliance_status = "EXCEPTION_REVIEW_REQUIRED"
            exception_status = values["width_standard_exception"]
            if not exception_status:
                missing.append("width_standard_exception")
            elif exception_status != "APPROVED":
                conflicts.append(
                    f"WIDTH_EXCEPTION_NOT_APPROVED:{exception_status}"
                )
            else:
                for field in [
                    "width_exception_authority",
                    "width_exception_basis",
                    "width_exception_reason",
                ]:
                    if not values[field]:
                        missing.append(field)
                if (
                    values["width_exception_basis"]
                    and values["width_exception_basis"]
                    not in ALLOWED_WIDTH_EXCEPTION_BASES
                ):
                    conflicts.append(
                        "WIDTH_EXCEPTION_BASIS_NOT_ALLOWED:"
                        f"{values['width_exception_basis']}"
                    )

                if width_number < PASSING_ZONE_TRIGGER_WIDTH_M:
                    passing_status = values["passing_zone_compliance"]
                    if not passing_status:
                        missing.append("passing_zone_compliance")
                    elif passing_status != "YES":
                        conflicts.append(
                            f"PASSING_ZONE_NOT_CONFIRMED:{passing_status}"
                        )

                    if values["slope_condition"] == "MODERATE":
                        landing_status = values[
                            "sloped_rest_landing_compliance"
                        ]
                        if not landing_status:
                            missing.append("sloped_rest_landing_compliance")
                        elif landing_status != "YES":
                            conflicts.append(
                                "SLOPED_REST_LANDING_NOT_CONFIRMED:"
                                f"{landing_status}"
                            )

                width_evidence_fields = {
                    "width_exception_authority",
                    "width_exception_basis",
                    "width_exception_reason",
                    "passing_zone_compliance",
                    "sloped_rest_landing_compliance",
                }
                width_conflict_prefixes = (
                    "WIDTH_EXCEPTION_NOT_APPROVED",
                    "WIDTH_EXCEPTION_BASIS_NOT_ALLOWED",
                    "PASSING_ZONE_NOT_CONFIRMED",
                    "SLOPED_REST_LANDING_NOT_CONFIRMED",
                )
                if not any(
                    code.startswith(width_conflict_prefixes) for code in conflicts
                ) and not any(field in width_evidence_fields for field in missing):
                    width_compliance_status = "EXCEPTION_PASS"
        else:
            width_compliance_status = "STANDARD_PASS"

    if values["existing_seat_verified"] and values["existing_seat_verified"] != "NO":
        conflicts.append(
            f"EXISTING_SEAT_NOT_ABSENT:{values['existing_seat_verified']}"
        )
    if values["continuous_sidewalk"] and values["continuous_sidewalk"] != "YES":
        conflicts.append(
            f"SIDEWALK_NOT_CONTINUOUS:{values['continuous_sidewalk']}"
        )
    if values["slope_condition"] in {"STEEP", "UNCERTAIN"}:
        conflicts.append(f"SLOPE_NOT_ACCEPTABLE:{values['slope_condition']}")
    if values["crossing_risk"] and values["crossing_risk"] != "LOW":
        conflicts.append(f"CROSSING_RISK_NOT_LOW:{values['crossing_risk']}")
    if (
        values["ownership_installability"]
        and values["ownership_installability"] != "PUBLIC_LIKELY"
    ):
        conflicts.append(
            "OWNERSHIP_NOT_PUBLIC_LIKELY:"
            f"{values['ownership_installability']}"
        )

    if conflicts:
        gate_status = "BLOCKED_CONFLICT"
    elif missing:
        gate_status = "BLOCKED_INCOMPLETE"
    else:
        gate_status = "PASS"

    reasons = [f"MISSING:{field}" for field in missing] + conflicts
    return {
        "safety_gate_status": gate_status,
        "safety_gate_reasons": " | ".join(reasons),
        "safety_gate_missing_fields": " | ".join(missing),
        "safety_gate_conflicts": " | ".join(conflicts),
        "effective_width_m_numeric": width_number,
        "required_effective_width_m": STANDARD_MIN_EFFECTIVE_WIDTH_M,
        "width_compliance_status": width_compliance_status,
        "width_exception_path_used": width_exception_path_used,
        "width_standard_source": WIDTH_STANDARD_SOURCE,
        "width_standard_url": WIDTH_STANDARD_URL,
        "safety_gate_overrode_status": gate_status != "PASS",
    }


def apply_installable_safety_gate(review: pd.DataFrame) -> pd.DataFrame:
    gate = pd.DataFrame(
        [evaluate_installable_safety(row) for row in review.to_dict("records")],
        index=review.index,
    )
    return pd.concat([review.copy(), gate], axis=1)


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
    for field in OPTIONAL_WIDTH_EVIDENCE_FIELDS:
        if field not in review.columns:
            review[field] = ""
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
    invalid_manual_values = {
        field: sorted(set(review[field]) - allowed)
        for field, allowed in ALLOWED_MANUAL_VALUES.items()
        if set(review[field]) - allowed
    }
    checks.append(
        {
            "check_id": "FR2-05",
            "status": "PASS" if not invalid_manual_values else "FAIL",
            "detail": (
                "현장 안전 필드의 코드값이 허용 범위와 일치"
                if not invalid_manual_values
                else f"허용되지 않은 안전 필드 값: {invalid_manual_values}"
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

    review = apply_installable_safety_gate(review)
    safety_findings = review[
        review["safety_gate_status"].isin(
            ["BLOCKED_CONFLICT", "BLOCKED_INCOMPLETE"]
        )
    ].copy()
    safety_findings.to_csv(
        QA / "v2_installable_safety_findings.csv",
        index=False,
        encoding="utf-8-sig",
    )
    checks.append(
        {
            "check_id": "FR2-06",
            "status": "PASS" if safety_findings.empty else "WARN",
            "detail": (
                "INSTALLABLE 입력과 세부 안전 필드 간 모순·누락 없음"
                if safety_findings.empty
                else (
                    f"INSTALLABLE {len(safety_findings)}개를 직접 검토군에서 차단: "
                    f"모순 {int(safety_findings['safety_gate_status'].eq('BLOCKED_CONFLICT').sum())}개, "
                    f"증거 누락만 {int(safety_findings['safety_gate_status'].eq('BLOCKED_INCOMPLETE').sum())}개"
                )
            ),
        }
    )

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
        review["review_status"].eq("REVISIT"),
        "decision_class",
    ] = "REVISIT_FIELD_REVIEW"
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
        & review["safety_gate_status"].eq("BLOCKED_INCOMPLETE"),
        "decision_class",
    ] = "REVISIT_SAFETY_EVIDENCE_INCOMPLETE"
    review.loc[
        review["review_status"].eq("INSTALLABLE")
        & review["safety_gate_status"].eq("BLOCKED_CONFLICT"),
        "decision_class",
    ] = "CONDITIONAL_SAFETY_CONFLICT"
    review.loc[
        review["review_status"].eq("INSTALLABLE")
        & review["safety_gate_status"].eq("PASS"),
        "decision_class",
    ] = "INSTALLABLE_FIELD_PRIORITY"
    review.loc[
        review["review_status"].eq("INSTALLABLE")
        & review["existing_seat_verified"].eq("YES"),
        "decision_class",
    ] = "EXCLUDE_EXISTING_SEAT_FOUND"

    decision_order = {
        "INSTALLABLE_FIELD_PRIORITY": 1,
        "REVISIT_SAFETY_EVIDENCE_INCOMPLETE": 2,
        "CONDITIONAL_SAFETY_CONFLICT": 3,
        "REVISIT_FIELD_REVIEW": 4,
        "CONDITIONAL_FOLLOW_UP": 5,
        "ALTERNATIVE_SITE_SEARCH_TRAFFIC_CONFLICT": 6,
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
        review["decision_class"].eq("INSTALLABLE_FIELD_PRIORITY")
    ].copy()
    installable["installable_review_order"] = range(1, len(installable) + 1)
    conditional = review[
        review["decision_class"].isin(
            [
                "CONDITIONAL_OWNERSHIP_CHECK",
                "CONDITIONAL_FOLLOW_UP",
                "ALTERNATIVE_SITE_SEARCH_TRAFFIC_CONFLICT",
                "CONDITIONAL_SAFETY_CONFLICT",
                "REVISIT_SAFETY_EVIDENCE_INCOMPLETE",
                "REVISIT_FIELD_REVIEW",
            ]
        )
    ].copy()
    conditional["conditional_review_order"] = range(1, len(conditional) + 1)
    excluded = review[
        review["decision_class"].str.startswith("EXCLUDE")
    ].copy()

    direct_gate_pass = installable["safety_gate_status"].eq("PASS").all()
    direct_width_compliant = installable["width_compliance_status"].isin(
        ["STANDARD_PASS", "EXCEPTION_PASS"]
    ).all()
    partition_complete = len(installable) + len(conditional) + len(excluded) == len(review)
    checks.extend(
        [
            {
                "check_id": "FR2-07",
                "status": "PASS" if direct_gate_pass else "FAIL",
                "detail": "직접 설치 검토군은 safety gate PASS만 포함",
            },
            {
                "check_id": "FR2-08",
                "status": "PASS" if partition_complete else "FAIL",
                "detail": (
                    f"의사결정 분할 {len(review)} = 직접 {len(installable)} + "
                    f"조건부·재검수 {len(conditional)} + 제외 {len(excluded)}"
                ),
            },
            {
                "check_id": "FR2-09",
                "status": "PASS" if direct_width_compliant else "FAIL",
                "detail": (
                    "직접 설치 검토군은 유효폭 2.0m 일반 기준 또는 "
                    "문서화된 예외 기준만 포함"
                ),
            },
        ]
    )
    checks_frame = pd.DataFrame(checks)
    checks_frame.to_csv(
        QA / "v2_field_review_integration_checks.csv",
        index=False,
        encoding="utf-8-sig",
    )
    if checks_frame["status"].eq("FAIL").any():
        raise RuntimeError(checks_frame.to_string(index=False))

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
    safety_conflict = int(
        review["safety_gate_status"].eq("BLOCKED_CONFLICT").sum()
    )
    safety_incomplete = int(
        review["safety_gate_status"].eq("BLOCKED_INCOMPLETE").sum()
    )
    sharing_readiness = (
        "직접 설치 검토군으로 공유 가능"
        if len(installable) > 0
        else "직접 설치 검토군으로 공유 불가 — 안전 증거 보완 필요"
    )
    report = f"""# v2 현장검수 통합 결과

## 결론

40개 후보의 종합 판정은 모두 입력됐고 후보 ID도 원본 템플릿과 완전히
일치한다. 다만 종합 `INSTALLABLE` 입력과 세부 안전 필드를 교차검증한 결과,
모순 {safety_conflict}개와 필수 증거 누락 {safety_incomplete}개를 발견했다.
원본 종합판정은 감사 목적으로 보존하고 이 후보들은 직접 검토군에서 차단했다.

**공유 준비 상태: {sharing_readiness}.**

## 판정 결과

- 원본 종합판정 `INSTALLABLE`: {int(status_counts.get("INSTALLABLE", 0))}개
- 원본 종합판정 `CONDITIONAL`: {int(status_counts.get("CONDITIONAL", 0))}개
- 원본 종합판정 `NOT_INSTALLABLE`: {int(status_counts.get("NOT_INSTALLABLE", 0))}개
- 차량 통행 방해가 메모된 후보: {vehicle_conflict}개
- 사유지 가능성이 입력된 후보: {private_risk}개
- 기존 좌석 확인란이 비어 있는 후보: {blank_seat}개
- safety gate 모순 차단: {safety_conflict}개
- safety gate 필수 증거 누락 차단: {safety_incomplete}개

## 의사결정용 분리

### 직접 설치 검토군

{len(installable)}개다. 직접 검토군은 기존 좌석 없음, 연속 보도, 설치 후
최협소 유효폭 2.0m 이상, 허용 가능한 경사, 낮은 횡단 위험, 공공 설치 가능성,
검토자와 검토일을 모두 만족한 후보만 포함한다. 1.2m 이상 2.0m 미만은
관리청의 예외 인정 근거를 필수로 하고, 1.5m 미만은 교행구역과 경사구간
수평 참 요건도 확인한다. 현재 통과 후보의 연속 보도는
{installable_continuous}개, 일부 보도는 {installable_partial}개다.

### 조건부·대체 위치 탐색군

{len(conditional)}개다. 여기에 종합 `INSTALLABLE`이었지만 안전 필드가
모순되거나 필수 증거가 누락된 후보도 포함한다. `차량 통행 방해`가 기록된
골목은 도로 위 설치 후보가 아니라 반경 50~100m 내 공공부지·후퇴공간·시설
입구를 찾는 대상으로 분류했다.

### 제외군

{len(excluded)}개다. 현장 설치 부적합 판정을 우선하여 모델 점수와 관계없이
후속 우선순위에서 제외했다.

## 검증 평가

**{sharing_readiness}.** `review_status`만으로 직접 설치 후보를 공개하지 않고,
세부 안전 필드가 모두 통과한 `decision_class=INSTALLABLE_FIELD_PRIORITY`만
직접 검토군으로 내보낸다. 차단된 후보는 유효폭 실측, 횡단 위험 재평가,
경사·보도 연속성, 검토자·검토일을 보완한 뒤 파이프라인을 다시 실행한다.

유효폭 규칙은 「교통약자의 이동편의 증진법 시행규칙」 별표 1 제3호가목1)을
보수적인 후보 선별 기준으로 적용했다. 유효폭은 도로 전체 폭이나 OSM `width`
태그가 아니라 노상시설을 제외하고 보행에만 이용되는 설치 후 잔여 폭을
최협소 지점에서 현장 실측해야 한다. 이는 관계기관의 법적 판단을 대체하지 않는다.

- 현행 시행규칙: {WIDTH_STANDARD_URL}
- 유효폭 정의 참고: {WIDTH_DEFINITION_URL}

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
