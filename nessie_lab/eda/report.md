# Back of House: EDA (SIMULATED data)

Bank window 2025-10-25 to 2026-10-24. Backtest trains through 2026-07-24.


## 1. Integrity checks

| check                                  | pass   |
|:---------------------------------------|:-------|
| invoice total = sum of lines           | True   |
| invoice line = qty x unit price (+-1c) | True   |
| ticket gross = sum of lines            | True   |
| subtotal = gross - discount            | True   |
| bank dates inside window               | True   |
| bank amounts positive integers         | True   |
| bank ids unique                        | True   |
| every bank row categorized             | True   |
| no payroll dated after window in books | True   |
| no grader columns in bank feed         | True   |
| no negative daily balance              | True   |

Lowest daily balance by account ($): {'CHS': 99716, 'RES': 25000}


## 2. Revenue and operations

![](revenue_monthly.png)

| location   |   avg_daily_rev |   covers |   delivery_share |   rev_per_labor_hr |   covers_per_seat |
|:-----------|----------------:|---------:|-----------------:|-------------------:|------------------:|
| CHS        |         4777.76 |   111.69 |             0.14 |               79.9 |              1.86 |

Dine-in check per cover ($):

| location   |   dinner |   lunch |
|:-----------|---------:|--------:|
| CHS        |    47.87 |   31.93 |

Average revenue by weekday ($):

| dow   |   CHS |
|:------|------:|
| Mon   |  3324 |
| Tue   |  3544 |
| Wed   |  4006 |
| Thu   |  4590 |
| Fri   |  6175 |
| Sat   |  6567 |
| Sun   |  5196 |

Labor hours vs covers correlation: {'CHS': 0.91} (staff are scheduled from a forecast, then cut or called in).


## 3. Monthly P&L (accrual) vs NRA benchmarks

| month   |   revenue_$ |   cogs_% |   labor_excl_tips_% |   occupancy_% |   net_cash_$ |
|:--------|------------:|---------:|--------------------:|--------------:|-------------:|
| 2025-10 |     35428.2 |    0.288 |               0.843 |         0.265 |    -13655.1  |
| 2025-11 |    141165   |    0.302 |               0.416 |         0.067 |    -13600.9  |
| 2025-12 |    153032   |    0.304 |               0.376 |         0.061 |     29137.9  |
| 2026-01 |    124113   |    0.301 |               0.455 |         0.076 |    -23236.4  |
| 2026-02 |    121577   |    0.308 |               0.459 |         0.077 |     -9282.68 |
| 2026-03 |    143720   |    0.325 |               0.393 |         0.065 |      6798.66 |
| 2026-04 |    141714   |    0.327 |               0.409 |         0.068 |     -6109.25 |
| 2026-05 |    159478   |    0.325 |               0.546 |         0.061 |    -38555    |
| 2026-06 |    153234   |    0.323 |               0.388 |         0.063 |     11627    |
| 2026-07 |    148307   |    0.324 |               0.406 |         0.065 |     -9627.17 |
| 2026-08 |    140331   |    0.324 |               0.421 |         0.069 |     -2743.7  |
| 2026-09 |    144475   |    0.33  |               0.405 |         0.067 |      2490.26 |
| 2026-10 |    127756   |    0.331 |               0.472 |         0.076 |    -15612    |

Full months only:

| metric                          |   ours |   NRA median (full-service) |
|:--------------------------------|-------:|----------------------------:|
| food & bev cost (COGS)          |  0.318 |                       0.32  |
| payroll + benefits (excl. tips) |  0.425 |                       0.365 |
| occupancy                       |  0.067 |                       0.06  |
| net cash margin                 | -0.047 |                       0.028 |

Labor is accrued on pay date; May 2026 has three paydays, so its labor % is a cash-timing spike, not a rate change. First and last months are partial.


## 4. Menu economics

![](menu_engineering.png)

| item                      | course   |   avg_price 2025-11 |   avg_price 2026-10 |   cm_per_plate 2025-11 |   cm_per_plate 2026-10 |   plate_cost 2025-11 |   plate_cost 2026-10 |
|:--------------------------|:---------|--------------------:|--------------------:|-----------------------:|-----------------------:|---------------------:|---------------------:|
| Arancini                  | starter  |               14.31 |               14.2  |                   9.48 |                   9.25 |                 4.1  |                 4.33 |
| Bolognese                 | main     |               25.63 |               25.89 |                  16.03 |                  16.07 |                 8.19 |                 8.56 |
| Bottle of Wine            | bottle   |               56.66 |               56.88 |                  32.2  |                  32.8  |                22.74 |                22.35 |
| Braised Short Rib         | main     |               37.55 |               37.85 |                  18.2  |                  17.98 |                17.26 |                17.77 |
| Burrata & Heirloom Tomato | starter  |               17.35 |               17.36 |                   9.85 |                   9.63 |                 6.64 |                 6.87 |
| Cacio e Pepe              | main     |               22.66 |               22.19 |                  15.59 |                  15.24 |                 5.74 |                 5.92 |
| Caesar Salad              | starter  |               14.37 |               15.23 |                   9.47 |                  10.11 |                 4.11 |                 4.42 |
| Chicken Parmesan          | main     |               27.29 |               28.1  |                  17.29 |                  17.68 |                 8.47 |                 8.94 |
| Cocktail                  | drink    |               15    |               15    |                  12.42 |                  12.19 |                 2.12 |                 2.35 |
| Diavola Pizza             | main     |               22.54 |               22.68 |                  15.51 |                  15.54 |                 5.7  |                 5.93 |
| Draft Beer                | drink    |                9    |                9    |                   7.34 |                   7.34 |                 1.39 |                 1.39 |
| Espresso                  | drink    |                4.5  |                4.5  |                   3.71 |                   3.65 |                 0.65 |                 0.71 |
| Funghi Pizza              | main     |               21.55 |               21.77 |                  14.75 |                  14.67 |                 5.57 |                 5.93 |
| Garlic Bread              | starter  |                9.2  |                9.22 |                   7.04 |                   7    |                 1.71 |                 1.76 |
| Grilled Salmon            | main     |               32.84 |               32.95 |                  19.24 |                  18.79 |                11.82 |                12.42 |
| House Wine (glass)        | drink    |               14    |               14    |                  10.11 |                  10.03 |                 3.46 |                 3.54 |
| Lobster Ravioli           | main     |               27.68 |               27.25 |                  11.98 |                   0.03 |                14.04 |                25.87 |
| Margherita Pizza          | main     |               17.67 |               17.51 |                  11.81 |                  11.38 |                 4.89 |                 5.23 |
| Mushroom Risotto          | main     |               24.14 |               24.11 |                  15.44 |                  15.35 |                 7.41 |                 7.51 |
| Panna Cotta               | dessert  |               11.19 |               11.17 |                   8.25 |                   8.32 |                 2.41 |                 2.34 |
| Rigatoni alla Vodka       | main     |               21.44 |               21.19 |                  14.7  |                  14.56 |                 5.57 |                 5.58 |
| Shrimp Linguine           | main     |               28.35 |               28.22 |                  17.01 |                  16.76 |                 9.61 |                 9.94 |
| Soda                      | drink    |                4.59 |                4.58 |                   3.91 |                   3.89 |                 0.43 |                 0.45 |
| Spaghetti Pomodoro        | main     |               20.52 |               20.58 |                  13.78 |                  13.61 |                 5.66 |                 5.89 |
| Tiramisu                  | dessert  |               12.3  |               13.22 |                   8.53 |                   9.38 |                 3.14 |                 3.24 |

Lobster Ravioli by month:

| month   |   avg_price |   plate_cost |   cm_per_plate |   units |
|:--------|------------:|-------------:|---------------:|--------:|
| 2025-10 |       27.51 |        13.95 |          12    |      53 |
| 2025-11 |       27.68 |        14.04 |          11.98 |     202 |
| 2025-12 |       27.36 |        14.43 |          11.43 |     235 |
| 2026-01 |       27.37 |        14.08 |          11.79 |     194 |
| 2026-02 |       27.69 |        14.12 |          11.85 |     153 |
| 2026-03 |       27.32 |        25.45 |           0.39 |     207 |
| 2026-04 |       27.44 |        25.51 |           0.46 |     211 |
| 2026-05 |       27.53 |        24.29 |           1.79 |     264 |
| 2026-06 |       27.14 |        24.59 |           1.06 |     230 |
| 2026-07 |       26.83 |        25.61 |          -0.08 |     196 |
| 2026-08 |       27.35 |        25.56 |           0.33 |     202 |
| 2026-09 |       27.2  |        25.94 |          -0.26 |     228 |
| 2026-10 |       27.25 |        25.87 |           0.03 |     192 |

Menu-engineering classes, last month:

| item                      | course   | menu_class   |   units |   food_cost_pct |   loses_money |
|:--------------------------|:---------|:-------------|--------:|----------------:|--------------:|
| Panna Cotta               | dessert  | plowhorse    |     207 |           0.209 |             0 |
| Tiramisu                  | dessert  | star         |     375 |           0.245 |             0 |
| Draft Beer                | drink    | dog          |     316 |           0.154 |             0 |
| Espresso                  | drink    | dog          |     362 |           0.158 |             0 |
| Soda                      | drink    | plowhorse    |     670 |           0.098 |             0 |
| Cocktail                  | drink    | star         |     384 |           0.157 |             0 |
| House Wine (glass)        | drink    | star         |     866 |           0.253 |             0 |
| Lobster Ravioli           | main     | plowhorse    |     192 |           0.949 |             0 |
| Margherita Pizza          | main     | plowhorse    |     317 |           0.299 |             0 |
| Spaghetti Pomodoro        | main     | plowhorse    |     211 |           0.286 |             0 |
| Braised Short Rib         | main     | puzzle       |     137 |           0.469 |             0 |
| Funghi Pizza              | main     | puzzle       |     149 |           0.272 |             0 |
| Bolognese                 | main     | star         |     276 |           0.331 |             0 |
| Cacio e Pepe              | main     | star         |     205 |           0.267 |             0 |
| Chicken Parmesan          | main     | star         |     271 |           0.318 |             0 |
| Diavola Pizza             | main     | star         |     202 |           0.261 |             0 |
| Grilled Salmon            | main     | star         |     171 |           0.377 |             0 |
| Mushroom Risotto          | main     | star         |     166 |           0.311 |             0 |
| Rigatoni alla Vodka       | main     | star         |     319 |           0.263 |             0 |
| Shrimp Linguine           | main     | star         |     181 |           0.352 |             0 |
| Garlic Bread              | starter  | plowhorse    |     275 |           0.191 |             0 |
| Arancini                  | starter  | star         |     259 |           0.305 |             0 |
| Burrata & Heirloom Tomato | starter  | star         |     251 |           0.396 |             0 |
| Caesar Salad              | starter  | star         |     322 |           0.29  |             0 |


## 5. Ingredient prices

![](ingredient_prices.png)

| ingredient   |   index_vs_first |
|:-------------|-----------------:|
| lobster      |            2.12  |
| eggs         |            1.186 |
| olive_oil    |            1.136 |
| potato       |            1.134 |
| ground_beef  |            1.115 |
| tomato       |            1.114 |


## 6. Cash

![](cash_balance.png)

Cash on 2026-10-24: **$157,631**. Net cash over the window: **$-82,369**. See scenario_check.py for the forward view.


## 7. Bank feed by Nessie type and category

| nessie_type   | category                |   rows |   total_usd |
|:--------------|:------------------------|-------:|------------:|
| deposit       | Internal                |      1 |       15000 |
| deposit       | Sales - Card            |    362 |     1676843 |
| deposit       | Sales - Cash            |    102 |      139064 |
| deposit       | Sales - Delivery        |    104 |      206910 |
| deposit       | Supplier Credit         |      1 |          94 |
| purchase      | Beverage                |     52 |       98709 |
| purchase      | Direct Operating        |     24 |       61927 |
| purchase      | Food Supply - Dairy     |    103 |      150111 |
| purchase      | Food Supply - Dry Goods |     51 |       89822 |
| purchase      | Food Supply - Produce   |    157 |       45798 |
| purchase      | Food Supply - Protein   |    104 |      162562 |
| purchase      | G&A                     |     14 |       17315 |
| purchase      | Insurance               |     12 |       29400 |
| purchase      | Marketing               |     12 |       25000 |
| purchase      | Occupancy               |     12 |      114774 |
| purchase      | Repairs & Maintenance   |     27 |       11376 |
| purchase      | Software                |     13 |        2329 |
| purchase      | Utilities               |     48 |       56664 |
| withdrawal    | Chargeback              |      2 |         383 |
| withdrawal    | Debt Service            |     12 |       27000 |
| withdrawal    | Internal                |      1 |       15000 |
| withdrawal    | Labor                   |     26 |     1034157 |
| withdrawal    | Owner Draw              |     12 |       60000 |
| withdrawal    | Sales Tax               |     11 |      117954 |
