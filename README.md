# World Cup 2026 AI Betting Assistant

A small full-stack app: a FastAPI backend running a statistical prediction
engine, and a dashboard frontend that shows today's matches ranked by how
confident the model is in its top pick.

Seeded with **today's real fixtures** (17 June 2026, Group K & Group L):
Portugal vs DR Congo, England vs Croatia, Ghana vs Panama, Uzbekistan vs
Colombia — using real FIFA World Ranking positions and pre-tournament form
as inputs.

## Run it

```bash
pip install -r requirements.txt
python main.py
```

Then open **http://127.0.0.1:8000** in your browser. (The frontend HTML is embedded directly in `main.py` — there's no separate `backend/`/`frontend/` folder split, just one file plus its dependencies.)

## What's on the site

- **Today's Top Picks** — the single highest-confidence selection from every match, ranked.
- **Value & Longshot Picks** — lower-probability bets (roughly 12–45% model confidence, i.e. fair odds around 2.2–8) that still have a defensible playing-style case, sorted by payout. This is where the model surfaces longer-odds bets worth a look rather than just the safest favorites.
- **Player Spotlight** — anytime-goalscorer and to-assist probabilities for each match's key attacking players, scaled from their share of their team's expected goals.
- **Match by Match** — full breakdown per fixture: predicted possession, Goals (1X2, Over/Under at three lines, BTTS), Cards (totals + most-booked team), Corners (totals + most-corners team), and Players, each with the model's fair odds.
- **"Your odds" value-check** — every single pick has a small input where you paste in the odds your own bookmaker is offering; the page instantly computes the edge (model probability vs. the odds' implied probability) — no API key or subscription needed for this part.

## How the predictions work

This is not a black box. Goals, Cards, and Corners each run through an **independent Poisson
goal model**:

1. Each team has composite ratings: `power_rating` (goals), `card_rate` (discipline), `corner_rate`
   (attacking width) — all 0–100. For the four seeded matches, `power_rating` comes from the
   June 2026 FIFA World Ranking where available (England #4, Portugal #5, Croatia #11, Colombia
   #13 — exact ranking points) and from pre-tournament form reporting for sides outside the
   ranked top tier (DR Congo #56, Uzbekistan #50, Ghana #73, Panama). `card_rate`/`corner_rate`
   are illustrative composites based on each team's general playing style — not an official stat.
2. Each rating ratio converts into an expected value (xG, expected cards, expected corners) per
   side, then the full score-grid is summed with the Poisson distribution to get exact
   probabilities for every market.
3. **Player props** scale each key player's share of their team's expected goals (a "goal_share"
   / "assist_share" composite, e.g. Cristiano Ronaldo ≈ 42% of Portugal's attacking output) into
   their own scoring/assisting probability.
4. **Predicted possession** is a pre-match estimate derived from the same power-rating gap used
   for goals — it is **not** a live in-game reading, since there's no live match-event feed wired
   in here (see "Live data" below).
5. **Fair odds** on every pick is simply `100 ÷ confidence` — the price at which the bet would be
   break-even if the model's probability is correct.
6. The highest-confidence selection across every category becomes that match's "best pick."
   Selections in the 12–45% confidence band get pulled into the cross-match **Value & Longshot**
   list, sorted by payout.

This is the same family of model (Dixon-Coles style) used by most public football-prediction
tools, simplified for transparency. It is **not** the same as a sportsbook's actual odds, which
also price in injuries, breaking team news, weather, and the bookmaker's margin.

### Optional: AI-written rationale

Set an `OPENAI_API_KEY` environment variable before starting the backend
and it will use an LLM (via `langchain-openai`, install separately) to
rewrite each pick's rationale in plain language. Without a key, it falls
back to a template-generated explanation built directly from the model's
numbers — the site works fully either way.

### Optional: live bookmaker odds

Set an `ODDS_API_KEY` environment variable (get a free key at
[the-odds-api.com](https://the-odds-api.com)) and the 1X2 picks will automatically pull a real
bookmaker price and show the edge against the model — no manual paste-in needed for that market.
Without a key, this silently does nothing; the "your odds" boxes everywhere else on the site work
regardless, since they need no API key at all.

## Deploy it for free (Render)

Render needs your code in a GitHub (or GitLab/Bitbucket) repo — it doesn't accept a raw zip upload. No command line required:

1. **Get the code onto GitHub.** Delete anything currently in your repo, then upload these four files straight into the repo root (not inside any subfolder): `main.py`, `requirements.txt`, `render.yaml`, `README.md`. Commit to `main`.
2. **Create a Render account** at render.com — signing in with GitHub is fastest, since it connects your repos automatically.
3. **Dashboard → New → Blueprint**, then pick the repo. Render reads `render.yaml` and pre-fills everything (Python runtime, build/start commands, free plan). Review and click **Deploy Blueprint**.
   - If Render doesn't offer the Blueprint option, use **New → Web Service** instead, select the repo, and fill in manually: Build Command `pip install -r requirements.txt`, Start Command `uvicorn main:app --host 0.0.0.0 --port $PORT`, Instance Type `Free`. Leave Root Directory blank.
4. Wait for the build to finish (a few minutes), then open the `https://<your-service>.onrender.com` URL Render gives you.

One quirk of Render's free tier: the service spins down after periods of inactivity and takes 30–60 seconds to wake back up on the next visit. That's normal, not a bug.

## Wiring in live data

Replace the `FIXTURES` list in `main.py` with a call to a live
provider — API-Football, Sportmonks, and Sportradar all have World Cup
endpoints with live scores, lineups, and historical stats. Feed their
numbers into `power_rating` (or extend `expected_goals()` to use richer
inputs like attack/defense splits) and the rest of the pipeline — Poisson
math, ranking, frontend — works unchanged. `POST /api/predict-custom`
already accepts any two teams' stats if you want to test this without
touching the seed data.

## Scope and a few things worth knowing

- This is an **analysis tool**, not a sportsbook: it doesn't process bets,
  hold funds, or manage accounts.
- Treat its output as one statistical opinion, not a guarantee — upsets
  happen, and "74% probability" still means the other outcome happens
  roughly 1 in 4 times.
- If you plan to publish or commercialize something like this, check
  the gambling-advertising and licensing rules in your jurisdiction —
  Norway, for example, restricts advertising for gambling operators not
  licensed by Norsk Tipping/Rikstoto, and rules vary widely by country.
