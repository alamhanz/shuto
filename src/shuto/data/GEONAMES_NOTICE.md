# Place data (GeoNames)

`places.tsv.gz` is derived from the GeoNames `cities1000` extract plus `countryInfo.txt`,
`admin1CodesASCII.txt` and `admin2Codes.txt`, used for offline reverse geocoding
(GPS -> neighborhood, ward/city, prefecture/region, country).

- Source: <https://www.geonames.org> (GeoNames geographical database)
- License: Creative Commons Attribution 4.0 (CC BY 4.0), <https://creativecommons.org/licenses/by/4.0/>
- Changes: reduced to name, country, admin1/admin2 codes and names (only those in use),
  coordinates and population. For Japan, ward/city names have the "-ku"/"-shi" suffix removed
  ("Shibuya-ku" -> "Shibuya").
- Rebuild: `python scripts/build_geonames.py` (needs internet; dev time only).
  Records: `C` country, `A` admin1, `A2` admin2, `P` place (name, country, admin1, lat, lon,
  population, admin2).

Coordinates from your photos are looked up locally and never sent anywhere.
