# City seeds

`worldcities.csv` contains 7,531 US/Canada seed rows retained from the existing
workspace. Columns: `country` (two-letter code), `name`, `lat`, `lng` (degrees).
The repository does not record the dataset's original external provenance.

The GeoNames filter writes `worldcities.filtered.csv` plus an audit. Filtered
lists, shortlists and audit CSVs are local/generated and ignored; only the seed
input belongs in Git. The unused legacy `top_cities.csv` used incompatible
country names and a `long` column and has been removed.
