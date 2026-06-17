"""
World Cup 2026 AI Betting Assistant — backend
===============================================

A small FastAPI service that:
  1. Holds today's fixtures (seeded below with real Group K / Group L
     matchups and FIFA rankings as of 17 June 2026).
  2. Runs an explainable statistical prediction engine (independent
     Poisson goal model) to estimate 1X2, Over/Under 2.5, and BTTS
     probabilities for each match.
  3. Ranks the resulting picks by model confidence, i.e. "the bets that
     are most possible" across today's slate.
  4. Optionally hands the numbers to an LLM (only if OPENAI_API_KEY is
     set) to write a plain-language rationale on top of the math. With
     no key set, it falls back to a template-generated rationale, so the
     whole thing runs out of the box with zero paid API dependencies.

Run it:
    pip install -r requirements.txt
    python main.py
    open http://127.0.0.1:8000

Swap in real data:
  Replace the FIXTURES list below with a live call to a provider like
  API-Football, Sportmonks or Sportradar. The engine only needs each
  team's `power_rating` (or richer inputs — see `expected_goals()`) to run.
"""

import math
import os
from datetime import date
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="World Cup 2026 AI Betting Assistant", version="1.0.0")


# ---------------------------------------------------------------------------
# 1. Data models
# ---------------------------------------------------------------------------

class TeamStats(BaseModel):
    name: str
    code: str
    flag: str
    power_rating: float = Field(..., description="0-100 composite strength index")
    fifa_rank: Optional[int] = None
    form_note: str = ""


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
    market: str
    selection: str
    confidence: float
    risk_level: str
    rationale: str


class MatchPrediction(BaseModel):
    fixture_id: str
    matchup: str
    expected_goals: Dict[str, float]
    probabilities: OutcomeProbabilities
    best_pick: BettingPick
    all_picks: List[BettingPick]
    engine: str


# ---------------------------------------------------------------------------
# 2. Seed data — real Group K & Group L fixtures, 17 June 2026
#
# power_rating is a simplified 0-100 composite built from the June 2026
# FIFA World Ranking (England #4 / 1825.97 pts, Portugal #5 / 1763.83,
# Croatia #11 / 1717.07, Colombia #13 / 1693.09 — exact points) plus
# pre-tournament form reporting for sides outside the ranked top tier
# (DR Congo #56, Uzbekistan #50, Ghana #73, Panama unranked top-60).
# These are illustrative inputs for the demo engine, not an official
# FIFA metric — wire in a live ratings feed for production use.
# ---------------------------------------------------------------------------

def team(name, code, flag, rating, rank=None, note=""):
    return TeamStats(name=name, code=code, flag=flag, power_rating=rating, fifa_rank=rank, form_note=note)


PORTUGAL = team("Portugal", "POR", "🇵🇹", 87, 5, "Ronaldo-led attack, pot-1 seed, heavy Group K favorites")
DRCONGO = team("DR Congo", "COD", "🇨🇩", 40, 56, "First World Cup since 1974, knocked out Cameroon & Nigeria in playoffs")
ENGLAND = team("England", "ENG", "🏴", 90, 4, "Kane, Bellingham, Rice — several pundits' dark-horse pick to win it all")
CROATIA = team("Croatia", "CRO", "🇭🇷", 78, 11, "2018 runners-up, perennial knockout-stage overachievers")
UZBEKISTAN = team("Uzbekistan", "UZB", "🇺🇿", 36, 50, "Tournament debut, squad mostly plays in the domestic league")
COLOMBIA = team("Colombia", "COL", "🇨🇴", 76, 13, "Luis Díaz leads the attack, tipped to handle the US summer heat well")
GHANA = team("Ghana", "GHA", "🇬🇭", 56, 73, "Won just 1 of last 7, coach sacked in March, missing injured Kudus")
PANAMA = team("Panama", "PAN", "🇵🇦", 52, None, "Unbeaten through qualifying, still chasing a first-ever World Cup win")

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
# 3. Prediction engine — independent Poisson goal model
#
# Each team's power rating is converted into an expected-goals (xG) value
# via a rating ratio raised to a damping exponent, then the full score-grid
# Poisson distribution is summed to get 1X2 / Over-Under / BTTS probabilities.
# This is the same family of model used by most public football-prediction
# tools (e.g. the classic Dixon-Coles approach), simplified for clarity.
# ---------------------------------------------------------------------------

BASE_GOALS = 1.35       # average goals per side in a competitive international match
RATING_EXPONENT = 0.75  # damping factor so rating gaps don't blow up xG
MAX_GOALS = 8           # grid size for the Poisson summation


def poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def expected_goals(home: TeamStats, away: TeamStats) -> Dict[str, float]:
    ratio = home.power_rating / away.power_rating
    scaled = ratio ** RATING_EXPONENT
    return {
        "home": round(BASE_GOALS * scaled, 2),
        "away": round(BASE_GOALS / scaled, 2),
    }


def risk_level(p: float) -> str:
    if p >= 60:
        return "Low"
    if p >= 45:
        return "Medium"
    return "High"


def run_engine(fixture: Fixture) -> MatchPrediction:
    xg = expected_goals(fixture.home, fixture.away)
    lam_h, lam_a = xg["home"], xg["away"]

    home_win = draw = away_win = 0.0
    over_2_5 = btts_yes = 0.0
    best_score, best_score_p = (0, 0), 0.0

    for i in range(MAX_GOALS + 1):
        p_i = poisson_pmf(i, lam_h)
        for j in range(MAX_GOALS + 1):
            p = p_i * poisson_pmf(j, lam_a)
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p
            if i + j > 2.5:
                over_2_5 += p
            if i >= 1 and j >= 1:
                btts_yes += p
            if p > best_score_p:
                best_score_p, best_score = p, (i, j)

    probs = OutcomeProbabilities(
        home_win=round(home_win * 100, 1),
        draw=round(draw * 100, 1),
        away_win=round(away_win * 100, 1),
        over_2_5=round(over_2_5 * 100, 1),
        under_2_5=round((1 - over_2_5) * 100, 1),
        btts_yes=round(btts_yes * 100, 1),
        btts_no=round((1 - btts_yes) * 100, 1),
        most_likely_score=f"{best_score[0]}-{best_score[1]}",
    )

    candidates = [
        ("1X2", f"{fixture.home.name} to win", probs.home_win),
        ("1X2", "Draw", probs.draw),
        ("1X2", f"{fixture.away.name} to win", probs.away_win),
        ("Total goals", "Over 2.5 goals", probs.over_2_5),
        ("Total goals", "Under 2.5 goals", probs.under_2_5),
        ("Both teams to score", "Yes", probs.btts_yes),
        ("Both teams to score", "No", probs.btts_no),
    ]
    candidates.sort(key=lambda c: c[2], reverse=True)

    def rationale(selection: str, p: float) -> str:
        return (
            f"Model expects a {lam_h:.2f}-{lam_a:.2f} scoreline from power ratings of "
            f"{fixture.home.name} {fixture.home.power_rating} vs {fixture.away.name} "
            f"{fixture.away.power_rating}, putting '{selection}' at {p}% probability."
        )

    picks = [
        BettingPick(market=m, selection=s, confidence=p, risk_level=risk_level(p), rationale=rationale(s, p))
        for (m, s, p) in candidates[:5]
    ]

    return MatchPrediction(
        fixture_id=fixture.id,
        matchup=f"{fixture.home.name} vs {fixture.away.name}",
        expected_goals=xg,
        probabilities=probs,
        best_pick=picks[0],
        all_picks=picks,
        engine="poisson-v1",
    )


def llm_enhance(prediction: MatchPrediction) -> Optional[str]:
    """Optional: rewrite the top pick's rationale in natural language via an LLM.
    Only runs if OPENAI_API_KEY is set in the environment; otherwise the
    template-based rationale from run_engine() is used as-is."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model="gpt-4o", temperature=0.3)
        prompt = (
            "You are a football data analyst. In 2-3 plain-language sentences, explain why "
            f"a statistical model favors '{prediction.best_pick.selection}' "
            f"({prediction.best_pick.confidence}% probability) for {prediction.matchup}, given "
            f"expected goals {prediction.expected_goals}. End by noting this is a statistical "
            "estimate, not a guarantee."
        )
        response = llm.invoke(prompt)
        return response.content.strip()
    except Exception:
        return None


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
    prediction = run_engine(fixture)
    enhanced = llm_enhance(prediction)
    if enhanced:
        prediction.best_pick.rationale = enhanced
    return prediction


@app.get("/api/predictions/today")
async def predictions_today():
    """The 'most likely' bets across all of today's fixtures, ranked by model confidence."""
    results = []
    for fixture in FIXTURES:
        prediction = run_engine(fixture)
        results.append({"fixture": fixture, "prediction": prediction})
    results.sort(key=lambda r: r["prediction"].best_pick.confidence, reverse=True)
    return {"date": str(date.today()), "disclaimer": DISCLAIMER, "results": results}


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
<title>World Cup 2026 · AI Betting Assistant</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;800&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --pitch:#0E2B1C;
    --pitch-line:#173A26;
    --pitch-card:#163924;
    --chalk:#F4F7F2;
    --chalk-dim:#9FB3A8;
    --red:#E2342D;
    --amber:#E8A33D;
    --slate:#5C6B63;
    --display:'Big Shoulders Display', sans-serif;
    --mono:'IBM Plex Mono', monospace;
    --body:'IBM Plex Sans', sans-serif;
  }
  *{box-sizing:border-box;}
  body{
    margin:0;
    background:var(--pitch);
    background-image:
      repeating-linear-gradient(0deg, rgba(255,255,255,0.025) 0px, rgba(255,255,255,0.025) 1px, transparent 1px, transparent 64px);
    color:var(--chalk);
    font-family:var(--body);
    min-height:100vh;
  }
  a{color:inherit;}

  /* ---------- header ticker ---------- */
  .ticker{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:16px;
    padding:14px 28px;
    border-bottom:2px solid var(--pitch-line);
    background:rgba(0,0,0,0.18);
    flex-wrap:wrap;
  }
  .ticker__brand{
    display:flex;
    align-items:baseline;
    gap:10px;
  }
  .ticker__brand h1{
    font-family:var(--display);
    font-weight:800;
    font-size:clamp(22px, 3vw, 30px);
    letter-spacing:0.5px;
    margin:0;
    text-transform:uppercase;
  }
  .ticker__brand span{
    font-family:var(--mono);
    font-size:12px;
    color:var(--chalk-dim);
    letter-spacing:1px;
  }
  .ticker__live{
    display:flex;
    align-items:center;
    gap:8px;
    font-family:var(--mono);
    font-size:12px;
    letter-spacing:1.5px;
    color:var(--chalk-dim);
  }
  .ticker__dot{
    width:8px; height:8px; border-radius:50%;
    background:var(--red);
    box-shadow:0 0 0 0 rgba(226,52,45,0.7);
    animation:pulse 1.8s infinite;
  }
  @keyframes pulse{
    0%{box-shadow:0 0 0 0 rgba(226,52,45,0.55);}
    70%{box-shadow:0 0 0 7px rgba(226,52,45,0);}
    100%{box-shadow:0 0 0 0 rgba(226,52,45,0);}
  }

  main{max-width:1080px; margin:0 auto; padding:36px 24px 80px;}

  /* ---------- hero: today's best bets ---------- */
  .hero__eyebrow{
    font-family:var(--mono);
    font-size:12px;
    letter-spacing:2px;
    color:var(--amber);
    text-transform:uppercase;
    margin:0 0 8px;
  }
  .hero h2{
    font-family:var(--display);
    font-weight:800;
    font-size:clamp(34px, 6vw, 56px);
    line-height:0.98;
    margin:0 0 28px;
    text-transform:uppercase;
    max-width:680px;
  }
  .leaderboard{
    display:flex;
    flex-direction:column;
    border:1px solid var(--pitch-line);
    border-radius:4px;
    overflow:hidden;
  }
  .leaderboard__row{
    display:grid;
    grid-template-columns:56px 1fr auto auto;
    align-items:center;
    gap:18px;
    padding:16px 20px;
    border-bottom:1px solid var(--pitch-line);
    background:var(--pitch-card);
  }
  .leaderboard__row:last-child{border-bottom:none;}
  .leaderboard__rank{
    font-family:var(--display);
    font-weight:800;
    font-size:28px;
    color:var(--chalk-dim);
  }
  .leaderboard__match{
    font-family:var(--mono);
    font-size:12px;
    color:var(--chalk-dim);
    text-transform:uppercase;
    letter-spacing:0.5px;
  }
  .leaderboard__pick{
    font-family:var(--body);
    font-weight:600;
    font-size:17px;
    margin-top:3px;
  }
  .leaderboard__market{
    font-family:var(--mono);
    font-size:11px;
    color:var(--chalk-dim);
    text-transform:uppercase;
  }
  .leaderboard__conf{
    text-align:right;
  }
  .leaderboard__conf strong{
    font-family:var(--display);
    font-weight:800;
    font-size:30px;
    display:block;
  }
  .badge{
    font-family:var(--mono);
    font-size:11px;
    letter-spacing:0.5px;
    text-transform:uppercase;
    padding:4px 10px;
    border-radius:100px;
    border:1px solid currentColor;
    white-space:nowrap;
  }
  .badge--low{color:var(--chalk);}
  .badge--medium{color:var(--amber);}
  .badge--high{color:var(--red);}

  /* ---------- match cards ---------- */
  .section-eyebrow{
    font-family:var(--mono);
    font-size:12px;
    letter-spacing:2px;
    color:var(--chalk-dim);
    text-transform:uppercase;
    margin:64px 0 18px;
  }
  .card{
    border:1px solid var(--pitch-line);
    border-radius:4px;
    background:var(--pitch-card);
    padding:24px;
    margin-bottom:20px;
  }
  .card__meta{
    display:flex;
    justify-content:space-between;
    font-family:var(--mono);
    font-size:11px;
    color:var(--chalk-dim);
    text-transform:uppercase;
    letter-spacing:0.5px;
    margin-bottom:18px;
  }
  .card__teams{
    display:grid;
    grid-template-columns:1fr auto 1fr;
    align-items:center;
    gap:18px;
    margin-bottom:22px;
  }
  .team{display:flex; align-items:center; gap:12px;}
  .team--away{flex-direction:row-reverse; text-align:right;}
  .team__flag{font-size:30px; line-height:1;}
  .team__name{font-family:var(--display); font-weight:700; font-size:22px; text-transform:uppercase; line-height:1;}
  .team__note{font-size:12px; color:var(--chalk-dim); margin-top:4px; max-width:220px;}
  .card__score{
    font-family:var(--mono);
    font-size:13px;
    color:var(--chalk-dim);
    text-align:center;
    white-space:nowrap;
  }
  .card__score strong{
    display:block;
    font-family:var(--display);
    font-size:28px;
    color:var(--chalk);
    font-weight:800;
  }

  /* signature element: yard-line confidence gauge */
  .gauge{margin-bottom:20px;}
  .gauge__bar{
    position:relative;
    height:34px;
    display:flex;
    border-radius:3px;
    overflow:hidden;
    border:1px solid var(--pitch-line);
  }
  .gauge__seg{height:100%; transition:width 0.4s ease;}
  .gauge__seg--home{background:var(--chalk);}
  .gauge__seg--draw{background:var(--slate);}
  .gauge__seg--away{background:var(--amber);}
  .gauge__ticks{
    position:absolute; inset:0;
    display:flex;
    pointer-events:none;
  }
  .gauge__tick{flex:1; border-right:1px solid rgba(0,0,0,0.18);}
  .gauge__tick:last-child{border-right:none;}
  .gauge__labels{
    display:flex;
    justify-content:space-between;
    font-family:var(--mono);
    font-size:11px;
    color:var(--chalk-dim);
    margin-top:8px;
    text-transform:uppercase;
  }
  .gauge__labels b{color:var(--chalk); font-weight:600;}

  .pick-banner{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:16px;
    background:rgba(0,0,0,0.22);
    border-left:3px solid var(--amber);
    padding:14px 16px;
    border-radius:2px;
    flex-wrap:wrap;
  }
  .pick-banner__label{
    font-family:var(--mono);
    font-size:11px;
    letter-spacing:1px;
    color:var(--amber);
    text-transform:uppercase;
    margin-bottom:4px;
  }
  .pick-banner__text{font-family:var(--body); font-weight:600; font-size:16px;}
  .pick-banner__conf{
    font-family:var(--display);
    font-weight:800;
    font-size:26px;
    white-space:nowrap;
  }

  details.all-picks{margin-top:14px;}
  details.all-picks summary{
    cursor:pointer;
    font-family:var(--mono);
    font-size:11px;
    letter-spacing:0.5px;
    text-transform:uppercase;
    color:var(--chalk-dim);
  }
  .pick-list{margin:12px 0 0; padding:0; list-style:none;}
  .pick-list li{
    display:flex;
    justify-content:space-between;
    gap:12px;
    padding:8px 0;
    border-top:1px solid var(--pitch-line);
    font-size:13px;
  }
  .pick-list .market{color:var(--chalk-dim); font-family:var(--mono); font-size:11px; text-transform:uppercase;}
  .pick-list .conf{font-family:var(--mono); white-space:nowrap;}

  .rationale{font-size:13px; color:var(--chalk-dim); margin-top:14px; line-height:1.5;}

  footer{
    max-width:1080px; margin:0 auto;
    padding:0 24px 48px;
    font-size:12px;
    color:var(--chalk-dim);
    line-height:1.6;
    border-top:1px solid var(--pitch-line);
    padding-top:24px;
  }
  footer strong{color:var(--chalk);}

  .state-msg{
    font-family:var(--mono);
    color:var(--chalk-dim);
    padding:40px 0;
    font-size:14px;
  }
  .state-msg code{background:rgba(0,0,0,0.3); padding:2px 6px; border-radius:3px;}

  @media (max-width:640px){
    .card__teams{grid-template-columns:1fr; gap:10px;}
    .team--away{flex-direction:row; text-align:left;}
    .card__score{order:3;}
    .leaderboard__row{grid-template-columns:36px 1fr; row-gap:6px;}
    .leaderboard__conf{grid-column:2; text-align:left;}
  }
</style>
</head>
<body>

<div class="ticker">
  <div class="ticker__brand">
    <h1>World Cup 2026 AI Betting Assistant</h1>
    <span>GROUP STAGE</span>
  </div>
  <div class="ticker__live"><span class="ticker__dot"></span><span id="ticker-date">LOADING…</span></div>
</div>

<main>
  <section class="hero">
    <p class="hero__eyebrow">Ranked by model confidence</p>
    <h2>Today's most&nbsp;likely&nbsp;bets</h2>
    <div class="leaderboard" id="leaderboard">
      <div class="state-msg">Crunching today's fixtures…</div>
    </div>
  </section>

  <p class="section-eyebrow">Full match breakdown</p>
  <div id="cards">
    <div class="state-msg">Loading match cards…</div>
  </div>
</main>

<footer>
  <p><strong>Methodology.</strong> Each pick comes from an independent-Poisson goal model: team power ratings (derived from the June 2026 FIFA World Ranking and pre-tournament form) are converted into expected goals, then the full score-grid is summed to get win/draw/loss, Over/Under 2.5, and BTTS probabilities. "Confidence" is simply that probability; "risk level" buckets it (Low ≥60%, Medium 45–59%, High &lt;45%). Set an <code>OPENAI_API_KEY</code> environment variable to have an LLM rewrite the rationale in plain language — the math stays the same either way.</p>
  <p><strong>This isn't betting advice.</strong> It's a statistical estimate for analysis purposes — actual odds, injuries, lineups and form can all move the real probabilities. Betting always carries risk of loss, and rules on licensed gambling and gambling advertising vary by country, so check what applies where you are before using this for real wagers.</p>
</footer>

<script>
const API_BASE = "";

function riskClass(level){
  return level === "Low" ? "badge--low" : level === "Medium" ? "badge--medium" : "badge--high";
}

function renderLeaderboard(results){
  const el = document.getElementById('leaderboard');
  el.innerHTML = results.map((r, i) => {
    const p = r.prediction.best_pick;
    return `
      <div class="leaderboard__row">
        <div class="leaderboard__rank">${i + 1}</div>
        <div>
          <div class="leaderboard__match">${r.fixture.home.flag} ${r.prediction.matchup} ${r.fixture.away.flag}</div>
          <div class="leaderboard__pick">${p.selection}</div>
          <div class="leaderboard__market">${p.market}</div>
        </div>
        <div class="leaderboard__conf"><strong>${p.confidence}%</strong></div>
        <div><span class="badge ${riskClass(p.risk_level)}">${p.risk_level} risk</span></div>
      </div>`;
  }).join('');
}

function renderCards(results){
  const el = document.getElementById('cards');
  el.innerHTML = results.map(r => {
    const f = r.fixture, pr = r.prediction, probs = pr.probabilities;
    const otherPicks = pr.all_picks.slice(1).map(pk => `
      <li>
        <span><span class="market">${pk.market}</span><br>${pk.selection}</span>
        <span class="conf">${pk.confidence}% · ${pk.risk_level}</span>
      </li>`).join('');
    return `
    <div class="card">
      <div class="card__meta">
        <span>${f.group} · ${f.venue}</span>
        <span>${f.kickoff_local}</span>
      </div>
      <div class="card__teams">
        <div class="team">
          <span class="team__flag">${f.home.flag}</span>
          <div><div class="team__name">${f.home.name}</div><div class="team__note">${f.home.form_note}</div></div>
        </div>
        <div class="card__score">EXPECTED<strong>${pr.expected_goals.home} – ${pr.expected_goals.away}</strong>most likely: ${probs.most_likely_score}</div>
        <div class="team team--away">
          <span class="team__flag">${f.away.flag}</span>
          <div><div class="team__name">${f.away.name}</div><div class="team__note">${f.away.form_note}</div></div>
        </div>
      </div>

      <div class="gauge">
        <div class="gauge__bar">
          <div class="gauge__seg gauge__seg--home" style="width:${probs.home_win}%"></div>
          <div class="gauge__seg gauge__seg--draw" style="width:${probs.draw}%"></div>
          <div class="gauge__seg gauge__seg--away" style="width:${probs.away_win}%"></div>
          <div class="gauge__ticks">${Array.from({length:10}).map(()=>'<div class="gauge__tick"></div>').join('')}</div>
        </div>
        <div class="gauge__labels">
          <span><b>${probs.home_win}%</b> ${f.home.name}</span>
          <span><b>${probs.draw}%</b> Draw</span>
          <span><b>${probs.away_win}%</b> ${f.away.name}</span>
        </div>
      </div>

      <div class="pick-banner">
        <div>
          <div class="pick-banner__label">Best value pick · ${pr.best_pick.market}</div>
          <div class="pick-banner__text">${pr.best_pick.selection}</div>
        </div>
        <div class="pick-banner__conf">${pr.best_pick.confidence}%</div>
      </div>
      <p class="rationale">${pr.best_pick.rationale}</p>

      <details class="all-picks">
        <summary>Show all markets (${pr.all_picks.length})</summary>
        <ul class="pick-list">${otherPicks}</ul>
      </details>
    </div>`;
  }).join('');
}

async function load(){
  try{
    const res = await fetch(`${API_BASE}/api/predictions/today`);
    if(!res.ok) throw new Error('bad response');
    const data = await res.json();
    document.getElementById('ticker-date').textContent = data.date;
    renderLeaderboard(data.results);
    renderCards(data.results);
  }catch(err){
    const msg = `<div class="state-msg">Couldn't reach the backend. Make sure it's running:<br><br><code>cd backend && python main.py</code><br><br>then reload this page at <code>http://127.0.0.1:8000</code>.</div>`;
    document.getElementById('leaderboard').innerHTML = msg;
    document.getElementById('cards').innerHTML = '';
    document.getElementById('ticker-date').textContent = '—';
  }
}
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
