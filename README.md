<h1 align="center">달서구 어르신 휴식 좌석 공백 분석</h1>

<p align="center">
  <a href="https://github.com/cres17/daegu-dalseo-senior-bench/actions/workflows/deploy-pages.yml"><img src="https://github.com/cres17/daegu-dalseo-senior-bench/actions/workflows/deploy-pages.yml/badge.svg" alt="Deploy map" height="20"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3776AB?style=flat&amp;logoColor=white&amp;logo=python" alt="Python" height="20">
  <img src="https://img.shields.io/badge/pandas-150458?style=flat&amp;logoColor=white&amp;logo=pandas" alt="pandas" height="20">
  <img src="https://img.shields.io/badge/NumPy-013243?style=flat&amp;logoColor=white&amp;logo=numpy" alt="NumPy" height="20">
  <img src="https://img.shields.io/badge/OSMnx-7EBC6F?style=flat&amp;logoColor=black" alt="OSMnx" height="20">
  <br>
  <img src="https://img.shields.io/badge/OpenStreetMap-7EBC6F?style=flat&amp;logoColor=black&amp;logo=openstreetmap" alt="OpenStreetMap" height="20">
  <img src="https://img.shields.io/badge/Leaflet%201.9-199900?style=flat&amp;logoColor=white&amp;logo=leaflet" alt="Leaflet 1.9" height="20">
  <img src="https://img.shields.io/badge/GitHub%20Pages-181717?style=flat&amp;logoColor=white&amp;logo=github" alt="GitHub Pages" height="20">
  <img src="https://img.shields.io/badge/GitHub%20Actions-2088FF?style=flat&amp;logoColor=white&amp;logo=githubactions" alt="GitHub Actions" height="20">
</p>

대구 달서구에서 어르신의 보행이 많을 것으로 예상되면서 앉아 쉴 곳은 먼 구간을 찾는 프로젝트입니다. 공공데이터와 OSM 보행망으로 후보를 계산한 뒤 현장검수 결과를 반영했습니다.

[공개 지도](https://cres17.github.io/daegu-dalseo-senior-bench/) · [분석 보고서](https://cres17.github.io/daegu-dalseo-senior-bench/final-review.html) · [데이터 출처](DATA_SOURCES.md)

## 결과의 범위

결과는 설치 확정 위치가 아니라 추가 현장조사와 관계기관 협의가 필요한 구간입니다. 실제 고령자의 GPS 이동 기록을 사용하지 않았으며, 인구와 생활시설을 바탕으로 보행수요를 추정했습니다.

v2 현장검수 후보는 40개입니다. 이 중 `INSTALLABLE`로 분류된 8개도 설치 후 유효폭이나 측정 근거가 부족해 안전 게이트에서 차단되었습니다. 공개 지도에는 이 8개를 재검수 대상으로 표시합니다.

## 분석 과정

![분석부터 공개까지의 과정](docs/assets/readme/architecture.svg)

폴더는 CRISP-DM 순서로 구성했습니다. Python 스크립트가 단계별 파일을 만들고, 마지막 산출물을 Leaflet 지도가 읽습니다. 실행 중인 API 서버나 DB는 없습니다.

| 폴더 | 작업 |
|---|---|
| `01_business_understanding` | 분석 목적과 요구사항 |
| `02_data_understanding` | 원천자료 확인, 보행망 수집과 검수 |
| `03_data_preparation` | 좌표 통일과 분석 레이어 생성 |
| `04_modeling` | 예상 경로, 좌석 공백과 후보 점수 계산 |
| `05_evaluation` | 민감도 평가, 현장검수와 안전 게이트 |
| `06_deployment` | 갱신 절차와 운영 문서 |
| `docs/` | 공개 지도, JSON과 보고서 |

## 보행망과 후보 점수

OSMnx로 달서구 경계에 500m 버퍼를 둔 보행망을 수집합니다. 연결성, 계단과 서비스도로를 자동 검수하고 수동 판정을 반영합니다. 후속 분석은 저장된 GeoPackage를 입력으로 사용합니다.

고령인구와 생활시설, 기존 좌석을 EPSG:5179로 맞춥니다. 시설은 가까운 노드가 아니라 가까운 도로 엣지에 투영해 연결합니다. 동일 위치의 의료기관은 합쳐 수요가 중복 계산되지 않도록 했습니다.

예상 보행 경로는 Dijkstra로 계산합니다. 도로를 최대 25m 간격으로 나누고 다중 시작점 탐색으로 가장 가까운 벤치·정류장 의자까지의 보행망 거리를 구합니다.

우선 점수는 `√(예상 보행수요 백분위 × 휴식공백 백분위)`입니다. 수요나 공백 한쪽만 큰 구간이 상위에 몰리지 않도록 기하평균을 사용했습니다. 생활권 반경은 500·800·1,200m를 비교하고 주 분석에는 800m를 적용했습니다. 계단, 공백 기준과 가중치를 포함해 36개 조건에서 순위 변화를 확인했습니다.

## 현장검수

현장 결과를 반영해 후보를 재산정한 뒤 유효폭, 경사, 횡단 위험, 검토자와 증빙을 다시 검사합니다. 현장 기록이 `INSTALLABLE`이라는 이유만으로 공개 가능한 후보로 처리하지 않습니다.

프로젝트의 안전 게이트는 설치 후 유효폭 2.0m 이상을 원칙으로 합니다. 1.2~2.0m는 관리청 예외 승인 근거가 모두 필요하며, 1.2m 미만은 차단합니다. 이 값과 증빙 검사 로직은 `integrate_v2_field_review_results.py`와 테스트에서 관리합니다.

`build_public_review_data.py`는 공개할 필드만 `docs/candidates.json`으로 내보냅니다. 통과 후보가 없을 때는 차단 후보를 재검수 상태로 공개합니다. 통과 결과와 차단 결과가 잘못 섞인 경우에는 생성을 중단합니다.

## 기술별 역할

### OSMnx · OpenStreetMap

`collect_osmnx_walk_network.py`에서만 OSMnx(2.1 계열)를 씁니다. `graph_from_polygon`으로 보행 가능 도로를 받고, `project_graph`로 투영한 뒤 GraphML과 GeoPackage로 저장합니다. 이후 단계는 이 GeoPackage를 입력으로 씁니다. OSM은 계속 수정되므로 다시 수집하면 과거 보행망과 같지 않을 수 있어, 수집 시점과 결과 요약을 함께 기록합니다.

### pandas · NumPy

모든 분석 단계의 표 처리와 계산을 맡습니다. 공간 연산 라이브러리 없이 직접 구현한 부분이 많습니다.

| 기능 | 구현 |
|------|------|
| 좌표 변환 | 위경도 ↔ EPSG:5179(TM) 변환식을 NumPy로 구현 (`forward_tm_5179`, `inverse_epsg5179`) |
| SHP · DBF 읽기 | `struct`로 행정동 경계 Polygon과 속성 파일을 직접 파싱 |
| GeoPackage 읽기 | `sqlite3`로 GeoPackage의 WKB 바이너리를 읽어 LineString으로 디코딩 |
| 경로 탐색 | `heapq` 기반 Dijkstra. 시설을 가까운 노드가 아니라 가장 가까운 도로 엣지에 투영해 연결 |
| 좌석 공백 | 도로를 최대 25m 간격으로 나눠, 보행망을 따라 가장 가까운 벤치 · 정류장 의자까지의 거리를 다중 시작점 Dijkstra로 계산 |
| 우선 점수 | `√(예상 보행수요 백분위 × 휴식공백 백분위)` 기하평균. 한쪽 값만 큰 도로가 상위를 독차지하지 않게 함 |

생활권 반경은 500 · 800 · 1,200m를 비교하고 800m를 주 분석값으로 씁니다. 같은 좌표에 여러 행이 있는 의료기관은 하나의 위치로 합쳐 수요가 부풀지 않게 했습니다.

### openpyxl

새로 추가된 원천자료를 이전 프로파일과 비교 검증하는 `validate_updated_sources.py`에서 XLSX 파일을 읽을 때 씁니다.

### unittest — 안전 게이트 검증

`tests/test_v2_field_review_safety.py`의 14개 테스트가 안전 게이트 규칙을 고정합니다. 설치 후 유효폭은 2.0m 이상을 원칙으로 하고, 1.2~2.0m는 관리청 예외 승인 근거가 모두 있어야 하며, 1.2m 미만은 차단합니다. 공개 JSON에 차단 후보가 섞이지 않는지도 함께 검사합니다.

### Leaflet · JavaScript

`docs/index.html`이 Leaflet 1.9.4로 OpenStreetMap 타일 지도를 만들고, `fetch('./candidates.json')`으로 후보를 읽어 요약 숫자 · 마커 · 후보 카드를 그립니다. 후보를 누르면 예상 수요 · 휴식 공백 · 안전 게이트 차단 사유를 보여 줍니다. `docs/final-review.html`은 분석 과정과 검증 결과를 정리한 보고서 페이지입니다.

### GitHub Pages · GitHub Actions

`deploy-pages.yml`이 `main`의 `docs/` 폴더를 GitHub Pages에 배포합니다. 공개 결과가 바뀌면 커밋 단위로 언제 어떤 후보가 공개됐는지 추적할 수 있습니다.

## 재현과 갱신

단계별 requirements와 실행 가이드를 각 폴더에 두었습니다. 수집부터 다시 실행하면 OSM의 변경으로 보행망이 달라질 수 있으므로, 입력 시점과 SHA-256을 함께 기록해야 이전 결과와 비교할 수 있습니다.

정적 지도는 `docs/index.html`, 최종 보고서는 `docs/final-review.html`입니다. `deploy-pages.yml`이 `main`의 `docs/`를 GitHub Pages에 배포합니다.

안전 게이트 테스트는 아래 명령으로 실행합니다.

```bash
python -m unittest discover -s tests -p 'test_v2_field_review_safety.py'
```

테스트는 유효폭과 증빙 조건, 공개 데이터에 차단 후보가 잘못 포함되는 경우를 확인합니다.
