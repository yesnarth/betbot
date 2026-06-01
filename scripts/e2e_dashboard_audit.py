"""
End-to-end audit of the BetBot Streamlit dashboard with Playwright.

For each of the 9 tabs:
  1. Click the tab
  2. Wait for content to render
  3. Capture all console errors / warnings
  4. Capture network failures (4xx, 5xx)
  5. Take a screenshot
  6. Detect missing data / broken widgets

Outputs:
  - audit_report.json    : structured findings per tab
  - screenshots/*.png    : visual proof per tab
  - audit_summary.md     : human-readable summary

Run:
  python scripts/e2e_dashboard_audit.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page

DASHBOARD_URL = "http://localhost:8501"
OUT_DIR = Path("audit_output")
OUT_DIR.mkdir(exist_ok=True)
(OUT_DIR / "screenshots").mkdir(exist_ok=True)

TABS = [
    # Mirrors the CURRENT app.py layout (top-level sections → sub-tabs). Labels
    # are matched as substrings, so dynamic count suffixes like " (3)" are fine.
    # Each entry is (slug, [click_path]) — labels to click in order.
    ("picks",        ["🔔 Mes picks"]),
    ("validate",     ["🔔 Mes picks", "🔔 Picks à valider"]),
    ("pending",      ["🔔 Mes picks", "⏳ Paris en attente"]),
    ("performance",  ["📊 Performance"]),
    ("capital",      ["💰 Capital"]),
    ("model",        ["🔬 Modèle"]),
    ("backtest",     ["🔬 Modèle", "🧪 Backtest"]),
    ("calibrator",   ["🔬 Modèle", "🎚️ Calibrateur ML"]),
    ("tennis",       ["🔬 Modèle", "🎾 Tennis ELO"]),
    ("basketball",   ["🔬 Modèle", "🏀 Basketball"]),
    ("scan",         ["🛠️ Outils", "🎯 Scan manuel"]),
    ("local",        ["🛠️ Outils", "🧠 Agent local"]),
    ("agent",        ["🛠️ Outils", "🤖 Agent IA (Claude)"]),
    ("parlay1000",   ["🛠️ Outils", "🎰 Combiné ×1000"]),
    ("events",       ["🛠️ Outils", "📅 Matchs disponibles"]),
    ("sources",      ["🛠️ Outils", "🔌 Sources"]),
    ("history",      ["🛠️ Outils", "📜 Historique IA"]),
]


def audit() -> dict:
    findings: dict = {"tabs": {}, "global": {"started_at": time.time()}}
    console_messages: list[dict] = []
    network_errors: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        page = ctx.new_page()

        # Vega-Lite emits transient "Infinite extent" warnings on Streamlit
        # rerun edge cases (1-row dataframe between API state changes). They
        # don't affect rendering — filter them from the count.
        IGNORED_WARNING_PATTERNS = (
            "Infinite extent for field",
        )

        def _on_console(msg):
            if msg.type == "error":
                console_messages.append({
                    "type": msg.type, "text": msg.text[:500],
                    "location": str(msg.location.get("url", "")) if msg.location else "",
                })
            elif msg.type == "warning":
                if any(p in msg.text for p in IGNORED_WARNING_PATTERNS):
                    return  # noise we've decided to ignore
                console_messages.append({
                    "type": msg.type, "text": msg.text[:500],
                    "location": str(msg.location.get("url", "")) if msg.location else "",
                })
        page.on("console", _on_console)

        def _on_response(resp):
            if resp.status >= 400:
                network_errors.append({
                    "url": resp.url[:200],
                    "status": resp.status,
                })
        page.on("response", _on_response)

        # Initial load
        print(f"[load] {DASHBOARD_URL}")
        page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=30000)
        time.sleep(2)
        page.screenshot(path=OUT_DIR / "screenshots" / "00_initial.png")

        # Capture sidebar state
        sidebar_text = page.locator('section[data-testid="stSidebar"]').inner_text(timeout=5000)
        findings["global"]["sidebar_excerpt"] = sidebar_text[:1500]

        # Iterate tabs
        for slug, click_path in TABS:
            tab_findings = {"label": " > ".join(click_path), "ok": True, "issues": []}
            print(f"[tab] {slug}")
            try:
                start_console = len(console_messages)
                start_network = len(network_errors)

                # Walk the click path (top-level section first, then sub-tab if any)
                for label in click_path:
                    tab = page.get_by_role("tab", name=label).first
                    tab.click(timeout=10000)
                    time.sleep(1)
                page.wait_for_load_state("networkidle", timeout=15000)
                time.sleep(1)

                # Screenshot
                shot_path = OUT_DIR / "screenshots" / f"{slug}.png"
                page.screenshot(path=shot_path, full_page=True)

                # Capture body text (first 3000 chars)
                body = page.locator('div[data-testid="stAppViewContainer"]').inner_text()
                tab_findings["body_excerpt"] = body[:2000]

                # Diff console / network
                tab_findings["new_console"] = console_messages[start_console:]
                tab_findings["new_network_errors"] = network_errors[start_network:]
                if tab_findings["new_console"]:
                    tab_findings["issues"].append(f"{len(tab_findings['new_console'])} console errors")
                if tab_findings["new_network_errors"]:
                    tab_findings["issues"].append(f"{len(tab_findings['new_network_errors'])} HTTP errors")

                # Heuristics: detect common Streamlit error markers
                if "Traceback" in body or "AttributeError" in body or "Streamlit error" in body:
                    tab_findings["ok"] = False
                    tab_findings["issues"].append("Python traceback visible in UI")
                if "Erreur :" in body and "API injoignable" in body:
                    tab_findings["ok"] = False
                    tab_findings["issues"].append("Backend unreachable")

            except Exception as exc:
                tab_findings["ok"] = False
                tab_findings["issues"].append(f"Playwright error: {exc}")

            findings["tabs"][slug] = tab_findings

        # Sidebar interactions: click "Résoudre les paris terminés" if present
        try:
            resolve_btn = page.get_by_role("button", name="🔄 Résoudre les paris terminés")
            if resolve_btn.is_visible(timeout=2000):
                resolve_btn.click()
                time.sleep(3)
                page.screenshot(path=OUT_DIR / "screenshots" / "after_resolve_click.png")
                findings["global"]["resolve_button_clicked"] = True
        except Exception:
            pass

        ctx.close()
        browser.close()

    findings["global"]["total_console_errors"] = len(console_messages)
    findings["global"]["total_network_errors"] = len(network_errors)
    findings["global"]["console_errors"] = console_messages
    findings["global"]["network_errors"] = network_errors
    findings["global"]["finished_at"] = time.time()
    return findings


def write_summary(findings: dict) -> None:
    out = OUT_DIR / "audit_report.json"
    out.write_text(json.dumps(findings, indent=2, default=str), encoding="utf-8")

    md = ["# BetBot Dashboard Audit\n"]
    md.append(f"- Total console errors: {findings['global']['total_console_errors']}")
    md.append(f"- Total network errors: {findings['global']['total_network_errors']}\n")
    md.append("## Per-tab summary\n")
    md.append("| Tab | Status | Issues |")
    md.append("|---|---|---|")
    for slug, t in findings["tabs"].items():
        status = "OK" if t["ok"] and not t["issues"] else "ISSUES"
        issues = "; ".join(t["issues"]) if t["issues"] else "—"
        md.append(f"| {t['label']} | {status} | {issues} |")
    md.append("")
    md.append("## Network errors (status >= 400)\n")
    if findings['global']['network_errors']:
        for e in findings['global']['network_errors'][:30]:
            md.append(f"- HTTP {e['status']}: {e['url']}")
    else:
        md.append("(none)")
    md.append("")
    md.append("## Console errors\n")
    if findings['global']['console_errors']:
        for c in findings['global']['console_errors'][:30]:
            md.append(f"- [{c['type']}] {c['text'][:200]}")
    else:
        md.append("(none)")
    (OUT_DIR / "audit_summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport: {out}")
    print(f"Summary: {OUT_DIR / 'audit_summary.md'}")
    print(f"Screenshots: {OUT_DIR / 'screenshots'}")


if __name__ == "__main__":
    findings = audit()
    write_summary(findings)
    # Exit code 0 if no critical issues, 1 otherwise
    has_critical = any(not t["ok"] for t in findings["tabs"].values())
    sys.exit(1 if has_critical else 0)
