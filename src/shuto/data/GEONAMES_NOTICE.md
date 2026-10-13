# Place data (GeoNames)

`places.tsv.gz` is derived from the GeoNames `cities1000` extract, country list and admin1
codes, used for offline reverse geocoding (GPS -> city, region, country).

- Source: <https://www.geonames.org> (GeoNames geographical database)
- License: Creative Commons Attribution 4.0 (CC BY 4.0), <https://creativecommons.org/licenses/by/4.0/>
- Changes: reduced to name, country, region, coordinates and population; rebuilt with
  `scripts/build_geonames.py`.

Coordinates from your photos are looked up locally and never sent anywhere.
