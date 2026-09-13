"""Email notification via Gmail SMTP."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from betbot.analysis import ValueBet, Parlay

logger = logging.getLogger("betbot.notifier")


class EmailNotifier:
    def __init__(self, gmail_user: str, app_password: str, recipient: str):
        self._user = gmail_user
        self._password = app_password
        self._recipient = recipient

    def send(self, subject: str, html: str) -> bool:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._user
        msg["To"] = self._recipient
        msg.attach(MIMEText(html, "html"))
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(self._user, self._password)
                s.sendmail(self._user, self._recipient, msg.as_string())
            logger.info("Email envoyé → %s", self._recipient)
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error(
                "Erreur authentification Gmail. "
                "Vérifie GMAIL_APP_PASSWORD dans .env (mot de passe d'application, 16 car.)"
            )
            return False
        except Exception as exc:
            logger.error("Erreur envoi email : %s", exc)
            return False

    def render_html(
        self,
        bets: list[ValueBet],
        parlays: list[Parlay],
        stats: dict,
        bankroll: float,
        quota: dict | None = None,
        favorites: list | None = None,
    ) -> str:
        date_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
        return _build_html(bets, parlays, stats, date_str, bankroll, quota,
                           favorites)

    def render_no_value(self, quota: dict | None = None) -> str:
        date_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
        <body style="font-family:Arial,sans-serif;background:#f0f2f5;padding:20px;">
        <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;text-align:center;">
          <div style="color:#00e5a0;font-size:13px;letter-spacing:2px;">BETBOT CI</div>
          <h2 style="color:#1a1a2e;">Aucune valeur détectée</h2>
          <p style="color:#666;">Scan du {date_str} : aucun pari ne satisfait les critères de valeur (edge ≥ 4%).</p>
          <p style="color:#888;font-size:12px;">C'est normal — le bot ne recommande que quand il y a un vrai avantage statistique.</p>
          {_render_quota_section(quota)}
        </div></body></html>"""


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _render_quota_section(quota: dict | None) -> str:
    """Quota block — the user rotates Odds API keys BY HAND and this daily
    email is where they look. Credits alone are not actionable: what matters
    is how many scans they still buy, because a scan bills
    leagues x regions x markets (46 leagues in eu,uk with h2h+totals = 184).
    """
    if not quota:
        return ""
    remaining = int(quota.get("remaining", -1))
    cost = int(quota.get("scan_cost", 0))
    if remaining < 0 or cost <= 0:
        return ""
    n_scans = max(0, (remaining - int(quota.get("reserve", 0))) // cost)
    leagues = int(quota.get("leagues", 0))
    if n_scans == 0:
        bg, border, color, title = "#ffebee", "#c62828", "#b71c1c", "⚠️ Quota épuisé — rotation de clé nécessaire"
        body = (f"Il reste <b>{remaining}</b> requêtes, mais un scan en coûte "
                f"<b>{cost}</b> ({leagues} ligues). <b>Le prochain scan sera refusé.</b> "
                f"Ajoute une clé dans <code>ODDS_API_KEYS</code> (séparées par des virgules) : "
                f"le bot bascule seul sur la première qui a le budget.")
    elif n_scans <= 2:
        bg, border, color, title = "#fff8e1", "#ffa000", "#e65100", "⏳ Quota bientôt épuisé"
        body = (f"<b>{remaining}</b> requêtes restantes = <b>{n_scans} scan(s)</b> "
                f"({cost} req/scan, {leagues} ligues). Prépare ta prochaine clé.")
    else:
        bg, border, color, title = "#e8f5e9", "#2e7d32", "#1b5e20", "Quota Odds API"
        body = (f"<b>{remaining}</b> requêtes restantes = <b>{n_scans} scans</b> "
                f"({cost} req/scan, {leagues} ligues).")
    # Second subscription, same monthly cycle, same block: the user recharges
    # both together, so both deadlines belong in one glance.
    af = quota.get("apifootball") or {}
    extra = ""
    if af.get("state") == "ok" and af.get("days_left") is not None:
        j = int(af["days_left"])
        if j <= 3:
            extra = (f'<p style="margin:8px 0 0;color:#b71c1c;font-size:13px;">'
                     f"<b>api-football expire dans {j} jour(s)</b> — sans lui, plus "
                     f"de statistiques d'équipe ni de xG, et le modèle retombe sur "
                     f"le consensus.</p>")
        else:
            extra = (f'<p style="margin:8px 0 0;color:#777;font-size:12px;">'
                     f"api-football : {j} jour(s) d'abonnement restants.</p>")
    elif af.get("state") == "daily_limit_reached":
        extra = ('<p style="margin:8px 0 0;color:#777;font-size:12px;">'
                 "api-football : quota du jour épuisé (l'abonnement reste valide). "
                 "Sans effet sur les pronostics.</p>")
    elif af.get("state") == "inactive":
        extra = ('<p style="margin:8px 0 0;color:#b71c1c;font-size:13px;">'
                 "<b>Abonnement api-football inactif</b> — à renouveler.</p>")

    return (f'<div style="background:{bg};border-radius:10px;padding:14px;'
            f'margin-bottom:16px;border-left:4px solid {border};">'
            f'<b style="color:{color};">{title}</b>'
            f'<p style="margin:6px 0 0;color:#555;font-size:13px;line-height:1.6;">{body}</p>'
            f'{extra}</div>')


def _build_html(
    bets: list[ValueBet],
    parlays: list[Parlay],
    stats: dict,
    date_str: str,
    bankroll: float,
    quota: dict | None = None,
    favorites: list | None = None,
) -> str:
    bets_html = _render_bets_section(bets, bankroll)
    favorites_html = _render_favorites_section(favorites or [])
    parlays_html = _render_parlays_section(parlays)
    stats_html = _render_stats_section(stats)

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;">
<div style="max-width:620px;margin:0 auto;padding:20px;">

  <!-- Header -->
  <div style="background:#1a1a2e;border-radius:12px;padding:24px;text-align:center;margin-bottom:20px;">
    <div style="color:#00e5a0;font-size:12px;letter-spacing:3px;text-transform:uppercase;">BetBot CI</div>
    <div style="color:#fff;font-size:22px;font-weight:700;margin-top:6px;">Rapport Football</div>
    <div style="color:#aaa;font-size:13px;margin-top:4px;">{date_str}</div>
    <div style="margin-top:10px;color:#ccc;font-size:12px;">
      Capital : {bankroll:.0f}$ &nbsp;·&nbsp; Modèle : Poisson + Consensus
    </div>
  </div>

  {bets_html}
  {parlays_html}
  {favorites_html}

  {stats_html}

  <!-- Instructions -->
  <div style="background:#fff8e1;border-radius:10px;padding:16px;margin-bottom:16px;border-left:4px solid #ffa000;">
    <b style="color:#e65100;">Comment utiliser ces recommandations :</b>
    <ol style="margin:8px 0 0;padding-left:16px;color:#666;font-size:13px;line-height:1.8;">
      <li>Place les paris qui t'intéressent sur <b>Betclic</b> ou <b>Bet365</b> — les cotes ci-dessus viennent de ces deux books, donc elles sont réellement jouables</li>
      <li><b>Ces picks sont déjà comptés comme placés</b> dans les statistiques du bot. Il n'y a rien à valider : si tu n'en joues pas un, le bilan affiché sera pessimiste, pas faux dans l'autre sens</li>
      <li>Les résultats sont notés automatiquement — tu n'as aucune saisie à faire</li>
      <li>Suis la performance dans <b>dashboard → 📊 Performance</b> (ROI à plat, tes mises réelles étant variables)</li>
      <li>Vérifie toujours la cote avant de parier : elle bouge entre le scan et ton clic</li>
    </ol>
  </div>

  {_render_quota_section(quota)}

  <!-- Disclaimer -->
  <div style="text-align:center;color:#999;font-size:11px;padding:10px;">
    Parie de manière responsable. Ces recommandations sont basées sur des modèles statistiques
    et ne garantissent pas les résultats. Ne mise jamais plus que tu peux perdre.
  </div>

</div></body></html>"""


def _render_favorites_section(favorites: list) -> str:
    """Favoris calibrés — the agreement channel, honestly labelled.

    Every wording choice here is deliberate. This channel claims NO edge: the
    model and the de-vigged market simply agree the outcome is likely. Its
    long-run expectation is minus the bookmaker's margin, and the owner chose
    it knowing that — his goal is hit rate. The section must never dress these
    up as value picks: no edge column, no Kelly stake, and the expectation
    stated in plain text where every reader of the email will see it.
    """
    if not favorites:
        return ""
    rows = []
    for b in favorites[:12]:
        rows.append(
            f'<tr>'
            f'<td style="padding:7px 8px;font-size:13px;color:#1a1a2e;">'
            f'{b.home_team} – {b.away_team}'
            f'<div style="color:#888;font-size:11px;">{b.league_label}</div></td>'
            f'<td style="padding:7px 8px;font-size:13px;">{b.selection_label}</td>'
            f'<td style="padding:7px 8px;text-align:center;font-size:13px;">'
            f'<b>{b.best_odds:.2f}</b><div style="color:#888;font-size:11px;">'
            f'{b.best_book}</div></td>'
            f'<td style="padding:7px 8px;text-align:center;font-size:13px;">'
            f'{b.model_prob*100:.0f}% / {(b.market_prob or 0)*100:.0f}%</td>'
            f'</tr>'
        )
    return (
        '<div style="background:#fff;border-radius:12px;padding:18px;'
        'margin-bottom:16px;border-left:4px solid #5c6bc0;">'
        '<b style="color:#3949ab;font-size:15px;">⭐ Favoris calibrés</b>'
        '<p style="margin:6px 0 10px;color:#666;font-size:12px;line-height:1.6;">'
        'Le modèle <b>et</b> le marché s\'accordent : probabilité ≥ 70 % des '
        'deux côtés. Aucun avantage sur la cote n\'est revendiqué — sur la '
        'durée, ce canal paie la marge du bookmaker (≈ −3 à −5 %). Son objectif '
        'est le <b>taux de réussite</b>, pas le rendement. Mise libre.</p>'
        '<table style="width:100%;border-collapse:collapse;">'
        '<tr style="color:#888;font-size:11px;text-align:left;">'
        '<th style="padding:4px 8px;">Match</th>'
        '<th style="padding:4px 8px;">Sélection</th>'
        '<th style="padding:4px 8px;text-align:center;">Cote</th>'
        '<th style="padding:4px 8px;text-align:center;">Modèle / Marché</th></tr>'
        + "".join(rows) + '</table></div>'
    )


def _render_bets_section(bets: list[ValueBet], bankroll: float = 100.0) -> str:
    if not bets:
        return ""

    rows = ""
    for i, bet in enumerate(bets, 1):
        edge_pct = round(bet.value_edge * 100, 1)
        edge_color = "#00c853" if edge_pct >= 0 else "#e53935"
        model_badge = "🤖 Poisson" if bet.model_type == "poisson" else "📊 Consensus"
        lambda_info = ""
        if bet.lambda_home and bet.lambda_away:
            lambda_info = f"λ dom={bet.lambda_home:.2f} / λ ext={bet.lambda_away:.2f}"

        rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0;">
          <td style="padding:12px 10px;font-size:13px;">
            <b style="color:#1a1a2e;">{bet.home_team} vs {bet.away_team}</b><br>
            <span style="color:#888;font-size:11px;">{bet.league_label}</span>
          </td>
          <td style="padding:12px 10px;text-align:center;">
            <span style="background:#e8f5e9;color:#2e7d32;padding:3px 10px;border-radius:10px;font-size:12px;white-space:nowrap;">
              {bet.selection_label}
            </span>
          </td>
          <td style="padding:12px 8px;text-align:center;font-weight:700;color:#e65100;font-size:15px;">{bet.best_odds:.2f}</td>
          <td style="padding:12px 8px;text-align:center;color:{edge_color};font-weight:700;font-size:13px;">+{edge_pct}%</td>
          <td style="padding:12px 8px;text-align:center;color:#555;font-size:13px;">{bet.kelly_stake}$</td>
          <td style="padding:12px 8px;text-align:center;color:#888;font-size:11px;">
            {model_badge}<br>{bet.best_book}
          </td>
        </tr>"""

    return f"""
    <div style="background:#fff;border-radius:12px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,0.08);overflow:hidden;">
      <div style="background:#0d47a1;padding:14px 18px;">
        <div style="color:#bbdefb;font-size:11px;letter-spacing:1px;">PARIS INDIVIDUELS ({len(bets)} sélection(s))</div>
        <div style="color:#fff;font-size:15px;font-weight:700;margin-top:4px;">Valeurs détectées par le modèle</div>
      </div>
      <div style="overflow-x:auto;">
      <table style="width:100%;border-collapse:collapse;min-width:500px;">
        <tr style="background:#f5f5f5;">
          <th style="padding:8px 10px;text-align:left;font-size:11px;color:#999;">MATCH</th>
          <th style="padding:8px;text-align:center;font-size:11px;color:#999;">SÉLECTION</th>
          <th style="padding:8px;text-align:center;font-size:11px;color:#999;">COTE</th>
          <th style="padding:8px;text-align:center;font-size:11px;color:#999;">EDGE</th>
          <th style="padding:8px;text-align:center;font-size:11px;color:#999;">MISE</th>
          <th style="padding:8px;text-align:center;font-size:11px;color:#999;">SOURCE</th>
        </tr>
        {rows}
      </table>
      </div>
      <div style="padding:10px 16px;background:#fafafa;font-size:11px;color:#888;">
        Edge = probabilité modèle × cote − 1. Positif = avantage statistique réel.
        Mise = Kelly fractionnel (25%) sur capital {bankroll:.0f}$.
      </div>
    </div>"""


def _render_parlays_section(parlays: list[Parlay]) -> str:
    if not parlays:
        return ""

    medals = ["Combiné #1", "Combiné #2", "Combiné #3"]
    cards = ""
    for i, parlay in enumerate(parlays):
        ev_color = "#00c853" if parlay.combined_ev >= 0 else "#e53935"
        rows = ""
        for bet in parlay.bets:
            rows += f"""
            <tr>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;">
                <b>{bet.home_team} vs {bet.away_team}</b>
                <span style="color:#888;"> — {bet.league_label}</span>
              </td>
              <td style="padding:8px;border-bottom:1px solid #eee;text-align:center;">
                <span style="background:#e8f5e9;color:#2e7d32;padding:2px 8px;border-radius:8px;font-size:12px;">
                  {bet.selection_label}
                </span>
              </td>
              <td style="padding:8px;border-bottom:1px solid #eee;text-align:center;font-weight:700;color:#e65100;font-size:13px;">
                {bet.best_odds:.2f}
              </td>
              <td style="padding:8px;border-bottom:1px solid #eee;text-align:center;color:#666;font-size:12px;">
                {bet.model_prob*100:.0f}%
              </td>
            </tr>"""

        cards += f"""
        <div style="background:#fff;border-radius:12px;margin-bottom:16px;box-shadow:0 2px 8px rgba(0,0,0,0.08);overflow:hidden;">
          <div style="background:#1a1a2e;padding:14px 18px;">
            <div style="color:#aaa;font-size:11px;">{medals[i] if i < len(medals) else f"Combiné #{i+1}"}</div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px;">
              <div style="color:#fff;font-size:26px;font-weight:700;">× {parlay.combined_odds}</div>
              <div style="text-align:right;">
                <div style="color:{ev_color};font-size:12px;">EV {'+' if parlay.combined_ev>=0 else ''}{parlay.combined_ev}%</div>
                <div style="color:#aaa;font-size:11px;">Prob. combinée : {parlay.combined_prob*100:.1f}%</div>
              </div>
            </div>
          </div>
          <table style="width:100%;border-collapse:collapse;">
            <tr style="background:#f5f5f5;">
              <th style="padding:6px 12px;text-align:left;font-size:10px;color:#999;">MATCH</th>
              <th style="padding:6px;text-align:center;font-size:10px;color:#999;">SÉLECTION</th>
              <th style="padding:6px;text-align:center;font-size:10px;color:#999;">COTE</th>
              <th style="padding:6px;text-align:center;font-size:10px;color:#999;">PROB.</th>
            </tr>
            {rows}
          </table>
        </div>"""

    return f"""
    <div style="margin-bottom:20px;">
      <div style="background:#37474f;border-radius:12px;padding:14px 18px;margin-bottom:12px;">
        <div style="color:#b0bec5;font-size:11px;letter-spacing:1px;">COMBINÉS ({len(parlays)} proposé(s))</div>
        <div style="color:#fff;font-size:15px;font-weight:700;margin-top:4px;">Meilleurs accumulateurs</div>
      </div>
      {cards}
    </div>"""


def _render_stats_section(stats: dict) -> str:
    if not stats or stats.get("n_bets", 0) == 0:
        return ""

    roi_color = "#00c853" if stats.get("roi", 0) >= 0 else "#e53935"
    return f"""
    <div style="background:#fff;border-radius:12px;padding:16px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
      <div style="color:#888;font-size:11px;letter-spacing:1px;margin-bottom:10px;">HISTORIQUE 30 JOURS</div>
      <div style="display:flex;justify-content:space-around;text-align:center;flex-wrap:wrap;gap:8px;">
        <div>
          <div style="font-size:22px;font-weight:700;color:#1a1a2e;">{stats['n_bets']}</div>
          <div style="font-size:11px;color:#888;">paris joués</div>
        </div>
        <div>
          <div style="font-size:22px;font-weight:700;color:#1a1a2e;">{stats['hit_rate']}%</div>
          <div style="font-size:11px;color:#888;">taux de réussite</div>
        </div>
        <div>
          <div style="font-size:22px;font-weight:700;color:{roi_color};">{'+' if stats['roi']>=0 else ''}{stats['roi']}%</div>
          <div style="font-size:11px;color:#888;">ROI</div>
        </div>
        <div>
          <div style="font-size:22px;font-weight:700;color:#0d47a1;">+{stats['avg_edge']}%</div>
          <div style="font-size:11px;color:#888;">edge moyen</div>
        </div>
      </div>
    </div>"""
