#!/usr/bin/env python3
"""
Homeland Ranch -- Friday sexed-semen breeding calculator.

Rebuilt 2026-09-11 after the original source was lost (only a compiled
.exe survived). Reconstructed from hr_run_log.txt history, the live
hr_breeding.db (which still has full farm_params history), and the raw
DC305 CSV exports. See CHANGELOG below for what was deliberately changed
from the old behavior and why.

CHANGELOG vs. the old tool (all confirmed from hr_run_log.txt / hr_breeding.db):

1. Non-determinism fix: the old tool produced different LACT-group splits
   on identical input data across repeated runs (proven 2026-05-05, three
   runs on the same CSVs gave 170/0/0, 46/26/27, 54/23/24). Every
   candidate ranking below sorts on an explicit tuple ending in cow ID,
   so results are 100% reproducible for the same input files.

2. LACT-quota starvation fix: farm_params.fill_l1_first was set to 1 on
   2026-05-05 ("fill all eligible LACT 1 first then split remainder").
   Since then, hr_run_log.txt shows LACT 2 / LACT 3+ coming up SHORT by
   20-65 head almost every single week while LACT 1 always hit 100%.
   This is the single biggest reason the ranch has been undershooting its
   monthly pregnancy target. Fixed by going back to proportional quotas
   (the farm's own 65/25/10 sexed_quota_l*_pct params, still in the DB
   but unused since 5/5) with automatic spillover: if a group can't fill
   its quota, the unused slots flow to whichever other group(s) still
   have eligible candidates, so the cow-slot budget is never wasted.

3. Heifer-volume floor fix: cow_pregs_needed = weekly_target -
   heifer_pregs_expected, with heifer_pregs_expected computed from a
   single week's heifer-breeding count. On 2026-09-10 a high heifer week
   (179 head) exceeded the entire weekly target by itself, zeroing the
   ENTIRE cow quota for that week. Fixed with (a) a rolling 3-week
   average of heifer breedings instead of a single snapshot, and (b) a
   floor so cows always get at least cow_pregs_floor_pct of the weekly
   target even in an extreme heifer week.

4. pregs_monthly_target updated 360 -> 380 per user request.

Everything else (tier classification, low-prod exclusion, elite
overrides, DIM/TBRD rules) is reproduced as faithfully as possible from
the tier labels recovered from sir_assignments (L1_T0, L1_T1, L1_T2_AN,
L1_LOWPROD, HFR, NONH, BEEF, DNB, PRESERVED, UNKNOWN) and the exact
farm_params values in hr_breeding.db.
"""

from __future__ import annotations

import configparser
import csv
import datetime as dt
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from statistics import mean

# When frozen by PyInstaller (--onefile), __file__ resolves to the
# transient _MEIxxxxx extraction dir, and the exe itself is deployed to
# HRBreeding/ (separate from the data in Dropbox/breeding/Calculator) --
# the scheduled task sets WorkingDirectory=Calculator specifically so the
# frozen exe can find its files via cwd. Use that; fall back to the
# script's own directory when run directly as a .py.
if getattr(sys, "frozen", False):
    BASE_DIR = Path.cwd().resolve()
else:
    BASE_DIR = Path(__file__).resolve().parent      # .../breeding/Calculator
BREEDING_DIR = BASE_DIR.parent                       # .../breeding
COWS_CSV = BREEDING_DIR / "COWS.CSV"
BREED_CSV = BREEDING_DIR / "BREED.CSV"
SEXED_CSV = BREEDING_DIR / "SEXED.CSV"
CULL_CSV = BREEDING_DIR / "CULL.CSV"
PROJECTIONS_DIR = BREEDING_DIR / "Projections"
PROJECTIONS_CSV = PROJECTIONS_DIR / "Homeland Ranch Breeding Assignments.csv"

DB_PATH = BASE_DIR / "hr_breeding.db"
CONFIG_PATH = BASE_DIR / "hr_config.ini"
RUN_LOG_PATH = BASE_DIR / "hr_run_log.txt"
ASSIGNMENTS_HTML = BASE_DIR / "hr_assignments.html"
DASHBOARD_HTML = BASE_DIR / "hr_dashboard.html"

# Local clone that mirrors github.com/cowdoc-coder/homeland-ranch-breeding
# and backs the GitHub Pages site. Overridable via hr_config.ini [github] repo_path.
DEFAULT_GIT_REPO_DIR = Path(r"C:\GitHub\homeland-ranch-breeding")

FARM = "Homeland Ranch"

# ---------------------------------------------------------------------------
# Tunable parameters. Defaults mirror the latest values recovered from
# hr_breeding.db's farm_params table, with the fixes described above.
# Any of these can be overridden by DB history (latest row per name wins)
# or by hr_config.ini [params].
# ---------------------------------------------------------------------------
DEFAULT_PARAMS = {
    "pregs_monthly_target": 380.0,      # was 360 -- user asked for 380
    "use_pregs_mode": 1.0,
    "sexed_hfr_cr_penalty_pct": 4.0,
    "sexed_cow_cr_penalty_pct": 6.0,
    "sexed_quota_l1_pct": 65.0,
    "sexed_quota_l2_pct": 25.0,
    "sexed_quota_l3_pct": 10.0,
    "fill_l1_first": 0.0,               # was 1 -- this starved L2/L3+ for months
    "cow_pregs_floor_pct": 35.0,        # NEW -- floor so cows are never zeroed out
    "heifer_smoothing_weeks": 3.0,      # NEW -- rolling avg instead of 1-wk snapshot
    "prod_exclude_l1_pct": 15.0,
    "prod_exclude_l2_pct": 15.0,
    "prod_exclude_l3_pct": 15.0,
    "prod_sparse_n_max": 2.0,
    "prod_sparse_pct_l1": 10.0,
    "prod_sparse_pct_l2": 10.0,
    "prod_sparse_pct_l3": 10.0,
    "elite_milk_pct": 25.0,
    "allow_lact4_t0_elite": 1.0,
    "allow_tbrd3_elite": 1.0,
    "max_sexed_svc_hfr": 4.0,
    "fresh_prestage_dim": 50.0,
    "sexed_assign_buffer": 15.0,
    "first_service_dim_lo": 70.0,
    "first_service_dim_hi": 76.0,
    "tai_dssyd_lo": 33.0,
    "tai_dssyd_hi": 38.0,
    "beef_sire_prefixes": "700AN",
    "extra_heifers_monthly": 0.0,
}


def month_variance_buffer(month: int) -> int:
    """Seasonal CI buffer % observed in hr_run_log.txt (17% Apr, 20% May-Sep)."""
    return 17 if month == 4 else 20


# ---------------------------------------------------------------------------
# generic DC305 CSV block parsing
# ---------------------------------------------------------------------------

def _read_raw_rows(path: Path) -> list[list[str] | None]:
    rows: list[list[str] | None] = []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                rows.append(None)
                continue
            parts = [p.strip().strip('"').lstrip("*").strip() for p in line.split(",")]
            while parts and parts[-1] == "":
                parts.pop()
            rows.append(parts)
    return rows


def _to_int(s: str) -> int:
    s = s.strip().lstrip("*").strip()
    if s in ("", "-"):
        return 0
    return int(re.sub(r"[^\d-]", "", s) or 0)


def _to_float(s: str) -> float:
    s = s.strip().lstrip("*").strip()
    if s in ("", "-"):
        return 0.0
    try:
        return float(re.sub(r"[^\d.\-]", "", s) or 0)
    except ValueError:
        return 0.0


MONTH_NAMES = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def parse_cull_csv(path: Path) -> dict:
    """Returns herd_size, annual cull rate %, early cull rate %, sold/died totals."""
    rows = _read_raw_rows(path)
    event_blocks = []
    count_values = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if row and row[0] == "Event":
            block = {"header": row, "data": {}}
            i += 1
            while i < len(rows) and rows[i] and rows[i][0] not in ("Event", "Count"):
                r = rows[i]
                block["data"][r[0]] = [_to_int(x) for x in r[1:]]
                i += 1
            event_blocks.append(block)
            continue
        if row and row[0] == "Count":
            i += 1
            if i < len(rows) and rows[i]:
                count_values.append(_to_int(rows[i][0]))
            i += 1
            continue
        i += 1

    herd_size = count_values[0] if count_values else 0

    # the canonical annual block is the LAST block that has a FRESH row
    annual_block = None
    early_block = None
    for b in event_blocks:
        if "FRESH" in b["data"]:
            annual_block = b
        elif early_block is None:
            early_block = b

    sold_total = annual_block["data"]["SOLD"][0] if annual_block else 0
    died_total = annual_block["data"]["DIED"][0] if annual_block else 0
    fresh_total = annual_block["data"]["FRESH"][0] if annual_block else herd_size
    annual_cull_pct = 100.0 * (sold_total + died_total) / herd_size if herd_size else 0.0

    early_count = 0
    if early_block:
        early_count = early_block["data"].get("SOLD", [0])[0] + early_block["data"].get("DIED", [0])[0]
    early_cull_pct = 100.0 * early_count / fresh_total if fresh_total else 0.0

    return {
        "herd_size": herd_size,
        "annual_cull_pct": annual_cull_pct,
        "early_cull_pct": early_cull_pct,
        "early_count": early_count,
        "fresh_total": fresh_total,
        "sold_total": sold_total,
        "died_total": died_total,
    }


def parse_sexed_csv(path: Path) -> dict:
    """Returns {'cow': {(year,month): (n_preg, n_total)}, 'hfr': {...}}."""
    rows = _read_raw_rows(path)
    month_tables = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if row and row[0] == "Month" and len(row) >= 8 and row[1] == "95% CI":
            table = {}
            i += 1
            while i < len(rows) and rows[i] and rows[i][0] not in (
                "Month", "By MODUE", "By TBRD", "95% CI", "Calf Sex", "Count"
            ):
                r = rows[i]
                m = re.match(r"(\d{4})\s+(\w+)", r[0])
                if m:
                    year = int(m.group(1))
                    mon_name = m.group(2)
                    month = MONTH_NAMES.get(mon_name)
                    if month:
                        n_preg = _to_int(r[3]) if len(r) > 3 else 0
                        n_total = _to_int(r[7]) if len(r) > 7 else 0
                        table[(year, month)] = (n_preg, n_total)
                i += 1
            month_tables.append(table)
            continue
        i += 1

    cow_table = month_tables[0] if len(month_tables) >= 1 else {}
    hfr_table = month_tables[1] if len(month_tables) >= 2 else {}
    return {"cow": cow_table, "hfr": hfr_table}


def three_year_cr(table: dict, ref_year: int, ref_month: int) -> tuple[float, int, int]:
    """Weighted (pooled) conception rate for calendar month `ref_month`
    across the 3 most recent years that have data for that month."""
    entries = []
    for back in range(1, 6):
        y = ref_year - back
        if (y, ref_month) in table:
            entries.append(table[(y, ref_month)])
        if len(entries) == 3:
            break
    if not entries:
        return 0.0, 0, 0
    n_preg = sum(e[0] for e in entries)
    n_total = sum(e[1] for e in entries)
    pct = 100.0 * n_preg / n_total if n_total else 0.0
    return pct, n_preg, n_total


def parse_breed_csv_heifer_week(path: Path) -> tuple[int, int]:
    """Heifer (Lact=0) sexed/beef bred in the last 7 days.
    This is the LAST 'By TBRD' block in BREED.CSV -- confirmed against
    hr_run_log.txt (matched exactly: 179 sexed / 13 beef on 2026-09-10)."""
    rows = _read_raw_rows(path)
    last_block = None
    i = 0
    while i < len(rows):
        row = rows[i]
        if row and row[0] == "By TBRD" and any("CSEX" in c for c in row):
            cols = row
            block_rows = []
            i += 1
            while i < len(rows) and rows[i] and rows[i][0] not in ("By TBRD", "By MODUE", "95% CI", "Count"):
                block_rows.append(rows[i])
                i += 1
            last_block = (cols, block_rows)
            continue
        i += 1

    if not last_block:
        return 0, 0
    cols, block_rows = last_block
    try:
        s_idx = cols.index("CSEX=S")
        b_idx = cols.index("CSEX=B")
    except ValueError:
        return 0, 0
    for r in block_rows:
        if r[0].strip().lower() == "total":
            sexed = _to_int(r[s_idx]) if s_idx < len(r) else 0
            beef = _to_int(r[b_idx]) if b_idx < len(r) else 0
            return sexed, beef
    return 0, 0


# ---------------------------------------------------------------------------
# COWS.CSV
# ---------------------------------------------------------------------------

class Cow:
    __slots__ = ("id", "lact", "cbrd", "dim", "sir1", "rpro", "tbrd", "ptbrd",
                 "wmlk1", "peak", "pdopn", "dssyd", "w4mk", "w8mk", "w12mk", "w24mk")

    def __init__(self, row: list[str]):
        self.id = row[0].strip()
        self.lact = _to_int(row[1])
        self.cbrd = row[2].strip()
        self.dim = _to_int(row[3])
        self.sir1 = row[4].strip()
        self.rpro = row[5].strip()
        self.tbrd = _to_int(row[6])
        self.ptbrd = _to_int(row[7])
        self.wmlk1 = _to_float(row[8])
        self.peak = _to_float(row[9])
        self.pdopn = _to_float(row[10])
        self.dssyd = _to_int(row[11])
        self.w4mk = _to_float(row[12])
        self.w8mk = _to_float(row[13])
        self.w12mk = _to_float(row[14])
        self.w24mk = _to_float(row[15]) if len(row) > 15 else 0.0

    def milk_score(self) -> tuple[float, int]:
        vals = [v for v in (self.wmlk1, self.peak, self.w4mk, self.w8mk, self.w12mk, self.w24mk) if v]
        if not vals:
            return 0.0, 0
        return mean(vals), len(vals)

    def lact_group(self) -> int:
        return 3 if self.lact >= 3 else self.lact


def parse_cows_csv(path: Path) -> list[Cow]:
    rows = _read_raw_rows(path)
    cows = []
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        try:
            cows.append(Cow(r))
        except (IndexError, ValueError):
            continue
    return cows


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS farm_params (
    id INTEGER PRIMARY KEY,
    farm TEXT NOT NULL DEFAULT 'Homeland Ranch',
    param_name TEXT NOT NULL,
    param_value REAL NOT NULL,
    effective_date TEXT NOT NULL DEFAULT (date('now')),
    notes TEXT
);
CREATE TABLE IF NOT EXISTS sires (
    sire_code TEXT PRIMARY KEY,
    name_short TEXT NOT NULL,
    sire_type TEXT NOT NULL,
    breed TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS weekly_breedings (
    id INTEGER PRIMARY KEY,
    farm TEXT NOT NULL DEFAULT 'Homeland Ranch',
    iso_week TEXT NOT NULL,
    week_start TEXT NOT NULL,
    sire_code TEXT NOT NULL,
    lact_group INTEGER NOT NULL,
    bred_count INTEGER NOT NULL DEFAULT 0,
    source TEXT DEFAULT 'BREED.CSV',
    notes TEXT,
    logged_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS friday_targets (
    id INTEGER PRIMARY KEY,
    farm TEXT NOT NULL DEFAULT 'Homeland Ranch',
    iso_week TEXT NOT NULL,
    run_date TEXT NOT NULL,
    breed_month INTEGER NOT NULL,
    hfr_sexed_bred INTEGER,
    hfr_beef_bred INTEGER,
    lact1_sexed_bred INTEGER,
    lact2plus_sexed_bred INTEGER,
    cow_sexed_target INTEGER,
    cow_sexed_actual INTEGER,
    hfr_cr_used REAL,
    cow_cr_used REAL,
    herd_size_used INTEGER,
    cull_rate_used REAL,
    notes TEXT,
    logged_at TEXT NOT NULL DEFAULT (datetime('now')),
    hfr_sexed_planned INTEGER,
    lact1_sexed_planned INTEGER,
    lact2_sexed_planned INTEGER,
    lact3plus_sexed_planned INTEGER
);
CREATE TABLE IF NOT EXISTS cr_history (
    id INTEGER PRIMARY KEY,
    farm TEXT NOT NULL DEFAULT 'Homeland Ranch',
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    group_type TEXT NOT NULL,
    cr_pct REAL NOT NULL,
    ci_lo REAL,
    ci_hi REAL,
    n_preg INTEGER,
    n_open INTEGER,
    n_other INTEGER,
    n_abort INTEGER,
    n_total INTEGER,
    spc REAL,
    excluded INTEGER NOT NULL DEFAULT 0,
    exclude_reason TEXT,
    source TEXT DEFAULT 'SEXED.CSV',
    logged_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sir_assignments (
    id INTEGER PRIMARY KEY,
    farm TEXT NOT NULL DEFAULT 'Homeland Ranch',
    iso_week TEXT NOT NULL,
    run_date TEXT NOT NULL,
    cow_id TEXT NOT NULL,
    lact INTEGER,
    dim INTEGER,
    rpro TEXT,
    tbrd INTEGER,
    ptbrd INTEGER,
    dssyd INTEGER,
    peak REAL,
    wmlk1 REAL,
    cbrd TEXT,
    sir1_prior TEXT,
    sir1_new TEXT,
    tier TEXT,
    mandatory INTEGER,
    in_tai_cohort INTEGER,
    logged_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def load_params(con: sqlite3.Connection, config: configparser.ConfigParser) -> dict:
    params = dict(DEFAULT_PARAMS)
    cur = con.execute(
        "SELECT param_name, param_value FROM farm_params "
        "WHERE id IN (SELECT MAX(id) FROM farm_params GROUP BY param_name)"
    )
    for name, value in cur.fetchall():
        if name in params and name not in ("pregs_monthly_target", "fill_l1_first",
                                             "cow_pregs_floor_pct", "heifer_smoothing_weeks"):
            # Deliberately do NOT inherit fill_l1_first=1 from history -- that's the bug we're fixing.
            params[name] = value
    if config.has_section("settings") and config.has_option("settings", "pregs_monthly_target"):
        params["pregs_monthly_target"] = float(config.get("settings", "pregs_monthly_target"))
    return params


def save_param(con: sqlite3.Connection, name: str, value: float, notes: str, today: str):
    con.execute(
        "INSERT INTO farm_params (farm, param_name, param_value, effective_date, notes) VALUES (?,?,?,?,?)",
        (FARM, name, value, today, notes),
    )


def recent_heifer_bred(con: sqlite3.Connection, weeks: int) -> list[int]:
    cur = con.execute(
        "SELECT bred_count FROM weekly_breedings WHERE lact_group=0 AND sire_code='SEXED_AGG' "
        "ORDER BY id DESC LIMIT ?",
        (weeks,),
    )
    return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Friday target calculation
# ---------------------------------------------------------------------------

def compute_friday_target(con, params, cull, sexed_hist, hfr_sexed_this_week, log):
    today = dt.date.today()
    month = today.month
    monthly_target = params["pregs_monthly_target"]
    weekly_target = monthly_target / (365.25 / 12 / 7)

    cow_pct, cow_npreg, cow_ntotal = three_year_cr(sexed_hist["cow"], today.year, month)
    hfr_pct, hfr_npreg, hfr_ntotal = three_year_cr(sexed_hist["hfr"], today.year, month)
    buffer_pct = month_variance_buffer(month)

    hfr_effective_cr = hfr_pct - params["sexed_hfr_cr_penalty_pct"]
    cow_effective_cr = cow_pct - params["sexed_cow_cr_penalty_pct"]

    # FIX: rolling average of last N weeks' heifer breeding volume instead of
    # a single snapshot, so one unusually heavy heifer week can't wipe out
    # the entire cow allocation.
    history = recent_heifer_bred(con, int(params["heifer_smoothing_weeks"]) - 1)
    smoothing_window = history + [hfr_sexed_this_week]
    hfr_bred_smoothed = mean(smoothing_window)

    hfr_pregs_expected = hfr_bred_smoothed * (hfr_effective_cr / 100.0)

    cow_pregs_needed_raw = weekly_target - hfr_pregs_expected
    cow_pregs_floor = weekly_target * (params["cow_pregs_floor_pct"] / 100.0)
    cow_pregs_needed = max(cow_pregs_needed_raw, cow_pregs_floor)

    base_friday_target = cow_pregs_needed / (cow_effective_cr / 100.0) if cow_effective_cr > 0 else 0
    friday_target = base_friday_target * (1 + buffer_pct / 100.0)
    ci_lo = friday_target * (1 - buffer_pct / 200.0)
    ci_hi = friday_target * (1 + buffer_pct / 200.0)

    log(f"  CR reference:           PREGS MODE -- target {monthly_target:.0f}/mo "
        f"({weekly_target:.1f}/wk) | SEXED.CSV 3yr avg ({today.strftime('%b')}, buf={buffer_pct}% seasonal)")
    log(f"  Herd size:              {cull['herd_size']:,}")
    log(f"  Cull rate:              {cull['annual_cull_pct']:.1f}%")
    log("")
    log("  *** PREGS MODE ***")
    log(f"  Monthly pregs target:   {monthly_target:.0f} confirmed sexed pregs")
    log(f"  Weekly pregs target:    {weekly_target:.1f} confirmed sexed pregs/wk")
    log("")
    log(f"  Heifer sexed bred (this wk): {hfr_sexed_this_week}")
    log(f"  Heifer sexed bred ({int(params['heifer_smoothing_weeks'])}-wk avg): {hfr_bred_smoothed:.1f}  "
        f"[FIX: was single-week snapshot]")
    log(f"  Heifer CR (3yr {today.strftime('%b')}): {hfr_pct:.1f}% observed -> {hfr_effective_cr:.1f}% effective "
        f"(-{params['sexed_hfr_cr_penalty_pct']:.1f}%)")
    log(f"  Heifer pregs expected:  {hfr_pregs_expected:.1f}")
    log(f"  Cow CR (3yr {today.strftime('%b')} observed): {cow_pct:.1f}%")
    log(f"  Sexed-semen penalty:    -{params['sexed_cow_cr_penalty_pct']:.1f}%  -->  "
        f"{cow_effective_cr:.1f}% effective cow CR")
    log(f"  Cow pregs needed (raw): {cow_pregs_needed_raw:.1f}  |  floor "
        f"({params['cow_pregs_floor_pct']:.0f}%): {cow_pregs_floor:.1f}  -->  used: {cow_pregs_needed:.1f}")
    log("")
    log(f"  Base Friday target:     {base_friday_target:.0f} cows")
    log(f"  Friday target: {friday_target:.0f} cows  ({buffer_pct}% buffer applied)")
    log(f"  95% CI range:           {ci_lo:.0f} - {ci_hi:.0f} cows")

    return {
        "weekly_target": weekly_target,
        "friday_target": round(friday_target),
        "hfr_pct": hfr_pct, "cow_pct": cow_pct,
        "hfr_effective_cr": hfr_effective_cr, "cow_effective_cr": cow_effective_cr,
        "hfr_pregs_expected": hfr_pregs_expected,
    }


# ---------------------------------------------------------------------------
# SIR1 classification / assignment
# ---------------------------------------------------------------------------

class Candidate:
    __slots__ = ("cow", "lact_group", "tbrd_tier", "score", "n_readings", "elite")

    def __init__(self, cow: Cow, lact_group: int, tbrd_tier: int, score: float, n_readings: int, elite: bool):
        self.cow = cow
        self.lact_group = lact_group
        self.tbrd_tier = tbrd_tier
        self.score = score
        self.n_readings = n_readings
        self.elite = elite


def classify_and_assign(cows: list[Cow], params: dict, log):
    beef_prefix = params["beef_sire_prefixes"]
    fresh_dim_cut = params["fresh_prestage_dim"]
    max_hfr_svc = params["max_sexed_svc_hfr"]
    fs_dim_lo, fs_dim_hi = params["first_service_dim_lo"], params["first_service_dim_hi"]

    results = {}   # cow_id -> (sir1, tier, mandatory, in_tai_cohort)
    preserved = 0
    dnb = 0
    nonh = 0
    beef_ct = 0
    hfr_sexed = 0
    hfr_beef = 0
    first_service_total = 0
    first_service_sexed = 0
    tai_cohort_ct = 0
    tai_sexed_ct = 0

    prod_pool = {1: [], 2: [], 3: []}   # lact_group -> [(score, n_readings), ...] among candidates
    group_candidates: dict[int, list[Candidate]] = {1: [], 2: [], 3: []}

    for cow in cows:
        rpro = cow.rpro
        in_tai = 1 if (rpro == "OK/OPEN" and params["tai_dssyd_lo"] <= cow.dssyd <= params["tai_dssyd_hi"]) else 0
        is_first_service_window = (cow.tbrd == 0 and fs_dim_lo <= cow.dim <= fs_dim_hi)

        if rpro in ("PREG", "BRED"):
            results[cow.id] = (cow.sir1, "PRESERVED", 0, in_tai)
            preserved += 1
            continue
        if rpro == "FRESH":
            # NOTE: fresh_prestage_dim exists as a param but the recovered
            # sir_assignments tier history shows FRESH is *always* PRESERVED
            # in practice (no distinct "re-evaluated FRESH" tier ever
            # appears) -- DC305 flips RPRO to OK/OPEN itself once a cow
            # clears the voluntary wait period. Treating FRESH+DIM>=cutoff
            # as a full candidate here massively over-counted the pool
            # (1,807 FRESH rows vs. only 197 true OK/OPEN rows) and blew
            # LACT-group quotas way past their real eligible pool sizes.
            results[cow.id] = (cow.sir1, "PRESERVED", 0, in_tai)
            preserved += 1
            continue
        if rpro == "NO BRED":
            results[cow.id] = ("-", "DNB", 0, in_tai)
            dnb += 1
            continue

        if rpro == "HEIFER" or (rpro == "OK/OPEN" and cow.lact == 0):
            if cow.cbrd != "H":
                results[cow.id] = ("AN", "HFR_BEEF", 0, in_tai)
                hfr_beef += 1
            elif cow.tbrd >= max_hfr_svc:
                results[cow.id] = ("AN", "HFR_BEEF", 0, in_tai)
                hfr_beef += 1
            else:
                results[cow.id] = ("SEXED", "HFR", 1, in_tai)
                hfr_sexed += 1
            continue

        if rpro == "OK/OPEN":
            if cow.cbrd != "H":
                results[cow.id] = ("AN", "NONH", 0, in_tai)
                nonh += 1
                continue

            lg = cow.lact_group()  # 1, 2, or 3 (3 = LACT>=3)
            score, n_readings = cow.milk_score()

            if is_first_service_window:
                first_service_total += 1

            if in_tai:
                tai_cohort_ct += 1

            if cow.tbrd <= 1:
                tier_n = cow.tbrd  # 0 or 1
                elite_thresh_key = f"prod_exclude_l{lg}_pct" if lg <= 2 else "prod_exclude_l3_pct"
                sparse = n_readings <= params["prod_sparse_n_max"]
                pct_key = f"prod_sparse_pct_l{lg}" if sparse and lg <= 3 else elite_thresh_key
                cand = Candidate(cow, lg, tier_n, score, n_readings, elite=False)
                group_candidates[lg].append(cand)
                prod_pool[lg].append(score)
            else:
                # TBRD >= 2: normally AN, narrow elite exception at TBRD==3
                score, n_readings = cow.milk_score()
                elite = False
                if cow.tbrd == 3 and params["allow_tbrd3_elite"]:
                    elite = True
                elif lg == 3 and cow.tbrd == 0 and params["allow_lact4_t0_elite"]:
                    elite = True
                if elite:
                    cand = Candidate(cow, lg, cow.tbrd, score, n_readings, elite=True)
                    group_candidates[lg].append(cand)
                    prod_pool[lg].append(score)
                else:
                    results[cow.id] = ("AN", f"L{lg}_T2_AN", 0, in_tai)
            continue

        results[cow.id] = (cow.sir1 or "-", "UNKNOWN", 0, in_tai)

    # --- low-production exclusion, computed per lactation group on the candidate pool ---
    lowprod_cut = {}
    for lg in (1, 2, 3):
        scores = sorted(s for s in prod_pool[lg] if s > 0)
        pct = params[f"prod_exclude_l{lg}_pct"] / 100.0
        idx = int(len(scores) * pct)
        lowprod_cut[lg] = scores[idx] if scores and idx < len(scores) else 0.0

    lowprod_excluded = {1: 0, 2: 0, 3: 0}
    ranked_pool: dict[int, list[Candidate]] = {1: [], 2: [], 3: []}

    for lg in (1, 2, 3):
        for cand in group_candidates[lg]:
            sparse = cand.n_readings <= params["prod_sparse_n_max"]
            cutoff = lowprod_cut[lg]
            sparse_pct = params[f"prod_sparse_pct_l{lg}"] / 100.0
            scores_all = sorted(s for s in prod_pool[lg] if s > 0)
            sparse_idx = int(len(scores_all) * sparse_pct)
            sparse_cutoff = scores_all[sparse_idx] if scores_all and sparse_idx < len(scores_all) else 0.0
            effective_cutoff = sparse_cutoff if sparse else cutoff

            if cand.score and cand.score < effective_cutoff and not cand.elite:
                results[cand.cow.id] = ("AN", f"L{lg}_LOWPROD", 0,
                                          1 if (cand.cow.rpro == "OK/OPEN" and params["tai_dssyd_lo"] <= cand.cow.dssyd <= params["tai_dssyd_hi"]) else 0)
                lowprod_excluded[lg] += 1
            else:
                ranked_pool[lg].append(cand)

    # sort deterministically: T0 first (near-mandatory), then score desc, then ID asc as tiebreak
    for lg in (1, 2, 3):
        ranked_pool[lg].sort(key=lambda c: (c.tbrd_tier, -c.score, c.cow.id))

    return {
        "results": results,
        "ranked_pool": ranked_pool,
        "preserved": preserved, "dnb": dnb, "nonh": nonh,
        "hfr_sexed": hfr_sexed, "hfr_beef": hfr_beef,
        "first_service_total": first_service_total,
        "tai_cohort_ct": tai_cohort_ct,
        "lowprod_excluded": lowprod_excluded,
        "lowprod_cut": lowprod_cut,
    }


def allocate_cow_quotas(ranked_pool: dict, cow_slot_budget: int, params: dict, log):
    """Proportional quota (65/25/10 by default) WITH spillover: if a group
    can't fill its share, the unused slots flow to groups that still have
    eligible candidates. This is the fix for the chronic LACT2/LACT3+
    starvation caused by the old fill_l1_first=1 behavior."""
    base_pct = {1: params["sexed_quota_l1_pct"], 2: params["sexed_quota_l2_pct"], 3: params["sexed_quota_l3_pct"]}
    total_pct = sum(base_pct.values()) or 1.0
    quota = {lg: cow_slot_budget * (base_pct[lg] / total_pct) for lg in (1, 2, 3)}

    assigned = {1: 0, 2: 0, 3: 0}
    remaining_budget = cow_slot_budget
    groups = [1, 2, 3]

    # water-filling: repeatedly hand each group min(quota, available), then
    # redistribute any shortfall to groups that still have spare candidates
    for _round in range(4):
        any_progress = False
        active_groups = [lg for lg in groups if assigned[lg] < len(ranked_pool[lg]) and remaining_budget > 0]
        if not active_groups:
            break
        active_total_pct = sum(base_pct[lg] for lg in active_groups) or 1.0
        for lg in active_groups:
            share = remaining_budget * (base_pct[lg] / active_total_pct)
            take = min(round(share), len(ranked_pool[lg]) - assigned[lg], remaining_budget)
            if take > 0:
                assigned[lg] += take
                remaining_budget -= take
                any_progress = True
        if not any_progress:
            break

    # any leftover budget (all groups exhausted their candidate pools) is simply unused
    realised_total = sum(assigned.values()) or 1
    realised_pct = {lg: round(100 * assigned[lg] / realised_total) for lg in (1, 2, 3)}

    log(f"  Quota mode: proportional-with-spillover ({base_pct[1]:.0f}/{base_pct[2]:.0f}/{base_pct[3]:.0f}% base) "
        f"-- realised split {realised_pct[1]}/{realised_pct[2]}/{realised_pct[3]}% (L1/L2/L3+):")
    for lg, label in ((1, "LACT 1 "), (2, "LACT 2 "), (3, "LACT 3+")):
        q = round(quota[lg])
        a = assigned[lg]
        short = f"  (SHORT {q - a})" if a < q else ""
        log(f"    {label}: quota {q}, assigned {a}{short}")

    return assigned


# ---------------------------------------------------------------------------
# output writers
# ---------------------------------------------------------------------------

def write_projections_csv(cows: list[Cow], results: dict):
    PROJECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROJECTIONS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ID", "SIR1"])
        for cow in cows:
            sir1 = results.get(cow.id, (cow.sir1, None, 0, 0))[0]
            w.writerow([cow.id, sir1])


def write_db_snapshot(con, iso_week, run_date, cows, results):
    # idempotent: rerunning the same iso_week (common -- the log shows
    # several same-day reruns) replaces that week's snapshot instead of
    # violating the (farm, iso_week, cow_id) unique index.
    con.execute("DELETE FROM sir_assignments WHERE farm=? AND iso_week=?", (FARM, iso_week))
    for cow in cows:
        sir1_new, tier, mandatory, in_tai = results.get(cow.id, (cow.sir1, "UNKNOWN", 0, 0))
        con.execute(
            "INSERT INTO sir_assignments (farm, iso_week, run_date, cow_id, lact, dim, rpro, tbrd, ptbrd, "
            "dssyd, peak, wmlk1, cbrd, sir1_prior, sir1_new, tier, mandatory, in_tai_cohort) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (FARM, iso_week, run_date, cow.id, cow.lact, cow.dim, cow.rpro, cow.tbrd, cow.ptbrd,
             cow.dssyd, cow.peak, cow.wmlk1, cow.cbrd, cow.sir1, sir1_new, tier, mandatory, in_tai),
        )


def write_dashboard_html(context: dict):
    total_sexed = context["hfr_sexed"] + sum(context["assigned"].values())
    rows_html = "\n".join(
        f"<tr><td>{c.id}</td><td>{sir1}</td></tr>"
        for c in context["cows"][:5000]
        for sir1 in [context["results"].get(c.id, (c.sir1,))[0]]
    )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>HR SIR1 Assignments -- {context['iso_week']}</title>
<style>
 *{{box-sizing:border-box;margin:0;padding:0}}
 body{{background:#0d1b2a;color:#c8d8e8;font-family:'Segoe UI',Arial,sans-serif;font-size:13px}}
 .header{{background:#0a1520;padding:16px 28px}}
 .header h1{{font-size:18px;color:#e8f4ff}}
 .container{{padding:20px 24px}}
 .card{{background:#0f2035;border:1px solid #1a3a55;border-radius:10px;padding:16px 20px;margin-bottom:14px}}
 .metric-val{{font-size:26px;font-weight:700;color:#5ba8ff}}
 .metric-label{{font-size:11px;color:#4a7a9a;margin-top:4px}}
 .four-col{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}}
 table{{width:100%;border-collapse:collapse;font-size:12px}}
 th{{text-align:left;color:#3a6a8a;padding:8px;border-bottom:1px solid #1a3a55;position:sticky;top:0;background:#0a1828}}
 td{{padding:6px 8px;border-bottom:1px solid #122030}}
 .table-wrap{{max-height:70vh;overflow:auto;border:1px solid #1a3a55;border-radius:8px}}
 .note{{background:#132a1a;border:1px solid #2a5a3a;border-radius:8px;padding:10px 14px;margin-bottom:14px;color:#8ae8a0;font-size:12px}}
</style></head>
<body>
<div class="header"><h1>Homeland Ranch -- SIR1 Assignments</h1>
<p style="color:#4a7a9a;font-size:12px">Generated {context['generated']} &middot; Week {context['iso_week']}</p></div>
<div class="container">
<div class="note">Rebuilt {dt.date.today()}: proportional LACT quotas with spillover (fixes chronic LACT2/LACT3+ shortfall),
3-week heifer smoothing + floor (fixes zeroed cow quota), deterministic ranking (fixes run-to-run inconsistency),
monthly target set to {int(context['params']['pregs_monthly_target'])}.</div>
<div class="four-col">
  <div class="card"><div class="metric-val">{total_sexed}</div><div class="metric-label">Total SEXED assigned</div></div>
  <div class="card"><div class="metric-val" style="color:#5ac87a">{context['cow_slot_budget']}</div><div class="metric-label">Cow slot budget (target {context['target']['friday_target']} + buffer {int(context['params']['sexed_assign_buffer'])})</div></div>
  <div class="card"><div class="metric-val" style="color:#e8a030">{context['tai_cohort_ct']}</div><div class="metric-label">This week's TAI cohort (DSSYD {int(context['params']['tai_dssyd_lo'])}-{int(context['params']['tai_dssyd_hi'])})</div></div>
  <div class="card"><div class="metric-val" style="font-size:20px">{context['assigned'][1]}/{context['assigned'][2]}/{context['assigned'][3]}</div><div class="metric-label">SEXED by LACT (1/2/3+)</div></div>
</div>
<div class="card">
  <div style="margin-bottom:8px;color:#4a7a9a;font-size:11px;text-transform:uppercase">Assignments ({len(context['cows'])} cows)</div>
  <div class="table-wrap"><table><thead><tr><th>ID</th><th>SIR1</th></tr></thead><tbody>
  {rows_html}
  </tbody></table></div>
</div>
</div>
</body></html>"""
    ASSIGNMENTS_HTML.write_text(html, encoding="utf-8")
    DASHBOARD_HTML.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


DASHBOARD_URL = "https://cowdoc-coder.github.io/homeland-ranch-breeding"
DEFAULT_SMS_TEMPLATE = "Homeland Breeding: Friday target is {target} cows. Dashboard: {url}"


def send_sms(cfg, friday_target: int, log):
    if not cfg.has_section("settings") or cfg.get("settings", "enable_sms", fallback="false").lower() != "true":
        log("  SMS disabled (hr_config.ini [settings] enable_sms=false) -- skipping")
        return
    if not cfg.has_section("twilio") or not cfg.has_section("recipients"):
        return
    sid = cfg.get("twilio", "account_sid")
    token = cfg.get("twilio", "auth_token")
    from_number = cfg.get("twilio", "from_number")
    for name, number in cfg.items("recipients"):
        template = cfg.get("messages", name, fallback=DEFAULT_SMS_TEMPLATE)
        body = template.format(target=friday_target, url=DASHBOARD_URL)
        data = urllib.parse.urlencode({"To": number, "From": from_number, "Body": body}).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data
        )
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
        try:
            urllib.request.urlopen(req, timeout=15)
            log(f"  Text sent to {name} ({number}): {body}")
        except Exception as e:
            log(f"  SMS to {name} FAILED: {e}")


def _run_git(repo_dir: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_dir)] + args,
        capture_output=True, text=True, timeout=60,
    )


def publish_to_github(cfg, summary: str, log):
    """Mirrors the old bot's behavior: push the fresh dashboard HTML to the
    repo backing https://cowdoc-coder.github.io/homeland-ranch-breeding."""
    if not cfg.has_section("settings") or cfg.get("settings", "enable_github_publish", fallback="false").lower() != "true":
        log("  GitHub publish disabled (hr_config.ini [settings] enable_github_publish=false) -- skipping")
        return

    repo_dir = Path(cfg.get("github", "repo_path", fallback=str(DEFAULT_GIT_REPO_DIR)))
    if not (repo_dir / ".git").exists():
        log(f"  GitHub publish FAILED: no git repo at {repo_dir}")
        return

    try:
        shutil.copy(DASHBOARD_HTML, repo_dir / "hr_dashboard.html")
        shutil.copy(DASHBOARD_HTML, repo_dir / "index.html")
        shutil.copy(ASSIGNMENTS_HTML, repo_dir / "hr_assignments.html")

        add = _run_git(repo_dir, ["add", "hr_dashboard.html", "index.html", "hr_assignments.html"])
        if add.returncode != 0:
            log(f"  GitHub publish FAILED (git add): {add.stderr.strip()}")
            return

        msg = f"HR Breeding update {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} | {summary}"
        env_overrides = {"GIT_AUTHOR_NAME": "HR Breeding Bot", "GIT_AUTHOR_EMAIL": "noreply@homeland-ranch.local",
                          "GIT_COMMITTER_NAME": "HR Breeding Bot", "GIT_COMMITTER_EMAIL": "noreply@homeland-ranch.local"}
        commit = subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-m", msg],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, **env_overrides},
        )
        if commit.returncode != 0:
            if "nothing to commit" in commit.stdout:
                log("  GitHub publish: no changes to publish (dashboard identical to last run)")
                return
            log(f"  GitHub publish FAILED (git commit): {commit.stdout.strip()} {commit.stderr.strip()}")
            return

        push = _run_git(repo_dir, ["push", "origin", "HEAD"])
        if push.returncode != 0:
            log(f"  GitHub publish FAILED (git push): {push.stderr.strip()}")
            return

        log("  GitHub publish: deployed to https://cowdoc-coder.github.io/homeland-ranch-breeding")
    except Exception as e:
        log(f"  GitHub publish FAILED: {e}")


def main():
    log_lines = []

    def log(msg=""):
        ts = dt.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        line = f"{ts} {msg}" if msg else ""
        log_lines.append(line)
        print(line)

    today = dt.date.today()
    iso_year, iso_week_num, _ = today.isocalendar()
    iso_week = f"{iso_year}-W{iso_week_num:02d}"

    log("=" * 60)
    log(f"Friday run -- {FARM}")
    log(f"Week: {iso_week}   Date: {today.strftime('%A %B %d, %Y')}")
    log("=" * 60)

    cfg = load_config()
    con = open_db()
    params = load_params(con, cfg)

    log(f"Reading {CULL_CSV} ...")
    cull = parse_cull_csv(CULL_CSV)
    log(f"  Herd size:        {cull['herd_size']:,}")
    log(f"  Annual cull rate: {cull['annual_cull_pct']:.2f}%")
    log(f"  Early cull rate:  {cull['early_cull_pct']:.2f}%  ({cull['early_count']} of {cull['fresh_total']} fresh)")
    log(f"  Total sold: {cull['sold_total']:,}   Died: {cull['died_total']:,}")

    log(f"Reading {SEXED_CSV} ...")
    sexed_hist = parse_sexed_csv(SEXED_CSV)
    log(f"  {len(sexed_hist['cow'])} cow-months, {len(sexed_hist['hfr'])} heifer-months loaded")

    log(f"Reading {BREED_CSV} ...")
    hfr_sexed_wk, hfr_beef_wk = parse_breed_csv_heifer_week(BREED_CSV)
    log(f"  Lact=0 (last 7d): sexed {hfr_sexed_wk}  beef {hfr_beef_wk}")

    log("")
    log("-" * 58)
    log(f"  FRIDAY TARGET  --  {FARM}  --  {iso_week}")
    log("-" * 58)
    target = compute_friday_target(con, params, cull, sexed_hist, hfr_sexed_wk, log)
    log("-" * 58)

    run_date = today.isoformat()
    save_param(con, "cull_rate_pct", cull["annual_cull_pct"], f"CULL.CSV {run_date}", run_date)
    save_param(con, "herd_size", cull["herd_size"], f"CULL.CSV {run_date}", run_date)

    log("")
    log("-" * 58)
    log(f"  SIR1 ASSIGNMENTS  --  {FARM}  --  {iso_week}")
    log("-" * 58)
    log(f"Reading {COWS_CSV} ...")
    cows = parse_cows_csv(COWS_CSV)
    log(f"  {len(cows):,} roster rows loaded")

    classification = classify_and_assign(cows, params, log)
    results = classification["results"]
    ranked_pool = classification["ranked_pool"]

    cow_slot_budget = target["friday_target"] + int(params["sexed_assign_buffer"])
    assigned = allocate_cow_quotas(ranked_pool, cow_slot_budget, params, log)

    for lg in (1, 2, 3):
        for i, cand in enumerate(ranked_pool[lg]):
            sir1 = "SEXED" if i < assigned[lg] else "AN"
            tier = f"L{lg}_T{cand.tbrd_tier}" if sir1 == "SEXED" else f"L{lg}_T{cand.tbrd_tier}_AN"
            in_tai = 1 if (cand.cow.rpro == "OK/OPEN" and params["tai_dssyd_lo"] <= cand.cow.dssyd <= params["tai_dssyd_hi"]) else 0
            results[cand.cow.id] = (sir1, tier, 0, in_tai)

    write_projections_csv(cows, results)
    log(f"  Projections CSV written: {PROJECTIONS_CSV}")

    write_db_snapshot(con, iso_week, run_date, cows, results)
    con.commit()
    log(f"  DB snapshot saved: {len(cows):,} rows in sir_assignments")

    total_sexed = classification["hfr_sexed"] + sum(assigned.values())
    total_an = sum(1 for v in results.values() if v[0] == "AN")
    total_dnb = sum(1 for v in results.values() if v[0] == "-")

    log(f"  Cow slot budget: {cow_slot_budget} (target {target['friday_target']} + fixed buffer {int(params['sexed_assign_buffer'])})")
    log(f"  Cow slots used: {sum(assigned.values())} (unused: {cow_slot_budget - sum(assigned.values())})")
    log(f"  SEXED total:   {total_sexed}  ({classification['hfr_sexed']} hfr + {sum(assigned.values())} cow)")
    log(f"  AN total:      {total_an:,}")
    log(f"  DNB ('-'):     {total_dnb:,}")
    log(f"  Preserved:     {classification['preserved']:,}")
    log(f"  Non-Holstein excluded (CBRD != 'H'):  {classification['nonh']} cows -> AN")
    log(f"  TAI cohort:    {classification['tai_cohort_ct']}")
    log(f"  First-service DIM {int(params['first_service_dim_lo'])}-{int(params['first_service_dim_hi'])} TBRD=0: "
        f"{classification['first_service_total']} total")
    total_lowprod = sum(classification["lowprod_excluded"].values())
    log(f"  Low-prod excluded (composite milk score filter):  {total_lowprod} cows")
    log("    Score = mean of non-zero [WMLK1, PEAK, W4MK, W8MK, W12MK, W24MK]")
    for lg in (1, 2, 3):
        label = "LACT 1" if lg == 1 else ("LACT 2" if lg == 2 else "LACT 3+")
        pct = int(params[f"prod_exclude_l{lg}_pct"])
        log(f"    {label} bottom {pct}% (score < {classification['lowprod_cut'][lg]:.1f}): "
            f"{classification['lowprod_excluded'][lg]} excluded")

    # log this week's counts for next week's rolling averages / cumulative displays
    # (idempotent on rerun -- weekly_breedings/friday_targets both have a
    # UNIQUE index keyed on farm+iso_week[+sire_code+lact_group])
    con.execute("DELETE FROM weekly_breedings WHERE farm=? AND iso_week=? AND sire_code='SEXED_AGG'",
                (FARM, iso_week))
    con.execute(
        "INSERT INTO weekly_breedings (farm, iso_week, week_start, sire_code, lact_group, bred_count, source) "
        "VALUES (?,?,?,?,?,?,?)",
        (FARM, iso_week, run_date, "SEXED_AGG", 0, hfr_sexed_wk, "BREED.CSV"),
    )
    con.execute(
        "INSERT INTO weekly_breedings (farm, iso_week, week_start, sire_code, lact_group, bred_count, source) "
        "VALUES (?,?,?,?,?,?,?)",
        (FARM, iso_week, run_date, "SEXED_AGG", 1, assigned[1], "sir_assignments"),
    )
    con.execute(
        "INSERT INTO weekly_breedings (farm, iso_week, week_start, sire_code, lact_group, bred_count, source) "
        "VALUES (?,?,?,?,?,?,?)",
        (FARM, iso_week, run_date, "SEXED_AGG", 2, assigned[2] + assigned[3], "sir_assignments"),
    )

    con.execute("DELETE FROM friday_targets WHERE farm=? AND iso_week=?", (FARM, iso_week))
    con.execute(
        "INSERT INTO friday_targets (farm, iso_week, run_date, breed_month, hfr_sexed_bred, hfr_beef_bred, "
        "cow_sexed_target, cow_sexed_actual, hfr_cr_used, cow_cr_used, herd_size_used, cull_rate_used, "
        "hfr_sexed_planned, lact1_sexed_planned, lact2_sexed_planned, lact3plus_sexed_planned) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (FARM, iso_week, run_date, today.month, hfr_sexed_wk, hfr_beef_wk,
         target["friday_target"], sum(assigned.values()), target["hfr_effective_cr"], target["cow_effective_cr"],
         cull["herd_size"], cull["annual_cull_pct"],
         classification["hfr_sexed"], assigned[1], assigned[2], assigned[3]),
    )
    con.commit()

    write_dashboard_html({
        "iso_week": iso_week, "generated": dt.datetime.now().strftime("%A %B %d, %Y %I:%M %p"),
        "cows": cows, "results": results, "params": params, "target": target,
        "cow_slot_budget": cow_slot_budget, "assigned": assigned,
        "hfr_sexed": classification["hfr_sexed"], "tai_cohort_ct": classification["tai_cohort_ct"],
    })
    log(f"Assignments dashboard written: {ASSIGNMENTS_HTML}")
    log(f"Dashboard written: {DASHBOARD_HTML}")

    summary = f"{total_sexed} SEXED / {total_an} AN / {total_dnb} DNB"
    publish_to_github(cfg, summary, log)

    if today.weekday() == 3:  # Thursday
        send_sms(cfg, target["friday_target"], log)
    else:
        log(f"  Not Thursday ({today.strftime('%A')}) -- skipping SMS")

    con.close()

    with open(RUN_LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")


if __name__ == "__main__":
    main()
