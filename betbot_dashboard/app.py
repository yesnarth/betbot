"""
BetBot Streamlit dashboard.

Run locally:
    streamlit run betbot_dashboard/app.py

Talks to the FastAPI backend (BETBOT_API_URL, default http://localhost:8000).
Two modes:
  - Manual scan (zero AI, free, deterministic) — uses the blended Poisson model
  - AI agent (requires ANTHROPIC_API_KEY) — same data + Claude reasoning
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import pandas as pd
import streamlit as st

API_URL = os.getenv("BETBOT_API_URL", "http://localhost:8000").rstrip("/")
BASIC_USER = os.getenv("API_BASIC_USER", "betbot")
BASIC_PASSWORD = os.getenv("API_BASIC_PASSWORD", "")
AUTH = (BASIC_USER, BASIC_PASSWORD) if BASIC_PASSWORD else None


def api_get(path: str, **params: Any) -> Any:
    r = httpx.get(f"{API_URL}{path}", params=params, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(path: str, json: dict | None = None, **params: Any) -> Any:
    r = httpx.post(f"{API_URL}{path}", params=params, json=json, auth=AUTH, timeout=180)
    r.raise_for_status()
    return r.json()


def render_picks_table(picks: list[dict]) -> None:
    if not picks:
        return
    df = pd.DataFrame(picks)
    cols = [c for c in [
        "home_team", "away_team", "league", "selection_label",
        "best_odds", "model_prob", "value_edge", "kelly_stake",
        "best_book", "model_type",
    ] if c in df.columns]
    display = df[cols].copy()
    rename = {
        "home_team": "Domicile", "away_team": "Extérieur", "league": "Ligue",
        "selection_label": "Pari", "best_odds": "Cote", "model_prob": "Proba modèle",
        "value_edge": "Edge", "kelly_stake": "Mise Kelly",
        "best_book": "Bookmaker", "model_type": "Modèle",
    }
    display = display.rename(columns=rename)
    column_config = {}
    if "Cote" in display.columns:
        column_config["Cote"] = st.column_config.NumberColumn(format="%.2f")
    if "Proba modèle" in display.columns:
        column_config["Proba modèle"] = st.column_config.NumberColumn(format="%.1f%%")
        display["Proba modèle"] = display["Proba modèle"] * 100
    if "Edge" in display.columns:
        column_config["Edge"] = st.column_config.NumberColumn(format="%+.1f%%")
        display["Edge"] = display["Edge"] * 100
    if "Mise Kelly" in display.columns:
        column_config["Mise Kelly"] = st.column_config.NumberColumn(format="$%.2f")
    st.dataframe(display, width='stretch', hide_index=True, column_config=column_config)

    # Caveat on suspiciously large edges
    if "value_edge" in df.columns:
        big_edges = (df["value_edge"] > 0.20).sum()
        if big_edges > 0:
            st.warning(
                f"⚠️ **{big_edges} pari(s) ont un edge > 20%.** Le marché des cotes est "
                "généralement bien calibré ; un edge aussi élevé révèle souvent un "
                "**défaut de modèle**. Privilégie les paris à edge **2-10%** où la valeur est plus fiable."
            )


def render_parlays(parlays: list[dict]) -> None:
    if not parlays:
        return
    for i, parlay in enumerate(parlays, 1):
        ev = parlay.get("combined_ev_pct", 0)
        odds = parlay.get("combined_odds", 0)
        with st.expander(f"Combiné #{i}  —  cote × {odds}  —  EV {ev:+.1f}%", expanded=(i == 1)):
            df = pd.DataFrame(parlay.get("legs", []))
            if not df.empty:
                show = [c for c in [
                    "home_team", "away_team", "selection_label",
                    "best_odds", "model_prob", "value_edge",
                ] if c in df.columns]
                disp = df[show].rename(columns={
                    "home_team": "Domicile", "away_team": "Extérieur",
                    "selection_label": "Pari", "best_odds": "Cote",
                    "model_prob": "Proba modèle", "value_edge": "Edge",
                })
                cfg = {}
                if "Cote" in disp.columns:
                    cfg["Cote"] = st.column_config.NumberColumn(format="%.2f")
                if "Proba modèle" in disp.columns:
                    cfg["Proba modèle"] = st.column_config.NumberColumn(format="%.1f%%")
                    disp["Proba modèle"] = disp["Proba modèle"] * 100
                if "Edge" in disp.columns:
                    cfg["Edge"] = st.column_config.NumberColumn(format="%+.1f%%")
                    disp["Edge"] = disp["Edge"] * 100
                st.dataframe(disp, width='stretch', hide_index=True, column_config=cfg)


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="BetBot Dashboard",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

      /* Typography */
      html, body, [class*="css"], .stApp, .stMarkdown, .stButton button, .stSelectbox, .stTextInput {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      }

      /* Hide Streamlit chrome */
      #MainMenu, header [data-testid="stToolbar"], footer { visibility: hidden; }
      .stDeployButton { display: none !important; }
      header { background: transparent !important; }

      /* Top padding tighter */
      .block-container { padding-top: 1.2rem !important; max-width: 1280px; }

      /* Header row compact */
      h1 { font-size: 1.65rem !important; font-weight: 700 !important; margin-bottom: 0.2rem !important; letter-spacing: -0.02em; }
      h2 { font-size: 1.25rem !important; font-weight: 600 !important; letter-spacing: -0.01em; }
      h3 { font-size: 1.05rem !important; font-weight: 600 !important; }

      /* Tabs — wider, bolder, cleaner separators */
      [data-baseweb="tab-list"] {
        gap: 4px !important;
        border-bottom: 1px solid #e5e7eb !important;
        padding-bottom: 0 !important;
      }
      [data-baseweb="tab"] {
        background: transparent !important;
        padding: 10px 16px !important;
        border-radius: 8px 8px 0 0 !important;
        font-weight: 500 !important;
        color: #475569 !important;
        transition: all 0.15s ease !important;
      }
      [data-baseweb="tab"]:hover { background: #f1f5f9 !important; color: #0f172a !important; }
      [data-baseweb="tab"][aria-selected="true"] {
        color: #10b981 !important; font-weight: 600 !important; background: #ecfdf5 !important;
      }
      [data-baseweb="tab-highlight"] { background: #10b981 !important; height: 3px !important; }

      /* Sidebar — softer with section cards */
      section[data-testid="stSidebar"] { background: #f8fafc !important; border-right: 1px solid #e5e7eb; }
      section[data-testid="stSidebar"] h2 { font-size: 0.78rem !important; text-transform: uppercase; letter-spacing: 0.06em; color: #64748b !important; font-weight: 600 !important; margin-top: 0.5rem; }
      section[data-testid="stSidebar"] [data-testid="stMetricValue"] { font-size: 1.5rem !important; font-weight: 700 !important; }
      section[data-testid="stSidebar"] [data-testid="stMetricDelta"] { font-size: 0.78rem !important; }
      section[data-testid="stSidebar"] hr { margin: 1.2rem 0 !important; border-color: #e2e8f0 !important; }

      /* Metric tiles */
      [data-testid="stMetric"] {
        background: #ffffff;
        padding: 12px 16px;
        border-radius: 10px;
        border: 1px solid #e5e7eb;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
      }
      [data-testid="stMetricLabel"] { font-size: 0.78rem !important; color: #64748b !important; font-weight: 500 !important; }
      [data-testid="stMetricValue"] { font-weight: 700 !important; color: #0f172a !important; }

      /* Buttons — primary punchier */
      .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #10b981 0%, #059669 100%) !important;
        border: none !important; font-weight: 600 !important; box-shadow: 0 1px 3px rgba(16, 185, 129, 0.3);
      }
      .stButton > button[kind="primary"]:hover { box-shadow: 0 4px 8px rgba(16, 185, 129, 0.35) !important; transform: translateY(-1px); }
      .stButton > button { border-radius: 8px !important; transition: all 0.15s ease !important; }

      /* Alerts (info / warning / success) */
      [data-testid="stAlert"] { border-radius: 10px !important; border-left-width: 4px !important; padding: 12px 16px !important; }

      /* Empty-state utility (used via st.markdown) */
      .empty-state {
        text-align: center; padding: 36px 24px; background: #f8fafc;
        border: 1px dashed #cbd5e1; border-radius: 12px; color: #64748b;
      }
      .empty-state .icon { font-size: 2.4rem; display: block; margin-bottom: 0.4rem; opacity: 0.7; }
      .empty-state .title { font-weight: 600; color: #334155; font-size: 1rem; margin-bottom: 0.3rem; }
      .empty-state .hint { font-size: 0.88rem; }

      /* Dataframe */
      [data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; border: 1px solid #e5e7eb; }
    </style>
    """,
    unsafe_allow_html=True,
)


def empty_state(icon: str, title: str, hint: str = "") -> None:
    """Render a polished empty state instead of a flat info alert."""
    st.markdown(
        f"""<div class="empty-state">
            <span class="icon">{icon}</span>
            <div class="title">{title}</div>
            <div class="hint">{hint}</div>
        </div>""",
        unsafe_allow_html=True,
    )


st.title("⚽ BetBot Dashboard")
st.caption("Pronostiqueur quantitatif — modèle Dixon-Coles + xG + ELO + agent local")

# Health is fetched once — needed by the rest of the page
try:
    health = api_get("/health")
except Exception as exc:
    st.error(f"API injoignable sur {API_URL} : {exc}")
    st.info("Vérifie que les containers Docker tournent : `docker compose ps`")
    st.stop()

agent_enabled = bool(health.get("agent_enabled"))


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    # ─── Section 1 : KPIs essentiels ──────────────────────────────────
    st.header("KPIs")
    balance = float(health.get("balance", 0))
    available = float(health.get("available", 0))
    initial = float(health.get("bankroll_initial", 0))
    delta_str = f"{balance - initial:+.0f} $" if initial > 0 else None
    st.metric("Solde courant", f"{balance:.0f} $", delta=delta_str)
    if balance != available:
        st.caption(f"Disponible : **{available:.0f} $** · "
                   f"Engagé : {balance - available:.0f} $")

    quota = int(health.get("odds_quota_remaining", -1))
    quota_min = int(health.get("odds_quota_minimum", 20))
    if quota >= 0:
        if health.get("odds_quota_exhausted"):
            st.metric("Quota Odds API", f"{quota} req",
                      delta=f"⚠ < {quota_min}", delta_color="inverse")
        else:
            delta_q = "OK" if quota >= 100 else f"min {quota_min}"
            st.metric("Quota Odds API", f"{quota} req", delta=delta_q,
                      delta_color="normal" if quota >= 100 else "off")

    st.metric("Équipes en DB", health["teams_in_db"])
    st.caption(f"Scans auto : {' · '.join(health['scan_hours'])}")

    active = health.get("active_sports", []) or []
    if active:
        sport_icons = []
        for k in active:
            if k.startswith("soccer_"):
                sport_icons.append("⚽")
            elif k.startswith("tennis_"):
                sport_icons.append("🎾")
            elif k.startswith("basketball_"):
                sport_icons.append("🏀")
            else:
                sport_icons.append("•")
        unique = " ".join(sorted(set(sport_icons)))
        st.caption(f"Sports actifs : {unique} ({len(active)} compétitions)")

    if agent_enabled:
        st.success("Agent IA Claude actif", icon="🤖")
    else:
        st.info("Agent IA Claude non configuré.", icon="ℹ️")

    st.divider()

    # ─── Section 2 : Actions rapides ──────────────────────────────────
    st.header("Actions")
    if st.button("🔄 Résoudre les paris terminés", width='stretch'):
        try:
            res = api_post("/predictions/resolve")
            st.success(f"Résolus : {res.get('resolved')} · "
                       f"En attente : {res.get('still_pending')}")
        except Exception as exc:
            st.error(f"Erreur : {exc}")

    st.divider()

    # ─── Section 3 : Filtres (collapsible) ──────────────────────────────
    with st.expander("🎚️ Filtres de scan", expanded=False):
        st.caption("S'appliquent à : **Scan manuel**, **Agent local**, **Agent IA**.")
        sport = st.selectbox(
            "Ligue / Compétition",
            options=[
                "Toutes",
                # Football
                "soccer_epl", "soccer_spain_la_liga",
                "soccer_germany_bundesliga", "soccer_italy_serie_a",
                "soccer_france_ligue1", "soccer_uefa_champs_league",
                # Tennis (auto-skipped si Grand Slam pas en cours)
                "tennis_atp_aus_open", "tennis_atp_french_open",
                "tennis_atp_wimbledon", "tennis_atp_us_open",
                # Basketball (auto-skipped hors saison)
                "basketball_nba", "basketball_euroleague",
            ],
            index=0,
            help="Les compétitions hors saison sont automatiquement ignorées au scan.",
        )
        today_only = st.checkbox("Seulement matchs d'aujourd'hui", value=False,
                                 help="Décoché par défaut : permet de scanner les 24-72h à venir.")
        min_edge_pct = st.slider("Edge minimum (%)", -10.0, 20.0, 4.0, 0.5)
        min_prob = st.slider("Probabilité modèle minimale", 0.10, 0.90, 0.40, 0.05)
        min_odds = st.slider("Cote minimum", 1.0, 5.0, 1.5, 0.1)
        n_legs = st.slider("Jambes par combiné", 1, 6, 3)
        n_combos = st.slider("Combinés à générer", 1, 10, 3)


# ---------------------------------------------------------------------------
# Tabs — top-level 5 sections, each with sub-tabs where needed
# ---------------------------------------------------------------------------

section_decision, section_matches, section_perf, section_history, section_system = st.tabs([
    "🎯 Décision",
    "📅 Matchs",
    "📊 Performance",
    "📜 Historique",
    "⚙️ Système",
])

with section_decision:
    st.caption("Génère des paris : scan déterministe, agent local avec règles, ou agent IA Claude.")
    tab_scan, tab_local, tab_agent = st.tabs([
        "🎯 Scan manuel",
        "🧠 Agent local",
        "🤖 Agent IA (Claude)",
    ])

with section_matches:
    st.caption("Matchs disponibles côté Odds API et paris déjà placés.")
    tab_events, tab_pending = st.tabs([
        "📅 Matchs disponibles",
        "⏳ Paris en attente",
    ])

with section_perf:
    st.caption("ROI, hit rate, CLV et gestion du bankroll.")
    tab_roi, tab_capital = st.tabs([
        "📊 ROI / Performance",
        "💰 Capital",
    ])

with section_history:
    tab_history = st.container()

with section_system:
    st.caption("État des sources, calibrateur ML et modèles dédiés (tennis ELO, basket pace+rating).")
    tab_sources, tab_calibrator, tab_tennis, tab_basket = st.tabs([
        "🔌 Sources",
        "🎚️ Calibrateur ML",
        "🎾 Modèle tennis",
        "🏀 Modèle basket",
    ])


# ---------------------------------------------------------------------------
# Tab 1 — Manual scan (no AI)
# ---------------------------------------------------------------------------

with tab_scan:
    st.subheader("Scan manuel — modèle Dixon-Coles + xG + ELO")
    st.caption(
        "Aucune IA. Reproductible. Gratuit (au-delà du quota Odds API). "
        "C'est exactement le calcul que fait le worker au quotidien à 09h, 15h, 20h."
    )

    if st.button("▶️ Lancer le scan", type="primary", width='stretch'):
        payload = {
            "sport_key": None if sport == "Toutes" else sport,
            "today_only": today_only,
            "min_edge": round(min_edge_pct / 100, 4),
            "min_prob": min_prob,
            "min_odds": min_odds,
            "n_legs": n_legs,
            "n_combos": n_combos,
        }
        with st.spinner("Récupération des cotes + calcul Poisson…"):
            try:
                res = api_post("/recommend/manual", json=payload)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                res = None

        if res:
            if res.get("odds_quota_exhausted"):
                empty_state(
                    "🚫",
                    f"Quota Odds API épuisé ({res.get('odds_quota_remaining', '?')} req restantes)",
                    f"Notre garde-fou bloque les scans en dessous de {health.get('odds_quota_minimum', 20)} "
                    "requêtes pour éviter d'épuiser le quota mensuel. Le quota se réinitialise "
                    "le 1er du mois. Pour abaisser le seuil, modifie ODDS_QUOTA_MINIMUM dans .env.",
                )
            else:
                cols = st.columns(3)
                cols[0].metric("Matchs scannés", res["n_events_scanned"])
                cols[1].metric("Paris détectés", res["n_picks"])
                cols[2].metric("Combinés générés", res["n_parlays"])

                if res["n_events_scanned"] == 0:
                    empty_state(
                        "🔎",
                        "Aucun match correspondant aux filtres",
                        "Décoche « Aujourd'hui seulement » ou élargis la ligue dans la sidebar.",
                    )
                elif res["n_picks"] == 0:
                    empty_state(
                        "🎯",
                        "Aucun pari ne passe les filtres",
                        "Le modèle n'a rien trouvé qui dépasse l'edge minimum. "
                        "Abaisse « Edge minimum » dans la sidebar et relance.",
                    )
                else:
                    st.markdown("### Paris individuels")
                    render_picks_table(res["picks"])
                    if res["parlays"]:
                        st.markdown("### Combinés")
                        render_parlays(res["parlays"])


# ---------------------------------------------------------------------------
# Tab 2 — Local deterministic agent (no AI cost, uses Tavily + ELO + weather)
# ---------------------------------------------------------------------------

with tab_local:
    st.subheader("Agent local — règles métier explicites")
    st.caption(
        "Prend les picks du scan, croise avec les news Tavily + blessures + météo + ELO, "
        "applique des règles explicites pour calibrer les edges fictifs. "
        "**Zéro coût** au-delà des quotas Tavily/Odds API. Reproductible et auditable."
    )

    col1, col2 = st.columns(2)
    with col1:
        agent_use_news = st.checkbox(
            "Consulter Tavily (news live)", value=True,
            help="Demande aux nouvelles du jour si une équipe a une blessure/suspension/coach viré.",
        )
    with col2:
        agent_use_weather = st.checkbox(
            "Consulter Open-Meteo (météo stade)", value=True,
            help="Pluie forte ou vent fort réduisent les paris Over.",
        )

    min_final = st.slider(
        "Edge minimum APRÈS calibration (%)",
        -5.0, 15.0, 2.0, 0.5,
        help="Seuil de rejet final. Les picks dont l'edge tombe sous ce seuil après calibration sont écartés.",
    )

    if st.button("🧠 Lancer l'agent local", type="primary", width='stretch'):
        payload = {
            "sport_key": None if sport == "Toutes" else sport,
            "today_only": today_only,
            "min_edge": round(min_edge_pct / 100, 4),
            "min_prob": min_prob,
            "min_odds": min_odds,
            "n_legs": n_legs,
            "n_combos": n_combos,
            "fetch_news": agent_use_news,
            "fetch_weather": agent_use_weather,
            "min_final_edge": round(min_final / 100, 4),
        }
        with st.spinner("Scan + appels Tavily/météo/ELO + règles…"):
            try:
                res = api_post("/recommend/agent-local", json=payload)
            except Exception as exc:
                st.error(f"Erreur : {exc}")
                res = None

        if res:
            if res.get("odds_quota_exhausted"):
                empty_state(
                    "🚫",
                    f"Quota Odds API épuisé ({res.get('odds_quota_remaining', '?')} req restantes)",
                    f"Notre garde-fou bloque les scans en dessous de {health.get('odds_quota_minimum', 20)} "
                    "requêtes. Le quota se réinitialise le 1er du mois. "
                    "Pour abaisser le seuil, modifie ODDS_QUOTA_MINIMUM dans .env.",
                )
                res = None  # skip the rest of the rendering
        if res:
            cols = st.columns(4)
            cols[0].metric("Picks bruts", res["n_picks_in"])
            cols[1].metric("Acceptés", res["n_accepted"])
            cols[2].metric("Rejetés", res["n_rejected"])
            cols[3].metric("Combinés", res["n_parlays"])

            sub = st.columns(3)
            sub[0].metric("Appels Tavily", res["n_news_calls"])
            sub[1].metric("Appels météo", res["n_weather_calls"])
            sub[2].metric("Tavily actif", "✓" if res["tavily_available"] else "non configuré")

            if not res["tavily_available"]:
                st.info(
                    "Tavily désactivé : pas de prise en compte des news live. "
                    "Active-le en mettant `TAVILY_API_KEY` dans `.env`."
                )

            if res["picks"]:
                st.markdown("### ✅ Paris validés")
                st.caption(
                    "🟢 **accepted** = pick à parier sereinement · "
                    "🟡 **flagged** = pick qui a déclenché une règle de prudence (edge anormalement élevé, "
                    "blessure du favori, etc.). À vérifier manuellement avant de placer la mise."
                )
                for i, p in enumerate(res["picks"], 1):
                    status = p.get("status", "accepted")
                    icon = "🟡" if status == "flagged" else "🟢"
                    label = (
                        f"{icon} #{i} · {p['home_team']} vs {p['away_team']} — "
                        f"{p['selection_label']} @ {p['best_odds']:.2f} · "
                        f"prob {p['model_prob']*100:.1f}% · edge {p['value_edge']*100:+.1f}%"
                    )
                    with st.expander(label):
                        kelly = p.get('kelly_stake', 0)
                        status_color = "orange" if status == "flagged" else "green"
                        st.markdown(
                            f"Statut : :{status_color}[**{status}**] · "
                            f"Mise Kelly : **${kelly:.2f}**"
                        )
                        for r in p.get("rationale", []):
                            st.write(f"- {r}")
            else:
                empty_state(
                    "🛑",
                    "Aucun pari n'a survécu aux règles de l'agent local",
                    "Les picks bruts ont tous été rejetés après calibration. "
                    "Cela peut être normal si le modèle vient d'être recalibré ou si les "
                    "ligues actives n'ont pas de matchs avec un edge fiable aujourd'hui.",
                )

            if res["rejected"]:
                with st.expander(f"❌ {res['n_rejected']} paris rejetés (cliquer pour voir)"):
                    for p in res["rejected"]:
                        st.markdown(
                            f"**{p['home_team']} vs {p['away_team']}** — "
                            f"{p['selection_label']} · edge final {p['value_edge']*100:+.1f}%"
                        )
                        for r in p.get("rationale", []):
                            st.write(f"- {r}")
                        st.divider()

            if res["parlays"]:
                st.markdown("### Combinés (paris validés uniquement)")
                render_parlays(res["parlays"])


# ---------------------------------------------------------------------------
# Tab 3 — Available events
# ---------------------------------------------------------------------------

with tab_events:
    st.subheader("Matchs disponibles")
    st.caption("Liste brute des matchs visibles côté Odds API, sans filtre de modèle.")
    if "events_data" not in st.session_state:
        st.session_state.events_data = None

    btn_label = "🔄 Rafraîchir la liste" if st.session_state.events_data else "🔍 Charger les matchs"
    if st.button(btn_label, width='stretch'):
        try:
            params = {"today_only": today_only}
            if sport != "Toutes":
                params["sport_key"] = sport
            st.session_state.events_data = api_get("/events", **params)
        except Exception as exc:
            st.error(f"Erreur : {exc}")
            st.session_state.events_data = None

    ev = st.session_state.events_data
    if ev:
        league_labels = {
            # Football
            "soccer_epl": "⚽ Premier League",
            "soccer_spain_la_liga": "⚽ La Liga",
            "soccer_germany_bundesliga": "⚽ Bundesliga",
            "soccer_italy_serie_a": "⚽ Serie A",
            "soccer_france_ligue1": "⚽ Ligue 1",
            "soccer_uefa_champs_league": "⚽ Champions League",
            "soccer_africa_cup_of_nations": "⚽ CAN",
            # Tennis
            "tennis_atp_aus_open": "🎾 Australian Open",
            "tennis_atp_french_open": "🎾 Roland Garros",
            "tennis_atp_wimbledon": "🎾 Wimbledon",
            "tennis_atp_us_open": "🎾 US Open",
            # Basketball
            "basketball_nba": "🏀 NBA",
            "basketball_euroleague": "🏀 EuroLeague",
        }
        st.metric("Total", ev["total"])
        for sk, items in ev["by_sport"].items():
            label = f"{league_labels.get(sk, sk)} ({len(items)})"
            with st.expander(label, expanded=True):
                df = pd.DataFrame(items)
                if not df.empty:
                    visible_cols = [c for c in [
                        "home_team", "away_team", "commence_time", "n_bookmakers",
                    ] if c in df.columns]
                    disp = df[visible_cols].rename(columns={
                        "home_team": "Domicile", "away_team": "Extérieur",
                        "commence_time": "Coup d'envoi", "n_bookmakers": "Books",
                    })
                    st.dataframe(disp, width='stretch', hide_index=True)


# ---------------------------------------------------------------------------
# Tab 3 — Pending predictions
# ---------------------------------------------------------------------------

with tab_pending:
    st.subheader("Paris en attente de résultat")
    try:
        rows = api_get("/predictions/pending")
        if not rows:
            empty_state(
                "⏳",
                "Aucun pari en attente",
                "Les paris arrivent ici quand un scan (manuel ou auto) sauvegarde "
                "des picks, ou quand tu valides l'output de l'agent IA.",
            )
        else:
            df = pd.DataFrame(rows)
            cols = [c for c in [
                "created_at", "home_team", "away_team", "selection",
                "best_odds", "closing_odds", "model_prob", "value_edge", "kelly_stake",
            ] if c in df.columns]
            st.dataframe(df[cols], width='stretch', hide_index=True)
    except Exception as exc:
        st.error(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Tab 4 — Performance (real metrics only, hidden when no data)
# ---------------------------------------------------------------------------

with tab_roi:
    st.subheader("Performance globale")
    period = st.selectbox("Période (jours)", [7, 14, 30, 60, 90, 180, 365], index=2)
    try:
        s = api_get("/stats/roi", days=period)
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        s = None

    if s is None:
        pass
    elif s["n_bets"] == 0:
        empty_state(
            "📊",
            f"Aucun pari résolu sur les {period} derniers jours",
            "Les métriques (ROI, hit rate, CLV) s'afficheront automatiquement dès "
            "qu'un pari sera résolu. Le worker le fait à 04h, ou clique "
            "« Résoudre les paris terminés » dans la sidebar.",
        )
    else:
        cols = st.columns(4)
        cols[0].metric("Paris résolus", s["n_bets"])
        cols[1].metric("Victoires", f"{s['n_wins']} ({s['hit_rate']}%)")
        cols[2].metric("ROI", f"{s['roi']:+.1f}%")
        cols[3].metric("Edge moyen", f"{s['avg_edge']:+.2f}%")

        if s.get("n_with_clv", 0) > 0:
            st.divider()
            st.markdown("### Closing Line Value (CLV)")
            st.caption(
                "Métrique de skill #1 des bettors pros : un CLV moyen positif "
                "signifie que tu paries à de meilleures cotes que la fermeture du marché."
            )
            clv_cols = st.columns(3)
            clv_cols[0].metric("Paris avec CLV", s["n_with_clv"])
            clv_cols[1].metric("CLV moyen", f"{s['avg_clv_pct']:+.2f}%")
            clv_cols[2].metric("% paris CLV > 0", f"{s['positive_clv_share']:.1f}%")
        else:
            st.caption(
                "Le CLV s'activera dès que le worker aura snappé les closing odds "
                "(automatique toutes les 10 min pour les matchs qui démarrent dans "
                "moins de 30 min)."
            )


# ---------------------------------------------------------------------------
# Tab — Capital (real bankroll tracking)
# ---------------------------------------------------------------------------

with tab_capital:
    st.subheader("💰 Gestion du capital")
    st.caption(
        "Toutes les mises consomment réellement le solde, tous les gains/pertes "
        "le mettent à jour automatiquement. Source unique de vérité : la table "
        "`bankroll_ledger` en DB."
    )

    try:
        bk_state = api_get("/bankroll/state")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        bk_state = None

    if bk_state:
        c = st.columns(4)
        c[0].metric("Solde courant", f"{bk_state['balance']:.2f} $")
        c[1].metric("Capital libre", f"{bk_state['available']:.2f} $")
        c[2].metric("Engagé sur paris", f"{bk_state['committed']:.2f} $")
        pnl = bk_state['pnl']
        c[3].metric("P&L cumulé", f"{pnl:+.2f} $",
                    delta=f"{pnl:+.2f} $" if pnl != 0 else None)

        st.divider()
        c2 = st.columns(4)
        c2[0].metric("Dépôts cumulés", f"{bk_state['total_deposits']:.2f} $")
        c2[1].metric("Retraits cumulés", f"{bk_state['total_withdrawals']:.2f} $")
        c2[2].metric("Gains cumulés", f"{bk_state['total_won']:.2f} $",
                     help="Somme des stakes × cote des paris gagnants (avant déduction de la mise).")
        c2[3].metric("Pertes (mises sur paris perdus)",
                     f"{bk_state['total_lost_stakes']:.2f} $",
                     help="Somme des stakes engagés sur les paris perdants.")

        # Evolution chart — guard against empty / single-point datasets to
        # avoid Vega-Lite "Infinite extent" warnings flooding the console.
        try:
            evo = api_get("/bankroll/evolution", days=60)
        except Exception as exc:
            evo = None
            st.caption(f"_Courbe d'évolution indisponible : {exc}_")
        st.markdown("### Évolution du solde (60 derniers jours)")
        if not evo:
            empty_state("💰", "Aucun mouvement sur la période",
                        "La courbe apparaîtra dès le premier dépôt ou pari.")
        elif len(evo) < 2:
            empty_state("📈", f"Un seul point de données ({len(evo)})",
                        "La courbe s'affichera dès le 2e mouvement de bankroll.")
        else:
            df = pd.DataFrame(evo)
            df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
            df = df.dropna(subset=["ts"]).set_index("ts").sort_index()
            # Vega-Lite emits "Infinite extent" warnings on dataframes < 2 rows
            # or with all-NaN columns. Filter both before calling line_chart.
            if len(df) < 2 or df["balance"].isna().all():
                empty_state("📈", "Pas assez de points pour tracer la courbe",
                            "La courbe s'affichera dès le 2e mouvement valide.")
            else:
                st.line_chart(df["balance"], height=260)

        # Deposit / withdraw
        st.divider()
        st.markdown("### Mouvements manuels")
        st.caption("Saisis un montant > 0 puis valide. Le solde est mis à jour immédiatement.")
        c3 = st.columns([1, 1, 2])
        with c3[0]:
            dep_amt = st.number_input("Montant dépôt ($)", min_value=0.0,
                                      value=0.0, step=10.0, format="%.2f", key="dep_amt")
            dep_note = st.text_input("Note dépôt", placeholder="ex : recharge mensuelle")
            if st.button("➕ Déposer", width='stretch', disabled=(dep_amt <= 0)):
                try:
                    api_post("/bankroll/deposit",
                             json={"amount": dep_amt, "note": dep_note or None})
                    st.success(f"+{dep_amt:.2f}$ déposés. Recharge la page.")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
        with c3[1]:
            wd_amt = st.number_input("Montant retrait ($)", min_value=0.0,
                                     value=0.0, step=10.0, format="%.2f", key="wd_amt")
            wd_note = st.text_input("Note retrait", placeholder="ex : retrait gains")
            if st.button("➖ Retirer", width='stretch', disabled=(wd_amt <= 0)):
                try:
                    api_post("/bankroll/withdraw",
                             json={"amount": wd_amt, "note": wd_note or None})
                    st.success(f"-{wd_amt:.2f}$ retirés. Recharge la page.")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")

        # Recent ledger
        st.markdown("### Journal récent")
        try:
            history = api_get("/bankroll/history", limit=50)
            if history:
                hdf = pd.DataFrame(history)
                hdf["ts"] = pd.to_datetime(hdf["ts"]).dt.strftime("%Y-%m-%d %H:%M")
                show = ["ts", "kind", "amount", "balance_after", "note"]
                show = [c for c in show if c in hdf.columns]
                st.dataframe(hdf[show], width='stretch', hide_index=True)
        except Exception:
            st.caption("(Pas encore d'entrées dans le journal.)")


# ---------------------------------------------------------------------------
# Tab 5 — AI agent (only enabled when key is set)
# ---------------------------------------------------------------------------

with tab_agent:
    st.subheader("Agent IA — Claude Sonnet 4.6")
    st.caption(
        "L'agent appelle les MÊMES outils que le scan manuel + en plus : recherche "
        "de news live (Tavily), blessures (API-Football), météo (Open-Meteo). "
        "Il raisonne en plusieurs étapes et justifie chaque pick."
    )

    if not agent_enabled:
        empty_state(
            "🤖",
            "Agent IA désactivé",
            "Configure-le en 3 étapes ci-dessous, puis recharge la page.",
        )
        st.markdown(
            """
            **Étapes pour activer Claude :**

            1. Crée une clé sur [console.anthropic.com](https://console.anthropic.com) (5$ de crédit gratuits)
            2. Ajoute `ANTHROPIC_API_KEY=sk-ant-...` dans le fichier `.env` à la racine du projet
            3. Recrée le container API pour charger la nouvelle clé :
               ```bash
               docker compose up -d --force-recreate api
               ```
               *Note : `docker compose restart api` ne suffit pas — il ne recharge pas `.env`.*

            En attendant, utilise l'onglet **🎯 Scan manuel** ou **🧠 Agent local** — ils calculent les
            mêmes probabilités, sans coût et sans raisonnement narratif.
            """
        )
    else:
        extra = st.text_area(
            "Instructions additionnelles",
            placeholder="ex : éviter les nuls, prioriser les favoris à domicile, "
                        "ne recommander que des combinés à 2 jambes max…",
        )
        if st.button("🚀 Demander une recommandation à l'agent", type="primary", width='stretch'):
            payload = {
                "sport_key": None if sport == "Toutes" else sport,
                "today_only": today_only,
                "min_edge": round(min_edge_pct / 100, 4),
                "min_prob": min_prob,
                "min_odds": min_odds,
                "n_legs": n_legs,
                "n_combos": n_combos,
                "extra_instructions": extra or None,
            }
            with st.spinner("L'agent raisonne… (~30-60 s)"):
                try:
                    res = api_post("/agent/recommend", json=payload)
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    res = None

            if res:
                if res.get("error"):
                    st.error(f"Agent en échec : {res['error']}")
                m = st.columns(4)
                m[0].metric("Tool calls", res.get("n_tool_calls", 0))
                m[1].metric("Durée", f"{res.get('duration_ms', 0)/1000:.1f} s")
                m[2].metric("Coût", f"${res.get('cost_usd') or 0:.4f}")
                m[3].metric("Modèle", res.get("model", "?").replace("claude-", ""))
                st.markdown("### Raisonnement")
                st.info(res.get("rationale") or "(vide)")
                if res.get("picks"):
                    st.markdown("### Paris individuels")
                    render_picks_table(res["picks"])
                if res.get("parlays"):
                    st.markdown("### Combinés")
                    render_parlays(res["parlays"])


# ---------------------------------------------------------------------------
# Tab — Agent runs history
# ---------------------------------------------------------------------------

with tab_history:
    st.subheader("📜 Historique des invocations de l'agent IA")
    st.caption(
        "Chaque appel à l'agent (Claude ou local) crée une entrée auditée : "
        "filtres utilisés, raisonnement complet, picks, durée, coût."
    )
    col_a, col_b = st.columns([1, 3])
    with col_a:
        trigger_filter = st.selectbox(
            "Source",
            options=[None, "api", "scheduled", "dashboard"],
            format_func=lambda x: "Toutes" if x is None else x,
        )
        history_limit = st.slider("Limite", 10, 200, 50)

    try:
        params = {"limit": history_limit}
        if trigger_filter:
            params["trigger"] = trigger_filter
        runs = api_get("/agent/runs", **params)
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        runs = []

    if not runs:
        empty_state(
            "📜",
            "Aucune exécution d'agent enregistrée",
            "Chaque appel à l'agent local OU Claude (via les onglets dédiés) "
            "crée une entrée auditée ici.",
        )
    else:
        df = pd.DataFrame(runs)

        # KPI summary header
        n_total = len(df)
        n_ok = int((df["status"] == "ok").sum()) if "status" in df.columns else n_total
        pct_ok = (n_ok / n_total * 100) if n_total else 0
        total_cost = float(df["cost_usd"].fillna(0).sum()) if "cost_usd" in df.columns else 0.0
        avg_duration_s = (
            float(df["duration_ms"].fillna(0).mean()) / 1000
            if "duration_ms" in df.columns else 0.0
        )
        kpi = st.columns(4)
        kpi[0].metric("Exécutions", n_total)
        kpi[1].metric("Succès", f"{n_ok}", delta=f"{pct_ok:.0f}%",
                      delta_color="normal" if pct_ok >= 95 else "inverse")
        kpi[2].metric("Coût cumulé", f"${total_cost:.4f}")
        kpi[3].metric("Durée moy.", f"{avg_duration_s:.1f}s")

        st.markdown("")
        st.markdown("**Exécutions récentes**")

        if "created_at" in df.columns:
            df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M")
        if "duration_ms" in df.columns:
            df["duration_s"] = (df["duration_ms"].fillna(0) / 1000).round(1)
        compact_cols = [c for c in [
            "id", "created_at", "trigger", "model", "status",
            "n_tool_calls", "duration_s", "cost_usd",
        ] if c in df.columns]
        rename_map = {
            "id": "ID", "created_at": "Date", "trigger": "Source", "model": "Modèle",
            "status": "Statut", "n_tool_calls": "Tool calls",
            "duration_s": "Durée (s)", "cost_usd": "Coût",
        }
        disp = df[compact_cols].rename(columns=rename_map)
        cfg = {}
        if "Coût" in disp.columns:
            cfg["Coût"] = st.column_config.NumberColumn(format="$%.4f")
        if "Durée (s)" in disp.columns:
            cfg["Durée (s)"] = st.column_config.NumberColumn(format="%.1fs")
        if "Statut" in disp.columns:
            cfg["Statut"] = st.column_config.TextColumn(help="ok = succès · error = échec")
        st.dataframe(disp, width='stretch', hide_index=True, column_config=cfg)

        # Drill-down
        st.markdown("### Détail d'une exécution")
        if "id" in df.columns:
            run_id = st.selectbox("Sélectionne un ID",
                                  options=df["id"].tolist())
            if st.button("📖 Charger le raisonnement complet", width='stretch'):
                try:
                    detail = api_get(f"/agent/runs/{run_id}")
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    detail = None
                if detail:
                    cols = st.columns(4)
                    cols[0].metric("Tool calls", detail.get("n_tool_calls", 0))
                    cols[1].metric("Durée", f"{(detail.get('duration_ms') or 0)/1000:.1f}s")
                    cols[2].metric("Coût USD", f"${detail.get('cost_usd') or 0:.4f}")
                    cols[3].metric("Statut", detail.get("status", "?"))
                    st.markdown("**Filtres utilisés :**")
                    st.json(detail.get("filters") or {})
                    st.markdown("**Raisonnement :**")
                    reasoning = detail.get("reasoning") or "(vide)"
                    st.text_area("trace", value=reasoning, height=300, disabled=True,
                                 label_visibility="collapsed")
                    if detail.get("picks"):
                        st.markdown("**Picks recommandés :**")
                        st.dataframe(pd.DataFrame(detail["picks"]),
                                     width='stretch', hide_index=True)
                    if detail.get("error"):
                        st.error(detail["error"])


# ---------------------------------------------------------------------------
# Tab — Data sources health
# ---------------------------------------------------------------------------

with tab_sources:
    st.subheader("🔌 État des sources externes")
    st.caption(
        "Probe en temps réel. Les sources sont groupées par criticité : "
        "**critical** = sans elles le système ne peut pas fonctionner, "
        "**important** = dégrade la qualité du modèle, "
        "**optional** = ne touche qu'une fonctionnalité (agent IA, news, etc.)."
    )

    if "sources_health" not in st.session_state:
        st.session_state.sources_health = None

    btn_label = "🔄 Re-tester" if st.session_state.sources_health else "🩺 Tester maintenant"
    if st.button(btn_label, type="primary"):
        try:
            st.session_state.sources_health = api_get("/health/sources")
        except Exception as exc:
            st.error(f"Erreur : {exc}")
            st.session_state.sources_health = None

    health_data = st.session_state.sources_health
    if not health_data:
        empty_state(
            "🩺",
            "Aucune vérification effectuée",
            "Clique sur « Tester maintenant » pour sonder chaque source en direct.",
        )
    else:
        # Summary KPIs at top
        sources = health_data.get("sources", [])
        n_ok = sum(1 for s in sources if s["status"] == "ok")
        n_ko = sum(1 for s in sources if s["status"] == "ko")
        n_unconf = sum(1 for s in sources if s["status"] == "not_configured")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Sources OK", f"{n_ok}/{len(sources)}")
        c2.metric("En panne", n_ko, delta="critique" if n_ko else None,
                  delta_color="inverse" if n_ko else "off")
        c3.metric("Non configurées", n_unconf)
        checked = health_data.get("checked_at", "")[:19].replace("T", " ")
        c4.metric("Dernière vérif.", checked or "—")

        st.markdown("")

        # Group by criticality
        groups = {"critical": [], "important": [], "optional": []}
        for s in sources:
            groups.setdefault(s.get("criticality", "optional"), []).append(s)

        labels = {
            "critical":  ("🚨 Critique", "Indispensable au fonctionnement du système."),
            "important": ("⚠️ Important", "Dégrade la qualité du modèle si indisponible."),
            "optional":  ("ℹ️ Optionnel", "N'affecte qu'une fonctionnalité spécifique."),
        }
        status_icons = {"ok": "🟢", "ko": "🔴", "not_configured": "⚪"}
        status_text = {"ok": "OK", "ko": "DOWN", "not_configured": "non configuré"}

        for crit_key in ("critical", "important", "optional"):
            items = groups.get(crit_key, [])
            if not items:
                continue
            title, hint = labels[crit_key]
            st.markdown(f"#### {title}")
            st.caption(hint)
            for s in items:
                cols = st.columns([0.4, 2.0, 1.0, 3.0])
                cols[0].markdown(status_icons.get(s["status"], "⚪"))
                cols[1].markdown(f"**{s['name']}**")
                if s["status"] == "ok":
                    cols[2].caption(f"{s.get('latency_ms', 0)} ms")
                else:
                    cols[2].caption(f":orange[{status_text[s['status']]}]" if s["status"] == "not_configured"
                                    else f":red[{status_text[s['status']]}]")
                reason = s.get("reason") or ""
                cols[3].caption(reason[:140])


# ---------------------------------------------------------------------------
# Tab — ML Calibrator
# ---------------------------------------------------------------------------

with tab_calibrator:
    st.subheader("🎚️ Calibrateur de probabilités")
    st.caption(
        "Apprend automatiquement la correction entre les **probabilités du modèle** "
        "et les **vrais taux de victoire** observés sur tes paris résolus, "
        "via une **régression isotonique** (Niculescu-Mizil & Caruana, 2005). "
        "Le retraînage est planifié chaque dimanche 03h30 UTC."
    )

    try:
        cal_status = api_get("/ml/calibrator/status")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        cal_status = None

    if cal_status:
        kpi = st.columns(4)
        if cal_status.get("available"):
            kpi[0].metric("Statut", "✓ actif", delta="opérationnel",
                          delta_color="normal")
            trained_at = cal_status.get("trained_at", "?")[:19].replace("T", " ")
            kpi[1].metric("Entraîné le", trained_at or "—")
        else:
            kpi[0].metric("Statut", "✗ inactif", delta="pas encore entraîné",
                          delta_color="off")
            kpi[1].metric("Entraîné le", "—")
        n_resolved = cal_status.get("n_resolved_bets", 0)
        min_samples = cal_status.get("min_samples_to_trust", 50)
        kpi[2].metric(
            "Paris résolus disponibles",
            f"{n_resolved}",
            delta=f"min {min_samples}" if n_resolved < min_samples else "OK",
            delta_color="off" if n_resolved < min_samples else "normal",
        )
        kpi[3].metric(
            "Prêt à entraîner",
            "Oui" if cal_status.get("ready_to_train") else "Non",
        )

        st.markdown("")

        # Manual retrain button
        if cal_status.get("ready_to_train"):
            st.success(
                f"Tu as {n_resolved} paris résolus — c'est suffisant pour calibrer.",
                icon="🎯",
            )
            if st.button("🔁 Re-entraîner maintenant", type="primary"):
                with st.spinner("Entraînement en cours…"):
                    try:
                        result = api_post("/ml/calibrator/train")
                    except Exception as exc:
                        st.error(f"Erreur : {exc}")
                        result = None
                if result:
                    if result.get("trained"):
                        st.success(
                            f"✅ Calibrateur ré-entraîné sur {result['n_samples']} paris. "
                            f"Brier score : **{result['brier_before']} → {result['brier_after']}** "
                            f"(plus bas = mieux calibré)."
                        )
                    else:
                        st.warning(f"Entraînement annulé : {result.get('reason')}")
        else:
            empty_state(
                "📊",
                f"Pas assez de paris résolus ({n_resolved}/{min_samples})",
                "Le calibrateur a besoin d'au moins {} paris résolus pour fitter "
                "une isotonic regression robuste. Tes scans génèrent des paris ; "
                "le worker les résout automatiquement à 04h chaque jour. "
                "En attendant, le système utilise les probabilités brutes du modèle.".format(min_samples),
            )


# ---------------------------------------------------------------------------
# Tab — Tennis ELO model
# ---------------------------------------------------------------------------

with tab_tennis:
    st.subheader("🎾 Modèle tennis — ELO surface-aware")
    st.caption(
        "Ratings ELO ATP construits depuis l'historique [Sackmann](https://github.com/JeffSackmann/tennis_atp). "
        "**ELO global** + **3 ratings par surface** (Hard / Clay / Grass), pondéré 50/50. "
        "Refresh hebdomadaire automatique (lundi 05h00 UTC)."
    )

    try:
        tn_status = api_get("/tennis/status")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        tn_status = None

    if tn_status:
        if tn_status.get("available"):
            kpi = st.columns(3)
            kpi[0].metric("Statut", "✓ actif")
            kpi[1].metric("Joueurs notés", tn_status.get("n_players", 0))
            most_recent = tn_status.get("most_recent_match", "?")
            kpi[2].metric("Dernier match analysé", most_recent or "—")

            st.markdown("")
            st.markdown("**Top 5 ATP (ELO global)**")
            top5 = tn_status.get("top5", [])
            if top5:
                df_top = pd.DataFrame(top5).rename(columns={"name": "Joueur", "overall": "ELO"})
                st.dataframe(df_top, width='stretch', hide_index=True,
                             column_config={"ELO": st.column_config.NumberColumn(format="%.1f")})

            st.markdown("")
            st.markdown("### 🔮 Prévisualiser un duel")
            c1, c2, c3 = st.columns([2, 2, 1])
            home_in = c1.text_input("Joueur 1", value="Carlos Alcaraz")
            away_in = c2.text_input("Joueur 2", value="Jannik Sinner")
            surf_in = c3.selectbox("Surface", ["Hard", "Clay", "Grass"], index=0)
            if st.button("🎾 Prédire"):
                try:
                    pred = api_get("/tennis/predict", home=home_in, away=away_in, surface=surf_in)
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    pred = None
                if pred and not pred.get("error"):
                    pcol = st.columns(2)
                    pcol[0].metric(
                        f"{pred['matched_home']}",
                        f"{pred['home_win']*100:.1f}%",
                        delta=f"ELO {pred['rating_home']:.0f}",
                        delta_color="off",
                    )
                    pcol[1].metric(
                        f"{pred['matched_away']}",
                        f"{pred['away_win']*100:.1f}%",
                        delta=f"ELO {pred['rating_away']:.0f}",
                        delta_color="off",
                    )
                elif pred:
                    st.warning(pred.get("error", "Joueur introuvable"))

            st.markdown("")
            st.markdown("### 🔁 Refresh manuel")
            st.caption("Le worker le fait automatiquement chaque lundi 05h00 UTC. "
                       "Force-le ici si tu veux les ratings les plus récents avant un Grand Slam.")
            if st.button("🔁 Re-calculer depuis Sackmann"):
                with st.spinner("Téléchargement + calcul ELO…"):
                    try:
                        result = api_post("/tennis/refresh")
                    except Exception as exc:
                        st.error(f"Erreur : {exc}")
                        result = None
                if result and result.get("trained"):
                    st.success(
                        f"✅ ELO ré-entraîné : {result['n_matches']} matchs, "
                        f"{result['n_players']} joueurs (années {result['years']})."
                    )
                elif result:
                    st.warning(f"Refresh échoué : {result}")
        else:
            empty_state(
                "🎾",
                "Aucun rating ELO calculé",
                "Lance un premier bootstrap : `docker compose exec api python -m betbot.tennis_bootstrap` "
                "ou clique sur le bouton refresh ci-dessous.",
            )
            if st.button("🔁 Calculer maintenant"):
                with st.spinner("Téléchargement Sackmann + calcul ELO…"):
                    try:
                        result = api_post("/tennis/refresh")
                        if result.get("trained"):
                            st.success(f"✅ {result['n_players']} joueurs notés.")
                            st.rerun()
                    except Exception as exc:
                        st.error(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Tab — Basketball pace + offensive/defensive rating
# ---------------------------------------------------------------------------

with tab_basket:
    st.subheader("🏀 Modèle basket — pace + offensive / defensive rating")
    st.caption(
        "Stats équipes scrapées depuis [basketball-reference.com](https://www.basketball-reference.com). "
        "Modèle Dean Oliver : projection des points via **pace × (ORtg + DRtg adverse) / 200**. "
        "Win prob via CDF normale (σ=11), home advantage NBA = 2.7 pts. "
        "Refresh hebdo automatique chaque mardi 05h00 UTC."
    )

    try:
        bb_status_res = api_get("/basketball/status")
    except Exception as exc:
        st.error(f"Erreur : {exc}")
        bb_status_res = None

    if bb_status_res:
        if bb_status_res.get("available"):
            kpi = st.columns(3)
            kpi[0].metric("Statut", "✓ actif")
            kpi[1].metric("Équipes notées", bb_status_res.get("n_teams", 0))
            by_league = bb_status_res.get("by_league", {})
            kpi[2].metric("Ligues couvertes", ", ".join(by_league.keys()) or "—")

            st.markdown("")
            st.markdown("**Top 5 par Net Rating (ORtg − DRtg)**")
            top5 = bb_status_res.get("top5_net_rating", [])
            if top5:
                df_top = pd.DataFrame(top5).rename(columns={
                    "name": "Équipe", "off": "ORtg", "def": "DRtg", "net": "Net",
                })
                st.dataframe(
                    df_top, width='stretch', hide_index=True,
                    column_config={
                        "ORtg": st.column_config.NumberColumn(format="%.1f"),
                        "DRtg": st.column_config.NumberColumn(format="%.1f"),
                        "Net":  st.column_config.NumberColumn(format="%+.1f"),
                    },
                )

            st.markdown("")
            st.markdown("### 🔮 Prévisualiser un match")
            c1, c2, c3 = st.columns([2, 2, 1])
            home_in = c1.text_input("Domicile", value="Boston Celtics", key="bb_home")
            away_in = c2.text_input("Extérieur", value="Los Angeles Lakers", key="bb_away")
            league_in = c3.selectbox("Ligue", ["nba", "euroleague"], index=0, key="bb_league")
            if st.button("🏀 Prédire", key="bb_predict_btn"):
                try:
                    pred = api_get("/basketball/predict",
                                   home=home_in, away=away_in, league=league_in)
                except Exception as exc:
                    st.error(f"Erreur : {exc}")
                    pred = None
                if pred and not pred.get("error"):
                    pcol = st.columns(2)
                    pcol[0].metric(
                        f"{pred['matched_home']}",
                        f"{pred['home_win']*100:.1f}%",
                        delta=f"{pred['expected_home_points']} pts",
                        delta_color="off",
                    )
                    pcol[1].metric(
                        f"{pred['matched_away']}",
                        f"{pred['away_win']*100:.1f}%",
                        delta=f"{pred['expected_away_points']} pts",
                        delta_color="off",
                    )
                    st.caption(
                        f"Total prévu : **{pred['expected_total']} pts** · "
                        f"Marge : **{pred['expected_margin']:+.1f}** "
                        f"(positive = équipe à domicile favorite)"
                    )
                elif pred:
                    st.warning(pred.get("error", "Équipe introuvable"))

            st.markdown("")
            st.markdown("### 🔁 Refresh manuel")
            st.caption("Le worker scrape automatiquement chaque mardi 05h00 UTC.")
            if st.button("🔁 Re-scraper bb-ref", key="bb_refresh_btn"):
                with st.spinner("Téléchargement basketball-reference + calcul…"):
                    try:
                        result = api_post("/basketball/refresh")
                    except Exception as exc:
                        st.error(f"Erreur : {exc}")
                        result = None
                if result and result.get("trained"):
                    st.success(f"✅ {result['n_teams']} équipes mises à jour.")
                elif result:
                    st.warning(f"Refresh échoué : {result}")
        else:
            empty_state(
                "🏀",
                "Aucune stat équipe calculée",
                "Lance un premier scrape : `docker compose exec api python -m betbot.basketball_bootstrap` "
                "ou clique sur le bouton ci-dessous.",
            )
            if st.button("🔁 Scraper maintenant", key="bb_first_scrape"):
                with st.spinner("Téléchargement basketball-reference…"):
                    try:
                        result = api_post("/basketball/refresh")
                        if result.get("trained"):
                            st.success(f"✅ {result['n_teams']} équipes notées.")
                            st.rerun()
                    except Exception as exc:
                        st.error(f"Erreur : {exc}")
