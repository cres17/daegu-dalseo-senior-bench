#!/usr/bin/env python3
"""달서구 예상 보행·벤치 공백 후보 Evaluation.

이 스크립트는 최종 설치 위치를 결정하지 않는다. Modeling 결과가 거리,
가중치, 계단 포함 여부에 얼마나 민감한지 평가하고 현장조사 대상을 만든다.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
MODEL_OUT = ROOT / "04_modeling" / "outputs"
MODEL_QA = ROOT / "04_modeling" / "qa"
PREP_QA = ROOT / "03_data_preparation" / "qa"
NETWORK_QA = ROOT / "data_external" / "osmnx_dalseo_walk" / "qa"
OUT = ROOT / "05_evaluation" / "outputs"
QA = ROOT / "05_evaluation" / "qa"
OUT.mkdir(parents=True, exist_ok=True)
QA.mkdir(parents=True, exist_ok=True)

SCENARIOS = ("steps_included", "steps_excluded")
BENCH_THRESHOLDS_M = (300, 500)
OD_RADII_M = (500, 800, 1200)
DEMAND_WEIGHTS = (0.35, 0.50, 0.65)
TOP_N = 20
CONSENSUS_SHORTLIST_N = 15
COMBINATIONS_PER_THRESHOLD = (
    len(SCENARIOS) * len(OD_RADII_M) * len(DEMAND_WEIGHTS)
)


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def percentile_component(series: pd.Series) -> pd.Series:
    return np.log1p(series.clip(lower=0)).rank(method="average", pct=True)


def stability_band(selection_count: int) -> str:
    if selection_count >= 12:
        return "STABLE_2_OF_3_PLUS"
    if selection_count >= 6:
        return "CONDITIONAL_1_OF_3_PLUS"
    return "SENSITIVE_BELOW_1_OF_3"


def clean_road_name(value: object) -> str:
    if pd.isna(value) or not str(value).strip():
        return "도로명 없음"
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                cleaned = [str(item).strip() for item in parsed if str(item).strip()]
                if cleaned:
                    return " / ".join(cleaned)
        except (SyntaxError, ValueError):
            pass
    return text


def calculate_sensitivity(
    overlap: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ranking_parts: list[pd.DataFrame] = []
    scenario_rows: list[dict[str, object]] = []
    for threshold in BENCH_THRESHOLDS_M:
        for scenario in SCENARIOS:
            scenario_base = overlap[
                overlap["scenario"].eq(scenario)
                & overlap["intersects_dalseo"].eq(1)
                & overlap[f"gap_length_{threshold}m"].gt(0)
            ].copy()
            for radius in OD_RADII_M:
                eligible = scenario_base[
                    scenario_base[f"route_weight_{radius}m"].gt(0)
                ].copy()
                demand_component = percentile_component(
                    eligible[f"route_weight_{radius}m"]
                )
                gap_component = percentile_component(
                    eligible[f"gap_severity_{threshold}m"]
                )
                for demand_weight in DEMAND_WEIGHTS:
                    gap_weight = 1.0 - demand_weight
                    score = demand_component.pow(demand_weight) * gap_component.pow(
                        gap_weight
                    )
                    ranked = pd.DataFrame(
                        {
                            "scenario": scenario,
                            "bench_threshold_m": threshold,
                            "od_radius_m": radius,
                            "demand_weight": demand_weight,
                            "gap_weight": gap_weight,
                            "physical_id": eligible["physical_id"].values,
                            "name": eligible["name"].values,
                            "highway": eligible["highway"].values,
                            "route_weight": eligible[
                                f"route_weight_{radius}m"
                            ].values,
                            "route_count": eligible[
                                f"route_count_{radius}m"
                            ].values,
                            "gap_length_m": eligible[
                                f"gap_length_{threshold}m"
                            ].values,
                            "gap_severity": eligible[
                                f"gap_severity_{threshold}m"
                            ].values,
                            "demand_component": demand_component.values,
                            "gap_component": gap_component.values,
                            "sensitivity_score": score.values,
                        }
                    )
                    ranked["sensitivity_rank"] = (
                        ranked["sensitivity_score"]
                        .rank(method="min", ascending=False)
                        .astype(int)
                    )
                    ranked["selected_top20"] = ranked["sensitivity_rank"].le(TOP_N)
                    ranking_parts.append(ranked)
                    scenario_rows.append(
                        {
                            "scenario": scenario,
                            "bench_threshold_m": threshold,
                            "od_radius_m": radius,
                            "demand_weight": demand_weight,
                            "gap_weight": gap_weight,
                            "eligible_edges": len(ranked),
                            "selected_edges": int(ranked["selected_top20"].sum()),
                            "top_score_min": float(
                                ranked.loc[
                                    ranked["selected_top20"], "sensitivity_score"
                                ].min()
                            ),
                            "top_score_max": float(
                                ranked.loc[
                                    ranked["selected_top20"], "sensitivity_score"
                                ].max()
                            ),
                        }
                    )
    rankings = pd.concat(ranking_parts, ignore_index=True)
    scenario_summary = pd.DataFrame(scenario_rows)
    selected = rankings[rankings["selected_top20"]].copy()
    consensus = (
        selected.groupby(["bench_threshold_m", "physical_id"], as_index=False)
        .agg(
            sensitivity_selection_count=("selected_top20", "size"),
            best_sensitivity_rank=("sensitivity_rank", "min"),
            median_selected_rank=("sensitivity_rank", "median"),
            mean_selected_score=("sensitivity_score", "mean"),
            min_selected_score=("sensitivity_score", "min"),
            max_selected_score=("sensitivity_score", "max"),
            mean_selected_demand_component=("demand_component", "mean"),
            mean_selected_gap_component=("gap_component", "mean"),
            scenario_count=("scenario", "nunique"),
            radius_count=("od_radius_m", "nunique"),
            demand_weight_count=("demand_weight", "nunique"),
        )
    )
    consensus["sensitivity_selection_rate"] = (
        consensus["sensitivity_selection_count"] / COMBINATIONS_PER_THRESHOLD
    )
    consensus["stability_band"] = consensus["sensitivity_selection_count"].map(
        stability_band
    )
    component_difference = (
        consensus["mean_selected_demand_component"]
        - consensus["mean_selected_gap_component"]
    )
    consensus["selection_driver"] = np.select(
        [component_difference.ge(0.15), component_difference.le(-0.15)],
        ["DEMAND_LED", "GAP_LED"],
        default="BALANCED",
    )
    return rankings, scenario_summary, consensus


def compare_to_baseline(
    rankings: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    comparison_rows = []
    step_rows = []
    for threshold in BENCH_THRESHOLDS_M:
        for scenario in SCENARIOS:
            scenario_rows = rankings[
                rankings["bench_threshold_m"].eq(threshold)
                & rankings["scenario"].eq(scenario)
            ]
            baseline = scenario_rows[
                scenario_rows["od_radius_m"].eq(800)
                & scenario_rows["demand_weight"].eq(0.50)
            ][["physical_id", "sensitivity_rank", "selected_top20"]].rename(
                columns={"sensitivity_rank": "baseline_rank"}
            )
            baseline_top = set(
                baseline.loc[baseline["selected_top20"], "physical_id"]
            )
            for (radius, demand_weight), variation in scenario_rows.groupby(
                ["od_radius_m", "demand_weight"]
            ):
                variation_top = set(
                    variation.loc[
                        variation["selected_top20"], "physical_id"
                    ]
                )
                overlap_count = len(baseline_top.intersection(variation_top))
                common_ranks = variation[
                    ["physical_id", "sensitivity_rank"]
                ].merge(
                    baseline[["physical_id", "baseline_rank"]],
                    on="physical_id",
                    how="inner",
                    validate="one_to_one",
                )
                comparison_rows.append(
                    {
                        "scenario": scenario,
                        "bench_threshold_m": threshold,
                        "od_radius_m": int(radius),
                        "demand_weight": float(demand_weight),
                        "top20_overlap_with_baseline": overlap_count,
                        "top20_retention_rate": overlap_count / TOP_N,
                        "top20_jaccard": overlap_count
                        / (2 * TOP_N - overlap_count),
                        "eligible_rank_correlation": float(
                            common_ranks["sensitivity_rank"].corr(
                                common_ranks["baseline_rank"]
                            )
                        ),
                    }
                )
        for radius in OD_RADII_M:
            for demand_weight in DEMAND_WEIGHTS:
                included = set(
                    rankings[
                        rankings["scenario"].eq("steps_included")
                        & rankings["bench_threshold_m"].eq(threshold)
                        & rankings["od_radius_m"].eq(radius)
                        & rankings["demand_weight"].eq(demand_weight)
                        & rankings["selected_top20"]
                    ]["physical_id"]
                )
                excluded = set(
                    rankings[
                        rankings["scenario"].eq("steps_excluded")
                        & rankings["bench_threshold_m"].eq(threshold)
                        & rankings["od_radius_m"].eq(radius)
                        & rankings["demand_weight"].eq(demand_weight)
                        & rankings["selected_top20"]
                    ]["physical_id"]
                )
                overlap_count = len(included.intersection(excluded))
                step_rows.append(
                    {
                        "bench_threshold_m": threshold,
                        "od_radius_m": radius,
                        "demand_weight": demand_weight,
                        "top20_overlap_between_step_scenarios": overlap_count,
                        "top20_retention_rate": overlap_count / TOP_N,
                        "top20_jaccard": overlap_count
                        / (2 * TOP_N - overlap_count),
                    }
                )
    return pd.DataFrame(comparison_rows), pd.DataFrame(step_rows)


def enrich_consensus(
    consensus: pd.DataFrame,
    modeled_candidates: pd.DataFrame,
) -> pd.DataFrame:
    preferred = modeled_candidates.sort_values(
        ["scenario", "priority_core_score"],
        ascending=[True, False],
    ).drop_duplicates(["bench_threshold_m", "physical_id"])
    metadata_columns = [
        "bench_threshold_m",
        "physical_id",
        "name",
        "highway",
        "midpoint_lat",
        "midpoint_lon",
        "kakao_map_url",
        "route_count_500m",
        "route_count_800m",
        "route_count_1200m",
        "route_weight_800m",
        "nearest_seat_mean_m",
        "nearest_seat_max_m",
        "gap_length_300m",
        "gap_length_500m",
        "destination_medical_routes_800m",
        "destination_senior_welfare_routes_800m",
        "destination_park_routes_800m",
        "destination_bus_stop_routes_800m",
        "origin_admin_population_routes_800m",
        "origin_senior_center_routes_800m",
        "origin_senior_welfare_routes_800m",
    ]
    enriched = consensus.merge(
        preferred[metadata_columns],
        on=["bench_threshold_m", "physical_id"],
        how="left",
        validate="one_to_one",
    )
    enriched["name"] = enriched["name"].map(clean_road_name)
    baseline = modeled_candidates[
        modeled_candidates["priority_rank"].le(TOP_N)
    ][["bench_threshold_m", "physical_id"]].drop_duplicates()
    baseline["selected_in_baseline_top20"] = True
    enriched = enriched.merge(
        baseline,
        on=["bench_threshold_m", "physical_id"],
        how="left",
        validate="one_to_one",
    )
    enriched["selected_in_baseline_top20"] = enriched[
        "selected_in_baseline_top20"
    ].eq(True)
    enriched["destination_evidence_type_count"] = (
        enriched[
            [
                "destination_medical_routes_800m",
                "destination_senior_welfare_routes_800m",
                "destination_park_routes_800m",
                "destination_bus_stop_routes_800m",
            ]
        ]
        .gt(0)
        .sum(axis=1)
    )
    return enriched.sort_values(
        [
            "bench_threshold_m",
            "sensitivity_selection_count",
            "median_selected_rank",
        ],
        ascending=[True, False, True],
    )


def make_field_review_shortlist(consensus: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for threshold in BENCH_THRESHOLDS_M:
        selected = (
            consensus[consensus["bench_threshold_m"].eq(threshold)]
            .sort_values(
                [
                    "sensitivity_selection_count",
                    "median_selected_rank",
                    "mean_selected_score",
                ],
                ascending=[False, True, False],
            )
            .head(CONSENSUS_SHORTLIST_N)
            .copy()
        )
        selected.insert(
            0, "field_review_order", range(1, len(selected) + 1)
        )
        parts.append(selected)
    shortlist = pd.concat(parts, ignore_index=True)
    shortlist.insert(
        0,
        "field_review_id",
        [
            f"FR_{int(threshold)}_{order:02d}"
            for threshold, order in zip(
                shortlist["bench_threshold_m"], shortlist["field_review_order"]
            )
        ],
    )
    shortlist["review_status"] = ""
    shortlist["existing_seat_verified"] = ""
    shortlist["continuous_sidewalk"] = ""
    shortlist["effective_width_m"] = ""
    shortlist["slope_condition"] = ""
    shortlist["crossing_risk"] = ""
    shortlist["shade_weather_protection"] = ""
    shortlist["ownership_installability"] = ""
    shortlist["recommended_segment_adjustment"] = ""
    shortlist["review_notes"] = ""
    shortlist["reviewer"] = ""
    shortlist["review_date"] = ""
    return shortlist


def make_unique_field_review_template(shortlist: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for physical_id, group in shortlist.groupby("physical_id", sort=False):
        representative = group.sort_values(
            ["sensitivity_selection_count", "field_review_order"],
            ascending=[False, True],
        ).iloc[0]
        threshold_orders = {
            int(row["bench_threshold_m"]): int(row["field_review_order"])
            for _, row in group.iterrows()
        }
        threshold_selection_counts = {
            int(row["bench_threshold_m"]): int(
                row["sensitivity_selection_count"]
            )
            for _, row in group.iterrows()
        }
        thresholds = sorted(group["bench_threshold_m"].astype(int).unique())
        drivers = sorted(group["selection_driver"].dropna().astype(str).unique())
        row = {
            "physical_id": physical_id,
            "candidate_thresholds_m": " | ".join(map(str, thresholds)),
            "threshold_count": len(thresholds),
            "review_order_300m": threshold_orders.get(300, ""),
            "review_order_500m": threshold_orders.get(500, ""),
            "selection_count_300m": threshold_selection_counts.get(300, 0),
            "selection_count_500m": threshold_selection_counts.get(500, 0),
            "selection_drivers": " | ".join(drivers),
            "name": representative["name"],
            "highway": representative["highway"],
            "route_count_800m": representative["route_count_800m"],
            "nearest_seat_mean_m": representative["nearest_seat_mean_m"],
            "nearest_seat_max_m": representative["nearest_seat_max_m"],
            "gap_length_300m": representative["gap_length_300m"],
            "gap_length_500m": representative["gap_length_500m"],
            "destination_evidence_type_count": representative[
                "destination_evidence_type_count"
            ],
            "midpoint_lat": representative["midpoint_lat"],
            "midpoint_lon": representative["midpoint_lon"],
            "kakao_map_url": representative["kakao_map_url"],
        }
        for column in [
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
        ]:
            row[column] = ""
        rows.append(row)
    result = pd.DataFrame(rows)
    result.insert(
        0,
        "field_review_unique_id",
        [f"FRU_{index:03d}" for index in range(1, len(result) + 1)],
    )
    return result


def evidence_coverage(
    baseline_unique: pd.DataFrame,
) -> pd.DataFrame:
    definitions = [
        (
            "destination_bus_stop",
            "destination_bus_stop_routes_800m",
            "버스정류장 연결 근거",
        ),
        (
            "destination_medical",
            "destination_medical_routes_800m",
            "의료기관 연결 근거",
        ),
        (
            "destination_senior_welfare",
            "destination_senior_welfare_routes_800m",
            "노인복지시설 연결 근거",
        ),
        (
            "destination_park",
            "destination_park_routes_800m",
            "공원 연결 근거",
        ),
        (
            "origin_admin_population",
            "origin_admin_population_routes_800m",
            "행정동 고령인구 출발 근거",
        ),
        (
            "origin_senior_center",
            "origin_senior_center_routes_800m",
            "경로당 출발 근거",
        ),
        (
            "origin_senior_welfare",
            "origin_senior_welfare_routes_800m",
            "노인복지시설 출발 근거",
        ),
    ]
    rows = []
    denominator = len(baseline_unique)
    for evidence_id, column, label in definitions:
        covered = int(baseline_unique[column].gt(0).sum())
        rows.append(
            {
                "evidence_id": evidence_id,
                "evidence_label": label,
                "covered_candidates": covered,
                "candidate_denominator": denominator,
                "coverage_rate": covered / denominator,
            }
        )
    destination_columns = [
        "destination_medical_routes_800m",
        "destination_senior_welfare_routes_800m",
        "destination_park_routes_800m",
        "destination_bus_stop_routes_800m",
    ]
    type_counts = baseline_unique[destination_columns].gt(0).sum(axis=1)
    for minimum in (1, 2, 3, 4):
        covered = int(type_counts.ge(minimum).sum())
        rows.append(
            {
                "evidence_id": f"destination_types_at_least_{minimum}",
                "evidence_label": f"도착 근거 유형 {minimum}개 이상",
                "covered_candidates": covered,
                "candidate_denominator": denominator,
                "coverage_rate": covered / denominator,
            }
        )
    return pd.DataFrame(rows)


def exclusion_checks(
    overlap: pd.DataFrame,
    modeled_candidates: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for scenario in SCENARIOS:
        for threshold in BENCH_THRESHOLDS_M:
            base = overlap[
                overlap["scenario"].eq(scenario)
                & overlap["intersects_dalseo"].eq(1)
                & overlap["route_weight_800m"].gt(0)
            ].copy()
            no_gap = base[base[f"gap_length_{threshold}m"].le(0)]
            high_demand_cutoff = float(base["route_weight_800m"].quantile(0.9))
            high_demand_no_gap = no_gap[
                no_gap["route_weight_800m"].ge(high_demand_cutoff)
            ]
            ranked_ids = set(
                modeled_candidates[
                    modeled_candidates["scenario"].eq(scenario)
                    & modeled_candidates["bench_threshold_m"].eq(threshold)
                ]["physical_id"]
            )
            rows.append(
                {
                    "scenario": scenario,
                    "bench_threshold_m": threshold,
                    "demand_bearing_edges": len(base),
                    "sufficient_supply_edges": len(no_gap),
                    "sufficient_supply_share": len(no_gap) / len(base),
                    "high_demand_cutoff_90pct": high_demand_cutoff,
                    "high_demand_but_sufficient_supply_edges": len(
                        high_demand_no_gap
                    ),
                    "sufficient_supply_edges_in_candidate_table": len(
                        ranked_ids.intersection(set(no_gap["physical_id"]))
                    ),
                }
            )
    return pd.DataFrame(rows)


def data_quality_risks() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "risk_id": "E-DQ-01",
                "severity": "High",
                "finding": "공원 개별 벤치 위치가 2022-12-09 기준",
                "evidence": "공원 벤치의 철거·신설·상태가 2026년과 다를 수 있음",
                "decision_impact": "공백 거리와 후보 순위 변동 가능",
                "required_action": "상위 후보 주변 공원 벤치 현장 확인",
            },
            {
                "risk_id": "E-DQ-02",
                "severity": "High",
                "finding": "버스 이용량은 전체 연령 이용량",
                "evidence": "고령자 전용 승하차 자료가 아님",
                "decision_impact": "정류장 수요를 고령자 수요로 단정할 수 없음",
                "required_action": "대리변수로 명시하고 가능하면 연령별 자료 확보",
            },
            {
                "risk_id": "E-DQ-03",
                "severity": "High",
                "finding": "좌석의 현존·상시 이용 가능 여부 미확인",
                "evidence": "공공데이터 상태값만 사용, 현장 검증 전",
                "decision_impact": "실제 공백이 과대·과소 산정될 수 있음",
                "required_action": "현장조사표로 좌석 존재·상태 확인",
            },
            {
                "risk_id": "E-DQ-04",
                "severity": "High",
                "finding": "경사·횡단안전·보도 폭·설치 가능성 미포함",
                "evidence": "Modeling 점수 제외 항목으로 명시",
                "decision_impact": "설치 결정에는 현재 순위를 직접 사용할 수 없음",
                "required_action": "현장조사와 안전 데이터 확보 후 2차 평가",
            },
            {
                "risk_id": "E-DQ-05",
                "severity": "Medium",
                "finding": "정류소 ID 확정 562/568",
                "evidence": "6개 정류장은 이용량 직접 연결 없이 대체값 사용",
                "decision_impact": "해당 정류장 주변 수요 가중치 불확실",
                "required_action": "6개 정류장 ID 수동 보완",
            },
            {
                "risk_id": "E-DQ-06",
                "severity": "Medium",
                "finding": "점-보행망 스냅 100m 초과 88건/시나리오",
                "evidence": "스냅 거리 중앙값 18.7m, 95백분위 66.6m",
                "decision_impact": "일부 출발·목적지·좌석 연결 오차 가능",
                "required_action": "상위 후보에 기여한 장거리 스냅 우선 확인",
            },
            {
                "risk_id": "E-DQ-07",
                "severity": "Low",
                "finding": "보행망 수동판정 출처 46/50건 미기입",
                "evidence": "MR-06 Low FAIL, 판정 자체는 50/50 완료",
                "decision_impact": "경로 계산보다 감사 추적성에 영향",
                "required_action": "공모전 제출 전 지도·로드뷰 확인 출처 보완",
            },
        ]
    )


def validation_checks(
    model_summary: dict[str, object],
    rankings: pd.DataFrame,
    scenario_summary: pd.DataFrame,
    consensus: pd.DataFrame,
    shortlist: pd.DataFrame,
    unique_shortlist: pd.DataFrame,
    coverage: pd.DataFrame,
    exclusions: pd.DataFrame,
    modeled_candidates: pd.DataFrame,
) -> pd.DataFrame:
    baseline_expected = set(
        modeled_candidates[
            modeled_candidates["priority_rank"].le(TOP_N)
        ][["scenario", "bench_threshold_m", "physical_id"]]
        .itertuples(index=False, name=None)
    )
    baseline_recomputed = set(
        rankings[
            rankings["od_radius_m"].eq(800)
            & rankings["demand_weight"].eq(0.50)
            & rankings["selected_top20"]
        ][["scenario", "bench_threshold_m", "physical_id"]]
        .itertuples(index=False, name=None)
    )
    checks = [
        (
            "EV-01",
            model_summary.get("status") == "PASS",
            "Modeling 파이프라인 PASS 결과 사용",
        ),
        (
            "EV-02",
            len(scenario_summary) == 36,
            "민감도 조합 2계단×2기준×3반경×3가중치=36",
        ),
        (
            "EV-03",
            scenario_summary["selected_edges"].eq(TOP_N).all(),
            "모든 민감도 조합에서 TOP 20 산출",
        ),
        (
            "EV-04",
            baseline_expected == baseline_recomputed,
            "800m·수요50% 기준 순위가 Modeling TOP 20과 일치",
        ),
        (
            "EV-05",
            rankings["sensitivity_score"].between(0, 1).all(),
            "민감도 점수 범위 0~1",
        ),
        (
            "EV-06",
            exclusions[
                "sufficient_supply_edges_in_candidate_table"
            ].eq(0).all(),
            "좌석 충분 도로는 후보표에서 제외",
        ),
        (
            "EV-07",
            coverage[
                coverage["evidence_id"].eq("destination_types_at_least_1")
            ]["coverage_rate"].eq(1).all(),
            "기존 39개 TOP 후보 모두 목적지 연결 근거 보유",
        ),
        (
            "EV-08",
            set(shortlist["bench_threshold_m"]) == set(BENCH_THRESHOLDS_M)
            and shortlist.groupby("bench_threshold_m").size().eq(
                CONSENSUS_SHORTLIST_N
            ).all(),
            "300m·500m 현장조사 우선 후보 각 15개",
        ),
        (
            "EV-09",
            shortlist[["midpoint_lat", "midpoint_lon"]].notna().all().all(),
            "현장조사 후보 좌표 완전",
        ),
        (
            "EV-10",
            consensus.groupby("bench_threshold_m")["stability_band"]
            .apply(lambda x: x.eq("STABLE_2_OF_3_PLUS").any())
            .all(),
            "각 기준거리에서 2/3 이상 안정 후보 존재",
        ),
        (
            "EV-11",
            len(unique_shortlist) == shortlist["physical_id"].nunique()
            and unique_shortlist["physical_id"].is_unique,
            f"현장조사 중복 제거 고유 도로 {len(unique_shortlist)}개",
        ),
    ]
    return pd.DataFrame(
        [
            {
                "check_id": check_id,
                "status": "PASS" if passed else "FAIL",
                "detail": detail,
            }
            for check_id, passed, detail in checks
        ]
    )


def main() -> None:
    started = datetime.now().astimezone()
    overlap = read_csv(MODEL_OUT / "route_overlap_and_bench_gap_edges.csv")
    modeled_candidates = read_csv(
        MODEL_OUT / "priority_edge_candidates_all.csv"
    )
    baseline_unique = read_csv(
        MODEL_OUT / "priority_edge_candidates_unique_review.csv"
    )
    model_summary = json.loads(
        (MODEL_QA / "modeling_summary.json").read_text(encoding="utf-8")
    )

    rankings, scenario_summary, consensus = calculate_sensitivity(overlap)
    baseline_comparison, step_comparison = compare_to_baseline(rankings)
    consensus = enrich_consensus(consensus, modeled_candidates)
    shortlist = make_field_review_shortlist(consensus)
    unique_shortlist = make_unique_field_review_template(shortlist)
    coverage = evidence_coverage(baseline_unique)
    exclusions = exclusion_checks(overlap, modeled_candidates)
    risks = data_quality_risks()

    stability_summary = (
        consensus.groupby(["bench_threshold_m", "stability_band"], as_index=False)
        .size()
        .rename(columns={"size": "candidate_count"})
    )
    trace_samples = (
        shortlist.groupby("bench_threshold_m", as_index=False, group_keys=False)
        .head(5)
        .copy()
    )
    checks = validation_checks(
        model_summary,
        rankings,
        scenario_summary,
        consensus,
        shortlist,
        unique_shortlist,
        coverage,
        exclusions,
        modeled_candidates,
    )

    rankings.to_csv(
        OUT / "sensitivity_rankings_all.csv",
        index=False,
        encoding="utf-8-sig",
    )
    scenario_summary.to_csv(
        OUT / "sensitivity_scenario_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    baseline_comparison.to_csv(
        OUT / "baseline_top20_sensitivity_comparison.csv",
        index=False,
        encoding="utf-8-sig",
    )
    step_comparison.to_csv(
        OUT / "step_scenario_top20_comparison.csv",
        index=False,
        encoding="utf-8-sig",
    )
    consensus.to_csv(
        OUT / "consensus_candidates_by_threshold.csv",
        index=False,
        encoding="utf-8-sig",
    )
    stability_summary.to_csv(
        OUT / "stability_band_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    shortlist.to_csv(
        OUT / "field_review_priority_and_template.csv",
        index=False,
        encoding="utf-8-sig",
    )
    unique_shortlist.to_csv(
        OUT / "field_review_unique_template.csv",
        index=False,
        encoding="utf-8-sig",
    )
    coverage.to_csv(
        OUT / "candidate_evidence_coverage.csv",
        index=False,
        encoding="utf-8-sig",
    )
    exclusions.to_csv(
        OUT / "sufficient_supply_exclusion_checks.csv",
        index=False,
        encoding="utf-8-sig",
    )
    risks.to_csv(
        OUT / "decision_data_quality_risks.csv",
        index=False,
        encoding="utf-8-sig",
    )
    trace_samples.to_csv(
        OUT / "candidate_trace_samples.csv",
        index=False,
        encoding="utf-8-sig",
    )
    checks.to_csv(
        QA / "evaluation_validation_checks.csv",
        index=False,
        encoding="utf-8-sig",
    )

    stable_counts = (
        consensus[consensus["stability_band"].eq("STABLE_2_OF_3_PLUS")]
        .groupby("bench_threshold_m")
        .size()
        .to_dict()
    )
    assessment = (
        "SHARE_WITH_CAVEATS"
        if checks["status"].eq("PASS").all()
        else "NEEDS_REVISION"
    )
    summary = {
        "pipeline": "dalseo_candidate_evaluation",
        "started_at": started.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "status": "PASS" if checks["status"].eq("PASS").all() else "FAIL",
        "overall_assessment": assessment,
        "decision_scope": "field_review_prioritization_not_final_installation",
        "sensitivity_design": {
            "scenarios": list(SCENARIOS),
            "bench_thresholds_m": list(BENCH_THRESHOLDS_M),
            "od_radii_m": list(OD_RADII_M),
            "demand_weights": list(DEMAND_WEIGHTS),
            "gap_weights": [round(1 - value, 2) for value in DEMAND_WEIGHTS],
            "combinations_total": len(scenario_summary),
            "combinations_per_threshold": COMBINATIONS_PER_THRESHOLD,
            "stable_definition": "selected_in_at_least_12_of_18_combinations",
        },
        "counts": {
            "ranked_sensitivity_rows": len(rankings),
            "unique_selected_candidates_300m": int(
                consensus["bench_threshold_m"].eq(300).sum()
            ),
            "unique_selected_candidates_500m": int(
                consensus["bench_threshold_m"].eq(500).sum()
            ),
            "stable_candidates_300m": int(stable_counts.get(300, 0)),
            "stable_candidates_500m": int(stable_counts.get(500, 0)),
            "field_review_rows": len(shortlist),
            "field_review_unique_edges": int(shortlist["physical_id"].nunique()),
            "field_review_unique_rows": len(unique_shortlist),
            "high_risks": int(risks["severity"].eq("High").sum()),
            "minimum_top20_retention_vs_baseline": float(
                baseline_comparison["top20_retention_rate"].min()
            ),
            "median_top20_retention_vs_baseline": float(
                baseline_comparison["top20_retention_rate"].median()
            ),
            "minimum_step_scenario_top20_overlap_rate": float(
                step_comparison["top20_retention_rate"].min()
            ),
        },
        "validation": checks.to_dict("records"),
        "required_decision_caveat": (
            "안전성·경사도·설치 가능성과 좌석 현장상태가 없으므로 "
            "현장조사 순서로만 사용"
        ),
    }
    (QA / "evaluation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
