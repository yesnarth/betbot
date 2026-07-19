"""Match-grouping of individual picks (best per match + correlated alternatives)."""
from betbot_dashboard.components.picks import group_picks_by_match


def _pick(eid, label, edge, prob=0.6, home="A", away="B"):
    return {"event_id": eid, "home_team": home, "away_team": away, "league": "L",
            "selection_label": label, "value_edge": edge, "model_prob": prob}


def test_best_per_match_and_alternatives():
    picks = [
        _pick("e1", "Double chance 1X", 0.06, 0.82),
        _pick("e1", "DNB domicile", 0.33, 0.77),      # best of e1 (highest edge)
        _pick("e1", "Moins de 3.5", 0.10, 0.62),
        _pick("e2", "Moins de 2.5", 0.13, 0.55, home="C", away="D"),
    ]
    primary, alternatives = group_picks_by_match(picks)

    assert len(primary) == 2                                   # one row per match
    e1 = next(p for p in primary if p["event_id"] == "e1")
    assert e1["selection_label"] == "DNB domicile"            # highest-edge market kept
    assert len(alternatives["e1"]) == 2                        # the other 2 markets
    assert "e2" not in alternatives                            # single market → no alt
    assert primary[0]["event_id"] == "e1"                     # sorted best-first (0.33 > 0.13)


def test_falls_back_to_team_key_without_event_id():
    picks = [
        {"home_team": "A", "away_team": "B", "league": "L",
         "selection_label": "1X", "value_edge": 0.05, "model_prob": 0.7},
        {"home_team": "A", "away_team": "B", "league": "L",
         "selection_label": "DNB1", "value_edge": 0.09, "model_prob": 0.7},
    ]
    primary, alternatives = group_picks_by_match(picks)
    assert len(primary) == 1 and primary[0]["selection_label"] == "DNB1"


def test_empty():
    assert group_picks_by_match([]) == ([], {})
