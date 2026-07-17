library(tidyverse)
library(duckdb)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)
df  <- dbGetQuery(con, "SELECT * FROM mart.hourly ORDER BY ts_utc") |> as_tibble()

# The Naive forecast
# lag(x, 168) shifts the column down 168 rows = 168 hours = exactly one week.
df <- df |> mutate(pred_naive = lag(demand_mw, 168))

# Judge everything on 2023, one fixed, fair test year.
test <- df |> filter(year(ts_local) == 2023, !is.na(pred_naive), !is.na(demand_mw))

# MAPE
mape <- function(actual, pred) mean(abs(pred - actual) / actual, na.rm = TRUE) * 100

mape(test$demand_mw, test$pred_naive)     # the naive floor
mape(test$demand_mw, test$forecast_mw)    # PJM's professional forecast, same hours
