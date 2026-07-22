library(tidyverse)
library(duckdb)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)

# View 1 feed: every summer day with peak + label
dbGetQuery(con, "
  SELECT l.date_local, l.summer_year, l.daily_peak_mw, l.is_top5, d.hour_of_peak
  FROM mart.summer_labels l JOIN mart.daily d USING (date_local)
  WHERE l.is_complete_summer") |>
  write_csv("extracts/summer_days.csv")

# View 2 feed: backtest outcomes at the chosen operating point
bt <- read_csv("extracts/classifier_backtest_raw.csv") |>
  mutate(alarm = p_hat > 0.10,
         outcome = case_when(alarm & is_top5   ~ "caught peak",
                             !alarm & is_top5  ~ "missed peak",
                             alarm & !is_top5  ~ "false alarm",
                             TRUE              ~ "quiet"))
daily_peaks <- dbGetQuery(con, "SELECT date_local, daily_peak_mw FROM mart.daily") |> as_tibble()
bt |> left_join(daily_peaks, by = "date_local") |>
  write_csv("extracts/backtest_outcomes.csv")

bt |> count(outcome)
