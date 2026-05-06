"""
BetBot CI — Main entry point.

Usage:
  python -m betbot.main              # mode daemon (3 scans/jour automatiques)
  python -m betbot.main --once       # un seul scan immédiat + email
  python -m betbot.main --dry-run    # scan sans envoyer d'email
  python -m betbot.main --update-stats  # mise à jour stats équipes uniquement
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from betbot.config import load_settings
from betbot.api import OddsAPIClient
from betbot.football_api import FootballDataClient, parse_match_results, LEAGUE_MAP
from betbot.models import build_team_stats, compute_league_averages, TeamStats
from betbot.analysis import (
    detect_value_bets, rank_value_bets, build_parlays,
    ValueBet, Parlay, kelly_stake,
)
from betbot.bankroll import bootstrap_initial_deposit
from betbot.clv import snapshot_closing_odds
from betbot.db import Database
from betbot.enrichment import enrich_team_stats
from betbot.notifier import EmailNotifier
from betbot.resolver import resolve_pending
from betbot.worker_health import WorkerHealthState, start_health_server


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_path: str) -> logging.Logger:
    """
    Logger setup — delegates to betbot.logging_setup which honors LOG_FORMAT
    (json|text) and LOG_LEVEL via environment variables. JSON in containers,
    plain text in local dev.
    """
    from betbot.logging_setup import configure
    return configure(log_path)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_team_stats_from_db(
    db: Database, sport_keys: object
) -> dict[str, dict]:
    """
    Returns {sport_key: {"teams": {name: TeamStats}, "home_avg": float, "away_avg": float}}.
    Falls back to default averages (1.35/1.10) if league_averages row is missing.
    """
    from betbot.models import DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG
    result: dict[str, dict] = {}
    for sport_key in sport_keys:
        rows = db.get_all_team_stats_for_league(sport_key)
        if not rows:
            continue
        teams: dict[str, TeamStats] = {}
        for row in rows:
            teams[row["team_name"]] = TeamStats(
                name=row["team_name"],
                attack_home=row["attack_home"],
                defense_home=row["defense_home"],
                attack_away=row["attack_away"],
                defense_away=row["defense_away"],
                matches_analyzed=row["matches_analyzed"],
                elo_rating=row.get("elo_rating"),
                xg_for=row.get("xg_for"),
                xg_against=row.get("xg_against"),
            )
        avgs = db.get_league_averages(sport_key)
        home_avg, away_avg = avgs if avgs else (DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG)
        result[sport_key] = {"teams": teams, "home_avg": home_avg, "away_avg": away_avg}
    return result


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------

def _filter_upcoming_today(events: list[dict], min_before_kickoff: int = 60) -> list[dict]:
    """
    Keep only events:
    - scheduled for today (UTC date)
    - starting at least min_before_kickoff minutes from now
    """
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    cutoff = now_utc + timedelta(minutes=min_before_kickoff)

    result = []
    for event in events:
        commence = event.get("commence_time", "")
        if not commence.startswith(today_str):
            continue
        try:
            event_time = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if event_time >= cutoff:
                result.append(event)
        except (ValueError, TypeError):
            pass
    return result


# ---------------------------------------------------------------------------
# Guaranteed minimum combos
# ---------------------------------------------------------------------------

def _ensure_min_combos(
    events_by_sport: dict,
    prebuilt_stats: dict,
    settings,
    min_combos: int,
    logger: logging.Logger,
) -> tuple[list[ValueBet], list[Parlay]]:
    """
    Try progressively relaxed thresholds until we have at least min_combos parlays.
    Falls back to 2-leg parlays if 3-leg combos are impossible.
    Returns (ranked_bets, parlays).
    """
    # (edge_threshold, min_model_prob, min_book_odds, n_legs, label)
    attempts = [
        (settings.min_value_edge, settings.min_model_prob, settings.min_book_odds,    3, f"strict (edge ≥ {settings.min_value_edge*100:.0f}%)"),
        (0.0,                     settings.min_model_prob, settings.min_book_odds,    3, "EV positif"),
        (-0.05,                   0.35,                    1.30,                      3, "relâché (prob ≥ 35%, cote ≥ 1.30)"),
        (-0.15,                   0.30,                    1.20,                      3, "très relâché (3 jambes)"),
        (-0.15,                   0.30,                    1.20,                      2, "2 jambes seulement"),
    ]

    ranked: list[ValueBet] = []
    parlays: list[Parlay] = []
    probs_cache: dict = {}  # shared across all relaxation passes — avoids 5x Poisson recomputation

    for edge_thr, prob_thr, odds_thr, n_legs, label in attempts:
        raw_bets = detect_value_bets(
            events_by_sport=events_by_sport,
            match_history_by_sport={},
            bankroll=settings.bankroll,
            kelly_fraction=settings.kelly_fraction,
            min_value_edge=edge_thr,
            min_model_prob=prob_thr,
            min_book_odds=odds_thr,
            prebuilt_stats_by_sport=prebuilt_stats,
            probs_cache=probs_cache,
        )
        ranked = rank_value_bets(raw_bets)[: settings.top_bets]
        parlays = build_parlays(ranked, n_legs=n_legs, top_n=settings.top_combos)

        if len(parlays) >= min_combos:
            if edge_thr < settings.min_value_edge:
                logger.info("Seuil relâché [%s] pour atteindre %d combiné(s)", label, min_combos)
            return ranked, parlays

        logger.info("[%s] → %d combiné(s), on relâche...", label, len(parlays))

    return ranked, parlays


# ---------------------------------------------------------------------------
# Team stats update
# ---------------------------------------------------------------------------

def update_team_stats(settings, db: Database, logger: logging.Logger) -> None:
    logger.info("=== Mise à jour des stats d'équipes ===")
    if not settings.football_data_api_key or "REMPLACE" in settings.football_data_api_key:
        logger.warning("FOOTBALL_DATA_API_KEY non configurée → Poisson désactivé")
        return

    fd_client = FootballDataClient(settings.football_data_api_key)
    raw_by_sport = fd_client.get_all_leagues(limit=80)

    for sport_key, raw_matches in raw_by_sport.items():
        comp_code = LEAGUE_MAP.get(sport_key)
        if not comp_code:
            continue
        parsed = parse_match_results(raw_matches)
        if not parsed:
            continue
        home_avg, away_avg = compute_league_averages(parsed)
        db.upsert_league_averages(sport_key, home_avg, away_avg, len(parsed))
        teams: set[str] = set()
        for m in parsed:
            if m.get("home_team"):
                teams.add(m["home_team"])
            if m.get("away_team"):
                teams.add(m["away_team"])

        saved = 0
        for team in teams:
            stats = build_team_stats(team, parsed, home_avg, away_avg)
            if stats:
                db.upsert_team_stats(
                    team_name=stats.name,
                    sport_key=sport_key,
                    league_code=comp_code,
                    attack_home=stats.attack_home,
                    defense_home=stats.defense_home,
                    attack_away=stats.attack_away,
                    defense_away=stats.defense_away,
                    matches_analyzed=stats.matches_analyzed,
                )
                saved += 1
        logger.info("  %s : %d équipes, moyennes ligue %.2f/%.2f buts", sport_key, saved, home_avg, away_avg)

    logger.info("Mise à jour stats terminée.")


# ---------------------------------------------------------------------------
# Daily scan
# ---------------------------------------------------------------------------

def run_daily_scan(
    settings,
    db: Database,
    notifier: EmailNotifier,
    logger: logging.Logger,
    dry_run: bool = False,
    scan_label: str = "",
) -> None:
    now = datetime.now()
    logger.info("=" * 55)
    logger.info("SCAN %s— %s", f"[{scan_label}] " if scan_label else "", now.strftime("%d/%m/%Y %H:%M"))
    logger.info("=" * 55)

    # 1. Fetch all sports
    odds_client = OddsAPIClient(settings.odds_api_key)
    all_events = odds_client.fetch_all_sports()

    # 2. Filtre : matchs du jour démarrant dans au moins MIN_BEFORE_KICKOFF minutes
    today_str = now.strftime("%Y-%m-%d")
    events_by_sport: dict[str, list[dict]] = {}
    total_before = sum(len(v) for v in all_events.values())

    for sport, events in all_events.items():
        upcoming = _filter_upcoming_today(events, settings.min_before_kickoff)
        if upcoming:
            events_by_sport[sport] = upcoming

    total_after = sum(len(v) for v in events_by_sport.values())
    logger.info(
        "Filtre : %d matchs totaux → %d matchs du jour non commencés (%s)",
        total_before, total_after, today_str,
    )

    if not events_by_sport:
        logger.info("Aucun match éligible pour ce scan.")
        if not dry_run:
            notifier.send(
                f"BetBot CI [{scan_label}] — Aucun match ce soir",
                notifier.render_no_value(),
            )
        return

    # 3. Load Poisson stats from DB
    prebuilt_stats = _load_team_stats_from_db(db, events_by_sport.keys())
    n_teams = sum(len(v) for v in prebuilt_stats.values())
    logger.info("Stats Poisson : %d équipes chargées", n_teams)

    # 4. Detect value bets — garantit MIN_COMBOS combinés
    ranked_bets, parlays = _ensure_min_combos(
        events_by_sport, prebuilt_stats, settings, settings.min_combos, logger
    )

    logger.info(
        "%d pari(s) de valeur, %d combiné(s)",
        len(ranked_bets), len(parlays),
    )

    # 5. Save predictions to DB
    for bet in ranked_bets:
        db.save_prediction(
            event_id=bet.event_id,
            sport_key=bet.sport_key,
            home_team=bet.home_team,
            away_team=bet.away_team,
            market=bet.market,
            selection=bet.selection_code,
            model_prob=bet.model_prob,
            best_odds=bet.best_odds,
            best_book=bet.best_book,
            value_edge=bet.value_edge,
            kelly_stake=bet.kelly_stake,
            lambda_home=bet.lambda_home,
            lambda_away=bet.lambda_away,
            model_type=bet.model_type,
        )

    # 6. Dry-run : afficher dans la console
    if dry_run:
        logger.info("--- DRY RUN : paris détectés ---")
        for bet in ranked_bets:
            logger.info(
                "  %s vs %s | %s | cote=%.2f | edge=%+.1f%% | mise=%.2f$ [%s]",
                bet.home_team, bet.away_team, bet.selection_label,
                bet.best_odds, bet.value_edge * 100, bet.kelly_stake, bet.model_type,
            )
        if parlays:
            logger.info("--- Combinés ---")
            for p in parlays:
                logger.info(
                    "  ×%.2f | prob=%.1f%% | EV=%+.1f%%",
                    p.combined_odds, p.combined_prob * 100, p.combined_ev,
                )
        return

    # 7. Build & send email
    stats = db.get_roi_stats(days=30)
    html = notifier.render_html(ranked_bets, parlays, stats, settings.bankroll)
    subject = (
        f"BetBot CI [{scan_label}] — {len(parlays)} combiné(s) — "
        f"{now.strftime('%d/%m/%Y %H:%M')}"
    )
    notifier.send(subject, html)
    logger.info("Scan terminé.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="BetBot CI — Pronostiqueur Football")
    parser.add_argument("--once",         action="store_true", help="Scan unique puis exit")
    parser.add_argument("--dry-run",      action="store_true", help="Scan sans email")
    parser.add_argument("--update-stats", action="store_true", help="Mise à jour stats équipes")
    parser.add_argument("--enrich",       action="store_true", help="Enrichissement ELO + xG (sources externes)")
    parser.add_argument("--resolve",      action="store_true", help="Résolution des résultats en attente")
    parser.add_argument("--backtest",     metavar="SPORT", help="Backtest sur N matchs récents (ex: --backtest soccer_epl)")
    parser.add_argument("--backtest-n",   type=int, default=100, help="Nombre de matchs holdout pour --backtest")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except EnvironmentError as exc:
        print(f"\nConfiguration manquante :\n{exc}\n")
        raise SystemExit(1)

    logger = setup_logging(settings.log_path)
    db = Database(settings.database_url)
    notifier = EmailNotifier(settings.gmail_user, settings.gmail_app_password, settings.gmail_recipient)

    # First-run bootstrap: if the bankroll ledger is empty and BANKROLL > 0 in .env,
    # create the inception deposit. Idempotent — only runs once per fresh DB.
    if bootstrap_initial_deposit(settings.bankroll):
        logger.info("Bankroll initialisé : dépôt initial de %.2f$", settings.bankroll)

    logger.info("BetBot CI démarré")
    logger.info("  Capital       : %.0f$", settings.bankroll)
    logger.info("  Edge min      : %.0f%%", settings.min_value_edge * 100)
    logger.info("  Combinés min  : %d", settings.min_combos)
    logger.info("  Scans/jour    : %s", " | ".join(settings.scan_hours))
    logger.info("  Destinataire  : %s", settings.gmail_recipient)

    if args.update_stats:
        update_team_stats(settings, db, logger)
        return

    if args.enrich:
        enrich_team_stats(db)
        return

    if args.resolve:
        odds_client = OddsAPIClient(settings.odds_api_key)
        resolve_pending(db, odds_client)
        return

    if args.backtest:
        from betbot.backtest import run_backtest, backtest_summary
        result = run_backtest(args.backtest, settings.football_data_api_key, args.backtest_n)
        print(backtest_summary(result))
        return

    if args.once or args.dry_run:
        run_daily_scan(settings, db, notifier, logger,
                       dry_run=args.dry_run, scan_label="Manuel")
        return

    # Mode daemon : démarre l'HTTP health server, fait un update_stats au boot,
    # puis lance APScheduler. Toutes les jobs notifient le state pour healthchecks.
    start_health_server(port=8001)
    health = WorkerHealthState.get()

    update_team_stats(settings, db, logger)
    health.record_job_fired("update_team_stats:boot")
    run_daily_scan(settings, db, notifier, logger, scan_label="Démarrage")
    health.record_job_fired("scan:boot")

    scheduler = BlockingScheduler(timezone="Europe/Paris")
    health.scheduler = scheduler

    def _wrap(name: str, fn, *args, **kwargs):
        """Wrap a scheduled callable so success/failure is recorded in health."""
        def _runner():
            err = None
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:200]
                logger.exception("Job '%s' failed", name)
                raise
            finally:
                health.record_job_fired(name, error=err)
        _runner.__name__ = f"wrapped_{name}"
        return _runner

    labels = {
        settings.scan_hours[0]: "Matin",
        settings.scan_hours[1]: "Après-midi",
        settings.scan_hours[2]: "Soir",
    } if len(settings.scan_hours) >= 3 else {h: h for h in settings.scan_hours}

    for hour in settings.scan_hours:
        h, m = hour.split(":")
        label = labels.get(hour, hour)
        scheduler.add_job(
            _wrap(f"scan:{label}", run_daily_scan,
                  settings, db, notifier, logger, False, label),
            trigger=CronTrigger(hour=int(h), minute=int(m)),
            id=f"scan_{hour}",
            name=f"scan-{label}",
            misfire_grace_time=600,
            coalesce=True,
        )
        logger.info("Scan planifié à %s (%s) — Europe/Paris", hour, label)

    # Mise à jour stats chaque lundi matin
    scheduler.add_job(
        _wrap("update_team_stats", update_team_stats, settings, db, logger),
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0),
        id="update_stats_weekly",
        name="update-stats",
        misfire_grace_time=3600,
    )

    # Enrichissement ELO + xG le mardi matin
    scheduler.add_job(
        _wrap("enrich", enrich_team_stats, db),
        trigger=CronTrigger(day_of_week="tue", hour=6, minute=0),
        id="enrich_weekly",
        name="enrich",
        misfire_grace_time=3600,
    )

    # CLV snapshot toutes les 10 min
    odds_client_for_clv = OddsAPIClient(settings.odds_api_key)
    scheduler.add_job(
        _wrap("clv_snapshot", snapshot_closing_odds, odds_client_for_clv),
        trigger=CronTrigger(minute="*/10"),
        id="clv_snapshot",
        name="clv-snapshot",
        misfire_grace_time=120,
        coalesce=True,
    )

    # Résolution quotidienne des résultats à 04h00 (UTC matchs Europe largement terminés)
    odds_client_for_resolver = OddsAPIClient(settings.odds_api_key)
    scheduler.add_job(
        _wrap("resolve", resolve_pending, db, odds_client_for_resolver),
        trigger=CronTrigger(hour=4, minute=0),
        id="resolve_daily",
        name="resolve",
        misfire_grace_time=3600,
    )

    logger.info("Bot actif (APScheduler, fuseau Europe/Paris). CTRL+C pour arrêter.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Arrêt demandé (Ctrl+C). Bye.")


if __name__ == "__main__":
    main()
