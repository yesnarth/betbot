# BetBot — AI-driven football pronosticator

Production-grade quantitative football betting system. Combines a Dixon-Coles
Poisson model with xG, Club ELO, weather, web-search news and an AI agent
(Claude Sonnet 4.6) that cross-checks every recommendation across multiple
independent signals.

## Stack

```
Streamlit dashboard  →  FastAPI  →  Claude Agent SDK  →  MCP server (20+ tools)
                                                              ↓
                          PostgreSQL + Redis        external data sources
                                                    (Odds, FootData, ClubElo,
                                                     Understat, Open-Meteo,
                                                     API-Football, Tavily)
```

## Quick start (local)

```bash
# 1. Configure .env
cp .env.example .env
# edit .env → fill in the API keys you have

# 2. Start the stack
make up

# 3. First-time data load (~5 min)
make migrate
make scan              # populates Postgres with team stats
make enrich            # adds ELO + xG signals

# 4. Open the dashboard
make dashboard         # → http://localhost:8501
```

## Production deployment

Requires a domain pointing at your VPS and ports 80/443 open.

```bash
# .env extras for prod:
#   BETBOT_DOMAIN=betbot.yourdomain.com
#   ACME_EMAIL=you@yourdomain.com
#   POSTGRES_PASSWORD=<random 32-char string>
#   API_BASIC_PASSWORD=<random 32-char string>
#   ANTHROPIC_API_KEY=...

make prod-up           # builds, runs migrations, provisions HTTPS via Let's Encrypt
```

Caddy automatically:
- terminates TLS, renews certs, supports HTTP/3
- routes `/api/*` → FastAPI, everything else → Streamlit
- adds HSTS, security headers, gzip/zstd

## Operations

| Action | Command |
|---|---|
| Run scan + email | `make scan` |
| Dry-run (no email) | `make dry-run` |
| Refresh ELO + xG | `make enrich` |
| Resolve pending bets | `make resolve` |
| Backtest a league | `make backtest` |
| Tail logs | `make logs` |
| Run tests | `make test` |
| Stop stack | `make down` |
| Wipe everything | `make fresh` |

## Architecture

### Prediction model (blended)

```
λ = (1 - elo_w - xg_w) × λ_dixon_coles
  + xg_w × λ_xG
  + weather_modifier on both sides

P(home, draw, away) = Poisson(λ) shrunk toward Elo prior (Bayesian)
```

Three independent signals, each contributing:
- **Dixon-Coles** on goals (base λ)
- **xG** from Understat — strips finishing variance
- **ELO** from Club Elo — long-term stable strength
- **Weather** from Open-Meteo — heavy rain/wind dampens goals

### Agent flow

1. User filters in the dashboard → `/agent/recommend`
2. FastAPI invokes Claude Sonnet 4.6 via `claude-agent-sdk`
3. Agent calls MCP tools in a reasoning loop:
   - `find_value_bets` — initial candidates
   - `predict_match` + `compare_elo` + `get_xg_stats` — cross-checks
   - `get_match_weather` — match-day conditions
   - `search_team_news` — last-minute injury / coach changes (Tavily)
   - `build_parlay` — combine survivors
4. Returns structured picks + rationale → stored in `agent_runs` for audit

### Skill metrics

| Metric | What it measures | Where |
|---|---|---|
| **ROI** | Total P&L on resolved bets | `/stats/roi`, dashboard |
| **Hit rate** | % of bets that won | `/stats/roi`, dashboard |
| **Avg edge** | Mean predicted edge at scan time | `/stats/roi`, dashboard |
| **CLV** | Closing Line Value — pro skill metric | `/stats/roi`, dashboard |
| **Brier score** | Probability calibration on holdout | `/stats/backtest` |
| **Log-loss** | Cross-entropy on holdout | `/stats/backtest` |
| **Calibration buckets** | Predicted vs observed hit rate by decile | `/stats/backtest` |

### CLV — the king metric

Every 10 minutes the worker snapshots the best market odds for every pending
prediction whose match starts within 30 min, and stores them as
`closing_odds`. CLV % = `(entry_odds / closing_odds - 1) × 100`. A
consistently positive average CLV is the strongest indicator of a winning
strategy — far more meaningful than ROI on small samples.

## File map

```
betbot/                         # core package
  analysis.py                   # value detection, Kelly, parlays
  api.py                        # The Odds API client
  backtest.py                   # Brier / log-loss / calibration engine
  clv.py                        # closing line value tracker
  config.py                     # .env loader + validation
  database.py                   # SQLAlchemy engine (Postgres only)
  db.py                         # Database façade
  enrichment.py                 # ELO + xG batch update
  football_api.py               # football-data.org client
  main.py                       # CLI + APScheduler daemon
  models.py                     # Dixon-Coles + xG + ELO blended model
  notifier.py                   # Gmail HTML
  orm_models.py                 # SQLAlchemy ORM models
  resolver.py                   # auto-resolve bet outcomes
  data_sources/
    api_football.py             # lineups, injuries (RapidAPI key required)
    club_elo.py                 # ELO ratings (free)
    news.py                     # Tavily web search (key required)
    understat.py                # xG scraping (free)
    weather.py                  # Open-Meteo (free)

betbot_mcp/                     # MCP server — 20+ tools
betbot_api/                     # FastAPI REST + Claude agent
betbot_dashboard/               # Streamlit UI

alembic/                        # DB migrations
tests/                          # pytest suite (39 tests)
docker-compose.yml              # dev stack
docker-compose.prod.yml         # prod overlay (Caddy + HTTPS)
Caddyfile                       # reverse proxy config
Dockerfile                      # multi-stage build
Makefile                        # operational shortcuts
```

## License

Personal project. Bet responsibly.
