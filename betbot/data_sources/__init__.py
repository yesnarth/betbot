"""External data sources that enrich the prediction model beyond raw odds + scores.

Each module is self-contained:
  - club_elo.py     — Club Elo ratings (free, unlimited)
  - weather.py      — Match-day weather (Open-Meteo, free, no key)
  - understat.py    — xG (expected goals) via Understat scraping
  - api_football.py — Lineups, injuries, suspensions (RapidAPI, free tier)
  - news.py         — Free-text web search for last-minute info (Tavily / Brave)
"""
