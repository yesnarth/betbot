"""
Validate the team-name matcher against The Odds API's canonical names.

A false team match is the costliest, quietest bug this project has: it prices
one club with another club's form, emits a confident probability, and under
AUTO_CONFIRM_PICKS is recorded as a real bet. Every incident so far (Dundee
United→Dundee, Makhachkala↔Moscow, and the 2026-09-08 Champions-League night:
Barcelona→Barcelona SC, Sporting Lisbon→Sporting Gijon, Red Star Belgrade→
RED Star FC 93) was discovered the expensive way — inside live picks.

This audit turns that discovery offline: it fetches the provider's own
participant whitelist per league (GET /participants, 1 credit each) and
replays every canonical name through the REAL production path — the same
`load_team_stats_from_db` cache (cup merge and continental borrowing
included) and the same `_fuzzy_lookup` the scan uses. Never a reimplemented
matcher: a validator that matches differently from production validates
nothing.

Each name lands in a resolution tier:

    exact      the stored row name, verbatim              — safe
    alias      resolved through the hand-maintained maps  — safe (each entry
               was verified against the real club when added)
    normalise  same name modulo accents/suffixes          — safe
    flou       token/fuzzy scoring decided                — REVIEW FIRST:
               this is the tier every false match above lived in
    echec      no resolution                              — degrades to the
               consensus model (honest); only worth fixing if the club
               actually appears in scans. The provider documents the list as
               a whitelist that may include inactive/relegated clubs, so a
               miss here is often just noise.

The report is written to audit_output/ and printed. This module NEVER writes
aliases — corrections go into _TEAM_NAME_ALIASES by hand, one by one, each
validated against the real club (règle mémoire appariement). Run it outside
scan windows, 1-2 times a season and after adding a league; ~46 credits for
a full soccer pass.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

from betbot.analysis import (
    _KNOWN_ALIASES,
    _TEAM_NAME_ALIASES,
    _fuzzy_lookup,
    _invalidate_norm_cache,
    _normalize_name,
)

SAFE_TIERS = ("exact", "alias", "normalise")
TIERS = SAFE_TIERS + ("flou", "echec")


def classify(name: str, cache: dict) -> tuple[str, str | None]:
    """Resolution tier for one canonical name against one league cache.

    Classification WRAPS the production matcher (the resolution itself is
    `_fuzzy_lookup`, untouched); the tier is derived afterwards from which
    bridge could explain the outcome, most conservative first — so a name
    only counts as 'flou' when nothing but token/fuzzy scoring explains it.
    """
    resolved, matched = _fuzzy_lookup(name, cache)
    if resolved is None:
        return "echec", None
    if name == matched:
        return "exact", matched

    norm_query = _normalize_name(name)
    alias_target = _TEAM_NAME_ALIASES.get(norm_query)
    if alias_target is not None and matched == alias_target:
        return "alias", matched
    fragment = _KNOWN_ALIASES.get(norm_query)
    if fragment is not None and fragment in _normalize_name(matched):
        return "alias", matched
    if norm_query == _normalize_name(matched):
        return "normalise", matched
    return "flou", matched


def audit_league(names: list[str], cache: dict) -> dict:
    """Classify every canonical name; returns counts + the rows to review."""
    counts = {t: 0 for t in TIERS}
    flou: list[tuple[str, str]] = []
    echec: list[str] = []
    _invalidate_norm_cache(cache)
    try:
        for name in names:
            if not name:
                continue
            tier, matched = classify(name, cache)
            counts[tier] += 1
            if tier == "flou":
                flou.append((name, matched or ""))
            elif tier == "echec":
                echec.append(name)
    finally:
        _invalidate_norm_cache(cache)
    return {"counts": counts, "flou": flou, "echec": echec}


def render_report(results: dict[str, dict], today: str) -> str:
    lines = [
        f"# Audit d'appariement /participants — {today}",
        "",
        "Noms canoniques The Odds API rejoués dans le matcher de PRODUCTION",
        "(cache par ligue avec emprunts, `_fuzzy_lookup` réel).",
        "Aucun alias n'est écrit automatiquement : les corrections vont dans",
        "`_TEAM_NAME_ALIASES` à la main, validées contre le vrai club.",
        "",
        "| Ligue | équipes | exact | alias | normalisé | **flou** | échec |",
        "|---|---|---|---|---|---|---|",
    ]
    tot = {t: 0 for t in TIERS}
    for sport, res in sorted(results.items()):
        c = res["counts"]
        for t in TIERS:
            tot[t] += c[t]
        lines.append(
            f"| {sport} | {sum(c.values())} | {c['exact']} | {c['alias']} "
            f"| {c['normalise']} | **{c['flou']}** | {c['echec']} |")
    lines.append(
        f"| **TOTAL** | {sum(tot.values())} | {tot['exact']} | {tot['alias']} "
        f"| {tot['normalise']} | **{tot['flou']}** | {tot['echec']} |")

    lines += ["", "## Résolutions FLOUES — à revoir en priorité", "",
              "Le palier où ont vécu tous les faux appariements de production.",
              ""]
    any_flou = False
    for sport, res in sorted(results.items()):
        for name, matched in res["flou"]:
            any_flou = True
            lines.append(f"- `{sport}` : « {name} » → « {matched} »")
    if not any_flou:
        lines.append("- aucune — chaque nom se résout sans le palier flou.")

    lines += ["", "## Échecs — repli consensus (bruit possible : clubs "
              "inactifs/relégués de la whitelist fournisseur)", ""]
    any_miss = False
    for sport, res in sorted(results.items()):
        for name in res["echec"]:
            any_miss = True
            lines.append(f"- `{sport}` : « {name} »")
    if not any_miss:
        lines.append("- aucun.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from betbot.api import OddsAPIClient
    from betbot.config import load_settings
    from betbot.db import Database
    from betbot.shared import load_team_stats_from_db

    args = list(argv if argv is not None else sys.argv[1:])
    settings = load_settings()
    db = Database(settings.database_url)
    client = OddsAPIClient(settings.odds_api_key)

    if args:
        sports = args
    else:
        # Every soccer league holding stats — the exact universe a pick can
        # be priced in. Distinct keys cost nothing; participants cost 1 each.
        from sqlalchemy import distinct, select

        from betbot.database import session_scope
        from betbot.orm_models import TeamStat
        with session_scope() as s:
            rows = s.execute(select(distinct(TeamStat.sport_key))).all()
        sports = sorted(k for (k,) in rows if k.startswith("soccer"))

    results: dict[str, dict] = {}
    for sport in sports:
        names = client.get_participants(sport)
        if not names:
            print(f"  {sport} : aucun participant renvoyé — ignoré")
            continue
        cache = load_team_stats_from_db(db, [sport]).get(sport, {}).get("teams", {})
        results[sport] = audit_league(names, cache)
        c = results[sport]["counts"]
        print(f"  {sport} : {sum(c.values())} noms — flou={c['flou']} "
              f"echec={c['echec']}")
        time.sleep(0.2)

    today = datetime.now().strftime("%Y-%m-%d")
    report = render_report(results, today)
    out_dir = Path("audit_output")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"participants_{today}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nRapport : {out_path}")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
