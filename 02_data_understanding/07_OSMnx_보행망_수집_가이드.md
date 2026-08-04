# OSMnx 달서구 보행망 수집 가이드

- 작성일: 2026-07-23
- 대상: 대구광역시 달서구
- 수집 유형: OSMnx `network_type="walk"`
- 실행 주체: 사용자 로컬 환경

## 1. 무엇을 수집하는가

달서구 센서스 행정동 경계 23개를 하나의 Polygon으로 합치고, 경계 근처 연결이 잘리지 않도록 외곽 500m까지 OSM 보행 가능 도로망을 수집한다. 결과에는 달서구 내부 노드와 달서구를 교차하는 링크를 구분하는 속성이 포함된다.

이 데이터는 실제 고령자 GPS 이동경로가 아니다. 또한 OSMnx `walk` 네트워크는 OSM 태그상 보행 가능한 도로·보행로의 중심선이며, 실제 보도의 완전한 위치 목록이 아니다. 보도가 도로 중심선으로 표현되거나 OSM에서 누락된 곳이 있을 수 있다.

## 2. 권장 설치 방법

OSMnx 공식 문서는 conda-forge 환경을 가장 안정적인 설치 방법으로 안내한다.

```bash
conda create --strict-channel-priority -c conda-forge -n dalseo-osmnx python=3.12 osmnx=2.1
conda activate dalseo-osmnx
```

pip을 사용하려면 프로젝트 루트에서 다음을 실행한다.

```bash
python3 -m venv .venv-osmnx
source .venv-osmnx/bin/activate
python -m pip install --upgrade pip
python -m pip install -r 02_data_understanding/requirements-osmnx.txt
```

## 3. 실행

프로젝트 루트 `/Users/moon/Downloads/대구`에서 실행한다.

```bash
python 02_data_understanding/scripts/collect_osmnx_walk_network.py
```

행정동 경계는 프로젝트 안의 `BND_ADM_DONG_PG.shp`를 자동 탐색한다. 자동 탐색이 실패하면 경로를 직접 지정한다.

```bash
python 02_data_understanding/scripts/collect_osmnx_walk_network.py \
  --boundary "국토교통부_(센서스경계)행정동경계BND_ADM_DONG_PG/BND_ADM_DONG_PG.shp" \
  --buffer-m 500 \
  --timeout 300 \
  --max-retries 3
```

전체 옵션은 다음으로 확인한다.

```bash
python 02_data_understanding/scripts/collect_osmnx_walk_network.py --help
```

## 4. 생성 파일

기본 출력 폴더는 `data_external/osmnx_dalseo_walk`이다.

| 파일 | 용도 |
|---|---|
| `dalseo_walk_buffered_wgs84.graphml` | OSMnx 재사용·원본 위경도 보존 |
| `dalseo_walk_buffered_epsg5179.graphml` | 미터 단위 네트워크 분석 |
| `dalseo_walk_buffered_epsg5179.gpkg` | QGIS 확인, 노드·방향성 링크 레이어 |
| `dalseo_boundary_core_wgs84.geojson` | 달서구 원경계 |
| `dalseo_boundary_download_buffer_wgs84.geojson` | 실제 다운로드 범위 |
| `collection_summary.json` | 수집시각·버전·노드/링크·연결성·품질 경고 |
| `highway_type_distribution.csv` | OSM `highway` 유형별 방향성 링크 수 |
| `dalseo_walk_network_preview.png` | 보행망 육안 확인용 미리보기 |
| `cache/` | Overpass 응답 캐시, 재실행 시 활용 |
| `logs/` | OSMnx 실행 로그 |

GeoPackage와 GraphML에는 다음 보조 속성이 추가된다.

- 노드 `in_dalseo`: 달서구 원경계 내부 여부
- 노드 `weak_component_id`: 약연결요소 ID, 1이 가장 큰 요소
- 링크 `intersects_dalseo`: 달서구 원경계 교차 여부
- 링크 `is_steps`: 계단 여부
- 링크 `has_sidewalk_tag`: `sidewalk` 태그 기록 여부
- 링크 `is_crossing`: 횡단 경로 태그 여부

## 5. 수집 후 반드시 확인할 것

1. `collection_summary.json`의 `status`가 `collected`인지 확인한다.
2. `boundary.dong_count`가 23인지 확인한다.
3. `network.nodes_in_dalseo`와 `directed_edges_intersecting_dalseo`가 0보다 큰지 확인한다.
4. `largest_weak_component_node_share`가 낮다면 분리된 네트워크의 위치를 지도에서 확인한다.
5. 하천·철도·고속도로 횡단부, 경로당·복지시설·의료기관·주요 정류장 주변을 표본 검수한다.
6. `sidewalk_tag_coverage`가 낮더라도 바로 보도 부재로 해석하지 않는다. 이는 OSM 태그 기록률이다.
7. `is_steps=true` 링크는 고령자 경로에서 제외하거나 비용을 높이는 민감도 분석을 적용한다.

## 6. 분석 적용 원칙

- 경로 분석은 EPSG:5179 GraphML을 사용하고 링크의 `length`를 거리비용으로 사용한다.
- 출발지와 목적지는 `in_dalseo=true`인 네트워크에 스냅한다.
- 서로 다른 `weak_component_id`에 스냅된 출발지·목적지는 경로 없음으로 기록하며 임의 직선 연결하지 않는다.
- 300m·500m 벤치 공백은 네트워크 거리로 계산한다.
- 계단, 보행 제한, 표면, 경사도는 이후 접근성 비용의 별도 속성으로 관리한다.
- OSM 자료 기준시점은 `collection_summary.json`의 `collected_at_utc`로 기록한다.

## 7. 출처와 라이선스

- 데이터: © OpenStreetMap contributors, Open Database License(ODbL)
- 수집·모델링: OSMnx
- 공모전 지도와 보고서에는 OpenStreetMap 출처표시를 포함한다.

공식 문서:

- [OSMnx 설치](https://osmnx.readthedocs.io/en/stable/installation.html)
- [OSMnx 시작하기](https://osmnx.readthedocs.io/en/stable/getting-started.html)
- [graph_from_polygon API](https://osmnx.readthedocs.io/en/stable/user-reference.html#osmnx.graph.graph_from_polygon)
- [GraphML·GeoPackage 저장 API](https://osmnx.readthedocs.io/en/stable/user-reference.html#module-osmnx.io)
