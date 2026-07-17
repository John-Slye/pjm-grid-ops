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
