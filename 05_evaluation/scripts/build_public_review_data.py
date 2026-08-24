#!/usr/bin/env python3
"""안전 게이트를 통과한 결과만 공개 지도용 JSON으로 내보낸다."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "05_evaluation" / "outputs" / "v2_field_review_validated.csv"
TARGET = ROOT / "docs" / "candidates.json"

FIELD_LABELS = {
    "effective_width_m": "유효폭",
    "width_measurement_method": "유효폭 측정방법",
    "width_evidence_ref": "유효폭 증빙",
    "reviewer": "검토자",
    "review_date": "검토일",
    "existing_seat_verified": "기존 좌석 확인",
    "continuous_sidewalk": "보도 연속성",
    "slope_condition": "경사",
    "crossing_risk": "횡단 위험",
    "ownership_installability": "공공 설치 가능성",
    "width_standard_exception": "유효폭 예외 승인",
    "width_exception_authority": "예외 승인기관",
    "width_exception_basis": "법정 예외 근거",
    "width_exception_reason": "예외 사유",
    "passing_zone_compliance": "교행구역 확인",
    "sloped_rest_landing_compliance": "경사구간 수평 참 확인",
}
CONFLICT_LABELS = {
    "INVALID_EFFECTIVE_WIDTH": "유효폭 값 오류",
    "EXISTING_SEAT_NOT_ABSENT": "기존 좌석 없음 조건 불충족",
    "SIDEWALK_NOT_CONTINUOUS": "연속 보도 조건 불충족",
    "SLOPE_NOT_ACCEPTABLE": "경사 조건 불충족",
    "CROSSING_RISK_NOT_LOW": "낮은 횡단 위험 조건 불충족",
    "OWNERSHIP_NOT_PUBLIC_LIKELY": "공공 설치 가능성 조건 불충족",
    "EFFECTIVE_WIDTH_BELOW_ABSOLUTE_MIN": "예외 최저 유효폭 1.2m 미달",
    "WIDTH_EXCEPTION_NOT_APPROVED": "유효폭 예외 미승인",
    "WIDTH_EXCEPTION_BASIS_NOT_ALLOWED": "허용되지 않은 유효폭 예외 근거",
    "PASSING_ZONE_NOT_CONFIRMED": "50m 간격 교행구역 미확인",
    "SLOPED_REST_LANDING_NOT_CONFIRMED": "30m 간격 수평 참 미확인",
}


def read_rows() -> list[dict[str, str]]:
    with SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value: str, digits: int = 1) -> float | None:
    if value == "":
        return None
    return round(float(value), digits)


def translate_reasons(value: str) -> list[str]:
    translated = []
    for token in filter(None, (part.strip() for part in value.split("|"))):
        if token.startswith("MISSING:"):
            field = token.split(":", 1)[1]
            translated.append(f"{FIELD_LABELS.get(field, field)} 누락")
            continue
        code, _, detail = token.partition(":")
        label = CONFLICT_LABELS.get(code, code)
        translated.append(f"{label} ({detail})" if detail else label)
    return translated


def public_candidate(row: dict[str, str], rank: int) -> dict[str, object]:
    return {
        "rank": rank,
        "id": row["field_review_unique_id"],
        "name": row["name"] or "도로명 없음",
        "dong": row["admin_dong"],
        "lat": number(row["midpoint_lat"], 7),
        "lon": number(row["midpoint_lon"], 7),
        "tier": (
            "고밀도 300m"
            if row["density_tier"] == "DENSE_300M"
            else "표준 500m"
        ),
        "population": int(float(row["senior_65plus_total"])),
        "density": number(row["senior_density_km2"], 0),
        "gap": number(row["adaptive_gap_length_m"]),
        "seat": number(row["nearest_seat_max_m"]),
        "routes": int(float(row["route_count_800m"])),
        "sensitivity": int(float(row["sensitivity_selection_count"])),
        "sidewalk": row["continuous_sidewalk"] or "미입력",
        "widthM": number(row["effective_width_m"]),
        "widthCompliance": row["width_compliance_status"],
        "requiredWidthM": number(row["required_effective_width_m"]),
        "widthException": row.get("width_standard_exception", "") or "미입력",
        "slope": row["slope_condition"] or "미입력",
        "crossing": row["crossing_risk"] or "미입력",
        "ownership": row["ownership_installability"] or "미입력",
        "gateStatus": row["safety_gate_status"],
        "gateReasons": translate_reasons(row["safety_gate_reasons"]),
        "decisionClass": row["decision_class"],
    }


def main() -> None:
    rows = read_rows()
    direct = [
        row
        for row in rows
        if row["decision_class"] == "INSTALLABLE_FIELD_PRIORITY"
    ]
    blocked = [
        row
        for row in rows
        if row["review_status"] == "INSTALLABLE"
        and row["safety_gate_status"] != "PASS"
    ]
    if any(row["safety_gate_status"] != "PASS" for row in direct):
        raise RuntimeError("공개 직접 검토군에 safety gate 미통과 후보가 있습니다.")
    if len(rows) != 40:
        raise RuntimeError(f"예상한 40개 후보가 아니라 {len(rows)}개입니다.")

    selected = direct if direct else blocked
    mode = "DIRECT_REVIEW" if direct else "SAFETY_REVIEW_REQUIRED"
    payload = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "summary": {
            "total": len(rows),
            "direct": len(direct),
            "safetyBlocked": len(blocked),
            "conditional": sum(
                row["review_status"] == "CONDITIONAL" for row in rows
            ),
            "excluded": sum(
                row["review_status"] == "NOT_INSTALLABLE" for row in rows
            ),
        },
        "candidates": [
            public_candidate(row, rank)
            for rank, row in enumerate(selected, start=1)
        ],
    }
    TARGET.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"mode={mode} direct={len(direct)} "
        f"safety_blocked={len(blocked)} target={TARGET}"
    )


if __name__ == "__main__":
    main()
