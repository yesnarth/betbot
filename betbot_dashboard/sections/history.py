"""History tab — audited record of agent invocations."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from betbot_dashboard.api_client import api_get
from betbot_dashboard.styles import empty_state


def render_history_tab() -> None:
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
        return

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
