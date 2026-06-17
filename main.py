"""
World Cup 2026 AI Betting Assistant — backend (v2)
====================================================

Adds, on top of the original Goals/1X2/BTTS engine:
  - Cards markets (total cards Over/Under, most-booked-team)
  - Corners markets (total corners Over/Under, most-corners team)
  - More goal lines (1.5 / 2.5 / 3.5)
  - "Fair odds" (model-implied decimal odds) on every single pick
  - An optional live bookmaker-odds overlay on the 1X2 market, IF an
    ODDS_API_KEY environment variable is set (via The Odds API). With no
    key set, this silently does nothing — the frontend's own "check value"
    calculator (paste in the odds you see at your own bookmaker) is the
    zero-dependency way to compare model vs. market, and works regardless.

Run it:
    pip install -r requirements.txt
    python main.py
    open http://127.0.0.1:8000
"""

import math
import os
from datetime import date
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="World Cup 2026 AI Betting Assistant", version="2.0.0")


# ---------------------------------------------------------------------------
# 1. Data models
# ---------------------------------------------------------------------------

class KeyPlayer(BaseModel):
    name: str
    role: str
    goal_share: float = Field(..., description="Illustrative share of team xG this player accounts for (0-1)")
    assist_share: float = Field(..., description="Illustrative share of team xG this player tends to assist (0-1)")


class TeamStats(BaseModel):
    name: str
    code: str
    flag: str
    power_rating: float = Field(..., description="0-100 composite strength index (goals)")
    card_rate: float = Field(50, description="0-100 composite discipline index (higher = more cards)")
    corner_rate: float = Field(50, description="0-100 composite attacking-width index (higher = more corners)")
    fifa_rank: Optional[int] = None
    form_note: str = ""
    key_players: List[KeyPlayer] = Field(default_factory=list)


class Fixture(BaseModel):
    id: str
    group: str
    date: str
    venue: str
    kickoff_local: str
    home: TeamStats
    away: TeamStats


class OutcomeProbabilities(BaseModel):
    home_win: float
    draw: float
    away_win: float
    over_2_5: float
    under_2_5: float
    btts_yes: float
    btts_no: float
    most_likely_score: str


class BettingPick(BaseModel):
    category: str               # "Goals" | "Cards" | "Corners"
    market: str
    selection: str
    confidence: float
    risk_level: str
    fair_odds: Optional[float] = None
    rationale: str
    market_odds: Optional[float] = None   # only populated if a live odds feed is configured
    bookmaker: Optional[str] = None
    edge_pct: Optional[float] = None      # model confidence minus the market's implied probability
    style_note: Optional[str] = None      # playing-style context, used on Value & Longshot picks


class MatchPrediction(BaseModel):
    fixture_id: str
    matchup: str
    expected_goals: Dict[str, float]
    expected_cards: Dict[str, float]
    expected_corners: Dict[str, float]
    expected_possession: Dict[str, float]
    probabilities: OutcomeProbabilities
    best_pick: BettingPick
    categories: Dict[str, List[BettingPick]]
    value_picks: List[BettingPick]
    market_odds_available: bool
    engine: str


# ---------------------------------------------------------------------------
# 2. Seed data — real Group K & Group L fixtures, 17 June 2026
#
# power_rating: simplified 0-100 composite from the June 2026 FIFA World
# Ranking (England #4 / 1825.97 pts, Portugal #5 / 1763.83, Croatia #11 /
# 1717.07, Colombia #13 / 1693.09 — exact points) plus pre-tournament form
# reporting for sides outside the ranked top tier (DR Congo #56,
# Uzbekistan #50, Ghana #73, Panama unranked top-60).
#
# card_rate / corner_rate: illustrative composites reflecting each team's
# general playing style (possession/width vs. low-block/foul-heavy) — not
# an official statistic. Swap in real per-team season averages (e.g. from
# Opta/Sportradar) for production use.
# ---------------------------------------------------------------------------

def team(name, code, flag, rating, rank=None, note="", cards=50, corners=50, players=None):
    return TeamStats(name=name, code=code, flag=flag, power_rating=rating, fifa_rank=rank,
                      form_note=note, card_rate=cards, corner_rate=corners,
                      key_players=players or [])


PORTUGAL = team("Portugal", "POR", "🇵🇹", 87, 5,
                "Ronaldo-led attack, pot-1 seed, heavy Group K favorites", cards=46, corners=64,
                players=[KeyPlayer(name="Cristiano Ronaldo", role="Striker", goal_share=0.42, assist_share=0.15)])
DRCONGO = team("DR Congo", "COD", "🇨🇩", 40, 56,
               "First World Cup since 1974, knocked out Cameroon & Nigeria in playoffs", cards=58, corners=36,
               players=[KeyPlayer(name="Yoane Wissa", role="Forward", goal_share=0.30, assist_share=0.10),
                        KeyPlayer(name="Cédric Bakambu", role="Striker", goal_share=0.25, assist_share=0.08)])
ENGLAND = team("England", "ENG", "🏴", 90, 4,
               "Kane, Bellingham, Rice — several pundits' dark-horse pick to win it all", cards=44, corners=61,
               players=[KeyPlayer(name="Harry Kane", role="Striker", goal_share=0.38, assist_share=0.08),
                        KeyPlayer(name="Jude Bellingham", role="Midfielder", goal_share=0.12, assist_share=0.28)])
CROATIA = team("Croatia", "CRO", "🇭🇷", 78, 11,
               "2018 runners-up, perennial knockout-stage overachievers", cards=61, corners=49,
               players=[KeyPlayer(name="Andrej Kramarić", role="Striker", goal_share=0.32, assist_share=0.10),
                        KeyPlayer(name="Ivan Perišić", role="Winger", goal_share=0.18, assist_share=0.22)])
UZBEKISTAN = team("Uzbekistan", "UZB", "🇺🇿", 36, 50,
                   "Tournament debut, squad mostly plays in the domestic league", cards=55, corners=34,
                   players=[KeyPlayer(name="Eldor Shomurodov", role="Striker (captain)", goal_share=0.40, assist_share=0.12)])
COLOMBIA = team("Colombia", "COL", "🇨🇴", 76, 13,
                "Luis Díaz leads the attack, tipped to handle the US summer heat well", cards=53, corners=58,
                players=[KeyPlayer(name="Luis Díaz", role="Winger", goal_share=0.28, assist_share=0.15),
                         KeyPlayer(name="James Rodríguez", role="Playmaker", goal_share=0.10, assist_share=0.25)])
GHANA = team("Ghana", "GHA", "🇬🇭", 56, 73,
             "Won just 1 of last 7, coach sacked in March, missing injured Kudus", cards=57, corners=51,
             players=[KeyPlayer(name="Antoine Semenyo", role="Forward", goal_share=0.30, assist_share=0.12),
                      KeyPlayer(name="Jordan Ayew", role="Forward (captain)", goal_share=0.20, assist_share=0.20)])
PANAMA = team("Panama", "PAN", "🇵🇦", 52, None,
              "Unbeaten through qualifying, still chasing a first-ever World Cup win", cards=49, corners=41,
              players=[KeyPlayer(name="Ismael Díaz", role="Forward", goal_share=0.30, assist_share=0.15)])

FIXTURES: List[Fixture] = [
    Fixture(id="k1-por-cod", group="Group K", date="2026-06-17",
            venue="NRG Stadium, Houston", kickoff_local="13:00 local",
            home=PORTUGAL, away=DRCONGO),
    Fixture(id="l1-eng-cro", group="Group L", date="2026-06-17",
            venue="AT&T Stadium, Arlington", kickoff_local="16:00 local",
            home=ENGLAND, away=CROATIA),
    Fixture(id="l1-gha-pan", group="Group L", date="2026-06-17",
            venue="BMO Field, Toronto", kickoff_local="19:00 local",
            home=GHANA, away=PANAMA),
    Fixture(id="k1-uzb-col", group="Group K", date="2026-06-17",
            venue="Estadio Azteca, Mexico City", kickoff_local="22:00 local",
            home=UZBEKISTAN, away=COLOMBIA),
]


# ---------------------------------------------------------------------------
# 3. Prediction engine — independent Poisson model, shared across markets
# ---------------------------------------------------------------------------

BASE_GOALS = 1.35        # average goals per side in a competitive international match
GOALS_EXPONENT = 0.75

BASE_CARDS = 1.90        # average cards per side  (~3.8 total, in line with recent World Cups)
CARDS_EXPONENT = 0.50

BASE_CORNERS = 5.10      # average corners per side (~10.2 total)
CORNERS_EXPONENT = 0.50

MAX_GOALS = 8
MAX_CARDS = 12
MAX_CORNERS = 16


def poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def expected_value(home_rate: float, away_rate: float, base: float, exponent: float) -> Dict[str, float]:
    ratio = (home_rate / away_rate) ** exponent
    return {"home": round(base * ratio, 2), "away": round(base / ratio, 2)}


def expected_possession(home: "TeamStats", away: "TeamStats") -> Dict[str, float]:
    """Pre-match model estimate, NOT a live in-game reading — there's no live possession feed
    wired in here. Built from the same power-rating gap used for the goals model, since
    higher-rated, more technical sides tend to control the ball for longer spells."""
    diff = home.power_rating - away.power_rating
    home_poss = max(28.0, min(72.0, 50 + diff * 0.35))
    return {"home": round(home_poss, 1), "away": round(100 - home_poss, 1)}


def build_grid(lam_h: float, lam_a: float, max_n: int) -> List[List[float]]:
    row_h = [poisson_pmf(i, lam_h) for i in range(max_n + 1)]
    row_a = [poisson_pmf(j, lam_a) for j in range(max_n + 1)]
    return [[ph * pa for pa in row_a] for ph in row_h]


def grid_total_over(grid: List[List[float]], line: float) -> float:
    return sum(p for i, row in enumerate(grid) for j, p in enumerate(row) if i + j > line)


def grid_more_equal(grid: List[List[float]]):
    home_more = sum(p for i, row in enumerate(grid) for j, p in enumerate(row) if i > j)
    equal = sum(grid[i][i] for i in range(len(grid)))
    away_more = max(0.0, 1.0 - home_more - equal)
    return home_more, equal, away_more


def grid_btts(grid: List[List[float]]) -> float:
    return sum(p for i, row in enumerate(grid) for j, p in enumerate(row) if i >= 1 and j >= 1)


def grid_best_cell(grid: List[List[float]]):
    best, best_p = (0, 0), 0.0
    for i, row in enumerate(grid):
        for j, p in enumerate(row):
            if p > best_p:
                best_p, best = p, (i, j)
    return best


def risk_level(p: float) -> str:
    if p >= 60:
        return "Low"
    if p >= 45:
        return "Medium"
    return "High"


def fair_odds(prob_pct: float) -> Optional[float]:
    if prob_pct <= 0:
        return None
    return round(100.0 / prob_pct, 2)


def fetch_live_odds(fixture: Fixture) -> Optional[dict]:
    """Optional: pull real 1X2 odds from The Odds API if ODDS_API_KEY is set.
    Returns {'home': decimal, 'draw': decimal, 'away': decimal, 'bookmaker': name} or None.
    Silently returns None on any failure (no key, no network, no match found, rate limit, etc.) —
    this is a bonus overlay, never a hard dependency for the rest of the app."""
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return None
    try:
        import requests
        sport_key = os.environ.get("ODDS_API_SPORT_KEY", "soccer_fifa_world_cup")
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        params = {"apiKey": api_key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
        resp = requests.get(url, params=params, timeout=6)
        resp.raise_for_status()
        for event in resp.json():
            if (fixture.home.name.lower() in event.get("home_team", "").lower()
                    and fixture.away.name.lower() in event.get("away_team", "").lower()):
                bookmakers = event.get("bookmakers", [])
                if not bookmakers:
                    continue
                outcomes = bookmakers[0]["markets"][0]["outcomes"]
                prices = {o["name"]: o["price"] for o in outcomes}
                return {
                    "home": prices.get(fixture.home.name),
                    "draw": prices.get("Draw"),
                    "away": prices.get(fixture.away.name),
                    "bookmaker": bookmakers[0].get("title"),
                }
    except Exception:
        return None
    return None


def run_engine(fixture: Fixture) -> MatchPrediction:
    home, away = fixture.home, fixture.away

    xg = expected_value(home.power_rating, away.power_rating, BASE_GOALS, GOALS_EXPONENT)
    xc = expected_value(home.card_rate, away.card_rate, BASE_CARDS, CARDS_EXPONENT)
    xk = expected_value(home.corner_rate, away.corner_rate, BASE_CORNERS, CORNERS_EXPONENT)
    poss = expected_possession(home, away)

    goal_grid = build_grid(xg["home"], xg["away"], MAX_GOALS)
    card_grid = build_grid(xc["home"], xc["away"], MAX_CARDS)
    corner_grid = build_grid(xk["home"], xk["away"], MAX_CORNERS)

    home_win, draw, away_win = grid_more_equal(goal_grid)
    over_25 = grid_total_over(goal_grid, 2.5)
    btts_yes = grid_btts(goal_grid)
    best_score = grid_best_cell(goal_grid)

    probs = OutcomeProbabilities(
        home_win=round(home_win * 100, 1), draw=round(draw * 100, 1), away_win=round(away_win * 100, 1),
        over_2_5=round(over_25 * 100, 1), under_2_5=round((1 - over_25) * 100, 1),
        btts_yes=round(btts_yes * 100, 1), btts_no=round((1 - btts_yes) * 100, 1),
        most_likely_score=f"{best_score[0]}-{best_score[1]}",
    )

    goals_rationale = f"Modeled from an expected scoreline of {xg['home']:.2f}–{xg['away']:.2f} goals."
    cards_rationale = f"Modeled from expected bookings of {xc['home']:.2f}–{xc['away']:.2f} cards per side."
    corners_rationale = f"Modeled from expected corners of {xk['home']:.2f}–{xk['away']:.2f} per side."

    def make_pick(category: str, market: str, selection: str, prob_fraction: float, rationale: str) -> BettingPick:
        p = round(prob_fraction * 100, 1)
        return BettingPick(category=category, market=market, selection=selection, confidence=p,
                            risk_level=risk_level(p), fair_odds=fair_odds(p), rationale=rationale)

    goals_picks = [
        make_pick("Goals", "1X2", f"{home.name} to win", home_win, goals_rationale),
        make_pick("Goals", "1X2", "Draw", draw, goals_rationale),
        make_pick("Goals", "1X2", f"{away.name} to win", away_win, goals_rationale),
        make_pick("Goals", "Total goals", "Over 1.5 goals", grid_total_over(goal_grid, 1.5), goals_rationale),
        make_pick("Goals", "Total goals", "Under 1.5 goals", 1 - grid_total_over(goal_grid, 1.5), goals_rationale),
        make_pick("Goals", "Total goals", "Over 2.5 goals", over_25, goals_rationale),
        make_pick("Goals", "Total goals", "Under 2.5 goals", 1 - over_25, goals_rationale),
        make_pick("Goals", "Total goals", "Over 3.5 goals", grid_total_over(goal_grid, 3.5), goals_rationale),
        make_pick("Goals", "Total goals", "Under 3.5 goals", 1 - grid_total_over(goal_grid, 3.5), goals_rationale),
        make_pick("Goals", "Both teams to score", "Yes", btts_yes, goals_rationale),
        make_pick("Goals", "Both teams to score", "No", 1 - btts_yes, goals_rationale),
    ]

    card_home_more, card_equal, card_away_more = grid_more_equal(card_grid)
    cards_picks = [
        make_pick("Cards", "Total cards", "Over 3.5 cards", grid_total_over(card_grid, 3.5), cards_rationale),
        make_pick("Cards", "Total cards", "Under 3.5 cards", 1 - grid_total_over(card_grid, 3.5), cards_rationale),
        make_pick("Cards", "Total cards", "Over 4.5 cards", grid_total_over(card_grid, 4.5), cards_rationale),
        make_pick("Cards", "Total cards", "Under 4.5 cards", 1 - grid_total_over(card_grid, 4.5), cards_rationale),
        make_pick("Cards", "Most cards", f"{home.name} booked more", card_home_more, cards_rationale),
        make_pick("Cards", "Most cards", "Equal cards", card_equal, cards_rationale),
        make_pick("Cards", "Most cards", f"{away.name} booked more", card_away_more, cards_rationale),
    ]

    corner_home_more, corner_equal, corner_away_more = grid_more_equal(corner_grid)
    corners_picks = [
        make_pick("Corners", "Total corners", "Over 8.5 corners", grid_total_over(corner_grid, 8.5), corners_rationale),
        make_pick("Corners", "Total corners", "Under 8.5 corners", 1 - grid_total_over(corner_grid, 8.5), corners_rationale),
        make_pick("Corners", "Total corners", "Over 9.5 corners", grid_total_over(corner_grid, 9.5), corners_rationale),
        make_pick("Corners", "Total corners", "Under 9.5 corners", 1 - grid_total_over(corner_grid, 9.5), corners_rationale),
        make_pick("Corners", "Total corners", "Over 10.5 corners", grid_total_over(corner_grid, 10.5), corners_rationale),
        make_pick("Corners", "Total corners", "Under 10.5 corners", 1 - grid_total_over(corner_grid, 10.5), corners_rationale),
        make_pick("Corners", "Most corners", f"{home.name} more corners", corner_home_more, corners_rationale),
        make_pick("Corners", "Most corners", "Equal corners", corner_equal, corners_rationale),
        make_pick("Corners", "Most corners", f"{away.name} more corners", corner_away_more, corners_rationale),
    ]

    player_picks: List[BettingPick] = []
    for side, opponent_name in ((home, away.name), (away, home.name)):
        team_xg = xg["home"] if side is home else xg["away"]
        for kp in side.key_players:
            goal_lambda = team_xg * kp.goal_share
            assist_lambda = team_xg * kp.assist_share
            p_score = 1 - poisson_pmf(0, goal_lambda)
            p_assist = 1 - poisson_pmf(0, assist_lambda)
            rationale = (f"{kp.name} ({kp.role}) is modeled as carrying ~{round(kp.goal_share*100)}% of "
                         f"{side.name}'s expected goals against {opponent_name} (xG {team_xg:.2f}).")
            player_picks.append(make_pick("Players", "To score anytime",
                                           f"{kp.name} ({side.name})", p_score, rationale))
            player_picks.append(make_pick("Players", "To register an assist",
                                           f"{kp.name} ({side.name})", p_assist, rationale))

    all_picks = goals_picks + cards_picks + corners_picks + player_picks
    best_pick = max(all_picks, key=lambda pk: pk.confidence)

    # "Value & longshot" picks: lower-probability selections (roughly 12-45%, i.e. fair odds
    # ~2.2 to ~8) that still have a defensible style-based case, rather than pure long-tail
    # noise. Each gets a one-line playing-style note pulled from the relevant team's form.
    def style_for(selection: str) -> Optional[str]:
        for side in (home, away):
            if side.name in selection or any(kp.name in selection for kp in side.key_players):
                return side.form_note
        return None

    value_candidates = [pk for pk in all_picks if 12.0 <= pk.confidence <= 45.0]
    value_candidates.sort(key=lambda pk: pk.confidence, reverse=True)
    value_picks = value_candidates[:4]
    for vp in value_picks:
        vp.style_note = style_for(vp.selection)

    live = fetch_live_odds(fixture)
    if live:
        for gp in goals_picks[:3]:
            if gp.selection.startswith(home.name):
                price = live.get("home")
            elif gp.selection.startswith(away.name):
                price = live.get("away")
            else:
                price = live.get("draw")
            if price:
                gp.market_odds = price
                gp.bookmaker = live.get("bookmaker")
                gp.edge_pct = round(gp.confidence - (100.0 / price), 1)

    return MatchPrediction(
        fixture_id=fixture.id,
        matchup=f"{home.name} vs {away.name}",
        expected_goals=xg, expected_cards=xc, expected_corners=xk, expected_possession=poss,
        probabilities=probs,
        best_pick=best_pick,
        categories={"Goals": goals_picks, "Cards": cards_picks, "Corners": corners_picks, "Players": player_picks},
        value_picks=value_picks,
        market_odds_available=bool(live),
        engine="poisson-v3",
    )


# ---------------------------------------------------------------------------
# 4. API routes
# ---------------------------------------------------------------------------

DISCLAIMER = (
    "Model output is a statistical estimate for analysis purposes, not betting advice "
    "or a guarantee of results. Odds and outcomes always carry risk of loss — bet only "
    "what you can afford to lose, and note that gambling-advertising and licensing rules "
    "vary by country."
)


@app.get("/api/fixtures")
async def get_fixtures():
    return {"date": str(date.today()), "fixtures": FIXTURES}


@app.get("/api/predict/{fixture_id}", response_model=MatchPrediction)
async def predict(fixture_id: str):
    fixture = next((f for f in FIXTURES if f.id == fixture_id), None)
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")
    return run_engine(fixture)


@app.get("/api/predictions/today")
async def predictions_today():
    """The 'most likely' bets across all of today's fixtures and markets, ranked by model confidence,
    plus a cross-match list of the best higher-payout 'value & longshot' picks of the day."""
    results = [{"fixture": f, "prediction": run_engine(f)} for f in FIXTURES]
    results.sort(key=lambda r: r["prediction"].best_pick.confidence, reverse=True)

    value_pool: List[dict] = []
    player_pool: List[dict] = []
    for r in results:
        for vp in r["prediction"].value_picks:
            value_pool.append({"fixture": r["fixture"], "pick": vp})
        for pp in r["prediction"].categories.get("Players", []):
            player_pool.append({"fixture": r["fixture"], "pick": pp})
    value_pool.sort(key=lambda v: (v["pick"].fair_odds or 0), reverse=True)
    player_pool.sort(key=lambda v: v["pick"].confidence, reverse=True)

    return {
        "date": str(date.today()),
        "disclaimer": DISCLAIMER,
        "results": results,
        "top_value_picks": value_pool[:8],
        "top_player_props": player_pool[:6],
    }


@app.post("/api/predict-custom", response_model=MatchPrediction)
async def predict_custom(home: TeamStats, away: TeamStats):
    """Run the engine on any custom matchup — plug live stats in here."""
    fixture = Fixture(id="custom", group="Custom", date=str(date.today()),
                       venue="", kickoff_local="", home=home, away=away)
    return run_engine(fixture)


# ---------------------------------------------------------------------------
# 5. Serve the frontend
# ---------------------------------------------------------------------------

INDEX_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>World Cup 2026 · AI Betting Intelligence</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,600;0,9..144,700;1,9..144,500&family=Manrope:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#0B0D12;
    --panel:#14171F;
    --panel-2:#1A1E29;
    --hairline:#272C3A;
    --gold:#C9A35C;
    --gold-bright:#E8C77E;
    --cream:#EFE9DD;
    --slate:#8590A3;
    --emerald:#4C9472;
    --crimson:#C2685F;
    --display:'Fraunces', serif;
    --body:'Manrope', sans-serif;
    --mono:'IBM Plex Mono', monospace;
  }
  *{box-sizing:border-box;}
  body{
    margin:0;
    background:
      radial-gradient(circle at 15% 0%, rgba(201,163,92,0.07), transparent 45%),
      var(--ink);
    color:var(--cream);
    font-family:var(--body);
    min-height:100vh;
    -webkit-font-smoothing:antialiased;
  }
  a{color:inherit;}
  ::selection{background:var(--gold); color:var(--ink);}

  /* ---------- sticky header / nav ---------- */
  .topbar{
    position:sticky; top:0; z-index:20;
    background:rgba(11,13,18,0.92);
    backdrop-filter:blur(8px);
    border-bottom:1px solid var(--hairline);
  }
  .topbar__row{
    max-width:1180px; margin:0 auto;
    display:flex; align-items:center; justify-content:space-between;
    padding:16px 24px; gap:20px; flex-wrap:wrap;
  }
  .brand{display:flex; align-items:baseline; gap:10px;}
  .brand__mark{
    font-family:var(--display); font-weight:700; font-size:20px;
    letter-spacing:0.3px;
  }
  .brand__mark em{color:var(--gold); font-style:normal;}
  .brand__tag{
    font-family:var(--mono); font-size:10.5px; letter-spacing:1.5px;
    color:var(--slate); text-transform:uppercase;
  }
  .nav{display:flex; gap:22px; flex-wrap:wrap;}
  .nav a{
    font-family:var(--mono); font-size:11px; letter-spacing:0.8px;
    text-transform:uppercase; color:var(--slate); text-decoration:none;
    border-bottom:1px solid transparent; padding-bottom:3px;
    transition:color 0.15s, border-color 0.15s;
  }
  .nav a:hover{color:var(--gold-bright); border-color:var(--gold-bright);}
  .live-tag{
    display:flex; align-items:center; gap:7px;
    font-family:var(--mono); font-size:11px; color:var(--slate); letter-spacing:0.5px;
  }
  .live-dot{
    width:7px; height:7px; border-radius:50%; background:var(--crimson);
    box-shadow:0 0 0 0 rgba(194,104,95,0.6); animation:pulse 1.8s infinite;
  }
  @keyframes pulse{
    0%{box-shadow:0 0 0 0 rgba(194,104,95,0.55);}
    70%{box-shadow:0 0 0 6px rgba(194,104,95,0);}
    100%{box-shadow:0 0 0 0 rgba(194,104,95,0);}
  }

  main{max-width:1180px; margin:0 auto; padding:0 24px 100px;}

  /* ---------- hero ---------- */
  .hero{padding:56px 0 36px; border-bottom:1px solid var(--hairline);}
  .hero__eyebrow{
    font-family:var(--mono); font-size:11.5px; letter-spacing:2px;
    color:var(--gold); text-transform:uppercase; margin:0 0 14px;
  }
  .hero h1{
    font-family:var(--display); font-weight:600; font-style:italic;
    font-size:clamp(32px, 4.6vw, 52px); line-height:1.05; margin:0 0 16px; max-width:760px;
  }
  .hero p{
    font-size:15px; color:var(--slate); max-width:600px; line-height:1.6; margin:0;
  }

  .section{padding:56px 0; border-bottom:1px solid var(--hairline);}
  .section:last-of-type{border-bottom:none;}
  .section__head{display:flex; align-items:baseline; justify-content:space-between; gap:18px; margin-bottom:26px; flex-wrap:wrap;}
  .section__title{font-family:var(--display); font-weight:600; font-size:clamp(24px,3vw,32px); margin:0;}
  .section__sub{font-size:13px; color:var(--slate); margin:6px 0 0; max-width:560px;}

  /* ---------- Top Picks leaderboard ---------- */
  .leaderboard{display:flex; flex-direction:column;}
  .lb-row{
    display:grid; grid-template-columns:46px 1fr auto auto; align-items:center;
    gap:18px; padding:18px 4px; border-bottom:1px solid var(--hairline);
  }
  .lb-row:first-child{border-top:1px solid var(--hairline);}
  .lb-rank{font-family:var(--display); font-style:italic; font-weight:600; font-size:26px; color:var(--gold);}
  .lb-match{font-family:var(--mono); font-size:11px; color:var(--slate); text-transform:uppercase; letter-spacing:0.4px;}
  .lb-pick{font-weight:600; font-size:16px; margin-top:3px;}
  .lb-market{font-family:var(--mono); font-size:10.5px; color:var(--slate); text-transform:uppercase;}
  .lb-conf{text-align:right; font-family:var(--display); font-weight:600; font-size:24px;}
  .lb-odds{font-family:var(--mono); font-size:12px; color:var(--gold-bright); text-align:right; margin-top:2px;}

  .badge{
    font-family:var(--mono); font-size:10px; letter-spacing:0.5px; text-transform:uppercase;
    padding:3px 9px; border-radius:100px; border:1px solid currentColor; white-space:nowrap;
  }
  .badge--low{color:var(--emerald);}
  .badge--medium{color:var(--gold-bright);}
  .badge--high{color:var(--crimson);}

  /* ---------- ticket cards (Value & Longshots / Player Spotlight) ---------- */
  .ticket-row{display:grid; grid-template-columns:repeat(auto-fill, minmax(250px, 1fr)); gap:16px;}
  .ticket{
    position:relative; overflow:hidden;
    background:linear-gradient(160deg, var(--panel-2), var(--panel));
    border:1px solid var(--hairline); border-radius:12px; padding:20px;
  }
  .ticket::before{
    content:''; position:absolute; top:0; left:0; right:0; height:2px;
    background:linear-gradient(90deg, transparent, var(--gold), transparent);
  }
  .ticket::after{
    content:''; position:absolute; top:50%; left:-9px; width:18px; height:18px;
    border-radius:50%; background:var(--ink); transform:translateY(-50%);
  }
  .ticket__match{font-family:var(--mono); font-size:10px; color:var(--slate); text-transform:uppercase; letter-spacing:0.5px;}
  .ticket__selection{font-family:var(--display); font-weight:600; font-size:18px; margin:8px 0 2px; line-height:1.25;}
  .ticket__market{font-family:var(--mono); font-size:10.5px; color:var(--gold); text-transform:uppercase; letter-spacing:0.4px;}
  .ticket__perf{border-top:1px dashed var(--hairline); margin:14px 0;}
  .ticket__stats{display:flex; justify-content:space-between; align-items:flex-end;}
  .ticket__odds{font-family:var(--display); font-weight:700; font-size:30px; color:var(--gold-bright);}
  .ticket__odds-label{font-family:var(--mono); font-size:9.5px; color:var(--slate); text-transform:uppercase; display:block; margin-top:2px;}
  .ticket__conf{text-align:right; font-family:var(--mono); font-size:12px; color:var(--slate);}
  .ticket__style{font-style:italic; font-size:12px; color:var(--slate); margin-top:12px; line-height:1.5;}
  .ticket__check{display:flex; align-items:center; gap:8px; margin-top:14px;}

  .odds-input{
    width:84px; background:rgba(0,0,0,0.3); border:1px solid var(--hairline); border-radius:6px;
    color:var(--cream); font-family:var(--mono); font-size:12px; padding:6px 8px;
  }
  .odds-input::placeholder{color:var(--slate);}
  .edge-out{font-family:var(--mono); font-size:11.5px; font-weight:600;}
  .edge-positive{color:var(--emerald);}
  .edge-negative{color:var(--crimson);}
  .edge-neutral{color:var(--slate);}

  /* ---------- match-by-match cards ---------- */
  .match-card{
    border:1px solid var(--hairline); border-radius:14px; background:var(--panel);
    padding:28px; margin-bottom:24px;
  }
  .match-card__meta{
    display:flex; justify-content:space-between; font-family:var(--mono); font-size:10.5px;
    color:var(--slate); text-transform:uppercase; letter-spacing:0.4px; margin-bottom:20px; flex-wrap:wrap; gap:8px;
  }
  .match-card__teams{display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:18px; margin-bottom:20px;}
  .team{display:flex; align-items:center; gap:12px;}
  .team--away{flex-direction:row-reverse; text-align:right;}
  .team__flag{font-size:28px; line-height:1;}
  .team__name{font-family:var(--display); font-weight:600; font-size:21px; line-height:1.1;}
  .team__rank{font-family:var(--mono); font-size:11px; color:var(--gold); margin-top:2px;}
  .match-card__score{font-family:var(--mono); font-size:12px; color:var(--slate); text-align:center; white-space:nowrap;}
  .match-card__score strong{display:block; font-family:var(--display); font-weight:700; font-size:26px; color:var(--cream);}

  .poss{margin-bottom:22px;}
  .poss__label{font-family:var(--mono); font-size:10px; color:var(--slate); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; display:flex; justify-content:space-between;}
  .poss__bar{height:7px; border-radius:4px; overflow:hidden; display:flex; background:var(--hairline);}
  .poss__seg--home{background:var(--gold);}
  .poss__seg--away{background:var(--slate);}

  .gauge{margin-bottom:20px;}
  .gauge__bar{position:relative; height:30px; display:flex; border-radius:4px; overflow:hidden; border:1px solid var(--hairline);}
  .gauge__seg{height:100%;}
  .gauge__seg--home{background:var(--gold);}
  .gauge__seg--draw{background:var(--slate);}
  .gauge__seg--away{background:var(--panel-2); border-right:1px solid var(--hairline);}
  .gauge__labels{display:flex; justify-content:space-between; font-family:var(--mono); font-size:10.5px; color:var(--slate); margin-top:8px; text-transform:uppercase;}
  .gauge__labels b{color:var(--cream); font-weight:600;}

  .pick-banner{
    display:flex; justify-content:space-between; align-items:center; gap:16px;
    background:rgba(201,163,92,0.07); border:1px solid rgba(201,163,92,0.35);
    border-radius:8px; padding:14px 18px; flex-wrap:wrap; margin-bottom:20px;
  }
  .pick-banner__label{font-family:var(--mono); font-size:10.5px; letter-spacing:1px; color:var(--gold); text-transform:uppercase; margin-bottom:4px;}
  .pick-banner__text{font-family:var(--display); font-weight:600; font-size:18px;}
  .pick-banner__odds{font-family:var(--display); font-weight:700; font-size:24px; color:var(--gold-bright); white-space:nowrap;}

  details.cat{margin-bottom:10px; border:1px solid var(--hairline); border-radius:8px; overflow:hidden;}
  details.cat[open]{border-color:rgba(201,163,92,0.3);}
  details.cat summary{
    cursor:pointer; padding:13px 16px; font-family:var(--mono); font-size:11px;
    letter-spacing:0.6px; text-transform:uppercase; color:var(--cream); background:var(--panel-2);
    display:flex; justify-content:space-between;
  }
  details.cat summary span{color:var(--slate);}

  .pick-row{
    display:grid; grid-template-columns:1fr auto auto; align-items:center; gap:14px;
    padding:12px 16px; border-top:1px solid var(--hairline); font-size:13px;
  }
  .pick-row__market{font-family:var(--mono); font-size:10px; color:var(--slate); text-transform:uppercase;}
  .pick-row__selection{font-weight:500;}
  .pick-row__style{font-style:italic; font-size:11.5px; color:var(--slate); margin-top:3px;}
  .pick-row__stats{text-align:right; white-space:nowrap;}
  .pick-row__conf{font-family:var(--mono); font-weight:600;}
  .pick-row__odds{font-family:var(--mono); font-size:11.5px; color:var(--gold-bright);}

  .rationale{font-size:12.5px; color:var(--slate); margin:0 0 18px; line-height:1.55;}

  footer{
    max-width:1180px; margin:0 auto; padding:40px 24px 60px; font-size:12px;
    color:var(--slate); line-height:1.65; border-top:1px solid var(--hairline);
  }
  footer strong{color:var(--cream);}
  footer code{background:rgba(0,0,0,0.3); padding:2px 6px; border-radius:3px; font-family:var(--mono);}

  .state-msg{font-family:var(--mono); color:var(--slate); padding:40px 0; font-size:13px;}
  .state-msg code{background:rgba(0,0,0,0.3); padding:2px 6px; border-radius:3px;}

  @media (max-width:640px){
    .match-card__teams{grid-template-columns:1fr; gap:10px;}
    .team--away{flex-direction:row; text-align:left;}
    .match-card__score{order:3;}
    .lb-row{grid-template-columns:32px 1fr; row-gap:6px;}
    .lb-conf{grid-column:2; text-align:left;}
  }
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar__row">
    <div class="brand">
      <div class="brand__mark">WC26 <em>Analytics</em></div>
      <div class="brand__tag">Group Stage</div>
    </div>
    <nav class="nav">
      <a href="#top-picks">Top Picks</a>
      <a href="#value">Value &amp; Longshots</a>
      <a href="#players">Player Spotlight</a>
      <a href="#matches">Match by Match</a>
    </nav>
    <div class="live-tag"><span class="live-dot"></span><span id="ticker-date">Loading…</span></div>
  </div>
</div>

<main>
  <section class="hero">
    <p class="hero__eyebrow">An explainable model, not a black box</p>
    <h1>Today's sharpest reads on the World Cup.</h1>
    <p>Every pick below comes from an independent-Poisson model run on goals, cards, corners,
       and key-player involvement — with the model's own fair odds shown next to each one, so
       you can judge for yourself whether a price is worth taking.</p>
  </section>

  <section class="section" id="top-picks">
    <div class="section__head">
      <div>
        <h2 class="section__title">Today's Top Picks</h2>
        <p class="section__sub">The single highest-confidence selection from every match, ranked by model certainty.</p>
      </div>
    </div>
    <div class="leaderboard" id="leaderboard"><div class="state-msg">Crunching today's fixtures…</div></div>
  </section>

  <section class="section" id="value">
    <div class="section__head">
      <div>
        <h2 class="section__title">Value &amp; Longshot Picks</h2>
        <p class="section__sub">Lower-probability selections with a defensible playing-style case and a meaningfully higher payout — sorted by the model's fair odds.</p>
      </div>
    </div>
    <div class="ticket-row" id="value-tickets"><div class="state-msg">Scouting for value…</div></div>
  </section>

  <section class="section" id="players">
    <div class="section__head">
      <div>
        <h2 class="section__title">Player Spotlight</h2>
        <p class="section__sub">Goalscorer and assist probabilities for each match's key attacking threats, built from their share of their team's expected goals.</p>
      </div>
    </div>
    <div class="ticket-row" id="player-tickets"><div class="state-msg">Sizing up the attackers…</div></div>
  </section>

  <section class="section" id="matches">
    <div class="section__head">
      <div>
        <h2 class="section__title">Match by Match</h2>
        <p class="section__sub">Full breakdown per fixture: predicted possession, goals, cards, corners, and player props — paste in your own bookmaker's odds anywhere to check the edge.</p>
      </div>
    </div>
    <div id="match-cards"><div class="state-msg">Loading match cards…</div></div>
  </section>
</main>

<footer>
  <p><strong>Methodology.</strong> Goals, cards, and corners each run through an independent-Poisson model: team composite ratings (power, discipline, attacking-width) convert into expected values, then the full score-grid is summed for exact probabilities. Player props scale each key player's share of their team's expected goals. Predicted possession is a pre-match model estimate from the same power-rating gap — not a live in-game reading, since no live match-event feed is wired in here. "Fair odds" is simply 100 ÷ confidence. Paste the odds you see at your own bookmaker into any "your odds" box to see the edge against the model instantly — that comparison, not a built-in odds feed, is the most reliable way to spot value without a paid data subscription. If you set <code>ODDS_API_KEY</code> (via The Odds API), the 1X2 picks will also show a live bookmaker price automatically when a match is found.</p>
  <p><strong>This isn't betting advice.</strong> Treat every number here as one statistical opinion, not a guarantee — a 75% pick still loses 1 time in 4. Betting always carries risk of loss, and gambling-advertising and licensing rules vary by country, so check what applies where you are.</p>
</footer>

<script>
const riskClass = (level) => level === "Low" ? "badge--low" : level === "Medium" ? "badge--medium" : "badge--high";

function pickRow(pick){
  const styleLine = pick.style_note ? `<div class="pick-row__style">${pick.style_note}</div>` : '';
  const marketOdds = pick.market_odds ? `<div class="pick-row__style">${pick.bookmaker || 'Market'}: ${pick.market_odds} (edge ${pick.edge_pct > 0 ? '+' : ''}${pick.edge_pct}%)</div>` : '';
  return `
    <div class="pick-row" data-confidence="${pick.confidence}">
      <div>
        <div class="pick-row__market">${pick.market}</div>
        <div class="pick-row__selection">${pick.selection}</div>
        ${styleLine}${marketOdds}
      </div>
      <div class="pick-row__stats">
        <div class="pick-row__conf">${pick.confidence}%</div>
        <div class="pick-row__odds">fair odds ${pick.fair_odds ?? '—'}</div>
      </div>
      <div><span class="badge ${riskClass(pick.risk_level)}">${pick.risk_level}</span></div>
    </div>`;
}

function ticket(pick, matchLabel){
  const styleLine = pick.style_note ? `<div class="ticket__style">${pick.style_note}</div>` : '';
  return `
    <div class="ticket" data-confidence="${pick.confidence}">
      <div class="ticket__match">${matchLabel}</div>
      <div class="ticket__market">${pick.market}</div>
      <div class="ticket__selection">${pick.selection}</div>
      <div class="ticket__perf"></div>
      <div class="ticket__stats">
        <div>
          <span class="ticket__odds">${pick.fair_odds ?? '—'}</span>
          <span class="ticket__odds-label">fair odds</span>
        </div>
        <div class="ticket__conf">${pick.confidence}% confidence<br><span class="badge ${riskClass(pick.risk_level)}">${pick.risk_level}</span></div>
      </div>
      ${styleLine}
      <div class="ticket__check">
        <input type="number" step="0.01" min="1.01" class="odds-input" placeholder="your odds">
        <span class="edge-out"></span>
      </div>
    </div>`;
}

function renderLeaderboard(results){
  document.getElementById('leaderboard').innerHTML = results.map((r, i) => {
    const p = r.prediction.best_pick;
    return `
      <div class="lb-row">
        <div class="lb-rank">${i + 1}</div>
        <div>
          <div class="lb-match">${r.fixture.home.flag} ${r.prediction.matchup} ${r.fixture.away.flag}</div>
          <div class="lb-pick">${p.selection}</div>
          <div class="lb-market">${p.category} · ${p.market}</div>
        </div>
        <div>
          <div class="lb-conf">${p.confidence}%</div>
          <div class="lb-odds">${p.fair_odds ?? '—'}</div>
        </div>
        <div><span class="badge ${riskClass(p.risk_level)}">${p.risk_level}</span></div>
      </div>`;
  }).join('');
}

function renderValueTickets(items){
  const el = document.getElementById('value-tickets');
  if(!items.length){ el.innerHTML = '<div class="state-msg">No standout value picks today.</div>'; return; }
  el.innerHTML = items.map(v => ticket(v.pick, `${v.fixture.home.flag} ${v.fixture.home.name} vs ${v.fixture.away.name} ${v.fixture.away.flag}`)).join('');
}

function renderPlayerTickets(items){
  const el = document.getElementById('player-tickets');
  if(!items.length){ el.innerHTML = '<div class="state-msg">No player props available.</div>'; return; }
  el.innerHTML = items.map(v => ticket(v.pick, `${v.fixture.home.flag} ${v.fixture.home.name} vs ${v.fixture.away.name} ${v.fixture.away.flag}`)).join('');
}

function categoryBlock(title, picks, openByDefault){
  return `
    <details class="cat" ${openByDefault ? 'open' : ''}>
      <summary>${title} <span>${picks.length} markets</span></summary>
      ${picks.map(pickRow).join('')}
    </details>`;
}

function renderMatchCards(results){
  document.getElementById('match-cards').innerHTML = results.map(r => {
    const f = r.fixture, pr = r.prediction, probs = pr.probabilities, poss = pr.expected_possession;
    return `
    <div class="match-card">
      <div class="match-card__meta">
        <span>${f.group} · ${f.venue}</span>
        <span>${f.kickoff_local}</span>
      </div>
      <div class="match-card__teams">
        <div class="team">
          <span class="team__flag">${f.home.flag}</span>
          <div><div class="team__name">${f.home.name}</div><div class="team__rank">${f.home.fifa_rank ? '#' + f.home.fifa_rank + ' FIFA' : ''}</div></div>
        </div>
        <div class="match-card__score">EXPECTED<strong>${pr.expected_goals.home} – ${pr.expected_goals.away}</strong>most likely: ${probs.most_likely_score}</div>
        <div class="team team--away">
          <span class="team__flag">${f.away.flag}</span>
          <div><div class="team__name">${f.away.name}</div><div class="team__rank">${f.away.fifa_rank ? '#' + f.away.fifa_rank + ' FIFA' : ''}</div></div>
        </div>
      </div>

      <div class="poss">
        <div class="poss__label"><span>${f.home.name} ${poss.home}%</span><span>Predicted possession</span><span>${poss.away}% ${f.away.name}</span></div>
        <div class="poss__bar">
          <div class="poss__seg--home" style="width:${poss.home}%"></div>
          <div class="poss__seg--away" style="width:${poss.away}%"></div>
        </div>
      </div>

      <div class="gauge">
        <div class="gauge__bar">
          <div class="gauge__seg gauge__seg--home" style="width:${probs.home_win}%"></div>
          <div class="gauge__seg gauge__seg--draw" style="width:${probs.draw}%"></div>
          <div class="gauge__seg gauge__seg--away" style="width:${probs.away_win}%"></div>
        </div>
        <div class="gauge__labels">
          <span><b>${probs.home_win}%</b> ${f.home.name}</span>
          <span><b>${probs.draw}%</b> Draw</span>
          <span><b>${probs.away_win}%</b> ${f.away.name}</span>
        </div>
      </div>

      <div class="pick-banner">
        <div>
          <div class="pick-banner__label">Best pick · ${pr.best_pick.category} · ${pr.best_pick.market}</div>
          <div class="pick-banner__text">${pr.best_pick.selection}</div>
        </div>
        <div class="pick-banner__odds">${pr.best_pick.confidence}%<br><span style="font-size:13px;color:var(--gold-bright)">${pr.best_pick.fair_odds ?? ''}</span></div>
      </div>
      <p class="rationale">${pr.best_pick.rationale}</p>

      ${categoryBlock('Goals', pr.categories.Goals, true)}
      ${categoryBlock('Cards', pr.categories.Cards, false)}
      ${categoryBlock('Corners', pr.categories.Corners, false)}
      ${categoryBlock('Players', pr.categories.Players, false)}
    </div>`;
  }).join('');
}

async function load(){
  try{
    const res = await fetch('/api/predictions/today');
    if(!res.ok) throw new Error('bad response');
    const data = await res.json();
    document.getElementById('ticker-date').textContent = data.date;
    renderLeaderboard(data.results);
    renderValueTickets(data.top_value_picks);
    renderPlayerTickets(data.top_player_props);
    renderMatchCards(data.results);
  }catch(err){
    const msg = `<div class="state-msg">Couldn't reach the backend. Make sure it's running:<br><br><code>python main.py</code><br><br>then reload this page at <code>http://127.0.0.1:8000</code>.</div>`;
    document.getElementById('leaderboard').innerHTML = msg;
    ['value-tickets','player-tickets','match-cards'].forEach(id => document.getElementById(id).innerHTML = '');
    document.getElementById('ticker-date').textContent = '—';
  }
}

document.addEventListener('input', (e) => {
  if(!e.target.matches('.odds-input')) return;
  const row = e.target.closest('.pick-row, .ticket');
  const conf = parseFloat(row.dataset.confidence);
  const odds = parseFloat(e.target.value);
  const out = row.querySelector('.edge-out');
  if(!odds || odds <= 1){ out.textContent=''; return; }
  const implied = 100 / odds;
  const edge = conf - implied;
  out.textContent = (edge > 0 ? '+' : '') + edge.toFixed(1) + '% edge';
  out.className = 'edge-out ' + (edge > 2 ? 'edge-positive' : edge < -2 ? 'edge-negative' : 'edge-neutral');
});

load();
</script>
</body>
</html>
'''


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    uvicorn.run("main:app", host=host, port=port, reload=False)
