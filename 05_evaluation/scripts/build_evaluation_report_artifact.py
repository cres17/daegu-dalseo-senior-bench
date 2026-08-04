#!/usr/bin/env python3
"""Evaluation 결과를 Data Analytics report artifact로 패키징한다."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "05_evaluation" / "outputs"
REPORTS = ROOT / "05_evaluation" / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)


def records(frame: pd.DataFrame) -> list[dict[str, object]]:
    return json.loads(frame.to_json(orient="records", force_ascii=False))


comparison = pd.read_csv(
    OUT / "baseline_top20_sensitivity_comparison.csv",
    encoding="utf-8-sig",
)
stability = pd.read_csv(
    OUT / "stability_band_summary.csv",
    encoding="utf-8-sig",
)
shortlist = pd.read_csv(
    OUT / "field_review_priority_and_template.csv",
    encoding="utf-8-sig",
)
coverage = pd.read_csv(
    OUT / "candidate_evidence_coverage.csv",
    encoding="utf-8-sig",
)
risks = pd.read_csv(
    OUT / "decision_data_quality_risks.csv",
    encoding="utf-8-sig",
)
summary = json.loads(
    (
        ROOT / "05_evaluation" / "qa" / "evaluation_summary.json"
    ).read_text(encoding="utf-8")
)

radius_retention = (
    comparison.groupby(["bench_threshold_m", "od_radius_m"], as_index=False)
    .agg(
        minimum_retention_rate=("top20_retention_rate", "min"),
        median_retention_rate=("top20_retention_rate", "median"),
        maximum_retention_rate=("top20_retention_rate", "max"),
        median_rank_correlation=("eligible_rank_correlation", "median"),
    )
)
radius_retention["threshold_label"] = (
    radius_retention["bench_threshold_m"].astype(str) + "m 공백"
)
radius_retention["radius_label"] = (
    radius_retention["od_radius_m"].astype(str) + "m"
)

stability_label_map = {
    "STABLE_2_OF_3_PLUS": "안정: 12/18 이상",
    "CONDITIONAL_1_OF_3_PLUS": "조건부: 6~11/18",
    "SENSITIVE_BELOW_1_OF_3": "민감: 5/18 이하",
}
stability["stability_label"] = stability["stability_band"].map(
    stability_label_map
)
stability["threshold_label"] = (
    stability["bench_threshold_m"].astype(str) + "m 공백"
)

shortlist_report = shortlist[
    [
        "field_review_id",
        "bench_threshold_m",
        "field_review_order",
        "name",
        "sensitivity_selection_count",
        "sensitivity_selection_rate",
        "selection_driver",
        "route_count_800m",
        "nearest_seat_max_m",
        "destination_evidence_type_count",
        "midpoint_lat",
        "midpoint_lon",
        "kakao_map_url",
    ]
].copy()
shortlist_report["name"] = shortlist_report["name"].fillna("도로명 없음")

generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
report_db = REPORTS / "evaluation_report_data.sqlite"
with sqlite3.connect(report_db) as connection:
    radius_retention.to_sql(
        "radius_retention", connection, if_exists="replace", index=False
    )
    stability.to_sql(
        "stability_summary", connection, if_exists="replace", index=False
    )
    shortlist_report.to_sql(
        "field_review_shortlist",
        connection,
        if_exists="replace",
        index=False,
    )
    coverage.to_sql(
        "evidence_coverage", connection, if_exists="replace", index=False
    )
    risks.to_sql(
        "decision_risks", connection, if_exists="replace", index=False
    )

radius_sql = """
SELECT bench_threshold_m, od_radius_m, minimum_retention_rate,
       median_retention_rate, maximum_retention_rate,
       median_rank_correlation, threshold_label, radius_label
FROM radius_retention
ORDER BY bench_threshold_m, od_radius_m
""".strip()
stability_sql = """
SELECT bench_threshold_m, stability_band, candidate_count,
       stability_label, threshold_label
FROM stability_summary
ORDER BY bench_threshold_m, candidate_count DESC
""".strip()
field_review_sql = """
SELECT field_review_id, bench_threshold_m, field_review_order, name,
       sensitivity_selection_count, sensitivity_selection_rate,
       selection_driver, route_count_800m, nearest_seat_max_m,
       destination_evidence_type_count, midpoint_lat, midpoint_lon,
       kakao_map_url
FROM field_review_shortlist
ORDER BY field_review_id
""".strip()
evidence_sql = """
SELECT evidence_id, evidence_label, covered_candidates,
       candidate_denominator, coverage_rate
FROM evidence_coverage
ORDER BY coverage_rate DESC, evidence_id
""".strip()
risk_sql = """
SELECT risk_id, severity, finding, evidence, decision_impact, required_action
FROM decision_risks
ORDER BY risk_id
""".strip()

with sqlite3.connect(report_db) as connection:
    radius_snapshot = pd.read_sql_query(radius_sql, connection)
    stability_snapshot = pd.read_sql_query(stability_sql, connection)
    field_review_snapshot = pd.read_sql_query(field_review_sql, connection)
    evidence_snapshot = pd.read_sql_query(evidence_sql, connection)
    risk_snapshot = pd.read_sql_query(risk_sql, connection)

sources = [
    {
        "id": "evaluation_summary",
        "label": "Evaluation 실행 요약",
        "path": "05_evaluation/qa/evaluation_summary.json",
        "query": {
            "engine": "python",
            "language": "python",
            "description": "36개 민감도 조합, 안정 후보 수, 현장조사 후보 수와 검증 상태를 집계한다.",
            "tables_used": [
                "04_modeling/outputs/route_overlap_and_bench_gap_edges.csv",
                "04_modeling/outputs/priority_edge_candidates_all.csv",
            ],
            "filters": [
                "달서구와 교차하는 보행 도로",
                "경로 가중치가 0보다 큰 도로",
                "300m 또는 500m 좌석 공백이 존재하는 도로",
            ],
            "metric_definitions": [
                "안정 후보: 기준거리별 18개 민감도 조합 중 12개 이상에서 TOP 20",
                "현장조사 후보: 기준거리별 민감도 선택횟수와 선택 순위로 정렬한 상위 15개",
            ],
        },
    },
    {
        "id": "sensitivity_results",
        "label": "후보 순위 민감도 비교",
        "path": "05_evaluation/reports/evaluation_report_data.sqlite",
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "description": "Python 민감도 결과에서 기준별·반경별 TOP 20 유지율을 집계한 SQLite 스냅샷을 조회한다.",
            "sql": radius_sql,
            "tables_used": ["radius_retention"],
            "metric_definitions": [
                "TOP 20 유지율: 기준 조합 TOP 20과 비교 조합 TOP 20의 공통 도로 수를 20으로 나눈 비율",
                "기준 조합: 같은 벤치 기준·계단 시나리오의 OD 800m, 수요 50%, 공백 50%",
                "순위상관: 공통 후보의 두 순위 열에 대한 Spearman 상관",
            ],
        },
    },
    {
        "id": "consensus_results",
        "label": "기준거리별 합의 후보",
        "path": "05_evaluation/reports/evaluation_report_data.sqlite",
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "description": "기준거리별 후보의 민감도 안정성 구간을 집계한 SQLite 스냅샷을 조회한다.",
            "sql": stability_sql,
            "tables_used": ["stability_summary"],
            "metric_definitions": [
                "선택횟수: 기준거리별 18개 조합에서 TOP 20에 포함된 횟수",
                "수요 주도·공백 주도: 선택된 조합의 평균 수요·공백 백분위 차이가 0.15 이상인 경우",
            ],
        },
    },
    {
        "id": "field_review_results",
        "label": "현장조사 우선순위표",
        "path": "05_evaluation/reports/evaluation_report_data.sqlite",
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "description": "300m·500m 기준별 안정성 우선 상위 15개 현장조사 스냅샷을 조회한다.",
            "sql": field_review_sql,
            "tables_used": ["field_review_shortlist"],
            "metric_definitions": [
                "현장조사 순서: 선택횟수 내림차순, 선택된 조합의 중앙 순위 오름차순",
                "최대 좌석거리: 검수 보행망을 따른 도로 대표지점의 최근접 활성 좌석거리 최댓값",
            ],
        },
    },
    {
        "id": "evidence_results",
        "label": "기존 TOP 후보 연결 근거",
        "path": "05_evaluation/reports/evaluation_report_data.sqlite",
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "description": "Modeling 고유 TOP 후보 39개의 연결 근거 포함률 스냅샷을 조회한다.",
            "sql": evidence_sql,
            "tables_used": ["evidence_coverage"],
            "metric_definitions": [
                "근거 포함률: 해당 근거 유형의 800m 예상 경로가 1건 이상 지난 후보 수 / 고유 TOP 후보 39개",
            ],
        },
    },
    {
        "id": "risk_results",
        "label": "설치 결정 전 데이터 위험",
        "path": "05_evaluation/reports/evaluation_report_data.sqlite",
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "description": "원자료 품질점검과 Modeling 제한을 설치 의사결정 영향 기준으로 재분류한 스냅샷을 조회한다.",
            "sql": risk_sql,
            "tables_used": ["decision_risks"],
            "metric_definitions": [
                "High: 현장조사 없이 설치 결정에 사용하면 후보 순위 또는 적합성 판단을 바꿀 수 있는 위험",
            ],
        },
    },
]

manifest = {
    "version": 1,
    "surface": "report",
    "title": "달서구 벤치 후보 Evaluation",
    "generatedAt": generated_at,
    "description": "예상 보행 수요와 300m·500m 좌석 공백 후보의 민감도, 근거, 현장조사 필요성을 평가한 보고서",
    "charts": [
        {
            "id": "radius_retention_chart",
            "title": "OD 반경별 TOP 20 유지율",
            "subtitle": "800m 기준 순위와 비교한 중앙 유지율이며 500m·1,200m에서 후보 변화가 커진다.",
            "type": "bar",
            "dataset": "radius_retention",
            "sourceId": "sensitivity_results",
            "encodings": {
                "x": {
                    "field": "radius_label",
                    "type": "nominal",
                    "label": "OD 반경",
                },
                "y": {
                    "field": "median_retention_rate",
                    "type": "quantitative",
                    "label": "TOP 20 중앙 유지율",
                    "format": "percent",
                },
                "color": {
                    "field": "threshold_label",
                    "type": "nominal",
                    "label": "벤치 공백 기준",
                },
                "tooltip": [
                    {
                        "field": "minimum_retention_rate",
                        "type": "quantitative",
                        "label": "최소 유지율",
                        "format": "percent",
                    },
                    {
                        "field": "maximum_retention_rate",
                        "type": "quantitative",
                        "label": "최대 유지율",
                        "format": "percent",
                    },
                    {
                        "field": "median_rank_correlation",
                        "type": "quantitative",
                        "label": "중앙 순위상관",
                        "format": "number",
                    },
                ],
            },
            "options": {"orientation": "vertical", "grouping": "grouped"},
        },
        {
            "id": "stability_band_chart",
            "title": "민감도 안정성 구간별 후보 수",
            "subtitle": "기준거리별 18개 조합에서 TOP 20에 선택된 횟수 기준",
            "type": "bar",
            "dataset": "stability_summary",
            "sourceId": "consensus_results",
            "encodings": {
                "x": {
                    "field": "stability_label",
                    "type": "nominal",
                    "label": "안정성 구간",
                },
                "y": {
                    "field": "candidate_count",
                    "type": "quantitative",
                    "label": "고유 후보 도로 수",
                    "format": "number",
                },
                "color": {
                    "field": "threshold_label",
                    "type": "nominal",
                    "label": "벤치 공백 기준",
                },
                "tooltip": [
                    {
                        "field": "bench_threshold_m",
                        "type": "quantitative",
                        "label": "공백 기준(m)",
                        "format": "number",
                    }
                ],
            },
            "options": {"orientation": "vertical", "grouping": "grouped"},
        },
    ],
    "tables": [
        {
            "id": "evidence_coverage_table",
            "title": "기존 TOP 후보의 연결 근거 포함률",
            "subtitle": "Modeling 고유 TOP 후보 39개 기준",
            "dataset": "evidence_coverage",
            "sourceId": "evidence_results",
            "defaultSort": {"field": "coverage_rate", "direction": "desc"},
            "columns": [
                {
                    "field": "evidence_label",
                    "label": "연결 근거",
                    "type": "text",
                },
                {
                    "field": "covered_candidates",
                    "label": "포함 후보",
                    "type": "number",
                },
                {
                    "field": "candidate_denominator",
                    "label": "전체 후보",
                    "type": "number",
                },
                {
                    "field": "coverage_rate",
                    "label": "포함률",
                    "type": "percent",
                    "format": "percent",
                },
            ],
        },
        {
            "id": "field_review_table",
            "title": "현장조사 우선 검토구간",
            "subtitle": "300m·500m 기준별 상위 15개, 총 30행·고유 도로 29개",
            "dataset": "field_review_shortlist",
            "sourceId": "field_review_results",
            "defaultSort": {"field": "field_review_id", "direction": "asc"},
            "columns": [
                {
                    "field": "field_review_id",
                    "label": "검토 ID",
                    "type": "text",
                },
                {
                    "field": "bench_threshold_m",
                    "label": "공백 기준(m)",
                    "type": "number",
                },
                {
                    "field": "field_review_order",
                    "label": "기준 내 순서",
                    "type": "number",
                },
                {"field": "name", "label": "도로명", "type": "text"},
                {
                    "field": "sensitivity_selection_count",
                    "label": "TOP 20 선택횟수",
                    "type": "number",
                },
                {
                    "field": "sensitivity_selection_rate",
                    "label": "선택률",
                    "type": "percent",
                    "format": "percent",
                },
                {
                    "field": "selection_driver",
                    "label": "순위 성격",
                    "type": "text",
                },
                {
                    "field": "route_count_800m",
                    "label": "800m 경로 중첩",
                    "type": "number",
                },
                {
                    "field": "nearest_seat_max_m",
                    "label": "최대 좌석거리(m)",
                    "type": "number",
                },
                {
                    "field": "destination_evidence_type_count",
                    "label": "목적지 근거 유형 수",
                    "type": "number",
                },
            ],
        },
        {
            "id": "risk_table",
            "title": "설치 결정 전 해결해야 할 데이터 위험",
            "subtitle": "High 4건을 포함한 의사결정 영향 중심 품질 위험",
            "dataset": "decision_risks",
            "sourceId": "risk_results",
            "defaultSort": {"field": "risk_id", "direction": "asc"},
            "columns": [
                {"field": "risk_id", "label": "위험 ID", "type": "text"},
                {"field": "severity", "label": "심각도", "type": "text"},
                {"field": "finding", "label": "발견사항", "type": "text"},
                {
                    "field": "decision_impact",
                    "label": "의사결정 영향",
                    "type": "text",
                },
                {
                    "field": "required_action",
                    "label": "필요 조치",
                    "type": "text",
                },
            ],
        },
    ],
    "sources": sources,
    "blocks": [
        {
            "id": "report_title",
            "type": "markdown",
            "body": "# 달서구 벤치 후보 Evaluation",
        },
        {
            "id": "executive_summary",
            "type": "markdown",
            "sourceId": "evaluation_summary",
            "body": (
                "## Executive Summary\n\n"
                "- **공모전 공유에는 사용할 수 있지만 설치 결정에는 아직 부족하다.** "
                "계산·민감도 검증은 모두 통과했으나 안전성, 경사, 보도 폭, 소유권과 좌석 현장상태가 빠져 있어 평가는 `Share with caveats`다.\n"
                "- **민감도 안정 후보는 300m 16개, 500m 15개다.** "
                "각 기준의 18개 조합 중 12개 이상에서 TOP 20을 유지했다.\n"
                "- **계단 처리보다 생활권 반경이 후보를 더 크게 바꾼다.** "
                "계단 포함·제외 TOP 20은 모든 조합에서 같았지만, OD 반경 변경 시 기준 TOP 20 유지율은 최저 50%였다.\n"
                "- **현장조사는 29개 고유 도로로 줄였다.** "
                "300m·500m 각 15행을 제공하며, 동일 도로 한 곳이 두 기준에 중복된다."
            ),
        },
        {
            "id": "radius_finding",
            "type": "markdown",
            "sourceId": "sensitivity_results",
            "body": (
                "## 후보 순위는 가중치보다 생활권 반경에 더 민감했다\n\n"
                "800m 반경을 유지한 채 수요 비중을 35% 또는 65%로 바꾸면 TOP 20 유지율은 85~90%였다. "
                "반면 500m·1,200m 반경에서는 최저 50%까지 낮아졌다. 따라서 800m를 유일한 정답으로 제시하지 말고 "
                "500m 단거리와 1,200m 넓은 생활권 결과를 민감도 범위로 함께 제시해야 한다."
            ),
        },
        {
            "id": "radius_retention_chart_block",
            "type": "chart",
            "chartId": "radius_retention_chart",
            "layout": "full",
        },
        {
            "id": "stability_finding",
            "type": "markdown",
            "sourceId": "consensus_results",
            "body": (
                "## 안정 후보의 수는 비슷하지만 500m 후보는 공백 주도 구간이 더 많다\n\n"
                "300m 안정 후보 16개 중 14개는 수요와 공백 근거가 균형적이었다. "
                "500m 안정 후보 15개 중 5개는 큰 좌석 공백이 순위를 이끄는 `GAP_LED` 구간이다. "
                "이 구간은 휴식시설 공백은 분명하지만 예상 경로 중첩이 적을 수 있으므로 실제 보행 수요를 현장에서 확인해야 한다."
            ),
        },
        {
            "id": "stability_band_chart_block",
            "type": "chart",
            "chartId": "stability_band_chart",
            "layout": "full",
        },
        {
            "id": "evidence_finding",
            "type": "markdown",
            "sourceId": "evidence_results",
            "body": (
                "## 기존 TOP 후보 39개는 모두 생활 목적지 연결 근거를 보유했다\n\n"
                "버스정류장 연결 근거는 37개, 의료기관은 28개 후보에서 확인됐다. "
                "39개 전부가 최소 한 종류의 목적지 연결 근거를 갖고 있으며 26개는 두 종류 이상이다. "
                "또한 좌석 공백이 0인 도로는 후보표에 들어오지 않아 공급 충분 구간 제외 로직도 정상 작동했다."
            ),
        },
        {
            "id": "evidence_coverage_block",
            "type": "table",
            "tableId": "evidence_coverage_table",
            "layout": "full",
        },
        {
            "id": "field_review_finding",
            "type": "markdown",
            "sourceId": "field_review_results",
            "body": (
                "## 다음 행동은 29개 도로의 현장조사다\n\n"
                "아래 표는 기준별 민감도 선택횟수와 선택 순위를 이용해 300m·500m 각각 15개를 정렬한 결과다. "
                "이 순서는 설치 순서가 아니라 현장조사 순서다. `GAP_LED`이면서 800m 경로 중첩이 적은 후보는 "
                "좌석 공백과 별개로 실제 고령자 보행 수요를 먼저 확인해야 한다."
            ),
        },
        {
            "id": "field_review_block",
            "type": "table",
            "tableId": "field_review_table",
            "layout": "full",
        },
        {
            "id": "recommended_next_steps",
            "type": "markdown",
            "body": (
                "## 권고되는 다음 단계\n\n"
                "1. 현장조사표에서 좌석 현존 여부, 연속 보도, 유효 폭, 경사, 횡단 위험, 그늘, 소유권·설치 가능성을 기록한다.\n"
                "2. 500m `GAP_LED` 후보는 시간대별 보행 관찰 또는 시설 이용자 인터뷰로 수요를 보완한다.\n"
                "3. 상위 후보 주변의 2022년 공원 벤치를 우선 재확인하고 정류소 ID 미확정 6개를 보완한다.\n"
                "4. 현장결과가 채워진 뒤에만 안전성·설치 가능성을 포함한 2차 우선순위를 계산한다."
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## Further Questions\n\n"
                "- 공모전 제안에서는 목표 휴식 간격을 300m와 500m 중 하나로 선택할 것인가, 두 정책 시나리오로 유지할 것인가?\n"
                "- 실제 설치비, 유지관리 주체, 사유지 협의 가능성을 어떤 평가척도로 받을 수 있는가?\n"
                "- 고령자 시간대별 정류장 이용량이나 보행 관찰 자료를 추가 확보할 수 있는가?"
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "sourceId": "risk_results",
            "body": (
                "## Caveats and Assumptions\n\n"
                "예상 보행 경로는 관측 GPS가 아니다. 버스 이용량은 전체 연령 자료이며, 공원 개별 벤치는 2022년 자료다. "
                "OSM 보행망과 좌석의 현존 상태에도 오차가 남아 있다. 특히 경사, 사고위험, 횡단안전, 보도 폭, "
                "그늘과 설치 가능성은 현재 점수에 없으므로 이 보고서는 최종 설치 위치를 확정하지 않는다."
            ),
        },
        {
            "id": "risk_block",
            "type": "table",
            "tableId": "risk_table",
            "layout": "full",
        },
    ],
}

snapshot = {
    "version": 1,
    "generatedAt": generated_at,
    "status": "ready",
    "datasets": {
        "radius_retention": records(radius_snapshot),
        "stability_summary": records(stability_snapshot),
        "evidence_coverage": records(evidence_snapshot),
        "field_review_shortlist": records(field_review_snapshot),
        "decision_risks": records(risk_snapshot),
    },
}

artifact = {
    "surface": "report",
    "manifest": manifest,
    "snapshot": snapshot,
    "sources": sources,
}

path = REPORTS / "evaluation_report_artifact.json"
path.write_text(
    json.dumps(artifact, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(path)
