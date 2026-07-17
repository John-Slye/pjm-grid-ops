library(tidyverse)
library(duckdb)
library(mgcv)

con <- dbConnect(duckdb(), "data/pjm_grid_ops.duckdb", read_only = TRUE)
df  <- dbGetQuery(con, "SELECT * FROM mart.hourly ORDER BY ts_utc") |> as_tibble() |>
  mutate(doy = yday(ts_local),          
         dow = factor(dow_local)) |>   
  filter(!is.na(demand_mw), !is.na(temp_composite_c))

train <- df |> filter(year(ts_local) <= 2022)   # learn from 2019-2022
test  <- df |> filter(year(ts_local) == 2023)   # judged on 2023, same as baseline

nrow(train)
nrow(test)    


m <- bam(demand_mw ~
           s(temp_composite_c, k = 20) +      # the temperature U, learned
           s(hr_local, bs = "cc", k = 24) +   # daily rhythm; "cc" joins hour 23 to hour 0
           s(doy, bs = "cc", k = 30) +        # yearly cycle; Dec 31 joins Jan 1
           dow +                              # one fixed shift per weekday
           ti(temp_composite_c, hr_local, bs = c("tp", "cc")),   # heat effect varies by hour; hour stays cyclic
         data = train, discrete = TRUE,
         knots = list(hr_local = c(0, 24), doy = c(1, 366)))

summary(m)

# what it learned
plot(m, pages = 1, scheme = 1)

# Score it on 2023
test$pred_gam <- as.numeric(predict(m, newdata = test))
mape <- function(actual, pred) mean(abs(pred - actual) / actual, na.rm = TRUE) * 100
mape(test$demand_mw, test$pred_gam)

# Export test-year predictions for downstream scripts
dir.create("extracts", showWarnings = FALSE)
test |> select(ts_local, demand_mw, forecast_mw, pred_gam) |>
  write_csv("extracts/gam_test_2023.csv")