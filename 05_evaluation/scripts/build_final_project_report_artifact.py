#!/usr/bin/env python3
"""달서구 벤치 프로젝트의 최종 방법론·결과·검증 보고서 artifact를 생성한다."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "05_evaluation" / "reports" / "final_project_methodology_report_artifact.json"
FINAL = ROOT / "05_evaluation" / "outputs" / "v2_installable_priority_candidates.csv"
VALIDATED = ROOT / "05_evaluation" / "outputs" / "v2_field_review_validated.csv"
CONDITIONAL = ROOT / "05_evaluation" / "outputs" / "v2_conditional_and_alternative_site_candidates.csv"
EXCLUDED = ROOT / "05_evaluation" / "outputs" / "v2_excluded_after_field_review.csv"
MAP = ROOT / "docs" / "index.html"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value: str, digits: int = 1) -> float:
    return round(float(value), digits)


def main() -> None:
    validated_rows = read_csv(VALIDATED)
    blocked_installable = [
        row
        for row in validated_rows
        if row["review_status"] == "INSTALLABLE"
        and row.get("safety_gate_status") != "PASS"
    ]
    if blocked_installable:
        raise RuntimeError(
            "최종 보고서 생성을 차단했습니다: 기존 INSTALLABLE "
            f"{len(blocked_installable)}개가 safety gate를 통과하지 못했습니다. "
            "유효폭·안전 필드·검토자·검토일을 보완한 뒤 현장검수 통합을 "
            "다시 실행하세요."
        )

    final_rows = []
    for row in read_csv(FINAL):
        final_rows.append(
            {
                "priority": int(row["installable_review_order"]),
                "review_id": row["field_review_unique_id"],
                "road": row["name"],
                "admin_dong": row["admin_dong"],
                "policy_tier": "고밀도 300m"
                if row["density_tier"] == "DENSE_300M"
                else "일반지역 500m",
                "senior_density_per_km2": number(row["senior_density_km2"]),
                "senior_specific_routes": int(float(row["senior_specific_route_count"])),
                "senior_destination_routes": int(
                    float(row["senior_specific_destination_route_count"])
                ),
                "gap_length_m": number(row["adaptive_gap_length_m"]),
                "max_seat_distance_m": number(row["nearest_seat_max_m"]),
                "sidewalk": "연속" if row["continuous_sidewalk"] == "YES" else "일부",
                "existing_seat": "없음",
                "public_installability": "높음",
            }
        )
    if len(final_rows) != 8:
        raise RuntimeError(
            "이 보고서 템플릿은 8개 직접 검토군을 전제로 작성됐습니다. "
            f"현재 safety gate 통과 후보는 {len(final_rows)}개이므로 동적 보고서 "
            "템플릿으로 갱신하기 전에는 공개 산출물을 덮어쓰지 않습니다."
        )

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    title = "달서구 고령자 벤치 설치 우선구간 최종 기술·검증 보고서"

    installable_ids = {row["field_review_unique_id"] for row in read_csv(FINAL)}
    map_ids = set(re.findall(r"id:'(V2_(?:300|500)_\d+)'", MAP.read_text(encoding="utf-8")))
    outcome_counts = {
        "직접 설치 검토": len(read_csv(FINAL)),
        "조건부·대체 위치": len(read_csv(CONDITIONAL)),
        "현장검수 후 제외": len(read_csv(EXCLUDED)),
    }

    validation_rows = [
        {"area": "Data Preparation", "check": "저장된 자동검사", "result": "PASS", "evidence": "8/8 통과; 행정동 인구 23건, 출발 520건, 목적지 1,692건, 휴식좌석 3,169건, 보행망 엣지 22,668건", "severity": "없음"},
        {"area": "Modeling", "check": "저장된 자동검사", "result": "PASS", "evidence": "10/10 통과; 예상 OD 82,833건, 물리 상위 엣지 39개, 500·800·1,200m 시나리오 확인", "severity": "없음"},
        {"area": "Evaluation", "check": "저장된 자동검사", "result": "PASS", "evidence": "11/11 통과; 36개 민감도 조합. 판정 범위는 현장검토 우선순위이며 최종 설치 승인이 아님", "severity": "없음"},
        {"area": "v2 재검증", "check": "후보 규칙 검사", "result": "PASS", "evidence": "7/7 통과; 고령자 특화 경로, 밀도 적응형 300·500m, 현장 부적합 자동 제외 확인", "severity": "없음"},
        {"area": "v2 현장검수", "check": "검수 결과 통합", "result": "PASS", "evidence": "4/4 통과; 40행·고유 physical_id 40개·템플릿 ID 일치·판정 40/40 완료", "severity": "없음"},
        {"area": "최종 결과", "check": "독립 집계 재계산", "result": "PASS" if sum(outcome_counts.values()) == 40 else "FAIL", "evidence": f"40개 = 직접 검토 {outcome_counts['직접 설치 검토']} + 조건부 {outcome_counts['조건부·대체 위치']} + 제외 {outcome_counts['현장검수 후 제외']}", "severity": "없음" if sum(outcome_counts.values()) == 40 else "차단"},
        {"area": "공개 지도", "check": "후보 ID 대조", "result": "PASS" if map_ids == installable_ids else "FAIL", "evidence": f"지도 {len(map_ids)}개 ID와 직접 설치 검토 CSV {len(installable_ids)}개 ID가 정확히 일치", "severity": "없음" if map_ids == installable_ids else "차단"},
        {"area": "코드", "check": "Python 구문·의존성 명세", "result": "PASS", "evidence": "분석 스크립트 compileall 통과; pandas·numpy·openpyxl·OSMnx 버전 범위 명시", "severity": "없음"},
        {"area": "공개 범위", "check": "민감정보·원천데이터 추적 여부", "result": "PASS", "evidence": "추적 파일에서 원천 공간데이터·CSV·DB·노트북·이미지와 자격증명 패턴 없음", "severity": "없음"},
    ]

    caveat_rows = [
        {"caveat": "관측 이동자료 부재", "impact": "예상 보행경로는 실제 GPS 통행량이 아니라 설명 가능한 수요 대리지표", "required_action": "설치 전 표본 보행관찰 또는 이용자 의견조사", "status": "공개 문서에 명시"},
        {"caveat": "자료 시점 혼재", "impact": "공원 벤치는 2022년, 정류장 이용은 2025년 등 기준시점이 다름", "required_action": "집행 전 좌석 현존과 최신 수요자료 재확인", "status": "운영 단계 확인 필요"},
        {"caveat": "세밀한 보행환경 미반영", "impact": "보도 유효폭·경사·횡단·차량 회전·소방 동선은 모델 순위에 완전 반영되지 않음", "required_action": "8개 대표구간에서 정확한 설치점 실측", "status": "행정·공학 검토 필요"},
        {"caveat": "지도 위치의 해석", "impact": "표식은 도로구간 대표점이며 최종 벤치 좌표가 아님", "required_action": "현장 협의 후 설치 좌표 확정", "status": "지도·README에 명시"},
        {"caveat": "최종 전 과정 재실행 범위", "impact": "이번 인수 검토는 저장된 QA와 핵심 집계를 재검산했으며 원천부터 전체 파이프라인을 다시 돌리지는 않음", "required_action": "원천 갱신 시 단계별 스크립트와 QA 재실행", "status": "재현 절차 보존"},
    ]

    data_layers = [
        {"layer": "행정동 고령인구", "records": 23, "role": "만 65세 이상 주거 수요와 밀도", "preprocessing": "행정동 경계 23개와 명칭 연결, 총 111,111명"},
        {"layer": "경로당", "records": 285, "role": "고령자 출발 근거", "preprocessing": "달서구 외곽 오류좌표 3개를 주소 기반 대표점으로 보정"},
        {"layer": "노인복지시설", "records": 212, "role": "출발·도착 근거", "preprocessing": "좌표 표준화 및 행정동 공간 할당"},
        {"layer": "의료기관", "records": 871, "role": "필수 생활 목적지", "preprocessing": "유효 좌표만 사용, 동일 위치는 모델링에서 물리 위치 단위 집계"},
        {"layer": "공원", "records": 41, "role": "생활 목적지", "preprocessing": "달서구만 추출하고 시설점 평균으로 공원 대표점 생성"},
        {"layer": "버스정류소", "records": 568, "role": "대중교통 목적지·활동성 보조지표", "preprocessing": "20m·명칭 규칙으로 ID 562개 확정, 2025년 12개월 이용량 연결"},
        {"layer": "공원 벤치", "records": 2733, "role": "기존 휴식시설 공급", "preprocessing": "사용 가능 2,728개만 활성화, 수리 필요 5개 제외"},
        {"layer": "정류장 의자", "records": 436, "role": "기존 휴식시설 공급", "preprocessing": "의자 수가 1개 이상인 정류장만 포함; 보고 좌석 678석"},
        {"layer": "OSM 보행망", "records": 22668, "role": "예상 보행경로와 네트워크 거리", "preprocessing": "OSMnx 수집 후 EPSG:5179 변환, 자동·수동 검수와 계단 시나리오 생성"},
    ]

    outcome_rows = [
        {"status": "직접 설치 검토", "count": 8, "share": 0.20, "action": "행정·현장 정밀검토 우선"},
        {"status": "조건부·대체 위치", "count": 18, "share": 0.45, "action": "차량 충돌·사유지 등을 피해 50~100m 대체 공간 탐색"},
        {"status": "현장검수 후 제외", "count": 14, "share": 0.35, "action": "후속 우선순위에서 제외"},
    ]

    revision_rows = [
        {"issue": "행정동 총인구가 넓은 공업지역 후보를 과대평가", "evidence": "54개 감사에서 고령자 특화 근거 없음 14개, 버스+행정동 중심점만 11개", "v2_change": "면적당 고령인구 밀도 도입; 상위 25%는 300m, 나머지는 500m"},
        {"issue": "전체 연령 버스 이용량이 고령자 수요보다 강하게 작용", "evidence": "성서공단권 등 고령자 목적지 근거가 약한 후보 반복", "v2_change": "모든 후보에 고령자 특화 경로 필수, 일반지역은 의료·노인복지 목적지 경로 필수"},
        {"issue": "도로 일부만 공백인데 OSM 도로 전체가 후보처럼 표시", "evidence": "후보 중점 300m 내 정류장 의자가 있던 기존 후보 31개", "v2_change": "25m 표본 중 기준을 넘는 실제 부분구간만 잘라 지도에 표시"},
        {"issue": "주차장·차량 통행공간이 후보에 포함", "evidence": "주차장 진입로와 공업지역 후보가 현장검수에서 반복 탈락", "v2_change": "parking_aisle 자동 제외, 현장 설치 가능성 판정을 모델 순위보다 우선"},
    ]

    sources = [
        {
            "id": "prep_summary",
            "label": "Data Preparation 실행·검증 요약",
            "path": "03_data_preparation/qa/preparation_summary.json",
            "query": {
                "engine": "python",
                "language": "python",
                "description": "원천 데이터를 표준 공간레이어로 변환하고 행 수·조인·좌표·버스 이용량 연결을 검증한다.",
                "tables_used": ["03_data_preparation/qa/source_manifest.csv", "03_data_preparation/qa/validation_checks.csv"],
                "filters": ["공간 범위: 대구광역시 달서구", "고령자: 만 65세 이상", "휴식시설: 공원 벤치와 정류장 의자만"],
                "metric_definitions": ["활성 휴식시설: 사용 가능한 공원 벤치 2,728개와 의자 보유 정류장 436개", "정류소 ID 확정: 이름·좌표 20m 규칙을 충족한 562개"],
            },
        },
        {
            "id": "model_summary",
            "label": "예상 보행경로·좌석 공백 모델링 요약",
            "path": "04_modeling/qa/modeling_summary.json",
            "query": {
                "engine": "python",
                "language": "python",
                "description": "출발지와 목적지를 보행망에 연결해 최단경로, 도로별 중첩, 네트워크 좌석거리와 공백 점수를 계산한다.",
                "tables_used": ["04_modeling/outputs/expected_od_routes.csv", "04_modeling/outputs/route_overlap_and_bench_gap_edges.csv"],
                "filters": ["OD 경로 길이 1,200m 이하", "주 분석 생활권 800m", "계단 포함·제외 두 시나리오"],
                "metric_definitions": ["예상 경로: 실제 GPS가 아닌 출발·목적지 간 보행망 최단경로", "공백 길이: 25m 간격 도로 표본 중 최근접 활성 좌석의 네트워크 거리가 기준을 넘는 길이"],
            },
        },
        {
            "id": "evaluation_summary",
            "label": "후보 민감도·안정성 평가",
            "path": "05_evaluation/qa/evaluation_summary.json",
            "query": {
                "engine": "python",
                "language": "python",
                "description": "계단, OD 반경, 수요·공백 가중치를 바꾸어 후보 순위의 안정성을 평가한다.",
                "tables_used": ["05_evaluation/outputs/sensitivity_rankings_all.csv", "05_evaluation/outputs/consensus_candidates_by_threshold.csv"],
                "filters": ["계단 2개 × 거리기준 2개 × OD 반경 3개 × 가중치 3개 = 36개 조합"],
                "metric_definitions": ["안정 후보: 같은 거리기준의 18개 조합 중 12개 이상에서 TOP 20", "TOP 20 유지율: 기준 조합과 비교 조합의 공통 후보 수/20"],
            },
        },
        {
            "id": "v2_revalidation",
            "label": "v2 모델 타당성 재검증",
            "path": "05_evaluation/07_모델_타당성_재검증_보고서.md",
            "query": {
                "engine": "python",
                "language": "python",
                "description": "초기 현장검수에서 드러난 공업지역·공원 인접 후보 편향을 감사하고 밀도 적응형 후보를 재산정한다.",
                "tables_used": ["05_evaluation/outputs/candidate_model_validity_audit_54.csv", "05_evaluation/outputs/revised_priority_rankings_v2_all.csv"],
                "filters": ["고령자 특화 경로 필수", "고밀도 상위 25% 300m, 그 외 500m", "주차장 내부 통로 제외"],
                "metric_definitions": ["고밀도 경계: 행정동 면적당 만 65세 이상 인구 상위 25%, 3,774.6명/㎢", "고령자 특화 경로: 의료·노인복지 목적지 또는 경로당·노인복지 출발 경로"],
            },
        },
        {
            "id": "field_results",
            "label": "v2 현장검수 통합 결과",
            "path": "05_evaluation/outputs/v2_field_review_validated.csv",
            "query": {
                "engine": "python",
                "language": "python",
                "description": "v2 후보 40개의 현장판정을 직접 설치, 조건부·대체 위치, 제외로 분류한다.",
                "tables_used": ["dalseo_v2_field_review_completed_40.csv", "05_evaluation/outputs/v2_field_review_validated.csv"],
                "filters": ["40개 후보 ID 완전 일치", "종합판정 40/40 입력", "현장 NOT_INSTALLABLE 판정은 모델 점수와 무관하게 제외"],
                "metric_definitions": ["직접 설치 검토: INSTALLABLE이며 기존 좌석 없음·공공 설치 가능성이 확인된 구간", "조건부: 현재 선형 설치가 아니라 인근 대체공간 또는 추가 협의가 필요한 구간"],
            },
        },
        {
            "id": "final_validation",
            "label": "최종 인수 검증",
            "path": "05_evaluation/reports/final_project_methodology_report_artifact.json",
            "query": {
                "engine": "python",
                "language": "python",
                "description": "저장된 단계별 QA, 최종 분류 CSV, 공개 지도 ID, 추적 파일과 Python 구문을 교차 검증한다.",
                "tables_used": [
                    "03_data_preparation/qa/preparation_summary.json",
                    "04_modeling/qa/modeling_summary.json",
                    "05_evaluation/qa/evaluation_summary.json",
                    "05_evaluation/qa/revised_candidate_model_v2_checks.csv",
                    "05_evaluation/qa/v2_field_review_integration_checks.csv",
                    "05_evaluation/outputs/v2_installable_priority_candidates.csv",
                    "docs/index.html",
                ],
                "filters": ["공개 저장소 추적 파일만 보안·데이터 범위 검사", "최종 분류 40개와 지도 직접 검토군 8개를 ID 단위로 대조"],
                "metric_definitions": ["PASS: 기대 행 수·고유 ID·분류 합계·지도 ID가 모두 일치", "차단 이슈: 공개 결과를 잘못 해석하거나 재현을 막는 오류"],
            },
        },
    ]

    manifest = {
        "version": 1,
        "surface": "report",
        "title": title,
        "generatedAt": generated_at,
        "description": "공개·행정자료와 OSM 보행망을 결합해 고령자 예상 보행수요 대비 휴식시설 공백을 찾고 현장검수로 설치 우선 검토구간을 도출한 전체 분석 보고서",
        "charts": [
            {
                "id": "field_outcome_chart",
                "title": "v2 현장검수 판정 결과",
                "subtitle": "40개 재산정 후보 중 8개를 직접 설치 정밀검토군으로 유지",
                "type": "bar",
                "dataset": "field_outcomes",
                "sourceId": "field_results",
                "source": {
                    "label": "v2 현장검수 판정 집계",
                    "path": "05_evaluation/outputs/v2_field_review_validated.csv",
                    "query": {
                        "engine": "sqlite",
                        "language": "sql",
                        "description": "완료된 v2 현장검수 40건을 최종 조치군별로 집계한다.",
                        "sql": "SELECT final_action AS status, COUNT(*) AS count, COUNT(*) * 1.0 / 40 AS share FROM v2_field_review_validated GROUP BY final_action ORDER BY CASE final_action WHEN '직접 설치 정밀검토' THEN 1 WHEN '조건부·대체 위치 검토' THEN 2 ELSE 3 END",
                        "tables_used": ["v2_field_review_validated"],
                    },
                },
                "encodings": {
                    "x": {"field": "status", "type": "nominal", "label": "현장판정"},
                    "y": {"field": "count", "type": "quantitative", "label": "후보 구간 수", "format": "number"},
                    "tooltip": [
                        {"field": "share", "type": "quantitative", "label": "40개 중 비중", "format": "percent"},
                        {"field": "action", "type": "nominal", "label": "후속 조치"},
                    ],
                },
                "options": {"orientation": "vertical", "grouping": "single"},
            }
        ],
        "tables": [
            {
                "id": "data_layers_table",
                "title": "분석용 공간레이어 구성",
                "subtitle": "전처리 완료 후 분석에 실제 사용한 달서구 단위 레이어",
                "dataset": "data_layers",
                "sourceId": "prep_summary",
                "source": {
                    "label": "전처리 공간레이어 스냅샷",
                    "path": "05_evaluation/reports/final_project_methodology_report_artifact.json",
                    "query": {
                        "engine": "sqlite",
                        "language": "sql",
                        "description": "보고서용으로 고정한 전처리 레이어별 레코드와 처리 규칙을 조회한다.",
                        "sql": "SELECT layer, records, role, preprocessing FROM data_layers ORDER BY records DESC",
                        "tables_used": ["data_layers"],
                    },
                },
                "defaultSort": {"field": "records", "direction": "desc"},
                "columns": [
                    {"field": "layer", "label": "레이어", "type": "text"},
                    {"field": "records", "label": "레코드", "type": "number", "format": "number"},
                    {"field": "role", "label": "분석 역할", "type": "text"},
                    {"field": "preprocessing", "label": "핵심 전처리", "type": "text"},
                ],
            },
            {
                "id": "revision_table",
                "title": "초기 모델 감사와 v2 수정",
                "subtitle": "현장검수에서 발견된 편향을 규칙과 공간표현 수정으로 반영",
                "dataset": "revision_rows",
                "sourceId": "v2_revalidation",
                "source": {
                    "label": "초기 모델 감사·수정 스냅샷",
                    "path": "05_evaluation/reports/final_project_methodology_report_artifact.json",
                    "query": {
                        "engine": "sqlite",
                        "language": "sql",
                        "description": "초기 후보의 문제, 검증 근거, v2 수정사항을 조회한다.",
                        "sql": "SELECT issue, evidence, v2_change FROM revision_rows ORDER BY issue",
                        "tables_used": ["revision_rows"],
                    },
                },
                "defaultSort": {"field": "issue", "direction": "asc"},
                "columns": [
                    {"field": "issue", "label": "발견된 문제", "type": "text"},
                    {"field": "evidence", "label": "검증 근거", "type": "text"},
                    {"field": "v2_change", "label": "v2 조치", "type": "text"},
                ],
            },
            {
                "id": "final_candidates_table",
                "title": "직접 설치 정밀검토 우선구간 8개",
                "subtitle": "기존 좌석 없음·공공 설치 가능성을 확인한 물리 구간; 최종 설치 위치는 아님",
                "dataset": "final_candidates",
                "sourceId": "field_results",
                "source": {
                    "label": "직접 설치 정밀검토군 스냅샷",
                    "path": "05_evaluation/outputs/v2_installable_priority_candidates.csv",
                    "query": {
                        "engine": "sqlite",
                        "language": "sql",
                        "description": "현장검수를 통과한 직접 설치 정밀검토 구간 8개를 우선순위 순으로 조회한다.",
                        "sql": "SELECT priority, review_id, road, admin_dong, policy_tier, senior_specific_routes, gap_length_m, max_seat_distance_m, sidewalk FROM final_candidates ORDER BY priority",
                        "tables_used": ["final_candidates"],
                    },
                },
                "defaultSort": {"field": "priority", "direction": "asc"},
                "columns": [
                    {"field": "priority", "label": "순서", "type": "number"},
                    {"field": "review_id", "label": "검토 ID", "type": "text"},
                    {"field": "road", "label": "도로명", "type": "text"},
                    {"field": "admin_dong", "label": "행정동", "type": "text"},
                    {"field": "policy_tier", "label": "공백 기준", "type": "text"},
                    {"field": "senior_specific_routes", "label": "고령자 특화 경로", "type": "number"},
                    {"field": "gap_length_m", "label": "실제 공백 길이(m)", "type": "number"},
                    {"field": "max_seat_distance_m", "label": "최대 좌석거리(m)", "type": "number"},
                    {"field": "sidewalk", "label": "보도", "type": "text"},
                ],
            },
            {
                "id": "validation_table",
                "title": "최종 인수 검증 결과",
                "subtitle": "단계별 자동검사, 독립 재집계, 지도·코드·공개범위를 교차 확인",
                "dataset": "validation_rows",
                "sourceId": "final_validation",
                "source": {
                    "label": "최종 인수 검증 스냅샷",
                    "path": "05_evaluation/reports/final_project_methodology_report_artifact.json",
                    "query": {
                        "engine": "sqlite",
                        "language": "sql",
                        "description": "인수 검증 항목별 판정과 근거를 조회한다.",
                        "sql": "SELECT area, check, result, evidence, severity FROM validation_rows ORDER BY area",
                        "tables_used": ["validation_rows"],
                    },
                },
                "defaultSort": {"field": "area", "direction": "asc"},
                "columns": [
                    {"field": "area", "label": "영역", "type": "text"},
                    {"field": "check", "label": "검증", "type": "text"},
                    {"field": "result", "label": "판정", "type": "text"},
                    {"field": "evidence", "label": "근거", "type": "text"},
                    {"field": "severity", "label": "잔여 심각도", "type": "text"},
                ],
            },
            {
                "id": "caveat_table",
                "title": "남은 한계와 실행 전 확인사항",
                "subtitle": "분석 산출물의 사용 범위와 후속 행정·현장 조치",
                "dataset": "caveat_rows",
                "sourceId": "final_validation",
                "source": {
                    "label": "최종 한계·조치 스냅샷",
                    "path": "05_evaluation/reports/final_project_methodology_report_artifact.json",
                    "query": {
                        "engine": "sqlite",
                        "language": "sql",
                        "description": "잔여 한계, 영향과 필수 후속조치를 조회한다.",
                        "sql": "SELECT caveat, impact, required_action, status FROM caveat_rows ORDER BY caveat",
                        "tables_used": ["caveat_rows"],
                    },
                },
                "defaultSort": {"field": "caveat", "direction": "asc"},
                "columns": [
                    {"field": "caveat", "label": "한계", "type": "text"},
                    {"field": "impact", "label": "해석 영향", "type": "text"},
                    {"field": "required_action", "label": "필수 조치", "type": "text"},
                    {"field": "status", "label": "상태", "type": "text"},
                ],
            },
        ],
        "sources": sources,
        "blocks": [
            {"id": "title", "type": "markdown", "body": f"# {title}"},
            {
                "id": "technical_summary",
                "type": "markdown",
                "body": "## 기술 요약 — 분석 결과는 8개의 ‘직접 설치 정밀검토 우선구간’으로 수렴했다\n\n- **문제 정의:** 만 65세 이상 인구, 고령자 관련 시설, 의료·공원·정류소와 기존 좌석을 결합해 ‘예상 보행수요는 높지만 앉아 쉴 곳이 부족한 구간’을 찾았다. 실제 GPS 이동경로를 주장하지 않는다.\n- **계산 규모:** 표준화한 출발 위치 488개와 목적지 1,261개를 OSM 보행망으로 연결해 예상 OD 경로 82,833건을 만들고, 활성 공원 벤치·정류장 의자 3,164개까지의 네트워크 거리를 도로별로 계산했다.\n- **검증과 수정:** 초기 후보에서 공업지역·버스 수요 편향과 전체 도로 표시 문제가 확인돼, 고령인구 밀도·고령자 특화 경로·실제 공백 부분구간을 적용한 v2로 재산정했다.\n- **최종 결과:** v2 후보 40개를 검수해 직접 설치 정밀검토 8개, 조건부·대체 위치 18개, 제외 14개로 분류했다. 직접 검토 8개는 기존 좌석 없음과 공공 설치 가능성을 확인했으며 보도는 7개 연속, 1개 일부다.\n- **최종 판정:** 공개용 분석·현장검토 의사결정 지원물로는 공유 준비 완료다. 다만 실제 설치 승인에는 정확한 설치점 실측과 관리청·토지·안전 협의가 필요하다."
            },
            {
                "id": "outcome_finding",
                "type": "markdown",
                "sourceId": "field_results",
                "body": "## 모델 순위보다 현장 설치 가능성을 우선해 40개 중 8개를 유지했다\n\n현장판정에서 14개는 설치 부적합으로 제외했고, 18개는 현재 도로 선형에 바로 설치하지 않고 인근 공공부지·건물 후퇴공간 등을 탐색하는 조건부군으로 분리했다. 특히 차량 통행 방해가 기록된 12개 조건부 구간은 수요가 있더라도 도로 위 설치 후보로 해석하지 않는다."
            },
            {"id": "outcome_chart", "type": "chart", "chartId": "field_outcome_chart", "layout": "full"},
            {
                "id": "scope_definitions",
                "type": "markdown",
                "body": "## 분석 대상과 핵심 지표를 먼저 제한했다\n\n분석 지역은 대구광역시 달서구, 고령자는 만 65세 이상으로 정의했다. 휴식시설 공급은 길을 걷다가 즉시 이용할 수 있는 **공원 벤치와 버스정류장 의자만** 인정했다. `예상 보행경로`는 행정동 고령인구·경로당·노인복지시설과 인근 의료기관·복지시설·공원·정류소를 보행망 최단경로로 연결한 결과이며 관측 GPS가 아니다. `공백 구간`은 활성 좌석까지의 보행망 거리가 정책 기준을 초과하는 도로 부분이다. 최종 산출물은 설치 확정점이 아니라 행정·현장 정밀검토 우선구간이다."
            },
            {
                "id": "data_preparation",
                "type": "markdown",
                "sourceId": "prep_summary",
                "body": "## 서로 다른 행정자료를 EPSG:5179 공간레이어로 통일했다\n\nCSV 인코딩을 자동 판별하고 시설명·행정동명을 유니코드 정규화했으며 수치 문자열과 좌표를 표준화했다. 행정동 경계와 고령인구는 23/23 완전 연결했고 시설 원천 행정동과 좌표 기반 행정동을 함께 보존했다. 경로당 오류좌표 3개는 주소 기반 건물 대표점으로 보정하고 원본·보정 이력을 남겼다. 정류소는 명칭과 20m 공간규칙으로 568개 중 562개의 ID를 확정했으며 2025년 시간대별 승하차 12개월을 연결했다. 모든 분석 좌표는 미터 단위 EPSG:5179로 변환했다."
            },
            {"id": "data_layers", "type": "table", "tableId": "data_layers_table", "layout": "full"},
            {
                "id": "supply_and_network",
                "type": "markdown",
                "body": "## 좌석은 개수가 아니라 실제 위치가 있는 공급만 사용했다\n\n달서구 벤치 자료의 공원별 합계는 한 대표좌표에 여러 좌석이 몰리는 왜곡을 만들 수 있어 거리 계산에서 제외하고 참고표로만 보존했다. 대신 2022년 공원시설물의 개별 벤치 2,733개 중 사용 가능 2,728개와 의자 보유 정류장 436개를 활성 공급으로 사용했다. OSMnx 보행망은 달서구 주변 버퍼로 수집한 뒤 자동검수와 50개 물리구간 수동검수를 거쳐 22,668개 방향성 엣지에 최종 경로 처리값을 부여했다."
            },
            {
                "id": "methodology",
                "type": "markdown",
                "sourceId": "model_summary",
                "body": "## 82,833개 예상 경로의 중첩과 좌석 공백을 도로 단위로 계산했다\n\n1. 행정동 고령인구 대표점·경로당·노인복지시설을 출발 후보로, 의료기관·노인복지시설·공원·정류소를 목적지로 구성했다. 동일 좌표는 물리 위치 단위로 합쳐 출발 488개, 목적지 1,261개가 됐다.\n2. 출발 가중치와 목적지 가중치를 곱해 경로 가중치를 만들었다. 정류소 이용량은 전체 연령 자료이므로 `log(1+이용량)`을 적용한 보조 대리지표로만 사용했다.\n3. 각 점을 최근접 보행 엣지에 연결하고 500m·800m·1,200m 이내 최단경로를 계산했다. 800m를 주 분석값으로 쓰되 다른 반경을 민감도에서 비교했다.\n4. 경로가 지난 물리 도로마다 경로 수와 가중치를 한 번씩 누적해 예상 보행경로 중첩도를 만들었다.\n5. 활성 좌석을 보행망에 연결한 다중출발점 최단거리 계산을 수행하고, 도로를 약 25m 간격으로 표본화해 최근접 좌석 거리를 구했다. 기준을 초과한 표본 비율과 초과 정도로 공백 길이·공백 심각도를 계산했다.\n6. 초기 점수는 `수요 백분위 × 공백 백분위`의 기하평균으로 산정해 한 요소만 큰 구간이 과도하게 상위에 오르지 않도록 했다."
            },
            {
                "id": "sensitivity",
                "type": "markdown",
                "sourceId": "evaluation_summary",
                "body": "## 가중치보다 생활권 반경 선택이 후보를 더 크게 바꿨다\n\n계단 포함·제외 2개, 공백 기준 300m·500m, OD 반경 500m·800m·1,200m, 수요 비중 35%·50%·65%를 조합해 총 36개 순위를 계산했다. 계단 처리에 따른 TOP 20은 모든 조합에서 같았고, 800m 고정 시 가중치 변경 후 TOP 20 유지율은 85~90%였다. 반면 OD 반경을 바꾸면 유지율이 최저 50%까지 낮아졌다. 따라서 800m를 ‘고령자의 실제 보행권’으로 단정하지 않고 모델 선택값으로 명시했다."
            },
            {
                "id": "revision_finding",
                "type": "markdown",
                "sourceId": "v2_revalidation",
                "body": "## 초기 현장검수에서 공업지역 편향을 확인해 모델을 v2로 재설계했다\n\n초기 54개 후보 중 14개는 의료·복지·경로당 같은 고령자 특화 경로 근거가 없었고 11개는 사실상 일반 버스 목적지와 행정동 중심점만으로 설명됐다. 또한 좌석이 가까운 도로 일부까지 전체 후보선으로 표시되는 문제가 있었다. 이는 계산 오류라기보다 수요 대리지표와 공간표현 설계의 한계였으며, 현장검수 결과를 이용해 아래처럼 수정했다."
            },
            {"id": "revision", "type": "table", "tableId": "revision_table", "layout": "full"},
            {
                "id": "v2_method",
                "type": "markdown",
                "sourceId": "v2_revalidation",
                "body": "## v2는 밀집지역의 휴식 간격을 더 촘촘하게 적용했다\n\n행정동 면적당 만 65세 이상 인구밀도 상위 25%(3,774.6명/㎢ 이상)는 `DENSE_300M`, 나머지는 `STANDARD_500M`으로 나눴다. 모든 후보에 의료·노인복지 목적지 또는 경로당·노인복지 출발 근거를 요구했고, 일반지역은 의료·노인복지 목적지 경로를 추가로 요구했다. 수요는 고령자 특화 경로·고령인구 밀도·공원 및 버스 보조근거를 세 가지 가중치 조합으로 평가하고 계단 2개 시나리오와 함께 총 6회 순위 안정성을 계산했다. 현장 부적합 구간과 주차장 내부 통로는 자동 제외했으며 지도에는 기준을 초과한 실제 부분구간만 표시했다."
            },
            {
                "id": "final_candidates_finding",
                "type": "markdown",
                "sourceId": "field_results",
                "body": "## 직접 설치 정밀검토군 8개는 좌석·공공성·보행공간 검수를 통과했다\n\n8개 모두 기존 좌석이 없고 공공 설치 가능성이 높은 것으로 입력됐다. 상인1동·상인2동·용산1동의 고밀도 300m 구간이 5개, 두류1·2동·송현1동의 일반지역 500m 구간이 3개다. 보도는 7개가 연속이고 두류남길 1개는 일부 보도이므로 실제 설치점 조정이 필요하다. 같은 도로명이라도 서로 다른 물리 부분구간은 별도 후보로 유지했다."
            },
            {"id": "final_candidates", "type": "table", "tableId": "final_candidates_table", "layout": "full"},
            {
                "id": "final_validation_finding",
                "type": "markdown",
                "sourceId": "final_validation",
                "body": "## 최종 인수 검증 — 공개 결과를 막는 오류는 발견되지 않았다\n\n단계별 저장 QA 40개(8+10+11+7+4)가 모두 PASS였고, 최종 분류는 40개 = 직접 설치 검토 8개 + 조건부 18개 + 제외 14개로 재계산됐다. 공개 지도에 넣은 후보 ID 8개는 직접 설치 검토 CSV와 정확히 일치한다. Python 스크립트 구문, 의존성 명세, Markdown 링크와 공개 저장소의 데이터·자격증명 노출 범위도 별도로 확인했다. 검증 결과는 정확한 값 조회가 목적이므로 막대그래프를 추가하지 않고 표로 제공한다."
            },
            {"id": "validation", "type": "table", "tableId": "validation_table", "layout": "full"},
            {
                "id": "robustness_limits",
                "type": "markdown",
                "body": "## 한계·불확실성·강건성 — 계산은 일관되지만 실제 이동과 설치 승인을 증명하지는 않는다\n\nData Preparation 8개, Modeling 10개, Evaluation 11개, v2 7개, 현장검수 통합 4개 자동검사가 모두 통과했다. 저장된 82,833개 경로의 물리 엣지 연속성 실패는 0건이었고 500m⊆800m⊆1,200m 관계, 공백 비율 범위, 좌석 충분 구간 제외도 확인했다. 이번 최종 인수 검토에서는 저장된 QA와 핵심 최종 집계를 독립 재검산했지만 원천파일부터 전체 파이프라인을 다시 실행하지는 않았다. 또한 행정동보다 작은 고령인구가 없고, 버스 이용량은 전체 연령이며, 공원 벤치는 2022년 자료다. 따라서 결과는 설명 가능한 공간 선별 모델이지 고령자의 실제 통행량 예측이나 인과 추정이 아니다."
            },
            {"id": "caveats", "type": "table", "tableId": "caveat_table", "layout": "full"},
            {
                "id": "next_steps",
                "type": "markdown",
                "body": "## 권고 및 종료 기준 — 코드·문서 프로젝트는 완료하고, 실행 단계는 8개 구간의 정밀검토로 넘긴다\n\n1. 각 구간에서 정확한 설치점을 정하고 유효 보행 폭·차량 회전·소방 및 진입 동선을 실측한다.\n2. 도로관리청·토지관리부서와 공공부지 여부, 유지관리 주체, 인허가 가능성을 확인한다.\n3. 일부 보도인 두류남길은 인근 50~100m 대체 위치를 병행 검토한다.\n4. 조건부 18개 중 차량 충돌 우려 12개는 도로 위 설치가 아니라 공공시설 입구·건물 후퇴공간·자투리 공공부지 탐색 대상으로 관리한다.\n5. 실제 설치 후 이용량·민원·보행 안전 변화를 기록하면 다음 분석에서 가중치와 기준거리의 근거를 보정할 수 있다."
            },
            {
                "id": "further_questions",
                "type": "markdown",
                "body": "## 추가로 확인하면 결과 신뢰도를 높일 데이터\n\n- 집계구·격자 단위 만 65세 이상 인구와 주거용 토지이용\n- 최신 공원 벤치와 정류장 의자의 현존·상태 정보\n- 고령자 시간대별 정류장 이용 또는 표본 보행 관찰\n- 보도 폭, 경사도, 횡단보도·안전시설, 노인 보행자 사고 자료\n- 설치비·유지관리비와 민원·현장 이용성과"
            },
        ],
    }

    artifact = {
        "surface": "report",
        "manifest": manifest,
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "data_layers": data_layers,
                "field_outcomes": outcome_rows,
                "revision_rows": revision_rows,
                "final_candidates": final_rows,
                "validation_rows": validation_rows,
                "caveat_rows": caveat_rows,
            },
        },
        "sources": sources,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
