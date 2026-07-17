
library(tidyverse)
library(forecast)

hh <- read_csv("extracts/gam_test_2023.csv") |>
  filter(!is.na(forecast_mw), !is.na(demand_mw)) |>   
  mutate(month_local = month(ts_local),
         e_gam = demand_mw - pred_gam,
         e_pjm = demand_mw - forecast_mw)

# Overall verdict, full 2023
dm.test(hh$e_gam, hh$e_pjm, h = 24, power = 2, alternative = "two.sided")

# July + August only 
summer <- hh |> filter(month_local %in% c(7, 8))
dm.test(summer$e_gam, summer$e_pjm, h = 24, power = 2, alternative = "two.sided")

#Contrast: the transition months where PJM dominates
spring <- hh |> filter(month_local %in% c(4, 5))
dm.test(spring$e_gam, spring$e_pjm, h = 24, power = 2, alternative = "two.sided")

#Robustness: absolute-error loss instead of squared
dm.test(summer$e_gam, summer$e_pjm, h = 24, power = 1, alternative = "two.sided")
