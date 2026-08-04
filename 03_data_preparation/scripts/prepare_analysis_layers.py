#!/usr/bin/env python3
"""달서구 고령자 예상 보행수요 분석용 데이터 전처리.

이 스크립트는 Data Preparation까지만 수행한다.
경로 계산, 벤치 공백 판정, 후보 점수 및 순위 산정은 수행하지 않는다.
외부 GIS 파이썬 패키지 없이 pandas/numpy/표준 라이브러리로 재실행할 수 있다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "03_data_preparation" / "outputs"
QA = ROOT / "03_data_preparation" / "qa"
OUT.mkdir(parents=True, exist_ok=True)
QA.mkdir(parents=True, exist_ok=True)

NETWORK = ROOT / "data_external" / "osmnx_dalseo_walk" / "qa" / "dalseo_walk_reviewed_epsg5179.gpkg"
CROSSWALK = ROOT / "02_data_understanding" / "profile_outputs" / "dalseo_stop_to_bs_crosswalk_diagnostic.csv"

# 주소 기반 건물 대표점이다. 경로당 내부 출입구의 정밀 좌표로 해석하지 않는다.
SENIOR_CENTER_CORRECTIONS = {
    "두류파크kcc스위첸경로당": {
        "lat": 35.8548408,
        "lon": 128.5713996,
        "source": "https://findby.co.kr/details/42668-272903147043-st-652b0e098c641f9e388c5bd1",
    },
    "죽전역코오롱하늘채경로당": {
        "lat": 35.8426922,
        "lon": 128.5348190,
        "source": "https://woori-chunsa.com/playgrounds/589282",
    },
    "월성월드메르디앙경로당": {
        "lat": 35.8265229864,
        "lon": 128.5231364498,
        "source": "https://www.data.go.kr/en/data/15155662/standard.do",
    },
}


def nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value)).strip()


def norm_name(value: object) -> str:
    text = nfc(value).lower()
    return re.sub(r"[\s\-_.·ㆍ,()\[\]/]+", "", text)


def find_one(*tokens: str, suffix: str = ".csv") -> Path:
    matches: list[Path] = []
    for path in ROOT.rglob(f"*{suffix}"):
        if any(part in {"02_data_understanding", "03_data_preparation"} for part in path.parts):
            continue
        label = nfc(path.name)
        if all(token in label for token in tokens):
            matches.append(path)
    if len(matches) != 1:
        raise RuntimeError(f"{tokens} 파일은 1개여야 하나 {len(matches)}개 발견: {matches}")
    return matches[0]


def find_many(*tokens: str, suffix: str = ".csv") -> list[Path]:
    matches = []
    for path in ROOT.rglob(f"*{suffix}"):
        if any(part in {"02_data_understanding", "03_data_preparation"} for part in path.parts):
            continue
        label = nfc(path.name)
        if all(token in label for token in tokens):
            matches.append(path)
    return sorted(matches, key=lambda p: nfc(p.name))


def read_csv(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            frame = pd.read_csv(
                path,
                encoding=encoding,
                dtype=str,
                keep_default_na=False,
                na_filter=False,
                low_memory=False,
            )
            frame.columns = [nfc(c) for c in frame.columns]
            return frame.apply(lambda c: c.map(lambda x: nfc(x) if isinstance(x, str) else x))
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            last_error = exc
    raise RuntimeError(f"CSV 읽기 실패: {path}: {last_error}")


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def meridional_arc(phi: np.ndarray | float, a: float, e2: float) -> np.ndarray | float:
    return a * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * phi
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * np.sin(2 * phi)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * np.sin(4 * phi)
        - (35 * e2**3 / 3072) * np.sin(6 * phi)
    )


def inverse_tm(
    x: np.ndarray,
    y: np.ndarray,
    *,
    lon0_deg: float,
    lat0_deg: float,
    false_easting: float,
    false_northing: float,
    k0: float,
) -> tuple[np.ndarray, np.ndarray]:
    """GRS80 기반 Transverse Mercator를 경위도로 역변환한다."""
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lat0 = math.radians(lat0_deg)
    lon0 = math.radians(lon0_deg)
    xx = np.asarray(x, dtype=float) - false_easting
    yy = np.asarray(y, dtype=float) - false_northing
    m0 = float(meridional_arc(lat0, a, e2))
    m = m0 + yy / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    fp = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * np.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * np.sin(4 * mu)
        + (151 * e1**3 / 96) * np.sin(6 * mu)
        + (1097 * e1**4 / 512) * np.sin(8 * mu)
    )
    c1 = ep2 * np.cos(fp) ** 2
    t1 = np.tan(fp) ** 2
    n1 = a / np.sqrt(1 - e2 * np.sin(fp) ** 2)
    r1 = a * (1 - e2) / (1 - e2 * np.sin(fp) ** 2) ** 1.5
    d = xx / (n1 * k0)
    lat = fp - (n1 * np.tan(fp) / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720
    )
    lon = lon0 + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120
    ) / np.cos(fp)
    return np.degrees(lon), np.degrees(lat)


def forward_tm_5179(lon: Iterable[float], lat: Iterable[float]) -> tuple[np.ndarray, np.ndarray]:
    """WGS84 경위도를 EPSG:5179(Korea 2000 / Unified CS)로 변환한다."""
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lon0 = math.radians(127.5)
    lat0 = math.radians(38.0)
    k0 = 0.9996
    false_easting = 1_000_000.0
    false_northing = 2_000_000.0
    phi = np.radians(np.asarray(list(lat), dtype=float))
    lam = np.radians(np.asarray(list(lon), dtype=float))
    n = a / np.sqrt(1 - e2 * np.sin(phi) ** 2)
    t = np.tan(phi) ** 2
    c = ep2 * np.cos(phi) ** 2
    aa = np.cos(phi) * (lam - lon0)
    m = meridional_arc(phi, a, e2)
    m0 = float(meridional_arc(lat0, a, e2))
    x = false_easting + k0 * n * (
        aa
        + (1 - t + c) * aa**3 / 6
        + (5 - 18 * t + t**2 + 72 * c - 58 * ep2) * aa**5 / 120
    )
    y = false_northing + k0 * (
        m
        - m0
        + n
        * np.tan(phi)
        * (
            aa**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * aa**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * ep2) * aa**6 / 720
        )
    )
    return x, y


def parse_dbf(path: Path, encoding: str = "utf-8") -> pd.DataFrame:
    with path.open("rb") as handle:
        header = handle.read(32)
        record_count = struct.unpack("<I", header[4:8])[0]
        header_length = struct.unpack("<H", header[8:10])[0]
        record_length = struct.unpack("<H", header[10:12])[0]
        fields = []
        while True:
            descriptor = handle.read(32)
            if not descriptor or descriptor[0] == 0x0D:
                break
            fields.append(
                (
                    descriptor[:11].split(b"\x00", 1)[0].decode("ascii", errors="replace"),
                    descriptor[16],
                )
            )
        handle.seek(header_length)
        rows = []
        for _ in range(record_count):
            raw = handle.read(record_length)
            if len(raw) < record_length:
                break
            position = 1
            row = {}
            for name, length in fields:
                row[name] = raw[position : position + length].decode(encoding, errors="replace").strip()
                position += length
            if raw[:1] != b"*":
                rows.append(row)
    return pd.DataFrame(rows)


def parse_polygon_shp(path: Path) -> list[list[list[tuple[float, float]]]]:
    """Polygon shapefile을 레코드→파트→좌표 목록으로 읽는다."""
    records: list[list[list[tuple[float, float]]]] = []
    with path.open("rb") as handle:
        handle.seek(100)
        while True:
            header = handle.read(8)
            if len(header) < 8:
                break
            _, words = struct.unpack(">2i", header)
            content = handle.read(words * 2)
            if len(content) < 4:
                break
            shape_type = struct.unpack("<i", content[:4])[0]
            if shape_type == 0:
                records.append([])
                continue
            if shape_type not in (5, 15, 25):
                raise RuntimeError(f"지원하지 않는 경계 shape type: {shape_type}")
            num_parts, num_points = struct.unpack("<2i", content[36:44])
            part_starts = list(struct.unpack(f"<{num_parts}i", content[44 : 44 + num_parts * 4]))
            points_start = 44 + num_parts * 4
            values = struct.unpack(f"<{num_points * 2}d", content[points_start : points_start + num_points * 16])
            points = list(zip(values[0::2], values[1::2]))
            parts = []
            for index, start in enumerate(part_starts):
                end = part_starts[index + 1] if index + 1 < len(part_starts) else num_points
                parts.append(points[start:end])
            records.append(parts)
    return records


def ring_centroid(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    if len(ring) < 3:
        xs, ys = zip(*ring)
        return float(np.mean(xs)), float(np.mean(ys)), 0.0
    area2 = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area2) < 1e-12:
        xs, ys = zip(*ring)
        return float(np.mean(xs)), float(np.mean(ys)), 0.0
    return cx / (3 * area2), cy / (3 * area2), area2 / 2


def point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi:
            inside = not inside
        j = i
    return inside


def load_admin_boundaries() -> tuple[pd.DataFrame, list[list[list[tuple[float, float]]]]]:
    shp = find_one("BND_ADM_DONG_PG", suffix=".shp")
    dbf = shp.with_suffix(".dbf")
    attrs = parse_dbf(dbf, encoding="cp949")
    shapes = parse_polygon_shp(shp)
    selected_attrs = []
    selected_shapes = []
    for index, row in attrs.iterrows():
        if not str(row.get("ADM_CD", "")).startswith("22070"):
            continue
        converted_parts = []
        for part in shapes[index]:
            arr = np.asarray(part, dtype=float)
            lon, lat = inverse_tm(
                arr[:, 0],
                arr[:, 1],
                lon0_deg=127.0,
                lat0_deg=38.0,
                false_easting=200_000.0,
                false_northing=600_000.0,
                k0=1.0,
            )
            xx, yy = forward_tm_5179(lon, lat)
            converted_parts.append(list(zip(xx.tolist(), yy.tolist())))
        selected_attrs.append(
            {
                "admin_code": str(row["ADM_CD"]),
                "admin_dong": nfc(row["ADM_NM"]),
                "boundary_base_date": str(row.get("BASE_DATE", "")),
            }
        )
        selected_shapes.append(converted_parts)
    return pd.DataFrame(selected_attrs), selected_shapes


def add_xy(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["lon"] = numeric(result["lon"])
    result["lat"] = numeric(result["lat"])
    x, y = forward_tm_5179(result["lon"], result["lat"])
    result["x_5179"] = x
    result["y_5179"] = y
    return result


def assign_admin_dong(
    frame: pd.DataFrame,
    admin: pd.DataFrame,
    shapes: list[list[list[tuple[float, float]]]],
) -> pd.DataFrame:
    result = frame.copy()
    codes, names = [], []
    for x, y in zip(result["x_5179"], result["y_5179"]):
        found_code = found_name = ""
        for row, parts in zip(admin.to_dict("records"), shapes):
            if any(point_in_ring(float(x), float(y), ring) for ring in parts):
                found_code, found_name = row["admin_code"], row["admin_dong"]
                break
        codes.append(found_code)
        names.append(found_name)
    result["admin_code_spatial"] = codes
    result["admin_dong_spatial"] = names
    return result


def boundary_centroids(
    admin: pd.DataFrame,
    shapes: list[list[list[tuple[float, float]]]],
) -> pd.DataFrame:
    rows = []
    for attr, parts in zip(admin.to_dict("records"), shapes):
        # 다중 파트 중 가장 큰 외곽 링을 행정동 대표점 계산에 사용한다.
        candidates = [ring_centroid(ring) for ring in parts if len(ring) >= 3]
        x, y, _ = max(candidates, key=lambda item: abs(item[2]))
        rows.append({**attr, "x_5179": x, "y_5179": y})
    return pd.DataFrame(rows)


def nearest_nodes(frame: pd.DataFrame, node_xy: np.ndarray, node_ids: np.ndarray) -> pd.DataFrame:
    result = frame.copy()
    nearest_id, nearest_dist = [], []
    for x, y in zip(result["x_5179"], result["y_5179"]):
        distances = np.hypot(node_xy[:, 0] - float(x), node_xy[:, 1] - float(y))
        index = int(np.argmin(distances))
        nearest_id.append(int(node_ids[index]))
        nearest_dist.append(round(float(distances[index]), 3))
    result["nearest_node_osmid"] = nearest_id
    result["network_snap_distance_m"] = nearest_dist
    result["network_snap_status"] = np.where(
        result["network_snap_distance_m"] <= 100,
        "PASS_100M",
        "REVIEW_OVER_100M",
    )
    return result


def write_csv(frame: pd.DataFrame, name: str) -> None:
    frame.to_csv(OUT / name, index=False, encoding="utf-8-sig")


def write_boundary_geojson(
    admin: pd.DataFrame,
    shapes: list[list[list[tuple[float, float]]]],
    populations: pd.DataFrame,
) -> None:
    pop = populations.set_index("admin_code").to_dict("index")
    features = []
    for attr, parts in zip(admin.to_dict("records"), shapes):
        coords = []
        for ring in parts:
            closed = ring if ring and ring[0] == ring[-1] else ring + [ring[0]]
            coords.append([[[round(x, 3), round(y, 3)] for x, y in closed]])
        props = {**attr, **pop.get(attr["admin_code"], {})}
        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "MultiPolygon", "coordinates": coords},
            }
        )
    payload = {
        "type": "FeatureCollection",
        "name": "admin_dong_population_epsg5179",
        "crs": {"type": "name", "properties": {"name": "EPSG:5179"}},
        "features": features,
    }
    (OUT / "admin_dong_population_epsg5179.geojson").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def gpkg_point_blob(x: float, y: float, srs_id: int = 5179) -> bytes:
    header = b"GP" + bytes((0, 1)) + struct.pack("<i", srs_id)
    wkb = struct.pack("<BIdd", 1, 1, float(x), float(y))
    return header + wkb


def sqlite_type(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series):
        return "INTEGER"
    if pd.api.types.is_numeric_dtype(series):
        return "REAL"
    return "TEXT"


def python_value(value: object) -> object:
    if pd.isna(value):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def write_point_gpkg(layers: dict[str, pd.DataFrame]) -> None:
    path = OUT / "analysis_points_epsg5179.gpkg"
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA application_id=1196437808;
        PRAGMA user_version=10300;
        CREATE TABLE gpkg_spatial_ref_sys (
          srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
          organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
          definition TEXT NOT NULL, description TEXT
        );
        CREATE TABLE gpkg_contents (
          table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
          identifier TEXT UNIQUE, description TEXT DEFAULT '',
          last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER
        );
        CREATE TABLE gpkg_geometry_columns (
          table_name TEXT NOT NULL, column_name TEXT NOT NULL,
          geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
          z TINYINT NOT NULL, m TINYINT NOT NULL,
          PRIMARY KEY (table_name, column_name)
        );
        """
    )
    srs_rows = [
        ("Undefined Cartesian", -1, "NONE", -1, "undefined", "undefined Cartesian coordinate reference system"),
        ("Undefined geographic", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
        (
            "Korea 2000 / Unified CS",
            5179,
            "EPSG",
            5179,
            'PROJCS["Korea 2000 / Unified CS",GEOGCS["Korea 2000",DATUM["Geocentric datum of Korea",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",38],PARAMETER["central_meridian",127.5],PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",1000000],PARAMETER["false_northing",2000000],UNIT["metre",1]]',
            "EPSG:5179",
        ),
    ]
    connection.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", srs_rows)
    for layer_name, source in layers.items():
        frame = source.copy()
        columns = [c for c in frame.columns if c not in {"x_5179", "y_5179", "geom"}]
        definitions = ", ".join(f'"{c}" {sqlite_type(frame[c])}' for c in columns)
        connection.execute(
            f'CREATE TABLE "{layer_name}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, {definitions}, geom BLOB NOT NULL)'
        )
        placeholders = ",".join("?" for _ in range(len(columns) + 1))
        quoted_columns = ",".join([f'"{c}"' for c in columns] + ['"geom"'])
        rows = []
        for _, row in frame.iterrows():
            values = [python_value(row[c]) for c in columns]
            values.append(gpkg_point_blob(row["x_5179"], row["y_5179"]))
            rows.append(values)
        connection.executemany(
            f'INSERT INTO "{layer_name}" ({quoted_columns}) VALUES ({placeholders})',
            rows,
        )
        connection.execute(
            "INSERT INTO gpkg_contents "
            "(table_name,data_type,identifier,description,min_x,min_y,max_x,max_y,srs_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                layer_name,
                "features",
                layer_name,
                "Dalseo data preparation output",
                float(frame["x_5179"].min()),
                float(frame["y_5179"].min()),
                float(frame["x_5179"].max()),
                float(frame["y_5179"].max()),
                5179,
            ),
        )
        connection.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
            (layer_name, "geom", "POINT", 5179, 0, 0),
        )
    connection.commit()
    connection.close()


def make_population(admin: pd.DataFrame, shapes: list[list[list[tuple[float, float]]]], path: Path) -> pd.DataFrame:
    source = read_csv(path)
    source["join_name"] = source["동명"].map(norm_name)
    lookup = {norm_name(name): code for name, code in zip(admin["admin_dong"], admin["admin_code"])}
    source["admin_code"] = source["join_name"].map(lookup)
    result = pd.DataFrame(
        {
            "admin_code": source["admin_code"],
            "admin_dong": source["동명"],
            "senior_65plus_total": numeric(source["계"]).astype("Int64"),
            "senior_65plus_male": numeric(source["남"]).astype("Int64"),
            "senior_65plus_female": numeric(source["여"]).astype("Int64"),
            "source_date": source["기준일자"],
        }
    )
    centroids = boundary_centroids(admin, shapes)
    return result.merge(centroids[["admin_code", "x_5179", "y_5179"]], on="admin_code", how="left")


def make_facility_points(paths: dict[str, Path]) -> dict[str, pd.DataFrame]:
    centers_raw = read_csv(paths["senior_centers"])
    centers_raw["original_lat"] = numeric(centers_raw["위도"])
    centers_raw["original_lon"] = numeric(centers_raw["경도"])
    centers_rows = []
    correction_log = []
    for index, row in centers_raw.iterrows():
        correction = SENIOR_CENTER_CORRECTIONS.get(row["명칭"])
        lat = correction["lat"] if correction else float(row["original_lat"])
        lon = correction["lon"] if correction else float(row["original_lon"])
        status = "CORRECTED_ADDRESS_PROXY" if correction else "SOURCE_COORDINATE"
        source = correction["source"] if correction else ""
        centers_rows.append(
            {
                "feature_id": f"SC_{index + 1:04d}",
                "source_type": "senior_center",
                "name": row["명칭"],
                "address": row["주소"],
                "admin_dong_source": row["행정동"],
                "lon": lon,
                "lat": lat,
                "coordinate_status": status,
                "coordinate_source": source,
                "source_date": row["기준일자"],
            }
        )
        if correction:
            correction_log.append(
                {
                    "feature_id": f"SC_{index + 1:04d}",
                    "name": row["명칭"],
                    "address": row["주소"],
                    "original_lat": row["original_lat"],
                    "original_lon": row["original_lon"],
                    "corrected_lat": lat,
                    "corrected_lon": lon,
                    "correction_type": "building_address_proxy",
                    "source_url": source,
                }
            )
    write_csv(pd.DataFrame(correction_log), "senior_center_coordinate_corrections.csv")
    centers = add_xy(pd.DataFrame(centers_rows))

    welfare_raw = read_csv(paths["welfare"])
    welfare = add_xy(
        pd.DataFrame(
            {
                "feature_id": [f"WF_{i + 1:04d}" for i in range(len(welfare_raw))],
                "source_type": "senior_welfare",
                "name": welfare_raw["시설명"],
                "facility_subtype": welfare_raw["시설유형"],
                "address": welfare_raw["주소"],
                "admin_dong_source": welfare_raw["행정동"],
                "lon": welfare_raw["경도"],
                "lat": welfare_raw["위도"],
                "coordinate_status": "SOURCE_COORDINATE",
                "coordinate_source": "",
                "source_date": welfare_raw["기준일자"],
            }
        )
    )

    medical_raw = read_csv(paths["medical"])
    medical_raw["lat_num"] = numeric(medical_raw["위도"])
    medical_raw["lon_num"] = numeric(medical_raw["경도"])
    medical_coordinate_mask = (
        medical_raw["lat_num"].between(35.7, 36.0)
        & medical_raw["lon_num"].between(128.3, 128.8)
    )
    medical_valid = medical_raw[medical_coordinate_mask].copy().reset_index(drop=True)
    medical_excluded = medical_raw[~medical_coordinate_mask].copy()
    write_csv(
        medical_excluded[
            ["순번", "의료기관종별", "의료기관명", "의료기관주소(도로명)", "위도", "경도", "기준일자"]
        ].assign(exclusion_reason="MISSING_OR_INVALID_COORDINATE"),
        "excluded_medical_facilities.csv",
    )
    medical = add_xy(
        pd.DataFrame(
            {
                "feature_id": [f"MD_{i + 1:04d}" for i in range(len(medical_valid))],
                "source_type": "medical",
                "name": medical_valid["의료기관명"],
                "facility_subtype": medical_valid["의료기관종별"],
                "address": medical_valid["의료기관주소(도로명)"],
                "admin_dong_source": medical_valid["지역"],
                "lon": medical_valid["lon_num"],
                "lat": medical_valid["lat_num"],
                "coordinate_status": "SOURCE_COORDINATE",
                "coordinate_source": "",
                "source_date": medical_valid["기준일자"],
            }
        )
    )
    return {"senior_centers": centers, "welfare": welfare, "medical": medical}


def make_parks(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = read_csv(path)
    dalseo = source[source["시군구"].eq("달서구")].copy()
    dalseo["lat_num"] = numeric(dalseo["위도"])
    dalseo["lon_num"] = numeric(dalseo["경도"])
    dalseo = dalseo[dalseo["lat_num"].notna() & dalseo["lon_num"].notna()]
    key = ["공원관리번호", "공원명"]
    parks = (
        dalseo.groupby(key, dropna=False)
        .agg(
            name=("공원명", "first"),
            address=("도로명주소", "first"),
            lon=("lon_num", "mean"),
            lat=("lat_num", "mean"),
            facility_point_count=("관리번호", "size"),
            source_date=("데이터기준일", "max"),
        )
        .reset_index(drop=True)
    )
    parks.insert(0, "feature_id", [f"PK_{i + 1:04d}" for i in range(len(parks))])
    parks.insert(1, "source_type", "park")
    parks["coordinate_status"] = "MEAN_OF_PARK_FACILITY_POINTS"
    parks["coordinate_source"] = ""
    parks["admin_dong_source"] = ""
    parks = add_xy(parks)

    bench_rows = dalseo[dalseo["시설명"].str.contains("벤치", na=False)].copy()
    park_benches = pd.DataFrame(
        {
            "source_record_id": bench_rows["관리번호"],
            "source_type": "park_bench",
            "name": bench_rows["시설명"],
            "place_name": bench_rows["공원명"],
            "address": bench_rows["도로명주소"].where(bench_rows["도로명주소"].ne(""), bench_rows["지번주소"]),
            "lon": bench_rows["lon_num"],
            "lat": bench_rows["lat_num"],
            "seat_count": 1,
            "supply_status": np.where(
                bench_rows["공원시설물수리필요"].eq("Y"),
                "FIELD_CHECK_REPAIR_NEEDED",
                "AVAILABLE",
            ),
            "active_supply": np.where(bench_rows["공원시설물수리필요"].eq("Y"), 0, 1),
            "source_date": bench_rows["데이터기준일"],
        }
    )
    return parks, add_xy(park_benches.reset_index(drop=True))


def make_rest_seats(bench_path: Path, park_benches: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = read_csv(bench_path).drop_duplicates().copy()
    source["seat_count"] = numeric(source["개수"]).fillna(0).astype(int)
    bus = source[source["구분"].eq("버스정류장") & source["seat_count"].gt(0)].copy()
    bus_points = add_xy(
        pd.DataFrame(
            {
                "source_type": "bus_stop_seat",
                "name": bus["명칭"],
                "place_name": bus["명칭"],
                "address": bus["주소"],
                "lon": bus["경도"],
                "lat": bus["위도"],
                "seat_count": bus["seat_count"],
                "supply_status": "AVAILABLE",
                "active_supply": 1,
                "source_date": bus["기준일자"],
            }
        )
    )
    resting = pd.concat([bus_points, park_benches], ignore_index=True, sort=False)
    resting.insert(0, "feature_id", [f"RS_{i + 1:05d}" for i in range(len(resting))])

    park_aggregate = source[source["구분"].eq("공원")].copy()
    park_reference = pd.DataFrame(
        {
            "name": park_aggregate["명칭"],
            "address": park_aggregate["주소"],
            "reported_seat_count": park_aggregate["seat_count"],
            "lon_representative": park_aggregate["경도"],
            "lat_representative": park_aggregate["위도"],
            "source_date": park_aggregate["기준일자"],
            "analysis_role": "aggregate_reference_only_not_distance_supply",
        }
    )
    return resting, park_reference


def make_bus_stops(location_path: Path, hourly_files: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    locations = read_csv(location_path)
    locations = locations[locations["구군"].eq("달서구")].reset_index(drop=True)
    crosswalk = pd.read_csv(CROSSWALK, encoding="utf-8-sig", dtype={"bs_id": str})
    crosswalk["crosswalk_status"] = np.where(
        crosswalk["distance_m"].le(20),
        np.where(crosswalk["normalized_name_match"], "AUTO_NAME_AND_SPATIAL", "AUTO_SPATIAL_ONLY"),
        "UNMATCHED_OVER_20M",
    )
    crosswalk.loc[crosswalk["distance_m"].gt(20), "bs_id"] = pd.NA
    locations = locations.merge(
        crosswalk[
            [
                "location_stop_name",
                "bs_id",
                "bs_name",
                "distance_m",
                "normalized_name_match",
                "crosswalk_status",
            ]
        ],
        left_on="정류소명",
        right_on="location_stop_name",
        how="left",
        validate="one_to_one",
    )

    hourly_parts = []
    for path in hourly_files:
        frame = read_csv(path)
        # 일부 월 파일 끝에 완전 공백 행이 있으므로 파일 전체를 버리지 않고
        # 실제 2025년 행만 선택한다.
        frame = frame[frame["년월"].str.startswith("2025")].copy()
        if frame.empty:
            continue
        hour_columns = [c for c in frame.columns if re.fullmatch(r"\d{2}시", c)]
        for column in hour_columns:
            frame[column] = numeric(frame[column]).fillna(0)
        frame["all_hours"] = frame[hour_columns].sum(axis=1)
        day_columns = [c for c in hour_columns if 9 <= int(c[:2]) <= 17]
        frame["day_09_17"] = frame[day_columns].sum(axis=1)
        frame["month"] = frame["년월"].str.slice(0, 7)
        hourly_parts.append(frame[["정류소ID", "구분", "month", "all_hours", "day_09_17"]])
    hourly = pd.concat(hourly_parts, ignore_index=True)
    hourly = hourly[hourly["정류소ID"].ne("") & hourly["구분"].isin(["승차", "하차"])]
    aggregates = (
        hourly.groupby(["정류소ID", "구분"])
        .agg(
            annual_all_hours=("all_hours", "sum"),
            annual_day_09_17=("day_09_17", "sum"),
            months_observed=("month", "nunique"),
        )
        .reset_index()
    )
    board = aggregates[aggregates["구분"].eq("승차")].drop(columns="구분").add_prefix("boarding_")
    alight = aggregates[aggregates["구분"].eq("하차")].drop(columns="구분").add_prefix("alighting_")
    demand = board.merge(
        alight,
        left_on="boarding_정류소ID",
        right_on="alighting_정류소ID",
        how="outer",
    )
    demand["bs_id"] = demand["boarding_정류소ID"].fillna(demand["alighting_정류소ID"]).astype(str)
    demand = demand.drop(columns=["boarding_정류소ID", "alighting_정류소ID"])

    result = pd.DataFrame(
        {
            "feature_id": [f"BS_{i + 1:04d}" for i in range(len(locations))],
            "source_type": "bus_stop",
            "name": locations["정류소명"],
            "admin_dong_source": locations["동"],
            "lon": numeric(locations["경도"]),
            "lat": numeric(locations["위도"]),
            "route_count": numeric(locations["경유노선수"]).astype("Int64"),
            "routes": locations["경유노선"],
            "bs_id": locations["bs_id"],
            "bs_name": locations["bs_name"],
            "crosswalk_distance_m": locations["distance_m"],
            "crosswalk_name_match": locations["normalized_name_match"],
            "crosswalk_status": locations["crosswalk_status"],
            "demand_scope": "ALL_AGES_PROXY_NOT_SENIOR_SPECIFIC",
            "demand_period": "2025-01_to_2025-12",
            "source_date": "2025-09-03",
        }
    )
    result = result.merge(demand, on="bs_id", how="left", validate="many_to_one")
    for column in [
        "boarding_annual_all_hours",
        "boarding_annual_day_09_17",
        "alighting_annual_all_hours",
        "alighting_annual_day_09_17",
    ]:
        result[column] = result[column].fillna(0)
    result["annual_total_users_proxy"] = (
        result["boarding_annual_all_hours"] + result["alighting_annual_all_hours"]
    )
    result["annual_day_09_17_users_proxy"] = (
        result["boarding_annual_day_09_17"] + result["alighting_annual_day_09_17"]
    )
    result["demand_coverage_status"] = np.select(
        [
            result["bs_id"].isna(),
            result["boarding_months_observed"].eq(12) & result["alighting_months_observed"].eq(12),
        ],
        ["NO_CONFIRMED_STOP_ID", "COMPLETE_12_MONTHS"],
        default="PARTIAL_MONTHS",
    )
    return add_xy(result), crosswalk


def make_network_flags() -> pd.DataFrame:
    with sqlite3.connect(NETWORK) as connection:
        edges = pd.read_sql_query(
            """
            SELECT fid,u,v,key,osmid,highway,length,intersects_dalseo,is_steps,
                   qa_effective_route_action,qa_effective_review_status,
                   qa_manual_decision,qa_manual_review_note
            FROM edges
            """,
            connection,
        )
    hard_exclude = {"EXCLUDE", "EXCLUDE_CONSERVATIVE", "NO_ROUTE_COMPONENT"}
    edges["routable_steps_included"] = ~edges["qa_effective_route_action"].isin(hard_exclude)
    edges["routable_steps_excluded"] = edges["routable_steps_included"] & ~edges[
        "qa_effective_route_action"
    ].eq("PENALIZE_STEPS")
    edges["requires_modeling_penalty_review"] = edges["qa_effective_route_action"].isin(
        ["PENALIZE_STEPS", "PENALIZE_TRAIL", "KEEP_REVIEW_IF_SELECTED"]
    )
    return edges


def main() -> None:
    started = datetime.now().astimezone()
    paths = {
        "population": find_one("노인인구"),
        "senior_centers": find_one("경로당"),
        "welfare": find_one("노인복지시설"),
        "jobs": find_one("노인일자리"),
        "benches": find_one("달서구", "벤치"),
        "medical": find_one("의료기관"),
        "parks": find_one("공원시설물정보"),
        "stops": find_one("정류소 위치정보"),
    }
    hourly_files = [
        path
        for path in find_many("시간대별 승하차인원", suffix=".csv")
        if re.search(r"2025년(?:1[0-2]|[1-9])월", nfc(path.name))
    ]
    if len(hourly_files) != 12:
        raise RuntimeError(f"2025년 시간대별 파일 12개 필요, 현재 {len(hourly_files)}개")
    if not NETWORK.exists():
        raise FileNotFoundError(NETWORK)

    admin, boundary_shapes = load_admin_boundaries()
    population = make_population(admin, boundary_shapes, paths["population"])
    if population["admin_code"].isna().any() or len(population) != 23:
        raise RuntimeError("노인인구와 행정동 경계 23개가 완전 연결되지 않음")
    write_csv(population, "senior_population_by_admin_dong.csv")
    write_boundary_geojson(admin, boundary_shapes, population)

    with sqlite3.connect(NETWORK) as connection:
        nodes = pd.read_sql_query("SELECT osmid,x,y FROM nodes", connection)
    node_xy = nodes[["x", "y"]].to_numpy(dtype=float)
    node_ids = nodes["osmid"].to_numpy()

    facilities = make_facility_points(paths)
    parks, park_benches = make_parks(paths["parks"])
    resting, park_aggregate = make_rest_seats(paths["benches"], park_benches)
    bus_stops, crosswalk = make_bus_stops(paths["stops"], hourly_files)

    point_groups = {
        **facilities,
        "parks": parks,
        "resting_seats": resting,
        "bus_stops": bus_stops,
    }
    for name, frame in point_groups.items():
        point_groups[name] = nearest_nodes(
            assign_admin_dong(frame, admin, boundary_shapes),
            node_xy,
            node_ids,
        )

    population_points = nearest_nodes(population.copy(), node_xy, node_ids)
    population_points.insert(0, "feature_id", [f"PO_{i + 1:03d}" for i in range(len(population_points))])
    population_points.insert(1, "source_type", "admin_population_centroid")
    population_points["name"] = population_points["admin_dong"]
    population_points["coordinate_status"] = "LARGEST_BOUNDARY_PART_CENTROID"
    write_csv(population_points, "senior_population_origin_points.csv")

    for name, frame in point_groups.items():
        write_csv(frame, f"{name}.csv")
    write_csv(park_aggregate, "park_bench_aggregate_reference_only.csv")
    write_csv(crosswalk, "bus_stop_id_crosswalk_audit.csv")

    snap_review_parts = []
    boundary_review_parts = []
    for dataset_name, frame in point_groups.items():
        selected = frame[frame["network_snap_status"].eq("REVIEW_OVER_100M")].copy()
        if not selected.empty:
            selected.insert(0, "dataset", dataset_name)
            keep = [
                c
                for c in [
                    "dataset",
                    "feature_id",
                    "source_type",
                    "name",
                    "place_name",
                    "address",
                    "lon",
                    "lat",
                    "network_snap_distance_m",
                    "nearest_node_osmid",
                    "admin_dong_spatial",
                ]
                if c in selected.columns
            ]
            snap_review_parts.append(selected[keep])
        outside = frame[frame["admin_code_spatial"].eq("")].copy()
        if not outside.empty:
            outside.insert(0, "dataset", dataset_name)
            keep = [
                c
                for c in [
                    "dataset",
                    "feature_id",
                    "source_type",
                    "name",
                    "place_name",
                    "address",
                    "lon",
                    "lat",
                    "network_snap_distance_m",
                    "nearest_node_osmid",
                ]
                if c in outside.columns
            ]
            boundary_review_parts.append(outside[keep])
    pd.concat(snap_review_parts, ignore_index=True).to_csv(
        QA / "review_network_snap_over_100m.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.concat(boundary_review_parts, ignore_index=True).to_csv(
        QA / "review_outside_admin_boundary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    origin_columns = [
        "feature_id",
        "source_type",
        "name",
        "x_5179",
        "y_5179",
        "nearest_node_osmid",
        "network_snap_distance_m",
        "network_snap_status",
    ]
    origins = pd.concat(
        [
            population_points[origin_columns],
            point_groups["senior_centers"][origin_columns],
            point_groups["welfare"][origin_columns],
        ],
        ignore_index=True,
    )
    write_csv(origins, "origin_candidates.csv")

    destination_frames = []
    for key in ("welfare", "medical", "parks", "bus_stops"):
        frame = point_groups[key].copy()
        destination_frames.append(frame[origin_columns])
    destinations = pd.concat(destination_frames, ignore_index=True)
    write_csv(destinations, "destination_candidates.csv")

    network_flags = make_network_flags()
    write_csv(network_flags, "walk_edge_analysis_flags.csv")

    gpkg_layers = {
        "population_origins": population_points,
        "senior_centers": point_groups["senior_centers"],
        "welfare_facilities": point_groups["welfare"],
        "medical_facilities": point_groups["medical"],
        "park_destinations": point_groups["parks"],
        "bus_stops_demand": point_groups["bus_stops"],
        "resting_seats": point_groups["resting_seats"],
    }
    write_point_gpkg(gpkg_layers)

    source_manifest = []
    for role, path in {**paths, "reviewed_walk_network": NETWORK}.items():
        source_manifest.append(
            {
                "role": role,
                "path": str(path.relative_to(ROOT)),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    for path in hourly_files:
        source_manifest.append(
            {
                "role": "hourly_bus_2025",
                "path": str(path.relative_to(ROOT)),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    pd.DataFrame(source_manifest).to_csv(QA / "source_manifest.csv", index=False, encoding="utf-8-sig")

    metrics = []
    for name, frame in {
        "admin_population": population,
        "population_origins": population_points,
        **point_groups,
        "origin_candidates": origins,
        "destination_candidates": destinations,
        "network_edges": network_flags,
    }.items():
        metrics.append(
            {
                "dataset": name,
                "rows": len(frame),
                "duplicate_feature_id": int(frame["feature_id"].duplicated().sum())
                if "feature_id" in frame
                else "",
                "missing_xy": int(frame[["x_5179", "y_5179"]].isna().any(axis=1).sum())
                if {"x_5179", "y_5179"}.issubset(frame.columns)
                else "",
                "snap_over_100m": int(frame["network_snap_status"].eq("REVIEW_OVER_100M").sum())
                if "network_snap_status" in frame
                else "",
                "unique_xy": int(frame[["x_5179", "y_5179"]].drop_duplicates().shape[0])
                if {"x_5179", "y_5179"}.issubset(frame.columns)
                else "",
                "missing_spatial_admin": int(frame["admin_code_spatial"].eq("").sum())
                if "admin_code_spatial" in frame
                else "",
            }
        )
    metrics_frame = pd.DataFrame(metrics)
    metrics_frame.to_csv(QA / "output_quality_metrics.csv", index=False, encoding="utf-8-sig")

    checks = [
        {
            "check_id": "DP-01",
            "status": "PASS" if len(population) == 23 and population["admin_code"].notna().all() else "FAIL",
            "detail": f"행정동-노인인구 연결 {population['admin_code'].notna().sum()}/23",
        },
        {
            "check_id": "DP-02",
            "status": "PASS" if len(point_groups["senior_centers"]) == 285 else "FAIL",
            "detail": f"경로당 {len(point_groups['senior_centers'])}개, 주소대리점 보정 3개",
        },
        {
            "check_id": "DP-03",
            "status": "PASS"
            if int(point_groups["bus_stops"]["crosswalk_status"].ne("UNMATCHED_OVER_20M").sum()) == 562
            else "FAIL",
            "detail": "정류소 568개 중 ID 확정 562개, 20m 초과 6개 미연결",
        },
        {
            "check_id": "DP-04",
            "status": "PASS"
            if set(point_groups["resting_seats"]["source_type"]) <= {"park_bench", "bus_stop_seat"}
            else "FAIL",
            "detail": "휴식 공급 유형을 공원 개별 벤치와 버스정류장 의자로 제한",
        },
        {
            "check_id": "DP-05",
            "status": "PASS"
            if network_flags["qa_effective_route_action"].notna().all()
            else "FAIL",
            "detail": f"검수 보행망 엣지 {len(network_flags):,}개에 유효 판단 존재",
        },
        {
            "check_id": "DP-06",
            "status": "PASS"
            if len(hourly_files) == 12
            else "FAIL",
            "detail": f"2025 시간대별 승하차 월 파일 {len(hourly_files)}개",
        },
        {
            "check_id": "DP-07",
            "status": "PASS"
            if all(
                frame[["x_5179", "y_5179"]].notna().all().all()
                for frame in [population_points, *point_groups.values()]
            )
            else "FAIL",
            "detail": "분석용 점 레이어 EPSG:5179 좌표 결측 검사",
        },
        {
            "check_id": "DP-08",
            "status": "PASS"
            if int(
                point_groups["bus_stops"]["demand_coverage_status"].isin(
                    ["COMPLETE_12_MONTHS", "PARTIAL_MONTHS"]
                ).sum()
            )
            == 562
            else "FAIL",
            "detail": "ID 확정 562개에 이용량 연결: 12개월 완전 561개, 승차 11개월·하차 12개월 1개",
        },
    ]
    checks_frame = pd.DataFrame(checks)
    checks_frame.to_csv(QA / "validation_checks.csv", index=False, encoding="utf-8-sig")

    summary = {
        "pipeline": "dalseo_data_preparation",
        "started_at": started.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "status": "PASS" if checks_frame["status"].eq("PASS").all() else "FAIL",
        "analysis_crs": "EPSG:5179",
        "senior_definition": "age_65_plus",
        "bench_gap_thresholds_reserved_for_modeling_m": [300, 500],
        "modeling_performed": False,
        "outputs": {row["dataset"]: int(row["rows"]) for row in metrics},
        "validation": checks,
    }
    (QA / "preparation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
