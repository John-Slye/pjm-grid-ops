library(tidyverse)
library(duckdb)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)

#PJM's error map, ALL years
df <- dbGetQuery(con, "SELECT * FROM mart.hourly
                       WHERE forecast_mw IS NOT NULL AND demand_mw IS NOT NULL") |>
  as_tibble() |>
  mutate(daytype = if_else(dow_local %in% c(6, 7), "weekend", "weekday"),
         ape = abs(forecast_mw - demand_mw) / demand_mw * 100)

regimes <- df |>
  group_by(month_local, hr_local, daytype) |>
  summarise(mape = mean(ape),
            bias_mw = mean(forecast_mw - demand_mw),
            n = n(), .groups = "drop")

# The 12 worst pockets
regimes |> filter(n >= 100) |> arrange(desc(mape)) |> print(n = 12)

# Overall bias
mean(df$forecast_mw - df$demand_mw)

# The heatmap (weekdays):
regimes |> filter(daytype == "weekday") |>
  ggplot(aes(factor(month_local), factor(hr_local), fill = mape)) +
  geom_tile() + scale_fill_viridis_c(name = "MAPE %") +
  labs(title = "PJM day-ahead forecast error by month and hour (weekdays, 2019-2026)",
       x = "month", y = "local hour")
ggsave("deck/error_heatmap.png", width = 8, height = 5)

write_csv(regimes, "extracts/error_regime.csv")

# head-to-head on 2023; where does the GAM lose, win, tie?
hh <- read_csv("extracts/gam_test_2023.csv") |>
  mutate(month_local = month(ts_local), hr_local = hour(ts_local),
         ape_pjm = abs(forecast_mw - demand_mw) / demand_mw * 100,
         ape_gam = abs(pred_gam    - demand_mw) / demand_mw * 100)

head2head <- hh |>
  group_by(month_local) |>
  summarise(mape_pjm = mean(ape_pjm, na.rm = TRUE),
            mape_gam = mean(ape_gam, na.rm = TRUE),
            gam_minus_pjm = mape_gam - mape_pjm, n = n(), .groups = "drop") |>
  arrange(gam_minus_pjm)

print(head2head, n = 12)   # negative gam_minus_pjm = months where YOU win
