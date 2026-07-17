import duckdb
con = duckdb.connect("data/pjm_grid_ops.duckdb", read_only=True)
print(con.execute("""
    SELECT f.date_local, ROUND(f.df_peak_mw) AS df_peak,
           ROUND(f.s2d_top5_cutoff) AS cutoff, ROUND(d.daily_peak_mw) AS actual_peak
    FROM mart.features f JOIN mart.daily d USING (date_local)
    WHERE f.summer_year = 2022 AND f.date_local BETWEEN '2022-07-15' AND '2022-08-05'
    ORDER BY f.date_local
""").df().to_string())
con.close()