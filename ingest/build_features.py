import duckdb
import pandas as pd

con = duckdb.connect("data/pjm_grid_ops.duckdb")

daily = con.execute("""
    SELECT date_local, summer_year, daily_peak_mw, df_peak_mw,
           temp_max_c AS temp_fc_max, dow_local
    FROM mart.daily
    WHERE month_local BETWEEN 6 AND 9
    ORDER BY date_local
""").df()

rows = []
for yr, g in daily.groupby("summer_year"):
    g = g.sort_values("date_local").reset_index(drop=True)
    season_end = g["date_local"].max()
    peaks_so_far = []                       # peaks observed BEFORE today
    for _, r in g.iterrows():
        top5 = sorted(peaks_so_far, reverse=True)[:5]
        cutoff = top5[-1] if len(top5) == 5 else None   # bar undefined until 5 days exist
        rows.append({
            "date_local":      r["date_local"],
            "summer_year":     yr,
            "df_peak_mw":      r["df_peak_mw"],       # PJM's forecast peak: knowable
            "temp_fc_max":     r["temp_fc_max"],      # actual temp as forecast proxy
            "is_weekend":      r["dow_local"] in (6, 7),   # ISO convention
            "days_left":       (season_end - r["date_local"]).days,
            "s2d_top5_cutoff": cutoff,                # 5th-best peak THROUGH YESTERDAY
            "df_vs_cutoff":    (r["df_peak_mw"] - cutoff) if cutoff is not None else None,
            "recent_max_7d":   max(peaks_so_far[-7:]) if peaks_so_far else None,
        })
        peaks_so_far.append(r["daily_peak_mw"])   # appended only AFTER the row is built
        # ^ this ordering IS the leakage protection: today's own peak can never
        #   influence today's features.

feat = pd.DataFrame(rows)
con.execute("CREATE OR REPLACE TABLE mart.features AS SELECT * FROM feat")

print(con.execute("SELECT COUNT(*), MIN(date_local), MAX(date_local) FROM mart.features").fetchall())
print(con.execute("SELECT summer_year, COUNT(*) FROM mart.features GROUP BY 1 ORDER BY 1").fetchall())
con.close()