"""
Weekly model-performance report — emailed every Monday.

Pure MEASUREMENT (never touches the bankroll). Summarises the resolved
"would-have" picks: win rate, ROI at flat 1u, calibration (predicted prob vs
actual win rate), and breakdowns by model / market / league.

The calibration block is the key signal: it shows whether the model's
probabilities are honest, and — week over week — whether the Sunday isotonic
recalibration is closing the gap. That's how the user sees the predictor
actually improving (or not).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from betbot.database import session_scope

logger = logging.getLogger("betbot.weekly_report")

_POS = "#1f9d6b"
_NEG = "#d1495b"
_BUCKETS = [(0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01)]

def _calib_gap(calib: list[dict]) -> float | None:
    """Sample-weighted mean of (actual − predicted) across buckets. Closer to 0 =
    better-calibrated; the KEY trend metric to watch shrink week over week."""
    num = den = 0.0
    for c in calib or []:
        if c.get("n") and c.get("predicted") is not None and c.get("actual") is not None:
            num += (c["actual"] - c["predicted"]) * c["n"]
            den += c["n"]
    return round(num / den, 1) if den else None


def _db() -> "Database":
    from betbot.config import load_settings
    from betbot.db import Database
    return Database(load_settings().database_url)


def _archive_report(data: dict) -> None:
    """Persist the snapshot in the report_snapshots TABLE (one row per UTC day,
    upserted). The raw picks stay in `predictions`; this is the report layer,
    queryable in SQL for trend analysis. Never breaks the email send."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ov = data.get("overall", {})
        _db().upsert_report_snapshot(
            today, data,
            total=ov.get("n", 0), win_rate=ov.get("win_rate"),
            roi=ov.get("roi"), pl=ov.get("pl"),
            calib_gap=_calib_gap(data.get("calibration")),
            period=data.get("period"),
        )
        logger.info("Report snapshot archived to DB (%s)", today)
    except Exception as exc:  # noqa: BLE001 — archiving must never break sending
        logger.warning("report archive failed: %s", exc)


def load_report_history() -> list[dict]:
    """Every archived snapshot (oldest first), full `data` included."""
    try:
        return _db().get_report_snapshots()
    except Exception as exc:  # noqa: BLE001
        logger.warning("report history read failed: %s", exc)
        return []


def report_history_trend() -> list[dict]:
    """Compact trend across snapshots — one row per report, straight from the
    stored columns: headline numbers + the calibration gap to watch shrink."""
    return [{"date": s.get("date"), "n": s.get("total"),
             "win_rate": s.get("win_rate"), "roi": s.get("roi"),
             "pl": s.get("pl"), "calib_gap": s.get("calib_gap")}
            for s in load_report_history()]


def _fetch(days: int | None = None) -> list[dict]:
    where = "result is not null"
    params: dict = {}
    if days:
        cut = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        where += " and created_at >= :cut"
        params["cut"] = cut
    with session_scope() as s:
        return [dict(r) for r in s.execute(text(
            "select model_prob, best_odds, result, model_type, market, sport_key, "
            "created_at from predictions where " + where), params).mappings()]


def _pl(r: dict) -> float:
    o = float(r.get("best_odds") or 0)
    if r["result"] == "win":
        return o - 1.0
    if r["result"] == "loss":
        return -1.0
    return 0.0


def _stats(sub: list[dict]) -> dict:
    w = sum(1 for r in sub if r["result"] == "win")
    l = sum(1 for r in sub if r["result"] == "loss")
    v = sum(1 for r in sub if r["result"] == "void")
    dec = w + l
    pl = sum(_pl(r) for r in sub)
    return {"n": len(sub), "w": w, "l": l, "v": v,
            "win_rate": round(w / dec * 100, 1) if dec else None,
            "pl": round(pl, 2), "roi": round(pl / dec * 100, 1) if dec else None}


def _calib(sub: list[dict]) -> list[dict]:
    out = []
    for lo, hi in _BUCKETS:
        b = [r for r in sub if lo <= (r["model_prob"] or 0) < hi
             and r["result"] in ("win", "loss")]
        w = sum(1 for r in b if r["result"] == "win")
        out.append({
            "range": f"{int(lo*100)}-{int(hi*100)}%", "n": len(b),
            "predicted": round(sum(r["model_prob"] for r in b) / len(b) * 100, 1) if b else None,
            "actual": round(w / len(b) * 100, 1) if b else None,
        })
    return out


def build_report_data(days_week: int = 7) -> dict:
    allrows = _fetch(None)
    weekrows = _fetch(days_week)
    dates = sorted(str(r["created_at"])[:10] for r in allrows if r["created_at"])
    mkts: dict = defaultdict(list)
    for r in allrows:
        mkts[r["market"] or "?"].append(r)
    lgs: dict = defaultdict(list)
    for r in allrows:
        lgs[(r["sport_key"] or "?").replace("soccer_", "")].append(r)
    return {
        "generated": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "period": f"{dates[0]} → {dates[-1]}" if dates else "—",
        "total": len(allrows),
        "week": _stats(weekrows),
        "overall": _stats(allrows),
        "by_model": {mt: _stats([r for r in allrows if r["model_type"] == mt])
                     for mt in ("blended", "consensus")},
        "calibration": _calib(allrows),
        "by_market": {k: _stats(v) for k, v in
                      sorted(mkts.items(), key=lambda x: -len(x[1]))},
        "by_league": {k: _stats(v) for k, v in
                      sorted(lgs.items(), key=lambda x: -len(x[1]))[:12]},
    }


def _roi_span(roi) -> str:
    if roi is None:
        return "—"
    col = _POS if roi >= 0 else _NEG
    return f'<span style="color:{col};font-weight:700;">{"+" if roi >= 0 else ""}{roi}%</span>'


def _render_email(d: dict) -> str:
    wk, ov = d["week"], d["overall"]
    # KPI cards
    def kpi(label, val, sub=""):
        return (f'<td style="padding:14px 10px;text-align:center;background:#fff;'
                f'border:1px solid #e2e6ec;border-radius:10px;">'
                f'<div style="font-size:24px;font-weight:700;color:#171a1f;">{val}</div>'
                f'<div style="font-size:11px;color:#616b7a;text-transform:uppercase;'
                f'letter-spacing:.5px;margin-top:2px;">{label}</div>'
                f'<div style="font-size:11px;color:#8a93a2;">{sub}</div></td>')

    week_roi = _roi_span(wk["roi"])
    ov_roi = _roi_span(ov["roi"])

    # calibration rows
    calib_rows = ""
    for c in d["calibration"]:
        if not c["n"]:
            continue
        gap = round((c["actual"] - c["predicted"]), 1)
        gcol = _NEG if gap < 0 else _POS
        calib_rows += (
            f'<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;">{c["range"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:right;color:#616b7a;">{c["n"]}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:right;">{c["predicted"]}%</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:right;font-weight:700;">{c["actual"]}%</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:right;color:{gcol};font-weight:700;">'
            f'{"+" if gap >= 0 else ""}{gap} pts</td></tr>')

    def stat_rows(dct):
        rows = ""
        for k, s in dct.items():
            if not s["n"]:
                continue
            rows += (
                f'<tr><td style="padding:7px 10px;border-bottom:1px solid #eee;text-transform:capitalize;">{k.replace("_"," ")}</td>'
                f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;color:#616b7a;">{s["n"]}</td>'
                f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;">{s["win_rate"]}%</td>'
                f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;">{s["pl"]:+.2f}u</td>'
                f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;">{_roi_span(s["roi"])}</td></tr>')
        return rows

    bm = d["by_model"]
    model_rows = ""
    for mt in ("blended", "consensus"):
        s = bm[mt]
        if not s["n"]:
            continue
        model_rows += (
            f'<tr><td style="padding:7px 10px;border-bottom:1px solid #eee;">{mt}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;color:#616b7a;">{s["n"]}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;">{s["win_rate"]}%</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;">{s["pl"]:+.2f}u</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;">{_roi_span(s["roi"])}</td></tr>')

    section = ('<div style="font-size:12px;color:#2c6e8f;text-transform:uppercase;'
               'letter-spacing:1.5px;font-weight:700;margin:22px 0 6px;">')
    thead = ('<tr style="background:#f0f2f5;"><th style="padding:7px 10px;text-align:left;'
             'font-size:11px;color:#616b7a;">{c0}</th>'
             '<th style="padding:7px 10px;text-align:right;font-size:11px;color:#616b7a;">Paris</th>'
             '<th style="padding:7px 10px;text-align:right;font-size:11px;color:#616b7a;">Réussite</th>'
             '<th style="padding:7px 10px;text-align:right;font-size:11px;color:#616b7a;">P/L</th>'
             '<th style="padding:7px 10px;text-align:right;font-size:11px;color:#616b7a;">ROI</th></tr>')

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;background:#f6f7f9;font-family:Arial,Helvetica,sans-serif;color:#171a1f;">
<div style="max-width:640px;margin:0 auto;padding:20px;">

  <div style="background:#161a20;border-radius:12px;padding:22px;text-align:center;">
    <div style="color:#5aa8c7;font-size:11px;letter-spacing:3px;text-transform:uppercase;">BetBot CI · Rapport hebdo</div>
    <div style="color:#fff;font-size:20px;font-weight:700;margin-top:6px;">Performance du modèle</div>
    <div style="color:#96a0af;font-size:12px;margin-top:4px;">Généré le {d['generated']} · période {d['period']}</div>
  </div>

  <div style="font-size:12px;color:#616b7a;margin:16px 2px;">
    Mesure « would-have » à plat 1u/pari sur tous les pronostics dont le match est fini.
    <b>Aucun argent réel</b> — c'est la trace du modèle.
  </div>

  <div style="font-size:12px;color:#2c6e8f;text-transform:uppercase;letter-spacing:1.5px;font-weight:700;margin:6px 0;">7 derniers jours</div>
  <table style="width:100%;border-collapse:separate;border-spacing:8px;"><tr>
    {kpi("Picks", wk["n"], f"{wk['w']}G / {wk['l']}P")}
    {kpi("Réussite", f"{wk['win_rate']}%" if wk['win_rate'] is not None else "—")}
    {kpi("ROI", week_roi)}
    {kpi("P/L", f"{wk['pl']:+.1f}u")}
  </tr></table>

  <div style="font-size:12px;color:#2c6e8f;text-transform:uppercase;letter-spacing:1.5px;font-weight:700;margin:16px 0 0;">Cumulé ({ov['n']} picks)</div>
  <table style="width:100%;border-collapse:separate;border-spacing:8px;"><tr>
    {kpi("Picks notés", ov["n"], f"{ov['w']}G / {ov['l']}P / {ov['v']}nul")}
    {kpi("Réussite", f"{ov['win_rate']}%" if ov['win_rate'] is not None else "—")}
    {kpi("ROI", ov_roi)}
    {kpi("P/L", f"{ov['pl']:+.1f}u")}
  </tr></table>

  <div style="background:#fff8ec;border:1px solid #f0e2c8;border-left:4px solid #b7801a;border-radius:10px;padding:14px 16px;margin-top:20px;">
    <b style="color:#b7801a;">📊 Calibration — le chiffre à suivre</b>
    <div style="font-size:13px;color:#5b5240;margin-top:6px;">
      « Prédit » = proba moyenne annoncée par le modèle ; « Réel » = % réellement gagné.
      L'écart doit se <b>resserrer semaine après semaine</b> (le calibrateur du dimanche le corrige).
      Écart négatif = le modèle est <b>trop confiant</b>.
    </div>
    <table style="width:100%;border-collapse:collapse;margin-top:10px;font-size:13px;background:#fff;border-radius:8px;overflow:hidden;">
      <tr style="background:#f0f2f5;"><th style="padding:8px 10px;text-align:left;font-size:11px;color:#616b7a;">Proba annoncée</th>
        <th style="padding:8px 10px;text-align:right;font-size:11px;color:#616b7a;">Paris</th>
        <th style="padding:8px 10px;text-align:right;font-size:11px;color:#616b7a;">Prédit</th>
        <th style="padding:8px 10px;text-align:right;font-size:11px;color:#616b7a;">Réel</th>
        <th style="padding:8px 10px;text-align:right;font-size:11px;color:#616b7a;">Écart</th></tr>
      {calib_rows}
    </table>
  </div>

  {section}Par modèle</div>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">{thead.format(c0="Modèle")}{model_rows}</table>

  {section}Par marché</div>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">{thead.format(c0="Marché")}{stat_rows(d["by_market"])}</table>

  {section}Par ligue · top 12</div>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">{thead.format(c0="Ligue")}{stat_rows(d["by_league"])}</table>

  <div style="font-size:12px;color:#8a93a2;text-align:center;margin-top:22px;padding-top:14px;border-top:1px solid #e2e6ec;">
    Échantillon encore petit → variance élevée. Le vrai verdict se lit sur la durée,
    pas d'une semaine à l'autre. Détail : dashboard → 📊 Performance → 🔬 Modèle.
  </div>
</div></body></html>"""


def send_weekly_report(notifier, subject: str | None = None) -> dict:
    """Build the report and email it. Returns {sent, total}."""
    data = build_report_data()
    _archive_report(data)  # keep the full history for trend analysis
    if data["total"] == 0:
        html = ('<p style="font-family:Arial;">Aucun pronostic noté cette période — '
                'rien à rapporter. Lance des scans, les résultats arriveront à la fin des matchs.</p>')
        subject = subject or "BetBot — Rapport hebdo (aucune donnée)"
    else:
        html = _render_email(data)
        roi = data["overall"]["roi"]
        subject = subject or (
            f"BetBot — Rapport hebdo : {data['overall']['n']} picks, "
            f"ROI {('+' if (roi or 0) >= 0 else '')}{roi}%")
    ok = notifier.send(subject=subject, html=html)
    logger.info("Weekly report email sent=%s (%d picks)", ok, data["total"])
    return {"sent": ok, "total": data["total"]}
