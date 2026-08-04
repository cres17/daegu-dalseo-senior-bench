#!/usr/bin/env python3
"""Evaluation 재현 노트북을 생성한다."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = (
    ROOT
    / "05_evaluation"
    / "notebooks"
    / "01_candidate_evaluation.ipynb"
)
NOTEBOOK.parent.mkdir(parents=True, exist_ok=True)


def markdown(text: str) -> dict[str, object]:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": text.splitlines(keepends=True),
    }


def code(text: str) -> dict[str, object]:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": text.splitlines(keepends=True),
    }


cells = [
    markdown(
        """# 달서구 벤치 설치 우선 검토구간 Evaluation

## tl;dr

- 평가 결과는 **공모전 공유에는 사용 가능하지만, 실제 설치 결정에는 현장조사 조건이 붙는다**.
- 36개 민감도 조합에서 300m 기준 16개, 500m 기준 15개 도로가 18개 조합 중 12개 이상에서 TOP 20을 유지했다.
- 계단 포함·제외의 TOP 20은 모든 조합에서 완전히 일치했다.
- 800m 반경을 고정하면 수요·공백 가중치 변경 후 TOP 20 유지율은 85~90%였지만, OD 반경을 500m·1,200m로 바꾸면 최저 50%까지 낮아졌다.
- 현장조사 우선표는 300m·500m 각 15개, 총 30행이며 중복을 제외하면 29개 도로다.
"""
    ),
    markdown(
        """## Context & Methods

평가는 기존 Modeling을 다시 최적화하는 단계가 아니다. 후보가 계단 포함 여부, OD 반경 500·800·1,200m, 수요 가중치 35·50·65%, 벤치 공백 300·500m 변화에도 유지되는지 확인한다.

### Key Assumptions

- 실제 고령자 GPS가 아닌 예상 보행 수요다.
- 18개 조합 중 12개 이상 TOP 20에 들면 현장조사 운영상 `안정 후보`로 분류한다.
- 안정성은 설치 적합성이 아니라 순위 민감도가 낮다는 뜻이다.
- 안전성·경사·보도 폭·사유지·실제 좌석 상태는 현재 점수에 없다.
"""
    ),
    code(
        """from pathlib import Path
import json
import subprocess
import sys
import numpy as np
import pandas as pd

ROOT = Path.cwd()
if not (ROOT / '05_evaluation').exists():
    ROOT = Path.cwd().parent
SCRIPT = ROOT / '05_evaluation' / 'scripts' / 'evaluate_priority_candidates.py'
OUT = ROOT / '05_evaluation' / 'outputs'
QA = ROOT / '05_evaluation' / 'qa'
print('ROOT:', ROOT)
print('PYTHON:', sys.executable)
"""
    ),
    markdown("## Data\n\n### 1. Re-run the evaluation pipeline\n"),
    code(
        """completed = subprocess.run(
    [sys.executable, str(SCRIPT)],
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
)
print(completed.stdout[-2500:])
"""
    ),
    code(
        """summary = json.loads((QA / 'evaluation_summary.json').read_text(encoding='utf-8'))
checks = pd.read_csv(QA / 'evaluation_validation_checks.csv', encoding='utf-8-sig')
comparison = pd.read_csv(OUT / 'baseline_top20_sensitivity_comparison.csv', encoding='utf-8-sig')
steps = pd.read_csv(OUT / 'step_scenario_top20_comparison.csv', encoding='utf-8-sig')
consensus = pd.read_csv(OUT / 'consensus_candidates_by_threshold.csv', encoding='utf-8-sig')
shortlist = pd.read_csv(OUT / 'field_review_priority_and_template.csv', encoding='utf-8-sig')
coverage = pd.read_csv(OUT / 'candidate_evidence_coverage.csv', encoding='utf-8-sig')
exclusions = pd.read_csv(OUT / 'sufficient_supply_exclusion_checks.csv', encoding='utf-8-sig')
risks = pd.read_csv(OUT / 'decision_data_quality_risks.csv', encoding='utf-8-sig')
print('assessment:', summary['overall_assessment'])
print(checks.to_string(index=False))
"""
    ),
    markdown("## Results\n\n### 2. Sensitivity to distance and weights\n"),
    code(
        """radius_summary = (
    comparison.groupby(['bench_threshold_m', 'od_radius_m'])['top20_retention_rate']
    .agg(['min', 'median', 'max'])
    .reset_index()
)
weight_at_800 = (
    comparison[comparison['od_radius_m'].eq(800)]
    .groupby(['bench_threshold_m', 'demand_weight'])['top20_retention_rate']
    .mean()
    .reset_index()
)
print('OD 반경별 baseline TOP20 유지율')
print(radius_summary.to_string(index=False))
print('\\n800m 고정 시 가중치별 유지율')
print(weight_at_800.to_string(index=False))
print('\\n계단 시나리오 최소 유지율:', steps['top20_retention_rate'].min())
"""
    ),
    markdown("### 3. Stable candidates and their drivers\n"),
    code(
        """stable = consensus[consensus['stability_band'].eq('STABLE_2_OF_3_PLUS')]
driver_summary = (
    stable.groupby(['bench_threshold_m', 'selection_driver'])
    .size()
    .rename('candidate_count')
    .reset_index()
)
preview_columns = [
    'bench_threshold_m', 'sensitivity_selection_count',
    'selection_driver', 'name', 'route_count_800m',
    'nearest_seat_max_m', 'gap_length_300m', 'gap_length_500m'
]
print(driver_summary.to_string(index=False))
print('\\n기준별 안정 후보 상위 8개')
print(
    stable.groupby('bench_threshold_m', group_keys=False)
    .head(8)[preview_columns]
    .to_string(index=False)
)
"""
    ),
    markdown("### 4. Evidence coverage and sufficient-supply exclusion\n"),
    code(
        """print(coverage.to_string(index=False))
print('\\n좌석 충분 구간 제외 점검')
print(exclusions.to_string(index=False))
"""
    ),
    markdown("### 5. Field-review handoff\n"),
    code(
        """review_columns = [
    'field_review_id', 'bench_threshold_m', 'field_review_order',
    'sensitivity_selection_count', 'selection_driver', 'name',
    'midpoint_lat', 'midpoint_lon', 'kakao_map_url'
]
print(shortlist[review_columns].head(12).to_string(index=False))
print('\\n현장조사 행:', len(shortlist), '고유 도로:', shortlist['physical_id'].nunique())
"""
    ),
    markdown("### 6. Independent assertions\n"),
    code(
        """assert summary['status'] == 'PASS'
assert checks['status'].eq('PASS').all()
assert len(comparison) == 36
assert steps['top20_retention_rate'].eq(1).all()
assert exclusions['sufficient_supply_edges_in_candidate_table'].eq(0).all()
assert shortlist.groupby('bench_threshold_m').size().eq(15).all()
assert shortlist[['midpoint_lat', 'midpoint_lon']].notna().all().all()
assert coverage.loc[
    coverage['evidence_id'].eq('destination_types_at_least_1'),
    'coverage_rate'
].eq(1).all()
print('독립 Evaluation 점검 통과')
"""
    ),
    markdown(
        """## Takeaways

- 모델은 좌석이 충분한 도로를 후보에서 제외하고, 기존 TOP 후보 39개 모두에 최소 한 종류의 목적지 연결 근거를 남겼다.
- 계단 처리에는 매우 안정적이지만 OD 생활권 반경 선택에는 중간 수준의 민감도가 있다. 따라서 800m만 절대 기준으로 주장하지 않고 500m·1,200m 결과를 함께 제시해야 한다.
- 500m 후보 일부는 큰 좌석 공백이 순위를 이끄는 `GAP_LED` 구간이다. 예상 경로 수가 적은 구간은 현장에서 실제 보행 수요를 추가 확인해야 한다.
- 최종 평가는 `Share with caveats`다. 공모전의 후보 선별 근거로는 사용 가능하지만, 설치 결정 전 안전·경사·보도 폭·소유권·좌석 현존 여부 확인이 필요하다.
"""
    ),
]

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python", "version": "3"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}
NOTEBOOK.write_text(
    json.dumps(notebook, ensure_ascii=False, indent=1),
    encoding="utf-8",
)
print(NOTEBOOK)
