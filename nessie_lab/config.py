"""Every assumption behind the SIMULATED restaurant, in one place (v2, after red-team review).

Calibration anchors:
- NRA 2025 Restaurant Operations Data Abstract (2024 data, full-service medians): food & bev 32.0%,
  payroll+benefits 36.5%, urban occupancy 6.0%, pretax income +2.8%. This restaurant is deliberately weaker.
- SF minimum wage $19.18 (2025-07-01), $19.61 (2026-07-01). sf.gov Minimum Wage Ordinance.
- SF third-party delivery commission cap 15% (Police Code Art. 53, 2020).
- SF sales tax 8.625%. Card processing modeled on flat-rate in-person pricing (2.6% + $0.15).
- Wholesale ingredient prices are round-number estimates of 2025 US foodservice prices, not quotes.
All names of the restaurant, suppliers, landlords, lenders and delivery platforms are fictional.
Utility payees use generic names. Every record is tagged SIMULATED when loaded to Nessie.
"""
from datetime import date

SEED = 13
START, END = date(2025, 10, 25), date(2026, 10, 24)      # bank window; END = day before demo day
TRAIN_END = date(2026, 7, 24)                            # backtest: last 3 months hidden
WARMUP_DAYS = 45                                         # sim starts earlier so AP/payroll carry in realistically
OPENING_BALANCE = {"CHS": 210_000_00, "UNI": 90_000_00, "RES": 40_000_00}   # cents, as of START
ACCOUNTS = {"CHS": ("Checking", "Chestnut Operating", "4417"), "UNI": ("Checking", "Union Operating", "8823"),
            "RES": ("Savings", "Reserve", "3051")}
GEO = {"zip": "94123", "neighborhood": {"CHS": "Marina", "UNI": "Cow Hollow"}, "cuisine": "Italian trattoria"}

LOCATIONS = {
    # street numbers are deliberately not real storefronts
    "CHS": dict(name="Demo Bistro Chestnut", street_number="0", street_name="Chestnut St (SIMULATED)",
                lat=37.8008, lng=-122.4380, seats=60, lunch=29, dinner=70, closed_dow=[],
                fixed_hrs=34, hrs_per_cover=0.24, gm_salary=82_000),
    "UNI": dict(name="Demo Bistro Union", street_number="0", street_name="Union St (SIMULATED)",
                lat=37.7976, lng=-122.4330, seats=45, lunch=21, dinner=50, closed_dow=[0],
                fixed_hrs=26, hrs_per_cover=0.24, gm_salary=76_000),
}
SHARED_SALARIES = {"sous_chef": 72_000}    # owner/chef takes a draw, not a salary
LEASES = {  # monthly cents incl. NNN/CAM; escalator applied on anniversary
    "CHS": dict(landlord="Chestnut Row Properties", base=9_400_00, escalator_pct=0.03, anniversary=date(2026, 4, 1)),
    "UNI": dict(landlord="Fillmore Union Holdings", base=6_500_00, escalator_pct=0.03, anniversary=date(2026, 11, 1)),
}

DOW_MULT = [0.72, 0.76, 0.86, 0.96, 1.24, 1.34, 1.10]          # Mon..Sun
MONTH_MULT = {10: 1.04, 11: 0.98, 12: 1.06, 1: 0.80, 2: 0.88, 3: 0.94,
              4: 0.97, 5: 1.01, 6: 1.04, 7: 0.99, 8: 0.96, 9: 1.00}
COVER_TREND_PER_YEAR = -0.06
RAIN_PROB = {10: .10, 11: .30, 12: .40, 1: .45, 2: .40, 3: .35, 4: .20, 5: .05,
             6: .02, 7: .01, 8: .01, 9: .03}
RAIN_PERSIST = 0.55                    # P(rain tomorrow | rain today): storms last several days
RAIN_DINEIN, RAIN_DELIVERY = 0.87, 1.25
CLOSED_DATES = {date(2025, 11, 27), date(2025, 12, 25)}
# per-service multipliers; 2026 Fleet Week dates are approximate
EVENTS = {
    "nye":             (date(2025, 12, 31), date(2025, 12, 31), dict(lunch=1.0, dinner=1.40, delivery=0.9)),
    "super_bowl_lx":   (date(2026, 2, 8), date(2026, 2, 8), dict(lunch=1.0, dinner=0.70, delivery=1.6)),
    "valentines":      (date(2026, 2, 14), date(2026, 2, 14), dict(lunch=1.0, dinner=1.45, delivery=1.1)),
    "mothers_day":     (date(2026, 5, 10), date(2026, 5, 10), dict(lunch=1.60, dinner=1.10, delivery=1.0)),
    "bay_to_breakers": (date(2026, 5, 17), date(2026, 5, 17), dict(lunch=1.30, dinner=1.0, delivery=1.0)),
    "fleet_week":      (date(2026, 10, 5), date(2026, 10, 11), dict(lunch=1.30, dinner=1.25, delivery=1.0)),
}
FLEET_WEEK_UNI_DAMPING = 0.5           # crowds are on the Marina Green side

DELIVERY_SHARE = 0.16                  # of covers
DELIVERY_MARKUP = 1.15                 # delivery menu priced above dine-in (standard practice)
PLATFORMS = {"RapidPlate": 0.6, "UrbanBite": 0.4}        # fictional delivery platforms
DELIVERY_COMMISSION = 0.15
CARD_SHARE_DINEIN = 0.91
CARD_PCT, CARD_FIXED = 0.026, 15       # cents
PROCESSOR = "TableTap Payments"
SETTLEMENT_DELAY_P = 0.02              # occasional extra business day (natural noise, not an error)
CASH_VARIANCE_CENTS = 500              # counted cash differs from POS by up to +/-$5 (natural noise)
BANK_COUNT_CORRECTION_P = 0.08         # bank recounts a cash deposit: +/-$1-9 (natural noise)
MATERIALITY_CENTS = 1000               # reconciliation variances <= $10 are immaterial
SALES_TAX = 0.08625
TIP_CHOICES, TIP_P = [0.0, 0.10, 0.15, 0.18, 0.20, 0.22, 0.25], [.02, .04, .08, .28, .33, .20, .05]
TIP_ROUND_UP_P = 0.30                  # guest rounds total up to whole dollars
PARTY_SIZE_P = {"lunch": [.30, .45, .12, .10, .02, .01], "dinner": [.14, .44, .14, .19, .05, .04]}
LUNCH_HOUR_P = [.20, .40, .30, .10]    # 11..14
DINNER_HOUR_P = [.12, .27, .31, .20, .10]   # 17..21
MAIN_PER_COVER = 0.95
MODIFIER_P = 0.12                      # "Add grilled chicken" on a main
DISCOUNT_P, DISCOUNT_RANGE = 0.05, (0.15, 0.50)   # comps/manager/loyalty, ~1.5% of sales
REFUND_P = 0.002                       # card tickets refunded next day
CHARGEBACKS = [(date(2026, 1, 21), "CHS", 218_40), (date(2026, 6, 9), "UNI", 164_75)]   # documented disputes
PRIVATE_EVENTS_PER_MONTH = 2           # CHS only, Fri/Sat dinner, 12-25 guests, prix fixe
PRIX_FIXE = 68
STAFF_MEAL = {"pasta": .20, "chicken": .30, "greens": .15, "canned_tomato": .20, "parmesan": .02, "olive_oil": .02}
STAFF_MEAL_PER_HOURS = 6               # one staff meal per 6 labor hours
GIFT_CARDS = dict(sell_from=date(2025, 12, 1), sell_to=date(2025, 12, 24), per_day={"CHS": 5, "UNI": 3},
                  amounts=[50, 75, 100, 150], redeem_p=0.012, redeem_from=date(2026, 1, 1),
                  breakage=0.15)          # share of gift-card value never redeemed

BOH_WAGE = {date(2025, 7, 1): 25.00, date(2026, 7, 1): 25.75}
FOH_HOURS_SHARE = 0.55                 # FOH paid minimum wage + tips (CA has no tip credit)
MIN_WAGE = {date(2025, 7, 1): 19.18, date(2026, 7, 1): 19.61}
EMPLOYER_BURDEN = 0.22                 # FICA, FUTA/SUI, workers comp, SF HCSO
TIP_FICA = 0.0765                      # employer FICA on reported tips
SCHEDULE_ADJ = 0.5                     # managers cut/call in staff for half of the forecast miss

# Suppliers: category, payment terms (days), delivery weekdays (Mon=0), invoice number shown in ACH memo?
SUPPLIERS = {
    "Bay Produce Co":            ("Food Supply - Produce", 7,  [0, 2, 4], True),
    "Northbay Protein Co":   ("Food Supply - Protein", 14, [1, 4], True),
    "Sonoma Dairy Supply":       ("Food Supply - Dairy", 14,   [1, 5], False),
    "Mission Dry Goods":         ("Food Supply - Dry Goods", 30, [3], True),
    "Bay Beverage Distributors": ("Beverage", 30, [1], False),
}
STRETCH_TOLERANCE_DAYS = {"Food Supply - Produce": 7, "Food Supply - Protein": 10, "Food Supply - Dairy": 10,
                          "Food Supply - Dry Goods": 15, "Beverage": 15}   # assumption: extra days a supplier tolerates
PAR_SAFETY = 1.50                      # order up to 150% of expected use until next delivery
SHRINK_PER_DAY = {"Food Supply - Produce": .006, "Food Supply - Protein": .003, "Food Supply - Dairy": .003}
PRICE_AR, PRICE_SIGMA = 0.7, 0.03      # weekly AR(1) log price noise per ingredient
USAGE_VARIANCE = 1.08                  # actual vs theoretical usage (prep loss, over-portioning)

# Bay Area broadline distributors deliver above national commodity prices (fuel surcharge, small drops).
# Premium-Italian specs (Parmigiano-Reggiano, San Marzano, imported pasta, EVOO, fior di latte) set the bases. Lobster is a delivered specialty-seafood quote already.
DELIVERED_PRICE_FACTOR = 1.26
DELIVERED_EXEMPT = {"lobster", "house_wine", "bottle_wine", "spirits", "beer", "soda",   # quoted delivered already
                    "flour", "onion", "potato", "sugar"}                                 # bulk staples
# ingredient: (unit, wholesale $/unit, supplier, monthly drift, {date: step multiplier}, pack size)
INGREDIENTS = {
    "tomato":        ("lb", 2.40, "Bay Produce Co", .003, {}, 25),
    "greens":        ("lb", 3.00, "Bay Produce Co", .003, {}, 10),
    "onion":         ("lb", 0.85, "Bay Produce Co", .002, {}, 50),
    "garlic":        ("lb", 3.20, "Bay Produce Co", .002, {}, 5),
    "basil":         ("lb", 12.00, "Bay Produce Co", .003, {}, 1),
    "lemon":         ("lb", 1.60, "Bay Produce Co", .003, {}, 10),
    "mushroom":      ("lb", 5.50, "Bay Produce Co", .003, {}, 10),
    "potato":        ("lb", 0.75, "Bay Produce Co", .002, {}, 50),
    "seasonal_veg":  ("lb", 2.40, "Bay Produce Co", .003, {}, 10),
    "arugula":       ("lb", 6.00, "Bay Produce Co", .003, {}, 4),
    "chicken":       ("lb", 3.60, "Northbay Protein Co", .004, {}, 40),
    "ground_beef":   ("lb", 6.50, "Northbay Protein Co", .006, {}, 10),
    "pork":          ("lb", 9.00, "Northbay Protein Co", .004, {}, 10),
    "short_rib":     ("lb", 13.00, "Northbay Protein Co", .007, {}, 20),
    "salmon":        ("lb", 14.00, "Northbay Protein Co", .004, {}, 10),
    "shrimp":        ("lb", 12.00, "Northbay Protein Co", .004, {}, 5),
    "lobster":       ("lb", 40.0, "Northbay Protein Co", .003, {date(2026, 3, 1): 2.00}, 2),   # lobster-meat shortage (cf. 2021)
    "mozzarella":    ("lb", 6.50, "Sonoma Dairy Supply", .003, {}, 5),
    "burrata":       ("lb", 12.00, "Sonoma Dairy Supply", .003, {}, 2),
    "parmesan":      ("lb", 17.00, "Sonoma Dairy Supply", .003, {}, 10),
    "butter":        ("lb", 5.00, "Sonoma Dairy Supply", .003, {}, 8),
    "cream":         ("qt", 5.00, "Sonoma Dairy Supply", .003, {}, 6),
    "eggs":          ("doz", 4.00, "Sonoma Dairy Supply", .000, {date(2026, 1, 15): 1.30, date(2026, 5, 1): 0.85}, 15),
    "ricotta":       ("lb", 4.50, "Sonoma Dairy Supply", .003, {}, 3),
    "pasta":         ("lb", 2.60, "Mission Dry Goods", .002, {}, 20),
    "flour":         ("lb", 0.90, "Mission Dry Goods", .002, {}, 50),
    "olive_oil":     ("L", 14.00, "Mission Dry Goods", .002, {date(2026, 1, 1): 1.12}, 3),
    "arborio":       ("lb", 3.20, "Mission Dry Goods", .002, {}, 10),
    "canned_tomato": ("lb", 2.00, "Mission Dry Goods", .002, {}, 40),
    "sugar":         ("lb", 0.60, "Mission Dry Goods", .002, {}, 25),
    "coffee":        ("lb", 12.00, "Mission Dry Goods", .004, {}, 5),
    "house_wine":    ("btl", 16.00, "Bay Beverage Distributors", .002, {}, 12),
    "bottle_wine":   ("btl", 20.00, "Bay Beverage Distributors", .002, {}, 12),
    "spirits":       ("btl", 30.00, "Bay Beverage Distributors", .002, {}, 6),
    "beer":          ("pint", 1.29, "Bay Beverage Distributors", .002, {}, 124),   # 1/2 keg
    "soda":          ("unit", 0.40, "Bay Beverage Distributors", .002, {}, 24),
}

# item: (course, dinner price $, popularity weight, recipe {ingredient: qty})
# drinks: weight = per-cover probability at dinner; bottle: per-party probability (parties of 2+)
MENU = {
    "Burrata & Heirloom Tomato": ("starter", 17, 1.0, {"burrata": .25, "tomato": .35, "basil": .02, "olive_oil": .03, "arugula": .06}),
    "Caesar Salad":              ("starter", 14, 1.2, {"greens": .35, "parmesan": .06, "eggs": .08, "lemon": .12, "flour": .12, "olive_oil": .03}),
    "Arancini":                  ("starter", 14, 0.9, {"arborio": .18, "mozzarella": .10, "flour": .06, "olive_oil": .06, "parmesan": .04, "eggs": .05}),
    "Garlic Bread":              ("starter", 9,  1.1, {"flour": .30, "butter": .08, "garlic": .04, "parmesan": .03}),
    "Spaghetti Pomodoro":        ("main", 21, 1.0, {"pasta": .30, "canned_tomato": .45, "tomato": .15, "basil": .03, "olive_oil": .05, "garlic": .03, "parmesan": .05}),
    "Rigatoni alla Vodka":       ("main", 22, 1.6, {"pasta": .30, "canned_tomato": .35, "cream": .25, "parmesan": .06, "onion": .12, "butter": .03}),
    "Cacio e Pepe":              ("main", 23, 1.0, {"pasta": .32, "parmesan": .18, "butter": .06}),
    "Bolognese":                 ("main", 26, 1.3, {"pasta": .30, "ground_beef": .35, "pork": .12, "canned_tomato": .30, "onion": .12, "cream": .10, "parmesan": .04}),
    "Lobster Ravioli":           ("main", 28, 0.9, {"lobster": .25, "flour": .18, "eggs": .12, "ricotta": .12, "butter": .08, "cream": .15}),
    "Mushroom Risotto":          ("main", 25, 0.8, {"arborio": .25, "mushroom": .40, "parmesan": .08, "butter": .08, "onion": .10, "cream": .12}),
    "Shrimp Linguine":           ("main", 29, 0.9, {"pasta": .30, "shrimp": .40, "garlic": .04, "olive_oil": .06, "lemon": .12, "butter": .06}),
    "Chicken Parmesan":          ("main", 28, 1.2, {"chicken": .60, "mozzarella": .15, "flour": .12, "eggs": .10, "canned_tomato": .25, "olive_oil": .08, "pasta": .18, "parmesan": .03}),
    "Braised Short Rib":         ("main", 38, 0.7, {"short_rib": .80, "potato": .45, "butter": .08, "cream": .12, "seasonal_veg": .25}),
    "Grilled Salmon":            ("main", 34, 0.8, {"salmon": .50, "seasonal_veg": .35, "lemon": .12, "olive_oil": .04, "potato": .35}),
    "Margherita Pizza":          ("main", 18, 1.5, {"flour": .50, "canned_tomato": .28, "mozzarella": .30, "basil": .03, "olive_oil": .03}),
    "Funghi Pizza":              ("main", 22, 0.8, {"flour": .50, "mozzarella": .30, "mushroom": .25, "garlic": .03, "olive_oil": .03}),
    "Diavola Pizza":             ("main", 23, 1.0, {"flour": .50, "mozzarella": .30, "canned_tomato": .28, "pork": .15}),
    "Tiramisu":                  ("dessert", 12, 1.3, {"eggs": .18, "cream": .22, "coffee": .03, "sugar": .06, "flour": .06}),
    "Panna Cotta":               ("dessert", 11, 0.8, {"cream": .32, "sugar": .06, "lemon": .05}),
    "House Wine (glass)":        ("drink", 14, 0.40, {"house_wine": .20}),
    "Cocktail":                  ("drink", 15, 0.18, {"spirits": .06, "lemon": .05, "soda": .3}),
    "Draft Beer":                ("drink", 9,  0.15, {"beer": 1}),
    "Soda":                      ("drink", 4.5, 0.20, {"soda": 1}),
    "Espresso":                  ("drink", 4.5, 0.15, {"coffee": .04}),
    "Bottle of Wine":            ("bottle", 56, 0.18, {"bottle_wine": 1}),
    "Add Grilled Chicken":       ("modifier", 6, 0.0, {"chicken": .25}),
    "Private Event Prix Fixe":   ("event", PRIX_FIXE, 0.0, {"greens": .2, "parmesan": .06, "pasta": .2, "short_rib": .45,
                                                           "potato": .3, "cream": .25, "flour": .15, "butter": .06, "sugar": .05, "eggs": .08}),
}
STARTER_PER_COVER, DESSERT_PER_COVER = 0.45, 0.24
LUNCH_MAIN_PRICE, LUNCH_ATTACH = 0.80, 0.55    # lunch: cheaper mains; fewer drinks/desserts
MENU_REPRICE = (date(2026, 4, 1), {"Bolognese": 27, "Chicken Parmesan": 29, "Caesar Salad": 15, "Tiramisu": 13})

# Recurring payees: (merchant, category, cents by location, day of month, variation)
RECURRING = [
    ("City Gas & Electric",        "Utilities", {"CHS": 3_300_00, "UNI": 2_350_00}, 18, "seasonal"),
    ("City Water & Sewer",         "Utilities", {"CHS": 560_00,   "UNI": 400_00},   22, "noise"),
    ("Bayside Waste & Recycling",  "Utilities", {"CHS": 520_00,   "UNI": 380_00},   5,  "volume"),
    ("Metro Business Internet",    "Utilities", {"CHS": 180_00,   "UNI": 180_00},   12, "flat"),
    ("TableTap POS",               "Software",  {"CHS": 165_00,   "UNI": 165_00},   3,  "flat"),
    ("Presidio Linen Service",     "Direct Operating", {"CHS": 1_400_00, "UNI": 1_050_00}, 28, "volume"),
    ("Golden State Restaurant Supply", "Direct Operating", {"CHS": 3_700_00, "UNI": 2_750_00}, 15, "volume"),
    ("Marina Pest Control",        "Repairs & Maintenance", {"CHS": 140_00, "UNI": 140_00}, 9, "flat"),
    ("Embarcadero Mutual Insurance (SIM)", "Insurance", {"CHS": 2_450_00}, 1, "flat"),
    ("Lombard Ledger Bookkeeping (SIM)",   "G&A",       {"CHS": 1_250_00}, 25, "flat"),
    ("Neighborhood Ads Co",        "Marketing", {"CHS": 2_500_00}, 10, "campaign"),
]
OWNER_DRAW = dict(monthly=10_000_00, day=15)
LOAN = dict(lender="Pacific Coast Small Business Lending (SIM)", type="SBA 7(a) equipment", original=220_000_00,
            balance_at_start=180_000_00, monthly=3_250_00, rate=0.085, day=20, credit_score=702, account="CHS")
REPAIRS = dict(merchant="Bay Kitchen Repair", per_month_per_loc=1.1, median_cents=380_00,
               big=(date(2026, 2, 17), "UNI", 3_960_00, "Convection oven control board replacement"))
PERMITS = [(date(2026, 1, 15), "CHS", 1_100_00, "SF Dept of Public Health", "Health permit"),
           (date(2026, 1, 15), "UNI", 1_100_00, "SF Dept of Public Health", "Health permit"),
           (date(2026, 6, 30), "CHS", 1_215_00, "CA ABC", "Liquor license renewal")]
SALES_TAX_REMIT_DAY = 24               # monthly prepayment of prior month (simplified; filed from CHS)
PAYROLL_FIRST = date(2025, 10, 31)     # biweekly Fridays
SWEEP_LOW, SWEEP_HIGH, SWEEP_TARGET = 20_000_00, 70_000_00, 45_000_00   # keep UNI inside this band
RESERVE_DRAW = (date(2026, 1, 28), 15_000_00)

# ---------------------------------------------------------------- active locations
# One restaurant for now. Set ACTIVE_LOCATIONS = ["CHS", "UNI"] to rebuild the two-location group (v2 data).
ACTIVE_LOCATIONS = ["CHS"]
ALT_LOC = "UNI" if "UNI" in ACTIVE_LOCATIONS else "CHS"   # hosts planted items E2, E5, E7, D3, D6 and the 2nd chargeback
SALARY_SHARE = {"CHS": 0.6, "UNI": 0.4}                   # shared salaries split across active locations
if "UNI" not in ACTIVE_LOCATIONS:                         # one restaurant carries all group costs: scale them to its size
    OPENING_BALANCE["CHS"] = 200_000_00
    OWNER_DRAW["monthly"] = 5_000_00
    LOAN.update(original=150_000_00, balance_at_start=120_000_00, monthly=2_250_00)
LOCATIONS = {k: v for k, v in LOCATIONS.items() if k in ACTIVE_LOCATIONS}
LEASES = {k: v for k, v in LEASES.items() if k in LOCATIONS}
ACCOUNTS = {k: v for k, v in ACCOUNTS.items() if k in LOCATIONS or k == "RES"}
OPENING_BALANCE = {k: OPENING_BALANCE[k] for k in ACCOUNTS}
GEO["neighborhood"] = {k: v for k, v in GEO["neighborhood"].items() if k in LOCATIONS}
GIFT_CARDS["per_day"] = {k: v for k, v in GIFT_CARDS["per_day"].items() if k in LOCATIONS}
CHARGEBACKS = [(d, loc if loc in LOCATIONS else ALT_LOC, amt) for d, loc, amt in CHARGEBACKS]
RECURRING = [(p, cat, {k: v for k, v in amts.items() if k in LOCATIONS}, day, var) for p, cat, amts, day, var in RECURRING]
PERMITS = [p for p in PERMITS if p[1] in LOCATIONS]
REPAIRS["big"] = (REPAIRS["big"][0], ALT_LOC, *REPAIRS["big"][2:])
SALARY_SHARE = {k: SALARY_SHARE[k] / sum(SALARY_SHARE[j] for j in LOCATIONS) for k in LOCATIONS}
