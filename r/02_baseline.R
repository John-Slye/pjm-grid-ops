library(tidyverse)
library(duckdb)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)
df  <- dbGetQuery(con, "SELECT * FROM mart.hourly ORDER BY ts_utc") |> as_tibble()

# The Naive forecast
naive_src <- df |> select(ts_utc, pred_naive = demand_mw) |>
  mutate(ts_utc = ts_utc + hours(168))
df <- df |> left_join(naive_src, by = "ts_utc")

# Judge everything on 2023, one fixed, fair test year.
test <- df |> filter(year(ts_local) == 2023, !is.na(pred_naive), !is.na(demand_mw))

# MAPE
mape <- function(actual, pred) mean(abs(pred - actual) / actual, na.rm = TRUE) * 100

mape(test$demand_mw, test$pred_naive)     # the naive floor
mape(test$demand_mw, test$forecast_mw)    # PJM's professional forecast, same hours
