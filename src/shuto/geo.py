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
_MIN_POP = 1000
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

    def nearest(self, lat: float, lon: float, max_km: float, min_pop: int = 0):
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
                        if city[5] < min_pop:
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
    # GeoNames lists many zero-population neighbourhoods (Tokyo has dozens). Prefer a real place
    # (population >= 1000) when it is about as close, so Shibuya wins over a nearby block name.
    town = idx.nearest(lat, lon, REGION_MAX_KM, min_pop=_MIN_POP)
    if town is not None and town[0] <= max(hit[0] + _TOWN_SLACK_KM, hit[0] * 2):
        hit = town
    dist, (_, _, name, cc, adm, _) = hit
    region = idx.regions.get(f"{cc}.{adm}") if adm else None
    country = idx.countries.get(cc, cc)
    if dist <= CITY_MAX_KM:
        return Place(name, region, country, round(dist, 1), "high" if dist <= 10 else "medium")
    return Place(None, region, country, round(dist, 1), "low")
