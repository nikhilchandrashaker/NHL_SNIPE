"""
SNIPE — Scoring, Net-impact, Ice-time/usage, Playmaking, Efficiency
A 0-100 composite hockey impact score for NHL skaters, 2004-2018.

Data quality notes handled here:
1. The 2009 season is stored differently than every other season in this
   dataset: traded players get one row per team (no combined-season row),
   while every other season is already pre-aggregated to one row per
   player. We rebuild 2009 into single season totals so it's consistent
   with 2004-2018 as a whole.
2. Rate stats are excluded from the raw counting-stat aggregation and
   recomputed from the aggregated totals (S%, ATOI, FO%) rather than
   averaged, which would be wrong.
3. Goalies aren't in this file at all — it's a skaters-only dataset, so
   the Hart validation target already reflects the "next eligible
   skater" caveat from the 2015 Carey Price year.
4. PS (Point Shares) is deliberately excluded from the SNIPE formula
   itself — it's already a compiled advanced stat, and using it as both
   an input and then validating against Hart voting (which correlates
   with PS) would be circular. It's kept as a comparison baseline only.
"""

import pandas as pd
import numpy as np

RAW_PATH = "/mnt/user-data/uploads/NHL_2004-2018_Player_Data.csv"
MIN_GP = 20  # filters out call-ups/injury seasons that would distort percentiles

# ---------------------------------------------------------------------------
# 1. LOAD + FIX THE 2009 SPLIT-ROW ISSUE
# ---------------------------------------------------------------------------
df = pd.read_csv(RAW_PATH, encoding="latin1")

SUM_COLS = ["GP", "G", "A", "PTS", "plusminus", "PIM", "PS",
            "EV", "PP", "SH", "GW", "EV.1", "PP.1", "SH.1",
            "S", "TOI", "BLK", "HIT", "FOW", "FOL"]
MAX_COLS = ["HART", "Votes"]
FIRST_COLS = ["Player", "Age", "Pos"]

def rebuild_season(season_df):
    """
    Collapse duplicate rows in a season into one row per player.

    2009 has two distinct problems, not one:
    - Real in-season trades: same player, DIFFERENT teams, complementary GP
      splits (e.g. Aaron Johnson: 38 GP CHI + 61 GP CBJ). These get summed
      into a season total.
    - Duplicate-entry rows: same player, SAME team, but two different stat
      lines entirely (e.g. Crosby 2009 shows up twice for PIT with
      different GP/G/A). This isn't a trade, it's a data-entry duplication
      bug that happens to hit stars disproportionately (Crosby, Ovechkin-
      adjacent names). Summing these would double their season. We keep
      only the row with the higher GP (the more complete snapshot).
    """
    dupe_players = season_df.Player[season_df.Player.duplicated(keep=False)].unique()
    if len(dupe_players) == 0:
        return season_df

    keep = season_df[~season_df.Player.isin(dupe_players)].copy()
    dupes = season_df[season_df.Player.isin(dupe_players)]

    real_trade_names, same_team_names = [], []
    for p, g in dupes.groupby("Player"):
        (real_trade_names if g.Tm.nunique() > 1 else same_team_names).append(p)

    # same-team duplicate-entry rows: keep the higher-GP row only
    same_team_fixed = (
        dupes[dupes.Player.isin(same_team_names)]
        .sort_values("GP", ascending=False)
        .drop_duplicates(subset="Player", keep="first")
    )

    # real multi-team trades: sum into a season total
    trades = dupes[dupes.Player.isin(real_trade_names)]
    agg = {c: "sum" for c in SUM_COLS}
    agg.update({c: "max" for c in MAX_COLS})
    agg.update({c: "first" for c in FIRST_COLS})
    rebuilt = trades.groupby("Player", as_index=False).agg(agg)
    rebuilt["Tm"] = "TOT"
    rebuilt["Season"] = season_df.Season.iloc[0]

    # sanity cap: a real 2008-09 season is 82 GP max. Any "trade" that sums
    # to more than that isn't a trade — it's the same cross-season/re-scrape
    # contamination seen in the same-team rows, just with mismatched team
    # labels. Fall back to the single highest-GP row for those instead of
    # trusting the sum.
    max_gp = trades.groupby("Player")["GP"].max()
    bad_sum_players = rebuilt.loc[rebuilt.GP > 82, "Player"]
    if len(bad_sum_players):
        fallback = (
            trades[trades.Player.isin(bad_sum_players)]
            .sort_values("GP", ascending=False)
            .drop_duplicates(subset="Player", keep="first")
        )
        rebuilt = rebuilt[~rebuilt.Player.isin(bad_sum_players)]
        rebuilt = pd.concat([rebuilt, fallback], ignore_index=True)

    rebuilt["S_percent"] = np.where(rebuilt["S"] > 0, rebuilt["G"] / rebuilt["S"] * 100, 0)
    rebuilt["ATOI"] = np.where(rebuilt["GP"] > 0, rebuilt["TOI"] / rebuilt["GP"], 0)
    rebuilt["FO_percent"] = np.where(
        (rebuilt["FOW"] + rebuilt["FOL"]) > 0,
        rebuilt["FOW"] / (rebuilt["FOW"] + rebuilt["FOL"]) * 100, np.nan
    )
    rebuilt["Rk"] = np.nan

    keep_cols = list(season_df.columns)
    return pd.concat([keep, same_team_fixed, rebuilt], ignore_index=True)[keep_cols]

df = pd.concat(
    [rebuild_season(df[df.Season == s].copy()) for s in sorted(df.Season.unique())],
    ignore_index=True,
)
print(f"After 2009 rebuild: {df.shape[0]} player-seasons, "
      f"{df.duplicated(subset=['Player', 'Season']).sum()} remaining duplicates")

# ---------------------------------------------------------------------------
# 2. FILTER + DERIVED RATE STATS
# ---------------------------------------------------------------------------
skaters = df[df.GP >= MIN_GP].copy()

skaters["PTS_per_GP"] = skaters.PTS / skaters.GP
skaters["PTS_per_60"] = skaters.PTS / (skaters.TOI / 60)
skaters["is_center"] = skaters.Pos.str.contains("C", na=False)

# ---------------------------------------------------------------------------
# 3. WITHIN-SEASON PERCENTILE NORMALIZATION
# ---------------------------------------------------------------------------
def pct_rank(series):
    return series.rank(pct=True) * 100

PCTL_STATS = {
    "G": "pctl_G", "GW": "pctl_GW", "S_percent": "pctl_Spct", "S": "pctl_S", "PP": "pctl_PPG",
    "A": "pctl_A", "EV.1": "pctl_EVA", "PP.1": "pctl_PPA",
    "plusminus": "pctl_PM", "BLK": "pctl_BLK", "HIT": "pctl_HIT", "PIM": "pctl_PIM",
    "TOI": "pctl_TOI", "GP": "pctl_GP",
    "PTS_per_GP": "pctl_PTSGP", "PTS_per_60": "pctl_PTS60",
}
for raw, out in PCTL_STATS.items():
    skaters[out] = skaters.groupby("Season")[raw].transform(pct_rank)

# FO% only meaningful for centers; non-centers get the median (neutral, not penalized)
skaters["pctl_FO"] = np.nan
skaters.loc[skaters.is_center, "pctl_FO"] = (
    skaters.loc[skaters.is_center].groupby("Season")["FO_percent"].transform(pct_rank)
)
skaters["pctl_FO"] = skaters["pctl_FO"].fillna(50)

# PIM is discipline — flip so fewer minutes = higher score
skaters["pctl_PIM"] = 100 - skaters["pctl_PIM"]

# ---------------------------------------------------------------------------
# 4. CATEGORY SCORES (0-100 each)
# ---------------------------------------------------------------------------
skaters["Scoring"] = (
    0.40 * skaters.pctl_G + 0.15 * skaters.pctl_GW + 0.15 * skaters.pctl_Spct
    + 0.15 * skaters.pctl_S + 0.15 * skaters.pctl_PPG
)
skaters["Playmaking"] = (
    0.60 * skaters.pctl_A + 0.20 * skaters.pctl_EVA + 0.20 * skaters.pctl_PPA
)
skaters["TwoWay"] = (
    0.50 * skaters.pctl_PM + 0.20 * skaters.pctl_BLK
    + 0.15 * skaters.pctl_HIT + 0.15 * skaters.pctl_PIM
)
skaters["Usage"] = (
    0.50 * skaters.pctl_TOI + 0.30 * skaters.pctl_GP + 0.20 * skaters.pctl_FO
)
skaters["Efficiency"] = (
    0.50 * skaters.pctl_PTSGP + 0.25 * skaters.pctl_Spct + 0.25 * skaters.pctl_PTS60
)

# ---------------------------------------------------------------------------
# 5. SNIPE — WEIGHTED COMPOSITE
# ---------------------------------------------------------------------------
WEIGHTS = {"Scoring": 0.30, "Playmaking": 0.25, "TwoWay": 0.15, "Usage": 0.15, "Efficiency": 0.15}
skaters["SNIPE"] = sum(skaters[cat] * w for cat, w in WEIGHTS.items())

# ---------------------------------------------------------------------------
# 6. OUTPUT
# ---------------------------------------------------------------------------
out_cols = ["Player", "Season", "Tm", "Pos", "Age", "GP", "G", "A", "PTS", "plusminus", "PS",
            "Scoring", "Playmaking", "TwoWay", "Usage", "Efficiency", "SNIPE", "HART", "Votes"]
leaderboard = skaters[out_cols].sort_values("SNIPE", ascending=False).reset_index(drop=True)
leaderboard["Player"] = leaderboard["Player"].str.split("\\").str[0]
for c in ["Scoring", "Playmaking", "TwoWay", "Usage", "Efficiency", "SNIPE"]:
    leaderboard[c] = leaderboard[c].round(1)

leaderboard.to_csv("/home/claude/snipe/snipe_leaderboard.csv", index=False)
skaters.to_csv("/home/claude/snipe/snipe_full_dataset.csv", index=False)

print("\nTop 15 all-time (2004-2018) by SNIPE:")
print(leaderboard.head(15).to_string(index=False))

print(f"\nTotal player-seasons scored: {len(leaderboard)} (GP >= {MIN_GP} filter applied, "
      f"{len(df) - len(leaderboard)} seasons dropped below that threshold)")
