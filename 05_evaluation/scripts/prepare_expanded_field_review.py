#!/usr/bin/env python3
"""민감도 출현 6회 이상인 54개 후보에 기존 29개 판정을 결합한다."""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "05_evaluation" / "outputs"
CONSENSUS = OUT / "consensus_candidates_by_threshold.csv"
ORIGINAL_TEMPLATE = OUT / "field_review_unique_template.csv"
OUTPUT = OUT / "field_review_54_prefilled.csv"
MIN_SELECTION_COUNT = 6

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


def clean_name(value: object) -> str:
    if pd.isna(value) or not str(value).strip():
        return "도로명 없음"
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                values = [str(item).strip() for item in parsed if str(item).strip()]
                if values:
                    return " / ".join(values)
        except (SyntaxError, ValueError):
            pass
    return text


def newest_completed_file() -> Path:
    candidates = sorted(
        ROOT.glob("dalseo_field_review_completed*.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("기존 29개 현장조사 완료 CSV가 없습니다.")
    return candidates[0]


def main() -> None:
    consensus = pd.read_csv(CONSENSUS, encoding="utf-8-sig")
    pool = consensus[
        consensus["sensitivity_selection_count"].ge(MIN_SELECTION_COUNT)
    ].copy()
    pool = pool.sort_values(
        [
            "bench_threshold_m",
            "sensitivity_selection_count",
            "median_selected_rank",
            "mean_selected_score",
            "physical_id",
        ],
        ascending=[True, False, True, False, True],
    )
    pool["pool_review_order"] = (
        pool.groupby("bench_threshold_m").cumcount() + 1
    )

    existing_template = pd.read_csv(
        ORIGINAL_TEMPLATE, encoding="utf-8-sig", dtype=str
    ).fillna("")
    existing_ids = dict(
        zip(
            existing_template["physical_id"],
            existing_template["field_review_unique_id"],
        )
    )
    completed_path = newest_completed_file()
    completed = pd.read_csv(
        completed_path, encoding="utf-8-sig", dtype=str
    ).fillna("")
    completed_manual = completed.set_index("physical_id")[MANUAL_COLUMNS]

    existing_numeric_ids = [
        int(value.split("_")[-1])
        for value in existing_ids.values()
        if value.startswith("FRU_")
    ]
    next_id = max(existing_numeric_ids, default=0) + 1
    new_id_by_physical: dict[str, str] = {}
    rows: list[dict[str, object]] = []

    ordered_physical_ids = list(existing_template["physical_id"])
    remaining = (
        pool[~pool["physical_id"].isin(ordered_physical_ids)]
        .sort_values(
            [
                "sensitivity_selection_count",
                "median_selected_rank",
                "mean_selected_score",
                "physical_id",
            ],
            ascending=[False, True, False, True],
        )["physical_id"]
        .drop_duplicates()
        .tolist()
    )
    ordered_physical_ids.extend(remaining)

    for physical_id in ordered_physical_ids:
        group = pool[pool["physical_id"].eq(physical_id)]
        if group.empty:
            continue
        representative = group.sort_values(
            [
                "sensitivity_selection_count",
                "median_selected_rank",
                "mean_selected_score",
            ],
            ascending=[False, True, False],
        ).iloc[0]
        thresholds = sorted(group["bench_threshold_m"].astype(int).unique())
        threshold_rows = {
            int(row["bench_threshold_m"]): row
            for _, row in group.iterrows()
        }
        if physical_id in existing_ids:
            unique_id = existing_ids[physical_id]
            prior_review = "YES"
        else:
            unique_id = new_id_by_physical.setdefault(
                physical_id, f"FRU_{next_id:03d}"
            )
            next_id += 1
            prior_review = "NO"

        row: dict[str, object] = {
            "field_review_unique_id": unique_id,
            "physical_id": physical_id,
            "prior_review_included": prior_review,
            "candidate_thresholds_m": " | ".join(map(str, thresholds)),
            "threshold_count": len(thresholds),
            "review_order_300m": (
                int(threshold_rows[300]["pool_review_order"])
                if 300 in threshold_rows
                else ""
            ),
            "review_order_500m": (
                int(threshold_rows[500]["pool_review_order"])
                if 500 in threshold_rows
                else ""
            ),
            "selection_count_300m": (
                int(threshold_rows[300]["sensitivity_selection_count"])
                if 300 in threshold_rows
                else 0
            ),
            "selection_count_500m": (
                int(threshold_rows[500]["sensitivity_selection_count"])
                if 500 in threshold_rows
                else 0
            ),
            "selection_drivers": " | ".join(
                sorted(group["selection_driver"].dropna().astype(str).unique())
            ),
            "stability_bands": " | ".join(
                sorted(group["stability_band"].dropna().astype(str).unique())
            ),
            "name": clean_name(representative["name"]),
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
        for column in MANUAL_COLUMNS:
            row[column] = (
                completed_manual.at[physical_id, column]
                if physical_id in completed_manual.index
                else ""
            )
        rows.append(row)

    result = pd.DataFrame(rows)
    if len(result) != 54:
        raise ValueError(f"54개 후보가 생성되지 않았습니다: {len(result)}")
    if result["physical_id"].nunique() != 54:
        raise ValueError("physical_id가 중복됐습니다.")
    if int(result["prior_review_included"].eq("YES").sum()) != 29:
        raise ValueError("기존 검토 29개가 모두 연결되지 않았습니다.")
    if int(result["review_status"].ne("").sum()) != 29:
        raise ValueError("기존 29개 판정이 모두 보존되지 않았습니다.")
    if int(result["prior_review_included"].eq("NO").sum()) != 25:
        raise ValueError("추가 검토 후보가 25개가 아닙니다.")

    result.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print(
        {
            "output": str(OUTPUT),
            "unique_candidates": len(result),
            "prefilled_reviews": int(result["review_status"].ne("").sum()),
            "new_candidates": int(result["prior_review_included"].eq("NO").sum()),
            "source_completed_file": completed_path.name,
        }
    )


if __name__ == "__main__":
    main()
