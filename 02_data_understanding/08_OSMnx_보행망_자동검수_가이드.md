# OSMnx 보행망 자동검수 가이드

- 작성일: 2026-07-23
- 입력: `data_external/osmnx_dalseo_walk/dalseo_walk_buffered_epsg5179.gpkg`
- 원칙: 원본 GeoPackage를 수정하지 않고 QA 복사본 생성

## 1. 실행

외부 패키지가 필요하지 않다. 프로젝트 루트에서 실행한다.

```bash
python3 02_data_understanding/scripts/audit_osmnx_walk_network.py
```

## 2. 자동 판정 규칙

| 상태 | 처리 |
|---|---|
| `AUTO_EXCLUDE` | 길이 오류, 보행 허용 근거 없는 접근제한·motorway를 자동 제외 |
| `AUTO_NO_ROUTE` | 가장 큰 연결요소 밖 링크는 다른 요소와 경로를 만들지 않음 |
| `REVIEW_REQUIRED` | 보행·보도 근거가 없는 `trunk`·`trunk_link`만 수동 확인 |
| `AUTO_PENALIZE` | 계단·비포장 가능 통로에 경로비용 페널티 |
| `REVIEW_IF_SELECTED` | 서비스도로·보행 근거가 빈약한 주요도로가 최종 경로에 선택될 때만 확인 |
| `AUTO_KEEP` | 현재 규칙에서 자동 제외 사유가 없는 링크 |

`AUTO_KEEP`은 실제 보도가 완전하게 존재한다는 보증이 아니다. OSM 중심선 자료에서 자동 제외할 근거가 없다는 뜻이다.

## 3. 결과

기본 출력 폴더는 `data_external/osmnx_dalseo_walk/qa`다.

| 파일 | 내용 |
|---|---|
| `dalseo_walk_qa_epsg5179.gpkg` | 원본 노드·링크와 QA 속성을 포함한 QGIS용 파일 |
| `manual_review_segments.csv` | 방향 중복을 제거한 최소 수동검수 후보 |
| `route_action_counts.csv` | 자동 경로 처리별 링크 수 |
| `qa_findings.csv` | 품질 이슈·영향·조치 |
| `qa_summary.json` | 전체 검수 결과와 정책 버전 |

## 4. 링크 QA 컬럼

- `qa_component_id`: 약연결요소 ID
- `qa_in_main_component`: 가장 큰 연결요소 포함 여부
- `qa_crossing_tag`: 링크 자체 횡단 태그 여부
- `qa_crossing_node_adjacent`: 횡단 노드에 접한 링크 여부
- `qa_is_crossing_candidate`: 두 횡단 기준의 합집합
- `qa_manual_review`: 사람이 확인해야 하는 최소 후보
- `qa_review_priority`: 0~3
- `qa_review_status`: 자동검수 상태
- `qa_route_action`: 경로 분석 적용 규칙
- `qa_review_reason`: 판정 근거

## 5. QGIS 확인

`dalseo_walk_qa_epsg5179.gpkg`의 `edges` 레이어를 열고 다음 필터를 적용한다.

```text
"qa_manual_review" = 1
```

이 필터에 남는 링크만 지도에서 확인한다. 기본 정책은 `EXCLUDE_CONSERVATIVE`이므로 확인하지 않더라도 보수적으로 경로에서 제외할 수 있다.

최종 후보구간을 계산한 뒤에는 다음 필터로 실제 선택된 서비스도로·주요도로만 재확인한다.

```text
"qa_review_status" = 'REVIEW_IF_SELECTED'
```

## 6. 경로 분석 적용

- `EXCLUDE`: 그래프에서 제거
- `EXCLUDE_CONSERVATIVE`: 확인 전 제거
- `NO_ROUTE_COMPONENT`: 다른 연결요소와 경로 연결 금지
- `PENALIZE_STEPS`: 높은 비용 또는 계단 제외 시나리오
- `PENALIZE_TRAIL`: 노면 자료 확보 전 보수적 비용 부여
- `KEEP_REVIEW_IF_SELECTED`: 기본 유지 후 실제 후보경로에 포함될 때 확인
- `BUFFER_SUPPORT`: 달서구 경계부 연결을 위해 유지
- `KEEP`: 기본 유지

300m·500m 벤치 공백 분석은 계단 포함·제외 두 시나리오로 수행하고 결과 안정성을 비교한다.

## 7. 수동검수 결과 반영

검수 화면에서 내보낸 `dalseo_manual_review_decisions.csv`를 프로젝트 루트에
저장하고 다음 명령을 실행한다.

```bash
python3 02_data_understanding/scripts/apply_manual_review_decisions.py
```

자동검수본은 수정하지 않으며 다음 파일이 생성된다.

| 파일 | 내용 |
|---|---|
| `qa/dalseo_walk_reviewed_epsg5179.gpkg` | 수동검수 판정이 추가된 분석용 보행망 |
| `qa/manual_review_validation.csv` | 후보 ID·중복·판정값·시각 검증 결과 |
| `qa/manual_review_decision_counts.csv` | 판정별 물리 구간·방향성 링크 수 |
| `qa/manual_review_apply_summary.json` | 입력 해시·반영 건수·정책 요약 |

원래 자동 판정은 `qa_auto_review_status`와 `qa_auto_route_action`에 보존된다.
경로 분석에서는 `qa_effective_route_action`을 사용한다.

| 수동 판정 | 최종 경로 처리 |
|---|---|
| `KEEP` | `KEEP` |
| `EXCLUDE` | `EXCLUDE` |
| `UNCERTAIN` | `EXCLUDE_CONSERVATIVE` |
| `FIELD_CHECK` | `EXCLUDE_CONSERVATIVE` |

`UNCERTAIN`과 `FIELD_CHECK`는 추가 확인 전까지 보수적으로 제외한다.
