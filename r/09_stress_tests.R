library(tidyverse)
library(duckdb)
library(glmnet)

set.seed(42)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)
d <- dbGetQuery(con, "
  SELECT f.*, l.is_top5, l.is_complete_summer
  FROM mart.features f
  JOIN mart.summer_labels l USING (date_local)
  ORDER BY f.date_local") |> as_tibble() |>
  filter(is_complete_summer) |>
  filter(!is.na(recent_max_7d), !is.na(df_peak_mw), !is.na(temp_fc_max))

feature_cols <- c("df_vs_cutoff", "temp_fc_max", "days_left",
                  "recent_max_7d", "is_weekend")
make_X <- function(dd) as.matrix(dd |> select(all_of(feature_cols)) |>
                                   mutate(is_weekend = as.numeric(is_weekend)))
summers <- sort(unique(d$summer_year))

# One full LOSO run at tau 0.10
run_loso <- function(dd, tau = 0.10) {
  out <- map_dfr(summers, function(s) {
    train <- dd |> filter(summer_year != s)
    test  <- dd |> filter(summer_year == s)
    floor_tr <- quantile(train$df_peak_mw, 0.75, na.rm = TRUE)
    fill <- function(x) x |> mutate(s2d_top5_cutoff = coalesce(s2d_top5_cutoff, floor_tr),
                                    df_vs_cutoff = df_peak_mw - s2d_top5_cutoff)
    train <- fill(train); test <- fill(test)
    m <- cv.glmnet(make_X(train), as.numeric(train$is_top5),
                   family = "binomial", alpha = 0)
    test$p_hat <- as.numeric(predict(m, newx = make_X(test),
                                     s = "lambda.1se", type = "response"))
    test |> mutate(alarm = p_hat > tau) |>
      summarise(summer_year = s, caught = sum(alarm & is_top5), alarms = sum(alarm))
  })
  tibble(caught = sum(out$caught), med_alarms = median(out$alarms))
}

# weather-noise corruption
# Day-ahead max-temp forecasts run ~1-1.5 C RMSE; test at 0 / 1 / 1.5 / 2.
noise_grid <- c(0, 1.0, 1.5, 2.0)
n_seeds <- 20                       # 20 seeds x 3 nonzero levels x 7 folds

noise_res <- map_dfr(noise_grid, function(sd_c) {
  if (sd_c == 0) return(run_loso(d) |> mutate(sd_c = 0, seed = NA))
  map_dfr(1:n_seeds, function(sd_i) {
    set.seed(1000 + sd_i)
    d2 <- d |> mutate(temp_fc_max = temp_fc_max + rnorm(n(), 0, sd_c))
    run_loso(d2) |> mutate(sd_c = sd_c, seed = sd_i)
  })
})

noise_summary <- noise_res |> group_by(sd_c) |>
  summarise(mean_caught = mean(caught), min_caught = min(caught),
            mean_med_alarms = mean(med_alarms), .groups = "drop")
print(noise_summary)
write_csv(noise_res, "extracts/noise_stress_raw.csv")

# bootstrap-over-summers EV band
base <- read_csv("extracts/classifier_backtest_raw.csv") |>
  mutate(alarm = p_hat > 0.10) |>
  group_by(summer_year) |>
  summarise(caught = sum(alarm & is_top5), alarms = sum(alarm), .groups = "drop")

CAP_RATE    <- 329.17    # $/MW-day, 2026/27 BRA, cleared at cap (pjm.com)
CAP_LOW     <- 269.92    # 2025/26 RTO
CAP_HIGH    <- 333.44    # 2027/28 BRA
CAPTURE_EFF <- 0.85
COST_EVENT  <- 2000      # $/event/MW -- assumption, swept in Excel

ev_fn <- function(caught, alarms, rate)
  rate * 365 * (caught / 5) * CAPTURE_EFF - alarms * COST_EVENT

per_summer_ev <- base |> mutate(ev_mid = ev_fn(caught, alarms, CAP_RATE))
print(per_summer_ev, n = 7)

set.seed(42)
boot <- map_dfr(1:2000, function(i) {
  smp <- per_summer_ev |> slice_sample(n = 7, replace = TRUE)
  tibble(rep = i,
         ev_low  = mean(ev_fn(smp$caught, smp$alarms, CAP_LOW)),
         ev_mid  = mean(ev_fn(smp$caught, smp$alarms, CAP_RATE)),
         ev_high = mean(ev_fn(smp$caught, smp$alarms, CAP_HIGH)))
})
boot |> summarise(across(starts_with("ev"),
                         list(p10 = ~quantile(., .10), p50 = ~quantile(., .50),
                              p90 = ~quantile(., .90)))) |>
  pivot_longer(everything()) |> print(n = 9)
write_csv(boot, "extracts/ev_bootstrap.csv")
