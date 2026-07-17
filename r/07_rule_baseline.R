library(tidyverse)
library(duckdb)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)

d <- dbGetQuery(con, "
  SELECT f.*, l.is_top5, l.is_complete_summer
  FROM mart.features f
  JOIN mart.summer_labels l USING (date_local)
  ORDER BY f.date_local") |> as_tibble() |>
  filter(is_complete_summer)          

nrow(d)                                
sum(d$is_top5)                         


FLOOR <- quantile(d$df_peak_mw, 0.75, na.rm = TRUE)
FLOOR                                 

score_rule <- function(d, buffer_mw) {
  d |>
    mutate(bar   = if_else(is.na(s2d_top5_cutoff),
                           FLOOR,
                           pmax(s2d_top5_cutoff, FLOOR)),
           alarm = !is_weekend & !is.na(df_peak_mw) &
             df_peak_mw >= bar - buffer_mw) |>
    group_by(summer_year) |>
    summarise(caught = sum(alarm & is_top5),
              alarms = sum(alarm), .groups = "drop")
}

# Sweep buffer; watch the trade-off

for (b in c(0, 1000, 2000, 3000, 5000, 8000)) {
  res <- score_rule(d, b)
  cat(sprintf("\nbuffer = %5d MW | total caught %2d/35 | median alarms/summer %4.1f\n",
              b, sum(res$caught), median(res$alarms)))
  print(res, n = 7)
}


# ---- Miss diagnosis at v1 operating points (found the weekend exclusion bug) ----
res_detail <- d |>
  mutate(bar   = if_else(is.na(s2d_top5_cutoff), FLOOR, pmax(s2d_top5_cutoff, FLOOR)),
         alarm0 = !is_weekend & !is.na(df_peak_mw) & df_peak_mw >= bar,
         alarm1 = !is_weekend & !is.na(df_peak_mw) & df_peak_mw >= bar - 1000)
res_detail |> filter(is_top5, !alarm1) |>
  select(date_local, is_weekend, df_peak_mw, s2d_top5_cutoff, bar) |> print()
# 2019-07-20: Saturday peak, forecast 7 GW clear of bar -- missed only by
# the !is_weekend exclusion. See DECISIONS.md "Rule-based baseline".

# ---- v2: weekends eligible; the bar itself filters them ----
score_rule_v2 <- function(d, buffer_mw) {
  d |>
    mutate(bar   = if_else(is.na(s2d_top5_cutoff), FLOOR, pmax(s2d_top5_cutoff, FLOOR)),
           alarm = !is.na(df_peak_mw) & df_peak_mw >= bar - buffer_mw) |>
    group_by(summer_year) |>
    summarise(caught = sum(alarm & is_top5), alarms = sum(alarm), .groups = "drop")
}
for (b in c(0, 1000)) {
  res <- score_rule_v2(d, b)
  cat(sprintf("\nv2 buffer = %d MW | caught %d/35 | median alarms %.1f\n",
              b, sum(res$caught), median(res$alarms)))
  print(res, n = 7)
}

# ---- CHOSEN OPERATING POINT: v2, buffer = 1000 MW ----
# 35/35 caught, median 17 alarms/summer, worst 21 (2020). Weekend eligibility
# cost 9 alarms across seven summers. Classifier must beat this on alarm
# efficiency; rationale in DECISIONS.md.
final <- score_rule_v2(d, 1000)
write_csv(final, "extracts/rule_baseline_by_summer.csv")
