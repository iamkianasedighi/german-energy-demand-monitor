-- Monthly market-price summary. Missing prices do not count as negative or zero.
SELECT month,
       COUNT(price_eur_mwh) AS priced_hours,
       SUM(CASE WHEN price_eur_mwh < 0 THEN 1 ELSE 0 END) AS negative_price_hours,
       ROUND(AVG(price_eur_mwh), 2) AS mean_price_eur_mwh
FROM hourly_metrics
GROUP BY month
ORDER BY month;

-- Mismatches between derived and published residual load, for review.
SELECT local_date, local_hour,
       ROUND(residual_calc_mwh / 1000.0, 2) AS calculated_residual_gw,
       ROUND(residual_official / 1000.0, 2) AS published_residual_gw,
       ROUND(residual_gap_mwh, 2) AS difference_mwh
FROM hourly_metrics
WHERE grid_load > 0
  AND residual_gap_mwh IS NOT NULL
  AND ABS(residual_gap_mwh) > 0.02 * grid_load
ORDER BY ABS(residual_gap_mwh) DESC;
