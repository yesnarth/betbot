"""
Ligue 1 was invisible end to end because of one spelling.

The Odds API emits `soccer_france_ligue_one`; `team_stats`, `LEAGUE_MAP`, the
Dixon-Coles rho table and the api-football / football-data mappings all write
`soccer_france_ligue1`. The two never met, so the 18 Ligue 1 rows in the
database — carrying the best xG coverage in the system — were never found and
every fixture fell through to the consensus model.

Translation happens in exactly one place, the Odds API client: canonical
inbound, provider-spelled outbound.
"""
from __future__ import annotations

import pytest

from betbot.sport_keys import known_aliases, to_api, to_canonical


def test_ligue_one_is_normalised_inbound():
    assert to_canonical("soccer_france_ligue_one") == "soccer_france_ligue1"


def test_ligue_one_is_restored_outbound():
    assert to_api("soccer_france_ligue1") == "soccer_france_ligue_one"


def test_round_trip_is_stable():
    for api_key, canonical in known_aliases().items():
        assert to_api(to_canonical(api_key)) == api_key
        assert to_canonical(to_api(canonical)) == canonical


def test_mapping_is_a_bijection():
    """A typo creating a third spelling must fail here, not in production."""
    aliases = known_aliases()
    assert len(set(aliases.values())) == len(aliases)


@pytest.mark.parametrize("key", [
    "soccer_epl", "soccer_italy_serie_a", "basketball_nba", "tennis_atp_wimbledon",
])
def test_unmapped_keys_pass_through_untouched(key):
    assert to_canonical(key) == key
    assert to_api(key) == key


def test_the_canonical_name_is_the_one_the_database_uses():
    """Guards against 'fixing' this by renaming the internal key instead: 310
    predictions and every team_stats row already carry the internal spelling."""
    from betbot.football_api import LEAGUE_MAP
    from betbot.models import DIXON_COLES_RHO_BY_LEAGUE

    canonical = to_canonical("soccer_france_ligue_one")
    assert canonical in LEAGUE_MAP
    assert canonical in DIXON_COLES_RHO_BY_LEAGUE
