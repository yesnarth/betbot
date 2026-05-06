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
    ) -> str:
        date_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
        return _build_html(bets, parlays, stats, date_str, bankroll)

    def render_no_value(self) -> str:
        date_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
        <body style="font-family:Arial,sans-serif;background:#f0f2f5;padding:20px;">
        <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;text-align:center;">
          <div style="color:#00e5a0;font-size:13px;letter-spacing:2px;">BETBOT CI</div>
          <h2 style="color:#1a1a2e;">Aucune valeur détectée</h2>
          <p style="color:#666;">Scan du {date_str} : aucun pari ne satisfait les critères de valeur (edge ≥ 4%).</p>
          <p style="color:#888;font-size:12px;">C'est normal — le bot ne recommande que quand il y a un vrai avantage statistique.</p>
        </div></body></html>"""


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _build_html(
    bets: list[ValueBet],
    parlays: list[Parlay],
    stats: dict,
    date_str: str,
    bankroll: float,
) -> str:
    bets_html = _render_bets_section(bets, bankroll)
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
  {stats_html}

  <!-- Instructions -->
  <div style="background:#fff8e1;border-radius:10px;padding:16px;margin-bottom:16px;border-left:4px solid #ffa000;">
    <b style="color:#e65100;">Comment utiliser ces recommandations :</b>
    <ol style="margin:8px 0 0;padding-left:16px;color:#666;font-size:13px;line-height:1.8;">
      <li>Chaque paris individuel est indépendant — tu peux jouer 1, 2 ou plusieurs</li>
      <li>La mise recommandée (Kelly) est calculée pour ton capital de {bankroll:.0f}$</li>
      <li>Pour les combinés : sélectionne les 3 matchs sur Betclic ou Bet365</li>
      <li>Vérifie toujours les cotes avant de parier (elles peuvent changer)</li>
    </ol>
  </div>

  <!-- Disclaimer -->
  <div style="text-align:center;color:#999;font-size:11px;padding:10px;">
    Parie de manière responsable. Ces recommandations sont basées sur des modèles statistiques
    et ne garantissent pas les résultats. Ne mise jamais plus que tu peux perdre.
  </div>

</div></body></html>"""


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
