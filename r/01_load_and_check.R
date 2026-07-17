
library(tidyverse)
library(duckdb)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)
df  <- dbGetQuery(con, "SELECT * FROM mart.hourly ORDER BY ts_utc") |> as_tibble()

# Check 1: size and sanity
nrow(df)
summary(df$demand_mw)

# Check 2: does demand peak in the late afternoon?
df |>  group_by(hr_local) |>
  summarise(avg_mw = mean(demand_mw, na.rm = TRUE)) |>
  ggplot(aes(hr_local, avg_mw)) + geom_line() +
  labs(title = "Average demand by local hour")

# Check 3: the temperature U-shape
df |> filter(!is.na(temp_composite_c)) |> slice_sample(n = 20000) |>
  ggplot(aes(temp_composite_c, demand_mw)) + geom_point(alpha = 0.1) +
  labs(title = "Demand vs temperature")
