"""Rebuild src/shuto/data/places.tsv.gz from GeoNames dumps (dev-time only, needs internet).

Source: https://download.geonames.org/export/dump/ (CC BY 4.0, https://www.geonames.org).
Usage: python scripts/build_geonames.py [--dump cities1000|cities5000] [--out PATH]
The output is a gzipped TSV with three record kinds:
  C <cc> <country name>
  A <cc>.<admin1 code> <region name>
  A2 <cc>.<admin1 code>.<admin2 code> <district/ward/county name>
  P <ascii name> <cc> <admin1 code> <lat> <lon> <population> <admin2 code>
Only countries and admin codes that occur in the chosen dump are kept, to stay small.
"""

from __future__ import annotations

import argparse
import gzip
import io
import re
import tempfile
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://download.geonames.org/export/dump/"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src" / "shuto" / "data" / "places.tsv.gz"


def _fetch(name: str) -> bytes:
    with urllib.request.urlopen(BASE + name, timeout=120) as resp:
        return resp.read()


_JP_SUFFIX = re.compile(r"[- ](ku|shi|gun|machi|cho|mura|shicho|chosho)$", re.IGNORECASE)


def _district_name(code: str, name: str) -> str:
    """Japan: "Shibuya-ku" -> "Shibuya", "Tama Shi" -> "Tama" (the plain ward/city name)."""
    if code.startswith("JP."):
        name = _JP_SUFFIX.sub("", name.strip()) or name
    return name


def build(dump: str) -> tuple[bytes, int]:
    countries: dict[str, str] = {}
    for line in _fetch("countryInfo.txt").decode("utf-8").splitlines():
        if line and not line.startswith("#"):
            cols = line.split("\t")
            countries[cols[0]] = cols[4]  # ISO code, country name
    admin1: dict[str, str] = {}
    for line in _fetch("admin1CodesASCII.txt").decode("utf-8").splitlines():
        code, _, ascii_name = line.split("\t")[:3]
        admin1[code] = ascii_name  # "JP.40" -> "Tokyo"
    admin2: dict[str, str] = {}
    for line in _fetch("admin2Codes.txt").decode("utf-8").splitlines():
        cols = line.split("	")
        if len(cols) >= 3:
            admin2[cols[0]] = cols[2]  # "JP.40.13113" -> "Shibuya-ku"
    with zipfile.ZipFile(io.BytesIO(_fetch(f"{dump}.zip"))) as zf:
        rows = zf.read(f"{dump}.txt").decode("utf-8").splitlines()
    cities: list[str] = []
    used_cc: set[str] = set()
    used_admin: set[str] = set()
    used_admin2: set[str] = set()
    for row in rows:
        c = row.split("\t")
        name, cc, adm, pop = c[2].strip(), c[8], c[10], c[14]  # asciiname, country, admin1, pop
        if not name or not cc:
            continue
        used_cc.add(cc)
        if adm and f"{cc}.{adm}" in admin1:
            used_admin.add(f"{cc}.{adm}")
        else:
            adm = ""
        adm2 = c[11]
        if adm and adm2 and f"{cc}.{adm}.{adm2}" in admin2:
            used_admin2.add(f"{cc}.{adm}.{adm2}")
        else:
            adm2 = ""
        lat, lon = float(c[4]), float(c[5])
        cities.append("\t".join(["P", name, cc, adm, f"{lat:.3f}", f"{lon:.3f}", pop, adm2]))
    out = [f"C\t{cc}\t{countries.get(cc, cc)}" for cc in sorted(used_cc)]
    out += [f"A\t{code}\t{admin1[code]}" for code in sorted(used_admin)]
    out += [f"A2	{code}	{_district_name(code, admin2[code])}" for code in sorted(used_admin2)]
    out += cities
    return ("\n".join(out) + "\n").encode("utf-8"), len(cities)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump", default="cities1000", choices=["cities1000", "cities5000"])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    raw, count = build(args.dump)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=args.out.parent, delete=False) as tmp:
        with gzip.GzipFile(fileobj=tmp, mode="wb", mtime=0, compresslevel=9) as gz:
            gz.write(raw)
    Path(tmp.name).replace(args.out)
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e6:.2f} MB, {count} places)")


if __name__ == "__main__":
    main()
