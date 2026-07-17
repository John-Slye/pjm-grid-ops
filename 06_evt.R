library(tidyverse)
library(duckdb)
library(extRemes)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)

# Complete summers only **2026 is in progress and excluded per DECISIONS.md
daily <- dbGetQuery(con, "
  SELECT l.date_local, l.summer_year, l.daily_peak_mw
  FROM mart.summer_labels l
  WHERE l.is_complete_summer
  ORDER BY l.date_local") |> as_tibble()

nrow(daily)                        
n_years <- n_distinct(daily$summer_year)
n_years                            

peaks <- daily$daily_peak_mw

# Choose the threshold u:
u <- as.numeric(quantile(peaks, 0.90))
u                                  
sum(peaks > u)                     

# Decluster: heat waves create runs of consecutive big days that
#         are one weather event, not independent draws. Keep each run's max.
dc <- decluster(peaks, threshold = u, r = 1)
sum(dc > u)                       

# Fit the Generalized Pareto Distribution to the exceedances
fit <- fevd(dc, threshold = u, type = "GP", time.units = "122/year")
summary(fit)

# eturn levels
return.level(fit, return.period = c(5, 10, 20), do.ci = TRUE)

# Threshold sensitivity
for (q in c(0.85, 0.90, 0.95)) {
  u2  <- as.numeric(quantile(peaks, q))
  dc2 <- decluster(peaks, threshold = u2, r = 1)
  f2  <- fevd(dc2, threshold = u2, type = "GP", time.units = "122/year")
  rl  <- return.level(f2, return.period = 10)
  cat(sprintf("q=%.2f  u=%.0f  exceed=%d  10yr RL=%.0f\n",
              q, u2, sum(dc2 > u2), as.numeric(rl)))
}

# Save parameters for the Excel model
p <- fit$results$par
tibble(u = u,
       sigma = as.numeric(p["scale"]),
       xi = as.numeric(p["shape"]),
       exceed_per_summer = sum(dc > u) / n_years) |>
  write_csv("extracts/gpd_params.csv")

# The tail plot
plot(fit, type = "density", main = "GPD fit to summer daily-peak exceedances")
