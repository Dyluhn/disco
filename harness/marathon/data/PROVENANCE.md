# PROVENANCE

- **Source URL**: https://raw.githubusercontent.com/plotly/datasets/master/2016-weather-data-seattle.csv
- **Dataset name**: Seattle Weather 2016 (temperature dataset)
- **Download date**: 2026-06-09
- **Reduction rule**: First 200 rows (including header) taken verbatim.
- **Validation output**:
```
Row count: 200
Max_TemperatureC: min=1.0, max=30.0
Mean_TemperatureC: min=-1.0, max=23.0
Min_TemperatureC: min=-6.0, max=16.0
SHA256: f87e795b97716b38a173ff443657eb68c0f16bdb9c141405a31de5d982f94d28
```

- **Supervisor verification (2026-06-09)**: file diffed byte-identical against
  `head -201` of the source URL (VERBATIM-MATCH); row count / min-max / no-empty-cells
  re-validated independently; the SHA256 above is the corrected, recomputed value
  (the worker's original receipt `1591d553…` did not match any artifact and was replaced).
  Note: despite the upstream filename, the dataset's rows begin 1/1/1948 (plotly's
  Seattle historical daily temperatures).
