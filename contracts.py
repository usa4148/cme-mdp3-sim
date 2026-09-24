"""
Futures contract specifications and front-month resolution.

CME's `config.xml` maps channels to products and multicast feeds, but it carries
no contract economics — tick size, contract multiplier and expiry cycle are in
the rulebook, not the channel config. This module holds that table, keyed by the
same product codes `config.xml` uses, plus the date arithmetic that turns "the
ES future" into "ESZ6, expiring 2026-12-18".

The expiry rules are faithful in *shape* to CME's rulebook (third Friday, N
business days before month end, and so on) but ignore exchange holidays, so a
computed date can be a day off when a holiday falls in the window. That is
accurate enough to pick a front month; it is not a settlement calendar.

    python3 contracts.py            # list every product and its front month
    python3 contracts.py ES CL GC   # just these
"""
from __future__ import annotations

import argparse
import calendar
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pyver import require_python

require_python()

# CME month codes: Jan..Dec
MONTH_CODES = "FGHJKMNQUVXZ"
QUARTERLY = (3, 6, 9, 12)
ALL_MONTHS = tuple(range(1, 13))

DEFAULT_PRODUCT = "ES"
DEFAULT_BAND_TICKS = 100          # 100 ticks == the old 25-point ES band


@dataclass(frozen=True)
class ContractSpec:
    """Economics of one futures product."""
    code: str
    name: str
    tick: float                   # minimum price increment
    tick_value: float             # USD per tick
    cycle: tuple                  # listed contract months
    expiry_rule: str
    yahoo: str                    # symbol for the prior-close lookup
    security_id: int              # synthetic; real ids come from the definition feed
    group: str = ""               # asset class, for grouping in the chooser
    ref_price: float = 0.0        # rough level, ONLY used offline with no cache

    @property
    def decimals(self) -> int:
        """Decimal places needed to render this product's tick exactly."""
        return max(0, -Decimal(str(self.tick)).normalize().as_tuple().exponent)

    @property
    def multiplier(self) -> float:
        return self.tick_value / self.tick


# Tick sizes and tick values are the real contract specs. Security ids are
# synthetic and stable — on a real feed they arrive in the security definition
# messages; ES keeps 42003, the id this simulator has always used.
_SPECS_LIST = [
    # --- CME equity index -------------------------------------------------
    ContractSpec("ES",  "E-mini S&P 500",        0.25,   12.50, QUARTERLY, "third_friday",  "ES=F",  42003),
    ContractSpec("NQ",  "E-mini Nasdaq-100",     0.25,    5.00, QUARTERLY, "third_friday",  "NQ=F",  42004),
    ContractSpec("RTY", "E-mini Russell 2000",   0.10,    5.00, QUARTERLY, "third_friday",  "RTY=F", 42005),
    # EMD has no usable Yahoo history, so it opens at its reference level offline
    ContractSpec("EMD", "E-mini S&P MidCap 400", 0.10,   10.00, QUARTERLY, "third_friday",  "EMD=F", 42006),
    ContractSpec("MES", "Micro E-mini S&P 500",  0.25,    1.25, QUARTERLY, "third_friday",  "MES=F", 42007),
    ContractSpec("MNQ", "Micro E-mini Nasdaq",   0.25,    0.50, QUARTERLY, "third_friday",  "MNQ=F", 42008),
    # --- CME FX -----------------------------------------------------------
    ContractSpec("6E",  "Euro FX",         0.00005,       6.25, QUARTERLY, "fx",            "6E=F",  42101),
    ContractSpec("6J",  "Japanese Yen",    0.0000005,     6.25, QUARTERLY, "fx",            "6J=F",  42102),
    ContractSpec("6B",  "British Pound",   0.0001,        6.25, QUARTERLY, "fx",            "6B=F",  42103),
    ContractSpec("6A",  "Australian Dollar", 0.0001,     10.00, QUARTERLY, "fx",            "6A=F",  42104),
    ContractSpec("6C",  "Canadian Dollar", 0.00005,       5.00, QUARTERLY, "fx",            "6C=F",  42105),
    # --- CBOT interest rates ---------------------------------------------
    ContractSpec("ZT",  "2-Year T-Note",   0.0078125,    15.625, QUARTERLY, "treasury",     "ZT=F",  42201),
    ContractSpec("ZF",  "5-Year T-Note",   0.0078125,     7.8125, QUARTERLY, "treasury",    "ZF=F",  42202),
    ContractSpec("ZN",  "10-Year T-Note",  0.015625,     15.625, QUARTERLY, "treasury",     "ZN=F",  42203),
    ContractSpec("ZB",  "30-Year T-Bond",  0.03125,      31.25, QUARTERLY, "treasury",      "ZB=F",  42204),
    ContractSpec("UB",  "Ultra T-Bond",    0.03125,      31.25, QUARTERLY, "treasury",      "UB=F",  42205),
    # --- CBOT agriculture -------------------------------------------------
    ContractSpec("ZC",  "Corn",            0.25,         12.50, (3, 5, 7, 9, 12),             "grain", "ZC=F", 42301),
    ContractSpec("ZW",  "Chicago Wheat",   0.25,         12.50, (3, 5, 7, 9, 12),             "grain", "ZW=F", 42302),
    ContractSpec("ZS",  "Soybeans",        0.25,         12.50, (1, 3, 5, 7, 8, 9, 11),       "grain", "ZS=F", 42303),
    ContractSpec("ZM",  "Soybean Meal",    0.10,         10.00, (1, 3, 5, 7, 8, 9, 10, 12),   "grain", "ZM=F", 42304),
    ContractSpec("ZL",  "Soybean Oil",     0.01,          6.00, (1, 3, 5, 7, 8, 9, 10, 12),   "grain", "ZL=F", 42305),
    # --- NYMEX energy -----------------------------------------------------
    ContractSpec("CL",  "WTI Crude Oil",   0.01,         10.00, ALL_MONTHS, "crude",        "CL=F",  42401),
    ContractSpec("NG",  "Henry Hub Natural Gas", 0.001,  10.00, ALL_MONTHS, "prior_month_end", "NG=F", 42402),
    ContractSpec("RB",  "RBOB Gasoline",   0.0001,        4.20, ALL_MONTHS, "prior_month_end", "RB=F", 42403),
    ContractSpec("HO",  "NY Harbor ULSD",  0.0001,        4.20, ALL_MONTHS, "prior_month_end", "HO=F", 42404),
    # --- COMEX metals -----------------------------------------------------
    ContractSpec("GC",  "Gold",            0.10,         10.00, (2, 4, 6, 8, 12),           "metal", "GC=F", 42501),
    ContractSpec("SI",  "Silver",          0.005,        25.00, (3, 5, 7, 9, 12),           "metal", "SI=F", 42502),
    ContractSpec("HG",  "Copper",          0.0005,       12.50, (3, 5, 7, 9, 12),           "metal", "HG=F", 42503),
    ContractSpec("PL",  "Platinum",        0.10,          5.00, (1, 4, 7, 10),              "metal", "PL=F", 42504),
]

# Last-resort price anchors, used only when there is no network AND no cached
# close AND no price was passed. They are rough round numbers to get the book
# somewhere sane, NOT quotes — anything reported from them is labelled
# "fallback" so it is never mistaken for a real level.
_REF_PRICES = {
    # Captured 2026-09-24 from each product's prior settlement. They go stale;
    # they exist only so an offline run opens somewhere plausible.
    "ES": 7772.50,   "NQ": 30764.75,  "RTY": 2860.20,  "EMD": 3400.00,
    "MES": 7772.50,  "MNQ": 30764.75,
    "6E": 1.139850,  "6J": 0.0063320, "6B": 1.324400,
    "6A": 0.7036500, "6C": 0.7102500,
    "ZT": 101.914063, "ZF": 103.820313, "ZN": 105.031250,
    "ZB": 105.875000, "UB": 107.468750,
    "ZC": 529.00,    "ZW": 708.50,    "ZS": 1318.00,
    "ZM": 370.30,    "ZL": 67.25,
    "CL": 92.16,     "NG": 3.023,     "RB": 3.587,     "HO": 4.7764,
    "GC": 4318.40,   "SI": 64.382,    "HG": 6.678,     "PL": 1745.70,
}

# Asset class per product, used to group the chooser.
_GROUPS = {
    "Equity":    ("ES", "NQ", "RTY", "EMD", "MES", "MNQ"),
    "FX":        ("6E", "6J", "6B", "6A", "6C"),
    "Rates":     ("ZT", "ZF", "ZN", "ZB", "UB"),
    "Grains":    ("ZC", "ZW", "ZS", "ZM", "ZL"),
    "Energy":    ("CL", "NG", "RB", "HO"),
    "Metals":    ("GC", "SI", "HG", "PL"),
}
GROUP_ORDER = tuple(_GROUPS)
_GROUP_OF = {code: g for g, codes in _GROUPS.items() for code in codes}

SPECS: dict[str, ContractSpec] = {
    s.code: replace(s, group=_GROUP_OF.get(s.code, "Other"),
                    ref_price=_REF_PRICES.get(s.code, 0.0))
    for s in _SPECS_LIST
}


@dataclass(frozen=True)
class Contract:
    """One listed contract: the front month of some product."""
    code: str                     # product code, e.g. 'ES'
    symbol: str                   # Globex symbol, e.g. 'ESZ6'
    year: int
    month: int
    expiry: date
    spec: ContractSpec

    @property
    def label(self) -> str:
        return f"{self.symbol} ({calendar.month_abbr[self.month]} {self.year})"


# --------------------------------------------------------------- calendar ----
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _is_business_day(d: date) -> bool:
    return d.weekday() < 5           # weekends only; exchange holidays not modeled


def _add_business_days(d: date, n: int) -> date:
    """Move `n` business days (negative = backwards)."""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if _is_business_day(d):
            remaining -= 1
    return d


def _business_day_on_or_before(d: date) -> date:
    while not _is_business_day(d):
        d -= timedelta(days=1)
    return d


def _last_business_day(year: int, month: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return _business_day_on_or_before(last)


def _prior_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def expiry_date(spec: ContractSpec, year: int, month: int) -> date:
    """Last trade date for one contract month, per the product's rule."""
    rule = spec.expiry_rule
    if rule == "third_friday":                     # equity index
        return _nth_weekday(year, month, 4, 3)
    if rule == "fx":                               # 2 business days before 3rd Wed
        return _add_business_days(_nth_weekday(year, month, 2, 3), -2)
    if rule == "treasury":                         # 7 business days before month end
        return _add_business_days(_last_business_day(year, month), -7)
    if rule == "grain":                            # business day before the 15th
        return _business_day_on_or_before(date(year, month, 15) - timedelta(days=1))
    if rule == "crude":                            # 3 business days before the 25th
        py, pm = _prior_month(year, month)         #   of the month *before* delivery
        return _add_business_days(_business_day_on_or_before(date(py, pm, 25)), -3)
    if rule == "prior_month_end":                  # last business day of prior month
        return _last_business_day(*_prior_month(year, month))
    if rule == "metal":                            # 3rd-to-last business day of prior month
        py, pm = _prior_month(year, month)
        return _add_business_days(_last_business_day(py, pm), -2)
    raise ValueError(f"unknown expiry rule {rule!r} for {spec.code}")


def month_code(month: int) -> str:
    return MONTH_CODES[month - 1]


def contract_symbol(code: str, year: int, month: int) -> str:
    """Globex symbol: root + month code + last digit of the year (ESZ6)."""
    return f"{code}{month_code(month)}{year % 10}"


def front_month(code: str, today: date | None = None) -> Contract:
    """The nearest listed contract that has not stopped trading.

    Walks the product's contract months forward from today and returns the first
    whose last trade date is on or after today — so on expiry day you are still
    on that contract, and the day after you have rolled.
    """
    spec = get_spec(code)
    today = today or date.today()
    year = today.year
    for _ in range(3):                             # at most ~3 years of listings
        for month in spec.cycle:
            if expiry_date(spec, year, month) >= today:
                return Contract(spec.code, contract_symbol(spec.code, year, month),
                                year, month, expiry_date(spec, year, month), spec)
        year += 1
    raise ValueError(f"no front month found for {code}")


def get_spec(code: str) -> ContractSpec:
    try:
        return SPECS[code.upper()]
    except KeyError:
        raise ValueError(
            f"unknown product {code!r}; known products: "
            f"{', '.join(sorted(SPECS))}") from None


def main():
    ap = argparse.ArgumentParser(description="List futures products and front months")
    ap.add_argument("codes", nargs="*", help="product codes (default: all)")
    args = ap.parse_args()
    codes = [c.upper() for c in args.codes] or sorted(SPECS)
    print(f"{'code':<5} {'product':<24} {'front':<8} {'expiry':<12} "
          f"{'tick':<10} {'$/tick':>8}")
    for c in codes:
        fm = front_month(c)
        s = fm.spec
        print(f"{s.code:<5} {s.name:<24} {fm.symbol:<8} {fm.expiry.isoformat():<12} "
              f"{s.tick:<10.7g} {s.tick_value:>8.4g}")


if __name__ == "__main__":
    main()
