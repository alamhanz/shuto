"""Offline reverse geocoding: GPS coordinates -> nearest city, region and country.

Uses a bundled GeoNames extract (src/shuto/data/places.tsv.gz). Place names from GeoNames
(geonames.org), CC BY 4.0. Nothing is sent anywhere: coordinates never leave this computer.
Rebuild the data with scripts/build_geonames.py.
"""

from __future__ import annotations

import gzip
import math
import threading
from dataclasses import asdict, dataclass
from importlib import resources

CITY_MAX_KM = 50.0  # farther than this: region and country only
REGION_MAX_KM = 200.0  # farther than this: nothing (open sea, desert, poles)
_EARTH_KM = 6371.0088
_CELL = 1.0  # grid cell size in degrees
_TOWN_SLACK_KM = 3.0

_lock = threading.Lock()
_index: _Index | None = None


@dataclass(frozen=True)
class Place:
    city: str | None
    region: str | None
    country: str | None
    distance_km: float
    confidence: str  # "high" (<= 10 km), "medium" (<= 50 km), "low" (region/country only)

    def to_dict(self) -> dict:
        return asdict(self)


class _Index:
    def __init__(self, text: str) -> None:
        self.countries: dict[str, str] = {}
        self.regions: dict[str, str] = {}
        self.cells: dict[tuple[int, int], list[tuple[float, float, str, str, str, int]]] = {}
        for line in text.splitlines():
            parts = line.split("\t")
            kind = parts[0]
            if kind == "P" and len(parts) >= 7:
                name, cc, adm = parts[1], parts[2], parts[3]
                lat, lon = float(parts[4]), float(parts[5])
                pop = int(parts[6] or 0)
                cell = self.cells.setdefault(self._cell(lat, lon), [])
                cell.append((lat, lon, name, cc, adm, pop))
            elif kind == "C" and len(parts) >= 3:
                self.countries[parts[1]] = parts[2]
            elif kind == "A" and len(parts) >= 3:
                self.regions[parts[1]] = parts[2]

    @staticmethod
    def _cell(lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / _CELL), math.floor(lon / _CELL)

    def best_near(self, lat: float, lon: float, nearest):
        """Most notable place within _TOWN_SLACK_KM of the nearest one (ties: the nearest)."""
        limit = nearest[0] + _TOWN_SLACK_KM
        if limit > CITY_MAX_KM:
            return nearest
        best, best_key = nearest, None
        row0, col0 = self._cell(lat, lon)
        span = int(limit / (_CELL * 111.19 * max(math.cos(math.radians(lat)), 0.05))) + 1
        col_count = int(360 / _CELL)
        dlat = limit / 111.19  # cheap bounding box before the exact distance
        dlon = dlat / max(math.cos(math.radians(lat)), 0.05)
        for row in range(row0 - span, row0 + span + 1):
            for col in range(col0 - span, col0 + span + 1):
                wrapped = (col + col_count // 2) % col_count - col_count // 2
                for city in self.cells.get((row, wrapped), ()):
                    if abs(city[0] - lat) > dlat or abs((city[1] - lon + 180) % 360 - 180) > dlon:
                        continue
                    d = _haversine(lat, lon, city[0], city[1])
                    if d > limit:
                        continue
                    key = (city[5] / (1 + d) ** 2, -d)
                    if best_key is None or key > best_key:
                        best, best_key = (d, city), key
        return best

    def nearest(self, lat: float, lon: float, max_km: float):
        """Nearest (distance_km, city tuple) within max_km, searching grid rings outwards."""
        row0, col0 = self._cell(lat, lon)
        cols_per_deg_km = max(math.cos(math.radians(lat)), 0.05) * 111.19
        cell_km = min(_CELL * 111.19, _CELL * cols_per_deg_km)  # safe lower bound per ring
        max_ring = int(max_km / cell_km) + 1
        col_count = int(360 / _CELL)
        best: tuple[float, tuple[float, float, str, str, str, int]] | None = None
        for ring in range(max_ring + 1):
            if best is not None and best[0] <= (ring - 1) * cell_km:
                break
            for row in range(row0 - ring, row0 + ring + 1):
                if row < -90 or row > 90:
                    continue
                edge_row = abs(row - row0) == ring
                step = 1 if edge_row else max(2 * ring, 1)
                for col in range(col0 - ring, col0 + ring + 1, step):
                    wrapped = (col + col_count // 2) % col_count - col_count // 2
                    for city in self.cells.get((row, wrapped), ()):
                        if best is not None and abs(city[0] - lat) * 111.19 > best[0]:
                            continue
                        d = _haversine(lat, lon, city[0], city[1])
                        if best is None or d < best[0]:
                            best = (d, city)
        if best is not None and best[0] <= max_km:
            return best
        return None


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def data_file():
    return resources.files("shuto").joinpath("data/places.tsv.gz")


def _load() -> _Index:
    global _index
    with _lock:
        if _index is None:
            with data_file().open("rb") as fh:
                _index = _Index(gzip.decompress(fh.read()).decode("utf-8"))
        return _index


def reverse_geocode(lat: float, lon: float) -> Place | None:
    """Nearest place for a coordinate, or None when invalid or nothing is within 200 km.

    Within 50 km: city, region and country. Between 50 and 200 km: region and country of the
    nearest city, with low confidence. Never raises on bad input.
    """
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or math.isnan(lat + lon):
        return None
    try:
        idx = _load()
    except Exception:
        return None
    hit = idx.nearest(lat, lon, REGION_MAX_KM)
    if hit is None:
        return None
    # GeoNames lists many tiny neighbourhoods (Tokyo has dozens). Among places about as close as
    # the nearest one, prefer the notable one: score = population / (1 + distance)^2.
    hit = idx.best_near(lat, lon, hit)
    dist, (_, _, name, cc, adm, _) = hit
    region = idx.regions.get(f"{cc}.{adm}") if adm else None
    country = idx.countries.get(cc, cc)
    if dist <= CITY_MAX_KM:
        return Place(name, region, country, round(dist, 1), "high" if dist <= 10 else "medium")
    return Place(None, region, country, round(dist, 1), "low")
