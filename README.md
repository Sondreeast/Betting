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

## How the predictions work

This is not a black box. Each match runs through an **independent Poisson
goal model**:

1. Each team has a `power_rating` (0–100). For the four seeded matches
   these come from the June 2026 FIFA World Ranking where available
   (England #4, Portugal #5, Croatia #11, Colombia #13 — exact ranking
   points) and from pre-tournament form reporting for sides outside the
   ranked top tier (DR Congo #56, Uzbekistan #50, Ghana #73, Panama).
2. The rating ratio between the two teams is converted into expected
   goals (xG) for each side.
3. The full score-grid (0–8 goals each way) is summed using the Poisson
   distribution to get exact probabilities for: match result (1X2),
   Over/Under 2.5 goals, and Both Teams To Score.
4. The highest-probability outcome across all markets becomes that
   match's "best value pick." All four matches' best picks are then
   ranked together — that ranking is "today's most likely bets."

This is the same family of model (Dixon-Coles style) used by most public
football-prediction tools, simplified for transparency. It is **not**
the same as a sportsbook's actual odds, which also price in injuries,
breaking team news, weather, and the bookmaker's margin.

### Optional: AI-written rationale

Set an `OPENAI_API_KEY` environment variable before starting the backend
and it will use an LLM (via `langchain-openai`, install separately) to
rewrite each pick's rationale in plain language. Without a key, it falls
back to a template-generated explanation built directly from the model's
numbers — the site works fully either way.

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
