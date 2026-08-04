#!/usr/bin/env python3
"""현장조사 결과를 검증하고 2차 우선 검토순위를 만든다.

현장 판정은 Modeling 점수에 임의 가중치를 더하지 않고 의사결정 게이트로
사용한다. 같은 게이트 안에서는 기존 민감도 출현 강도와 조사 우선순위를
사전식(lexicographic)으로 적용한다.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "05_evaluation"
OUT = EVAL / "outputs"
QA = EVAL / "qa"
TEMPLATE_CANDIDATES = [
    OUT / "field_review_54_prefilled.csv",
    OUT / "field_review_unique_template.csv",
]
MODEL_SCRIPT = (
    ROOT / "04_modeling" / "scripts" / "model_expected_walk_and_bench_gaps.py"
)

MANUAL_COLUMNS = [
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

ALLOWED = {
    "review_status": {
        "INSTALLABLE",
        "CONDITIONAL",
        "NOT_INSTALLABLE",
        "REVISIT",
    },
    "existing_seat_verified": {"YES", "NO", "UNCERTAIN"},
    "continuous_sidewalk": {"YES", "PARTIAL", "NO"},
    "slope_condition": {"FLAT", "MODERATE", "STEEP", "UNCERTAIN"},
    "crossing_risk": {"LOW", "MEDIUM", "HIGH", "UNCERTAIN"},
    "shade_weather_protection": {"GOOD", "PARTIAL", "NONE"},
    "ownership_installability": {
        "PUBLIC_LIKELY",
        "PRIVATE_LIKELY",
        "UNCERTAIN",
    },
}

CRITICAL_EVIDENCE = [
    "existing_seat_verified",
    "continuous_sidewalk",
    "effective_width_m",
    "slope_condition",
    "crossing_risk",
    "ownership_installability",
]
FINAL_TOP_N = 10


def read_text_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")


def newest_review_file() -> Path:
    candidates = sorted(
        ROOT.glob("dalseo_field_review_completed*.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            "프로젝트 루트에 dalseo_field_review_completed*.csv가 없습니다."
        )
    return candidates[0]


def matching_template(review: pd.DataFrame) -> tuple[Path, pd.DataFrame]:
    review_ids = set(review["field_review_unique_id"])
    for path in TEMPLATE_CANDIDATES:
        if not path.exists():
            continue
        template = read_text_csv(path)
        if (
            len(template) == len(review)
            and set(template["field_review_unique_id"]) == review_ids
        ):
            return path, template
    raise ValueError(
        "완료 CSV의 후보 ID 집합과 일치하는 29개 또는 54개 템플릿이 없습니다."
    )


def finding(
    finding_id: str,
    severity: str,
    check: str,
    affected_rows: int,
    denominator: int,
    evidence: str,
    impact: str,
    action: str,
) -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "check": check,
        "affected_rows": int(affected_rows),
        "denominator": int(denominator),
        "affected_rate": affected_rows / denominator if denominator else 0.0,
        "evidence": evidence,
        "decision_impact": impact,
        "recommended_action": action,
    }


def validate_review(
    review: pd.DataFrame, template: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = list(template.columns)
    missing_columns = [column for column in required if column not in review]
    if missing_columns:
        raise ValueError(f"필수 컬럼 누락: {missing_columns}")

    review = review[required].copy()
    findings: list[dict[str, object]] = []
    denominator = len(template)

    duplicate_ids = int(review["field_review_unique_id"].duplicated().sum())
    missing_ids = set(template["field_review_unique_id"]) - set(
        review["field_review_unique_id"]
    )
    extra_ids = set(review["field_review_unique_id"]) - set(
        template["field_review_unique_id"]
    )
    findings.append(
        finding(
            "FR-DQ-01",
            "Critical" if duplicate_ids or missing_ids or extra_ids else "Pass",
            "후보 ID 완전성·유일성",
            duplicate_ids + len(missing_ids) + len(extra_ids),
            denominator,
            (
                f"중복 {duplicate_ids}건, 누락 {len(missing_ids)}건, "
                f"추가 {len(extra_ids)}건"
            ),
            "후보 연결 오류와 잘못된 현장판정 전파 가능",
            "ID 오류가 있으면 통합을 중단하고 원본 템플릿으로 다시 내보낸다.",
        )
    )

    immutable = [
        column
        for column in required
        if column not in MANUAL_COLUMNS and column != "field_review_unique_id"
    ]
    static_left = template[
        ["field_review_unique_id", *immutable]
    ].set_index("field_review_unique_id")
    static_right = review[
        ["field_review_unique_id", *immutable]
    ].set_index("field_review_unique_id")
    common_ids = static_left.index.intersection(static_right.index)
    static_changed = pd.DataFrame(
        False, index=common_ids, columns=immutable
    )
    numeric_static = {
        "threshold_count",
        "review_order_300m",
        "review_order_500m",
        "selection_count_300m",
        "selection_count_500m",
        "route_count_800m",
        "nearest_seat_mean_m",
        "nearest_seat_max_m",
        "gap_length_300m",
        "gap_length_500m",
        "destination_evidence_type_count",
        "midpoint_lat",
        "midpoint_lon",
    }
    for column in immutable:
        left = static_left.loc[common_ids, column].astype(str).str.strip()
        right = static_right.loc[common_ids, column].astype(str).str.strip()
        if column in numeric_static:
            left_number = pd.to_numeric(left, errors="coerce")
            right_number = pd.to_numeric(right, errors="coerce")
            both_blank = left.eq("") & right.eq("")
            both_number = left_number.notna() & right_number.notna()
            equal_number = both_number & (
                (left_number - right_number).abs()
                <= 1e-9 * (1 + left_number.abs())
            )
            static_changed[column] = ~(both_blank | equal_number)
        else:
            static_changed[column] = left.ne(right)
    changed_static_cells = int(static_changed.sum().sum())
    findings.append(
        finding(
            "FR-DQ-02",
            "Critical" if changed_static_cells else "Pass",
            "원본 후보 속성 무결성",
            changed_static_cells,
            denominator * max(len(immutable), 1),
            f"수동입력 대상이 아닌 셀 변경 {changed_static_cells}건",
            "모델 근거가 현장 입력 과정에서 변조될 수 있음",
            "변경된 정적 셀은 원본 템플릿 값으로 복원한다.",
        )
    )

    invalid_total = 0
    invalid_parts = []
    for column, allowed in ALLOWED.items():
        values = review[column].astype(str).str.strip()
        invalid = values.ne("") & ~values.isin(allowed)
        count = int(invalid.sum())
        invalid_total += count
        if count:
            invalid_parts.append(f"{column}={count}")
    findings.append(
        finding(
            "FR-DQ-03",
            "Critical" if invalid_total else "Pass",
            "통제어휘 유효성",
            invalid_total,
            denominator * len(ALLOWED),
            ", ".join(invalid_parts) if invalid_parts else "허용값 위반 없음",
            "판정 게이트 오분류 가능",
            "화면의 선택값만 사용하고 임의 문자열은 정규화한다.",
        )
    )

    blank_status = int(review["review_status"].str.strip().eq("").sum())
    findings.append(
        finding(
            "FR-DQ-04",
            "Critical" if blank_status else "Pass",
            "설치 판정 완결성",
            blank_status,
            denominator,
            f"review_status 공란 {blank_status}건",
            "판정 없는 후보는 2차 순위에 반영할 수 없음",
            "모든 후보에 설치 판정을 입력한다.",
        )
    )

    width = pd.to_numeric(review["effective_width_m"], errors="coerce")
    width_text = review["effective_width_m"].str.strip()
    invalid_width = width_text.ne("") & (width.isna() | width.le(0) | width.gt(20))
    findings.append(
        finding(
            "FR-DQ-05",
            "High" if invalid_width.any() else "Pass",
            "유효 보행폭 형식·범위",
            int(invalid_width.sum()),
            denominator,
            f"입력 {int(width_text.ne('').sum())}건, 범위 오류 {int(invalid_width.sum())}건",
            "설치 후 통행 가능성 판단이 왜곡될 수 있음",
            "0m 초과 20m 이하 숫자로 입력한다.",
        )
    )

    actionable = review["review_status"].isin(["INSTALLABLE", "CONDITIONAL"])
    missing_seat = actionable & review["existing_seat_verified"].str.strip().eq("")
    findings.append(
        finding(
            "FR-DQ-06",
            "High" if missing_seat.any() else "Pass",
            "검토대상 기존 좌석 확인",
            int(missing_seat.sum()),
            int(actionable.sum()),
            f"설치 가능·조건부 {int(actionable.sum())}건 중 기존 좌석 미확인 {int(missing_seat.sum())}건",
            "좌석 공백을 과대평가할 수 있음",
            "우선 후보는 기존 벤치·정류장 의자 존재 여부를 재확인한다.",
        )
    )

    conflict = review["review_status"].eq("INSTALLABLE") & review[
        "existing_seat_verified"
    ].eq("YES")
    findings.append(
        finding(
            "FR-DQ-07",
            "High" if conflict.any() else "Pass",
            "설치 판정과 기존 좌석의 교차 일관성",
            int(conflict.sum()),
            denominator,
            (
                "INSTALLABLE이면서 기존 좌석 YES인 후보: "
                + " | ".join(review.loc[conflict, "field_review_unique_id"])
            )
            if conflict.any()
            else "교차 모순 없음",
            "이미 휴식 공급이 있는 구간을 신규 설치 후보로 유지할 수 있음",
            "기존 좌석의 사용 가능 상태를 재확인하기 전까지 제외한다.",
        )
    )

    critical_completion = (
        review[CRITICAL_EVIDENCE].apply(lambda s: s.str.strip().ne("")).mean(axis=1)
    )
    incomplete_installable = review["review_status"].eq("INSTALLABLE") & (
        critical_completion.lt(1)
    )
    findings.append(
        finding(
            "FR-DQ-08",
            "High" if incomplete_installable.any() else "Pass",
            "INSTALLABLE 세부근거 완결성",
            int(incomplete_installable.sum()),
            int(review["review_status"].eq("INSTALLABLE").sum()),
            (
                f"INSTALLABLE {int(review['review_status'].eq('INSTALLABLE').sum())}건 중 "
                f"핵심 6항목 미완료 {int(incomplete_installable.sum())}건"
            ),
            "설치 가능 판정을 독립적으로 재검증하기 어려움",
            "보도 연속성·폭·경사·횡단·소유권·기존 좌석을 모두 기록한다.",
        )
    )

    no_audit_identity = review["reviewer"].str.strip().eq("") | review[
        "review_date"
    ].str.strip().eq("")
    findings.append(
        finding(
            "FR-DQ-09",
            "Medium" if no_audit_identity.any() else "Pass",
            "조사자·조사일 추적성",
            int(no_audit_identity.sum()),
            denominator,
            f"조사자 또는 조사일 공란 {int(no_audit_identity.sum())}건",
            "검토 시점과 책임주체를 추적하기 어려움",
            "공모전 제출 전 조사자 식별명과 조사일을 일괄 보완한다.",
        )
    )

    status_only_not_installable = review["review_status"].eq(
        "NOT_INSTALLABLE"
    ) & review[
        [
            "continuous_sidewalk",
            "slope_condition",
            "crossing_risk",
            "ownership_installability",
            "review_notes",
        ]
    ].apply(
        lambda row: all(not str(value).strip() for value in row), axis=1
    )
    findings.append(
        finding(
            "FR-DQ-10",
            "Medium" if status_only_not_installable.any() else "Pass",
            "설치불가 판정 사유 추적성",
            int(status_only_not_installable.sum()),
            int(review["review_status"].eq("NOT_INSTALLABLE").sum()),
            (
                f"NOT_INSTALLABLE {int(review['review_status'].eq('NOT_INSTALLABLE').sum())}건 중 "
                f"사유 미기록 {int(status_only_not_installable.sum())}건"
            ),
            "제외 근거를 심사자에게 설명하기 어려움",
            "짧은 메모 또는 핵심 장애요인 하나 이상을 보완한다.",
        )
    )

    review["field_evidence_completion_rate"] = critical_completion
    return review, pd.DataFrame(findings)


def blocking_reasons(row: pd.Series) -> str:
    reasons = []
    if row["existing_seat_verified"] == "YES":
        reasons.append("기존 좌석 확인")
    elif not row["existing_seat_verified"]:
        reasons.append("기존 좌석 미확인")
    if not row["continuous_sidewalk"]:
        reasons.append("보도 연속성 미입력")
    elif row["continuous_sidewalk"] == "NO":
        reasons.append("연속 보도 없음")
    if not row["effective_width_m"]:
        reasons.append("유효폭 미입력")
    if not row["crossing_risk"]:
        reasons.append("횡단 위험 미입력")
    elif row["crossing_risk"] == "HIGH":
        reasons.append("횡단 위험 높음")
    if not row["ownership_installability"]:
        reasons.append("소유권 미입력")
    elif row["ownership_installability"] == "PRIVATE_LIKELY":
        reasons.append("사유지 가능성")
    return " | ".join(reasons)


def decision_band(row: pd.Series) -> str:
    if row["review_status"] == "NOT_INSTALLABLE":
        return "EXCLUDE_FIELD"
    if row["existing_seat_verified"] == "YES":
        return "EXCLUDE_EXISTING_SEAT"
    if row["review_status"] == "REVISIT":
        return "REVISIT"
    if row["review_status"] == "INSTALLABLE":
        verified = (
            row["field_evidence_completion_rate"] == 1
            and row["existing_seat_verified"] == "NO"
            and row["continuous_sidewalk"] in {"YES", "PARTIAL"}
            and row["crossing_risk"] in {"LOW", "MEDIUM"}
            and row["ownership_installability"] == "PUBLIC_LIKELY"
        )
        return "VERIFIED_PRIORITY" if verified else "PRELIMINARY_PRIORITY"
    if row["review_status"] == "CONDITIONAL":
        return "CONDITIONAL_REVIEW"
    return "UNREVIEWED"


def integrate(review: pd.DataFrame) -> pd.DataFrame:
    numeric_columns = [
        "threshold_count",
        "review_order_300m",
        "review_order_500m",
        "selection_count_300m",
        "selection_count_500m",
        "route_count_800m",
        "nearest_seat_mean_m",
        "nearest_seat_max_m",
        "gap_length_300m",
        "gap_length_500m",
        "destination_evidence_type_count",
    ]
    for column in numeric_columns:
        review[column] = pd.to_numeric(review[column], errors="coerce")

    review["model_selection_strength"] = (
        review[["selection_count_300m", "selection_count_500m"]].max(axis=1)
        / 18
    )
    review["best_original_review_order"] = review[
        ["review_order_300m", "review_order_500m"]
    ].min(axis=1, skipna=True)
    review["decision_band"] = review.apply(decision_band, axis=1)
    review["blocking_reasons"] = review.apply(blocking_reasons, axis=1)

    band_order = {
        "VERIFIED_PRIORITY": 1,
        "PRELIMINARY_PRIORITY": 2,
        "CONDITIONAL_REVIEW": 3,
        "REVISIT": 4,
        "EXCLUDE_EXISTING_SEAT": 5,
        "EXCLUDE_FIELD": 6,
        "UNREVIEWED": 7,
    }
    review["decision_band_order"] = review["decision_band"].map(band_order)
    review = review.sort_values(
        [
            "decision_band_order",
            "model_selection_strength",
            "best_original_review_order",
            "route_count_800m",
            "nearest_seat_max_m",
            "field_review_unique_id",
        ],
        ascending=[True, False, True, False, False, True],
    ).reset_index(drop=True)

    actionable = review["decision_band"].isin(
        [
            "VERIFIED_PRIORITY",
            "PRELIMINARY_PRIORITY",
            "CONDITIONAL_REVIEW",
        ]
    )
    review["field_adjusted_review_rank"] = pd.NA
    review.loc[actionable, "field_adjusted_review_rank"] = range(
        1, int(actionable.sum()) + 1
    )
    return review


def export_geojson(ranked: pd.DataFrame, path: Path) -> None:
    spec = importlib.util.spec_from_file_location("dalseo_modeling", MODEL_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Modeling 모듈을 불러올 수 없습니다.")
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
    _, _, _, physical = model.load_network()
    lookup = physical.set_index("physical_id")["coords"].to_dict()

    features = []
    for _, row in ranked.iterrows():
        coords = lookup[row["physical_id"]]
        lon, lat = model.inverse_epsg5179(
            [coordinate[0] for coordinate in coords],
            [coordinate[1] for coordinate in coords],
        )
        properties = {}
        for column, value in row.items():
            if pd.isna(value):
                value = None
            elif hasattr(value, "item"):
                value = value.item()
            properties[column] = value
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [round(float(x), 7), round(float(y), 7)]
                        for x, y in zip(lon, lat)
                    ],
                },
            }
        )
    payload = {
        "type": "FeatureCollection",
        "generated_at": datetime.now().astimezone().isoformat(),
        "name": "달서구 벤치 설치 2차 우선 검토구간",
        "features": features,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def markdown_table(frame: pd.DataFrame, columns: list[str]) -> str:
    labels = {
        "field_adjusted_review_rank": "2차 순위",
        "field_review_unique_id": "ID",
        "name": "도로",
        "candidate_thresholds_m": "기준(m)",
        "review_status": "현장판정",
        "decision_band": "의사결정 구간",
        "model_selection_strength": "모델 안정도",
        "route_count_800m": "경로 중첩",
        "nearest_seat_max_m": "최대 좌석거리(m)",
    }
    lines = [
        "| " + " | ".join(labels.get(column, column) for column in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for _, row in frame.iterrows():
        values = []
        for column in columns:
            value = row[column]
            if pd.isna(value):
                text = ""
            elif column == "model_selection_strength":
                text = f"{float(value):.1%}"
            elif column == "nearest_seat_max_m":
                text = f"{float(value):.1f}"
            else:
                text = str(value)
            values.append(text.replace("|", "/"))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def write_report(
    source: Path,
    ranked: pd.DataFrame,
    findings: pd.DataFrame,
    path: Path,
) -> None:
    status_counts = ranked["review_status"].value_counts()
    band_counts = ranked["decision_band"].value_counts()
    actionable = (
        ranked[ranked["field_adjusted_review_rank"].notna()]
        .head(FINAL_TOP_N)
        .copy()
    )
    top_columns = [
        "field_adjusted_review_rank",
        "field_review_unique_id",
        "name",
        "candidate_thresholds_m",
        "review_status",
        "decision_band",
        "model_selection_strength",
        "route_count_800m",
        "nearest_seat_max_m",
    ]
    quality_columns = [
        "finding_id",
        "severity",
        "check",
        "affected_rows",
        "denominator",
        "evidence",
    ]
    critical_failures = int(
        findings["severity"].isin(["Critical"]).sum()
        - (
            (findings["severity"] == "Critical")
            & (findings["affected_rows"] == 0)
        ).sum()
    )
    report = f"""# 현장조사 통합 2차 Evaluation 보고서

## 1. 결론

현장조사 {len(ranked)}건의 설치 판정은 모두 입력되었고 후보 ID·허용값·원본 속성
무결성 검사는 통과했다. 현장 판정 결과는 `INSTALLABLE`
{int(status_counts.get("INSTALLABLE", 0))}건, `CONDITIONAL`
{int(status_counts.get("CONDITIONAL", 0))}건, `NOT_INSTALLABLE`
{int(status_counts.get("NOT_INSTALLABLE", 0))}건이다.

다만 `VERIFIED_PRIORITY`는 {int(band_counts.get("VERIFIED_PRIORITY", 0))}건이다.
설치 가능 후보의 기존 좌석·보행폭·보도·횡단·소유권 세부근거가 충분하지
않아, 현재 상위 후보는 **최종 설치 위치가 아닌 2차 우선 확인 순서**다.

## 2. 입력과 분석 단위

- 입력 파일: `{source.name}`
- 입력 행: {len(ranked)}개 고유 도로구간
- 분석 단위: `physical_id` 기준 OSM 보행 도로구간
- 벤치 공백 기준: 300m·500m
- 실제 GPS 이동이 아닌 인구·시설·정류소 기반 예상 보행 수요

## 3. 현장판정 반영 원칙

임의의 신규 가중치는 사용하지 않았다.

1. `NOT_INSTALLABLE`은 현장 제외한다.
2. 기존 좌석 `YES`는 좌석의 사용 가능 상태를 재확인할 때까지 제외한다.
3. `INSTALLABLE`은 세부 근거가 완전하면 검증 우선 후보, 불완전하면 예비 후보로 둔다.
4. `CONDITIONAL`은 위치 조정·안전·소유권 협의 대상으로 둔다.
5. 같은 의사결정 구간 안에서는 18개 민감도 조합 출현비율, 기존 조사순위,
   예상 경로 중첩, 최대 좌석거리 순으로 정렬한다.

## 4. 2차 우선 확인 후보

{markdown_table(actionable, top_columns)}

## 5. 데이터 품질 점검

{markdown_table(findings, quality_columns)}

Critical 구조 오류는 {critical_failures}건이다. 구조상 통합은 가능하지만,
`INSTALLABLE`과 기존 좌석 `YES`가 동시에 입력된
{int((ranked["review_status"].eq("INSTALLABLE") & ranked["existing_seat_verified"].eq("YES")).sum())}건은
교차 모순으로 분리했고, 설치 가능 후보 중 기존 좌석 미확인
{int((ranked["review_status"].eq("INSTALLABLE") & ranked["existing_seat_verified"].eq("")).sum())}건은
추가 확인 대상으로 남겼다.

## 6. 해석과 다음 조치

- `PRELIMINARY_PRIORITY` {int(band_counts.get("PRELIMINARY_PRIORITY", 0))}건:
  기존 좌석, 유효폭, 보도 연속성, 횡단 위험, 소유권을 보완한다.
- `CONDITIONAL_REVIEW` {int(band_counts.get("CONDITIONAL_REVIEW", 0))}건:
  위치 이동 또는 관계기관 협의를 전제로 재검토한다.
- `EXCLUDE_EXISTING_SEAT` {int(band_counts.get("EXCLUDE_EXISTING_SEAT", 0))}건:
  기존 좌석의 실제 사용 가능 상태를 확인한다.
- `EXCLUDE_FIELD` {int(band_counts.get("EXCLUDE_FIELD", 0))}건:
  설치불가 사유 메모를 보완하면 공모전 설명력이 높아진다.

보완 후에만 최종 후보 TOP N을 확정한다. 지금 순위는 공모전에서
“현장조사를 반영한 2차 설치 우선 검토구간”으로 표현한다.
"""
    path.write_text(report, encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    QA.mkdir(parents=True, exist_ok=True)
    source = newest_review_file()
    review = read_text_csv(source)
    template_path, template = matching_template(review)
    validated, findings = validate_review(review, template)

    critical_structure = findings[
        findings["finding_id"].isin(["FR-DQ-01", "FR-DQ-02", "FR-DQ-03", "FR-DQ-04"])
        & findings["affected_rows"].gt(0)
    ]
    if not critical_structure.empty:
        raise ValueError(
            "현장조사 파일의 구조·ID·허용값 검증 실패: "
            + " | ".join(critical_structure["finding_id"])
        )

    ranked = integrate(validated)
    actionable = ranked[ranked["field_adjusted_review_rank"].notna()].copy()
    top10 = actionable.head(FINAL_TOP_N).copy()

    validated.to_csv(
        OUT / "field_review_validated.csv",
        index=False,
        encoding="utf-8-sig",
    )
    findings.to_csv(
        QA / "field_review_quality_findings.csv",
        index=False,
        encoding="utf-8-sig",
    )
    ranked.to_csv(
        OUT / "field_adjusted_priority_candidates.csv",
        index=False,
        encoding="utf-8-sig",
    )
    top10.to_csv(
        OUT / "field_adjusted_priority_top10.csv",
        index=False,
        encoding="utf-8-sig",
    )
    export_geojson(
        top10,
        OUT / "field_adjusted_priority_top10_wgs84.geojson",
    )
    export_geojson(
        ranked,
        OUT / "field_adjusted_priority_candidates_wgs84.geojson",
    )
    write_report(
        source,
        ranked,
        findings,
        EVAL / "06_현장조사_통합_평가_보고서.md",
    )

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_file": str(source.relative_to(ROOT)),
        "matched_template": str(template_path.relative_to(ROOT)),
        "row_count": len(ranked),
        "unique_candidate_ids": int(ranked["field_review_unique_id"].nunique()),
        "review_status_counts": ranked["review_status"].value_counts().to_dict(),
        "decision_band_counts": ranked["decision_band"].value_counts().to_dict(),
        "actionable_review_rows": len(actionable),
        "top10_rows": len(top10),
        "critical_structure_failures": int(len(critical_structure)),
        "quality_finding_counts": findings["severity"].value_counts().to_dict(),
        "decision_scope": "field_adjusted_review_priority_not_final_installation",
    }
    (QA / "field_review_integration_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
