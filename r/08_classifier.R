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

nrow(d)            
sum(d$is_top5)     

feature_cols <- c("df_vs_cutoff", "temp_fc_max", "days_left",
                  "recent_max_7d", "is_weekend")

make_X <- function(dd) {
  as.matrix(dd |> select(all_of(feature_cols)) |>
              mutate(is_weekend = as.numeric(is_weekend)))
}

#Leave-one-summer-out backtest
summers <- sort(unique(d$summer_year))
bt <- list()

for (s in summers) {
  train <- d |> filter(summer_year != s)
  test  <- d |> filter(summer_year == s)
  
  # Fold-local early-season floor: TRAINING summers only 
  floor_tr <- quantile(train$df_peak_mw, 0.75, na.rm = TRUE)
  fill <- function(dd) dd |>
    mutate(s2d_top5_cutoff = coalesce(s2d_top5_cutoff, floor_tr),
           df_vs_cutoff    = df_peak_mw - s2d_top5_cutoff)
  train <- fill(train); test <- fill(test)
  
  m <- cv.glmnet(make_X(train), as.numeric(train$is_top5),
                 family = "binomial", alpha = 0)          # ridge
  test$p_hat <- as.numeric(predict(m, newx = make_X(test),
                                   s = "lambda.1se", type = "response"))
  bt[[as.character(s)]] <- test
  cat("finished summer", s, "\n")
}
bt <- bind_rows(bt)

summary(bt$p_hat)                       
bt |> group_by(is_top5) |>              
  summarise(mean_p = mean(p_hat), max_p = max(p_hat))

#caught vs alarms across thresholds
frontier <- map_dfr(seq(0.02, 0.60, by = 0.02), function(tau) {
  per <- bt |> mutate(alarm = p_hat > tau) |>
    group_by(summer_year) |>
    summarise(caught = sum(alarm & is_top5), alarms = sum(alarm), .groups = "drop")
  tibble(tau = tau, total_caught = sum(per$caught),
         med_alarms = median(per$alarms), max_alarms = max(per$alarms))
})
print(frontier, n = 30)

# Rule's operating point for comparison: 35/35 caught, median 17, worst 21

frontier |> filter(total_caught == 35) |> slice_min(med_alarms, n = 3)
frontier |> filter(med_alarms <= 17) |> slice_max(total_caught, n = 3)

write_csv(bt |> select(date_local, summer_year, p_hat, is_top5),
          "extracts/classifier_backtest_raw.csv")

# Nested threshold selection
sel <- map_dfr(summers, function(s) {
  inner <- bt |> filter(summer_year != s)
  cand <- map_dfr(seq(0.02, 0.60, by = 0.02), function(tau) {
    per <- inner |> mutate(alarm = p_hat > tau) |>
      group_by(summer_year) |>
      summarise(caught = sum(alarm & is_top5), alarms = sum(alarm), .groups = "drop")
    tibble(tau = tau, caught = sum(per$caught), med = median(per$alarms))
  })
  best <- cand |> filter(caught == max(caught)) |> slice_min(med, n = 1) |> slice_head(n = 1)
  test <- bt |> filter(summer_year == s) |> mutate(alarm = p_hat > best$tau)
  tibble(summer_year = s, tau_star = best$tau,
         caught = sum(test$alarm & test$is_top5), alarms = sum(test$alarm))
})
print(sel, n = 7)
cat(sprintf("NESTED VERDICT: %d/35 | median %.1f | worst %d  (rule: 35/35, 17, 21)\n",
            sum(sel$caught), median(sel$alarms), max(sel$alarms)))
write_csv(sel, "extracts/classifier_nested_by_summer.csv")

