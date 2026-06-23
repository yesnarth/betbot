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
from betbot.elo_local import compute_elo_ratings
from betbot.analysis import (
    detect_value_bets, rank_value_bets, build_parlays,
    ValueBet, Parlay, kelly_stake,
)
from betbot.bankroll import bootstrap_initial_deposit
from betbot.clv import snapshot_closing_odds
from betbot.db import Database
from betbot.enrichment import enrich_team_stats
from betbot.notifier import EmailNotifier
from betbot.resolver import resolve_pending, resolve_stale_pending
from betbot.source_health import check_and_alert as source_health_check
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
# Shared helpers (canonical home: betbot.shared)
# ---------------------------------------------------------------------------

from betbot.shared import filter_upcoming_today, load_team_stats_from_db  # noqa: E402


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
    # IMPORTANT : on ne descend JAMAIS sous edge 0. Forcer des combinés à EV
    # négative pour atteindre min_combos est contraire à « robuste et sûr ».
    # On relâche uniquement prob / cote / nombre de jambes, jamais l'exigence
    # d'EV positive. Si min_combos n'est pas atteint, on en renvoie moins —
    # run_daily_scan gère le cas « aucun combiné » via render_no_value().
    attempts = [
        (settings.min_value_edge, settings.min_model_prob, settings.min_book_odds,    3, f"strict (edge ≥ {settings.min_value_edge*100:.0f}%)"),
        (0.0,                     settings.min_model_prob, settings.min_book_odds,    3, "EV ≥ 0"),
        (0.0,                     0.35,                    1.30,                      3, "EV ≥ 0, prob ≥ 35%, cote ≥ 1.30"),
        (0.0,                     0.30,                    1.20,                      3, "EV ≥ 0, seuils bas (3 jambes)"),
        (0.0,                     0.30,                    1.20,                      2, "EV ≥ 0, 2 jambes"),
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
            min_edge_vs_novig=settings.min_edge_vs_novig,
            max_book_odds=settings.max_book_odds,
            underdog_odds=settings.underdog_odds,
            underdog_min_prob=settings.underdog_min_prob,
            novig_required=settings.novig_required,
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

def _compute_h2h_for_league(parsed: list[dict]) -> dict[tuple[str, str], dict]:
    """
    Aggregate per-pair head-to-head stats from the parsed match list.

    Returns {(team_a, team_b): {team_a_wins, draws, team_b_wins,
    team_a_goals_avg, team_b_goals_avg}} with team_a < team_b alphabetically.
    """
    from collections import defaultdict

    pair_acc: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"team_a_wins": 0, "draws": 0, "team_b_wins": 0,
                 "team_a_goals_total": 0, "team_b_goals_total": 0,
                 "n_matches": 0}
    )
    for m in parsed:
        home = m.get("home_team")
        away = m.get("away_team")
        if not (home and away):
            continue
        team_a, team_b = (home, away) if home < away else (away, home)
        home_goals = int(m.get("home_goals", 0) or 0)
        away_goals = int(m.get("away_goals", 0) or 0)
        # Orient goals to team_a's perspective.
        if home == team_a:
            a_goals, b_goals = home_goals, away_goals
        else:
            a_goals, b_goals = away_goals, home_goals
        acc = pair_acc[(team_a, team_b)]
        acc["n_matches"] += 1
        acc["team_a_goals_total"] += a_goals
        acc["team_b_goals_total"] += b_goals
        if a_goals > b_goals:
            acc["team_a_wins"] += 1
        elif a_goals == b_goals:
            acc["draws"] += 1
        else:
            acc["team_b_wins"] += 1

    out: dict[tuple[str, str], dict] = {}
    for pair, acc in pair_acc.items():
        n = acc["n_matches"]
        if n == 0:
            continue
        out[pair] = {
            "team_a_wins":      acc["team_a_wins"],
            "draws":            acc["draws"],
            "team_b_wins":      acc["team_b_wins"],
            "team_a_goals_avg": round(acc["team_a_goals_total"] / n, 3),
            "team_b_goals_avg": round(acc["team_b_goals_total"] / n, 3),
        }
    return out


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

        # Self-computed Elo from these same results — makes the Elo signal
        # independent of ClubElo's (often-down) external API. ClubElo, when
        # reachable, overlays a better cross-league rating during enrichment
        # (update_team_enrichment only writes non-None, so it never nulls this).
        local_elo = compute_elo_ratings(parsed)

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
                _elo = local_elo.get(team)
                if _elo is not None:
                    db.update_team_enrichment(
                        team_name=stats.name, sport_key=sport_key, elo_rating=_elo)
                saved += 1

        # H2H per pair — cheap derivative of the same match list, persisted
        # for the blended model to apply a Bayesian H2H nudge at scan time.
        h2h_pairs = _compute_h2h_for_league(parsed)
        for (team_a, team_b), stats in h2h_pairs.items():
            db.upsert_head_to_head(
                sport_key=sport_key,
                team_a=team_a,
                team_b=team_b,
                team_a_wins=stats["team_a_wins"],
                draws=stats["draws"],
                team_b_wins=stats["team_b_wins"],
                team_a_goals_avg=stats["team_a_goals_avg"],
                team_b_goals_avg=stats["team_b_goals_avg"],
            )

        logger.info(
            "  %s : %d équipes, %d paires H2H, moyennes ligue %.2f/%.2f buts",
            sport_key, saved, len(h2h_pairs), home_avg, away_avg,
        )

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
        upcoming = filter_upcoming_today(events, settings.min_before_kickoff)
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
    prebuilt_stats = load_team_stats_from_db(db, events_by_sport.keys())
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
            reliability=bet.reliability,
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
        resolve_stale_pending(db, settings.football_data_api_key)
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
    # Skip the boot scan when auto-scan is disabled (SCAN_HOURS=) — the user
    # explicitly wants to control when Odds API requests happen.
    if settings.scan_hours:
        run_daily_scan(settings, db, notifier, logger, scan_label="Démarrage")
        health.record_job_fired("scan:boot")
    else:
        logger.info("Boot scan skippé (SCAN_HOURS vide, mode manuel uniquement)")

    scheduler = BlockingScheduler(timezone="Europe/Paris")
    health.scheduler = scheduler

    # Per-job cooldown for failure alerts. We keep an in-memory dict mapping
    # job name → timestamp of the last alert sent, and re-alert only if the
    # previous alert is older than _ALERT_COOLDOWN_SEC. This prevents a job
    # that fires every 10 min from spamming 144 emails per day if its
    # underlying API stays down.
    _last_alert_at: dict[str, float] = {}
    _ALERT_COOLDOWN_SEC = 3600  # one alert per job per hour

    def _alert_job_failure(name: str, err_msg: str) -> None:
        import time
        now = time.time()
        last = _last_alert_at.get(name, 0)
        if now - last < _ALERT_COOLDOWN_SEC:
            return  # cooldown — already notified recently
        _last_alert_at[name] = now

        subject = f"[BetBot] Job failure : {name}"
        body = (
            f"Scheduled job '{name}' raised an exception.\n\n"
            f"Error: {err_msg}\n\n"
            f"Time: {datetime.now(timezone.utc).isoformat()}\n"
            f"Next alerts for this job suppressed for {_ALERT_COOLDOWN_SEC // 60} min."
        )
        # Email — best effort, never raise
        try:
            from betbot.notifier import EmailNotifier
            EmailNotifier(settings.gmail_user, settings.gmail_app_password,
                          settings.gmail_recipient).send(
                subject=subject, html=f"<pre>{body}</pre>",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failure-alert email could not be sent: %s", e)
        # Telegram — best effort
        try:
            from betbot.telegram_notifier import notify_alert
            notify_alert(subject, body)
        except Exception as e:  # noqa: BLE001
            logger.debug("Failure-alert telegram skipped: %s", e)

    def _wrap(name: str, fn, *args, **kwargs):
        """Wrap a scheduled callable so success/failure is recorded in health
        AND triggers an email/Telegram alert on raise."""
        def _runner():
            err = None
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:200]
                logger.exception("Job '%s' failed", name)
                _alert_job_failure(name, err)
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

    # Scans : if the worker was offline at the scheduled time, APScheduler
    # will run a catch-up scan as soon as the worker boots — provided we
    # boot back within `misfire_grace_time` of the missed firing. We use
    # 4h grace : long enough to handle the realistic "computer was off
    # this morning" case, short enough that we don't spam a stale scan
    # 12 hours after the fact.
    #
    # `coalesce=True` means if multiple firings were missed (e.g. both 09h
    # and 20h), only ONE catch-up scan runs at boot — not two in a row.
    SCAN_MISFIRE_GRACE = 4 * 3600   # 4h
    if not settings.scan_hours:
        logger.info(
            "SCAN_HOURS vide → auto-scan DÉSACTIVÉ. "
            "Le worker continue (CLV snapshots, stats refresh, resolver) mais "
            "ne déclenchera jamais de scan auto. Tu peux scanner manuellement "
            "via le dashboard (🛠️ Outils → Scan manuel) ou l'endpoint "
            "/recommend/manual."
        )
    for hour in settings.scan_hours:
        h, m = hour.split(":")
        label = labels.get(hour, hour)
        scheduler.add_job(
            _wrap(f"scan:{label}", run_daily_scan,
                  settings, db, notifier, logger, False, label),
            trigger=CronTrigger(hour=int(h), minute=int(m)),
            id=f"scan_{hour}",
            name=f"scan-{label}",
            misfire_grace_time=SCAN_MISFIRE_GRACE,
            coalesce=True,
        )
        logger.info("Scan planifié à %s (%s) — Europe/Paris (catch-up grace : %dh)",
                    hour, label, SCAN_MISFIRE_GRACE // 3600)

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

    # CLV snapshot toutes les 10 min — GATED: this hits the Odds API every 10 min
    # and burns the (free-tier) quota fast. Off by default; enable only on a paid
    # plan / always-on host where closing-line value is actually capturable.
    if settings.clv_snapshot_enabled:
        odds_client_for_clv = OddsAPIClient(settings.odds_api_key)
        scheduler.add_job(
            _wrap("clv_snapshot", snapshot_closing_odds, odds_client_for_clv),
            trigger=CronTrigger(minute="*/10"),
            id="clv_snapshot",
            name="clv-snapshot",
            misfire_grace_time=120,
            coalesce=True,
        )
    else:
        logger.info("CLV snapshot DÉSACTIVÉ (CLV_SNAPSHOT_ENABLED=0) — quota Odds préservé")

    # Source health check daily at 06:30 UTC — alerts via email + Telegram
    # whenever a source transitions from OK to DOWN (e.g. Understat changes
    # its HTML structure, Tavily key revoked, etc.)
    scheduler.add_job(
        _wrap("source_health", source_health_check),
        trigger=CronTrigger(hour=6, minute=30),
        id="source_health_daily",
        name="source-health",
        misfire_grace_time=3600,
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

    # Fallback resolution at 05h00 UTC — picks up confirmed bets too old for the
    # Odds API /scores window (3 days) using football-data.org historical results,
    # so a confirmed bet never becomes a permanent 'zombie' (committed capital
    # stuck, excluded from ROI/CLV forever).
    def _resolve_stale_job():
        r = resolve_stale_pending(db, settings.football_data_api_key)
        logger.info("Résolution tardive (football-data) : %s", r)
    scheduler.add_job(
        _wrap("resolve_stale", _resolve_stale_job),
        trigger=CronTrigger(hour=5, minute=0),
        id="resolve_stale_daily",
        name="resolve-stale",
        misfire_grace_time=3600,
    )

    # ML calibrator retrain — Sunday 03:30 UTC, AFTER the daily resolve job has
    # had a chance to populate fresh resolved bets. Below MIN_SAMPLES_TO_TRUST
    # the function is a no-op, so safe to schedule from day 1.
    from betbot.ml import train_calibrator
    def _train_calibrator_job():
        result = train_calibrator()
        logger.info("Calibrator retrain: %s", result)
    scheduler.add_job(
        _wrap("ml_calibrator_retrain", _train_calibrator_job),
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=30),
        id="ml_calibrator_weekly",
        name="ml-calibrator",
        misfire_grace_time=3600,
    )

    # Blend-weight auto-tune — Wednesday 06:00 UTC. Re-fits per-league elo/xg
    # weights on the growing football-data history; persists ONLY improvements
    # (a league that no longer beats its defaults is left unchanged).
    from betbot.tuning import tune_all_leagues
    from betbot import blend_params as _blend_params
    def _blend_tune_job():
        from datetime import datetime as _dt, timezone as _tz
        results = tune_all_leagues(settings.football_data_api_key)
        now = _dt.now(_tz.utc).isoformat()
        saved = 0
        for sk, res in results.items():
            if res.get("tuned"):
                _blend_params.save_weights(sk, res["elo_weight"], res["xg_weight"],
                                           res["log_loss_after"], now)
                saved += 1
        logger.info("Blend auto-tune : %d/%d ligue(s) mises à jour", saved, len(results))
    scheduler.add_job(
        _wrap("blend_tune_weekly", _blend_tune_job),
        trigger=CronTrigger(day_of_week="wed", hour=6, minute=0),
        id="blend_tune_weekly",
        name="blend-tune",
        misfire_grace_time=3600,
    )

    # Tennis ELO refresh — every Monday 05:00 UTC. Pulls latest ATP/WTA matches
    # from Sackmann and rebuilds player ratings. Fast (~6k matches in <1s).
    from betbot.tennis_bootstrap import refresh_ratings as _refresh_tennis_ratings
    def _tennis_refresh_job():
        result = _refresh_tennis_ratings(tour="atp")
        logger.info("Tennis ELO refresh : %s", result)
    scheduler.add_job(
        _wrap("tennis_elo_refresh", _tennis_refresh_job),
        trigger=CronTrigger(day_of_week="mon", hour=5, minute=0),
        id="tennis_elo_weekly",
        name="tennis-elo",
        misfire_grace_time=3600,
    )

    # Basketball stats refresh — every Tuesday 05:00 UTC. Scrapes basketball-
    # reference for current-season pace + ORtg/DRtg per NBA team.
    from betbot.basketball_bootstrap import refresh_stats as _refresh_basket
    def _basket_refresh_job():
        result = _refresh_basket()
        logger.info("Basketball stats refresh : %s", result)
    scheduler.add_job(
        _wrap("basketball_stats_refresh", _basket_refresh_job),
        trigger=CronTrigger(day_of_week="tue", hour=5, minute=0),
        id="basketball_stats_weekly",
        name="basketball-stats",
        misfire_grace_time=3600,
    )

    # Auto-skip stale proposed picks — runs every 30 min. A pick that has been
    # 'proposed' for more than 36h is presumed past kickoff and gets archived
    # as 'skipped'. Prevents the dashboard from accumulating expired picks.
    def _auto_skip_job():
        n = db.auto_skip_expired_proposed(max_age_hours=36)
        if n:
            logger.info("auto-skip: archived %d expired proposed picks", n)
    scheduler.add_job(
        _wrap("auto_skip_proposed", _auto_skip_job),
        trigger=CronTrigger(minute="*/30"),
        id="auto_skip_proposed",
        name="auto-skip",
        misfire_grace_time=120,
    )

    # Catch-up on startup — this PC is often shut down, so the daily resolve
    # cron rarely fires. Settle any bets that finished while it was off, ONCE,
    # right after boot. resolve_pending only calls Odds /scores for sports that
    # actually have confirmed-pending bets (free otherwise); resolve_stale uses
    # football-data (free). A one-shot 'date' job → runs off the main thread.
    def _startup_catchup_job():
        oc = OddsAPIClient(settings.odds_api_key)
        live = resolve_pending(db, oc)
        stale = resolve_stale_pending(db, settings.football_data_api_key)
        logger.info("Catch-up démarrage : %s | tardifs résolus : %s",
                    live, stale.get("resolved", 0))
    scheduler.add_job(
        _wrap("startup_catchup", _startup_catchup_job),
        trigger="date",                 # no run_date → fires asap after start()
        id="startup_catchup",
        name="startup-catchup",
        misfire_grace_time=300,
    )

    logger.info("Bot actif (APScheduler, fuseau Europe/Paris). CTRL+C pour arrêter.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Arrêt demandé (Ctrl+C). Bye.")


if __name__ == "__main__":
    main()
