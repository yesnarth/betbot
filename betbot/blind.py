"""
Canal AVEUGLE — pronostics issus des seules données d'abonnement.

Sa décision, 2026-09-13 : « je ne veux pas que tu te fies même aux cotes (car
cela pourrait être une fausse piste ou nous induire en erreur ou nous manipuler
comme le souhaitent les bookmakers) ». Ce canal parie donc sur ce que les
statistiques disent, sans jamais regarder ce que le marché en pense, et propose
l'option quelle qu'elle soit — même si aucun book ne la cote, même si le prix
est ridicule. Le seul critère est la probabilité que l'événement se produise.

TROIS INFILTRATIONS DE COTE, toutes coupées ici — c'est l'essentiel du module :

1. shrink_toward_market() tire la probabilité du modèle VERS le prix du book,
   jusqu'à 70 % de poids au marché quand le désaccord dépasse 30 points. C'est
   exactement le mécanisme qu'il refuse : faire taire le modèle quand il
   contredit le bookmaker. Non appliqué ici.
2. Le modèle consensus EST la cote dévigée de plusieurs books. Un pronostic qui
   en sort ne fait que recopier le marché. Les matchs qui retombent sur lui
   (faute de stats d'équipe) sont écartés, pas repricés.
3. _market_total_lambda() lit le niveau de buts attendu DEPUIS le marché des
   totals. Il n'intervient que dans le chemin consensus, lequel est exclu — mais
   la règle est notée pour que personne ne le rebranche ici par commodité.

ml_calibrate (isotonique) n'est pas appliqué non plus, et c'est délibéré : il a
été entraîné sur des probabilités DÉJÀ rétrécies vers le marché. L'appliquer à
des probabilités brutes, c'est le sortir de son domaine — une correction ajustée
pour une autre distribution. Ce canal part donc en probabilités BRUTES et devra
gagner son propre calibrateur sur son propre historique noté, exactement comme
le canal historique l'a fait.

CONSÉQUENCE À DIRE À VOIX HAUTE : les premières semaines sont une mesure, pas
une promesse. Le 74,7 % de réussite mesuré au-dessus de 0,70 l'a été sur des
picks passés par les portes de marché. Sans le rétrécissement, le modèle brut
est probablement plus sûr de lui qu'il ne devrait — le plancher 0,70 ne vaudra
0,70 qu'une fois ce canal calibré sur ses propres résultats.
"""
from __future__ import annotations

import logging

from betbot.analysis import ValueBet, _compute_probs, _sport_key_to_label
from betbot.models import DEFAULT_HOME_AVG, DEFAULT_AWAY_AVG, MatchProbs

logger = logging.getLogger("betbot.blind")

CHANNEL = "modele"
_H2H = "h2h"

# ORDRE ÉDITORIAL DES FAMILLES — et non l'ordre des probabilités.
#
# Classer par probabilité fait toujours gagner les marchés les plus larges :
# double chance et draw-no-bet dépassent mécaniquement le 1X2 dont ils sont
# tirés, et l'avis sur le vainqueur disparaissait du bulletin. Or ce ne sont
# pas des prédictions indépendantes — ce sont des réécritures de la même
# distribution 1X2, avec un coussin.
#
# Les questions réellement distinctes passent devant : qui gagne, combien de
# buts, les deux marquent-elles. Les coussins ferment la marche.
_FAMILY_ORDER = ("h2h", "totals", "btts", "double_chance", "draw_no_bet")


def _h2h_options(p: MatchProbs, home: str, away: str) -> list[tuple]:
    """(code, libellé, marché, point, probabilité) — 1X2 et dérivés.

    Les doubles chances et le draw-no-bet sont calculés ICI À PARTIR DES
    PROBABILITÉS, jamais à partir des cotes comme le fait _derive_dc_dnb_odds.
    Même arithmétique, mais du bon côté de la frontière : 1X = P(1) + P(X), et
    non 1/(q1+qX) où q sort d'un prix.
    """
    h, d, a = p.home_win, p.draw, p.away_win
    opts = [
        ("1", "Victoire " + home, _H2H, None, h),
        ("X", "Match nul", _H2H, None, d),
        ("2", "Victoire " + away, _H2H, None, a),
        ("1X", home + " ou nul", "double_chance", None, h + d),
        ("X2", "Nul ou " + away, "double_chance", None, d + a),
        ("12", "Pas de nul", "double_chance", None, h + a),
    ]
    # Draw No Bet : le nul rembourse, donc on reconditionne sur les seuls cas où
    # le pari est tranché. Aucun prix n'intervient.
    ha = h + a
    if ha > 0:
        opts += [
            ("DNB1", home + " (nul remboursé)", "draw_no_bet", None, h / ha),
            ("DNB2", away + " (nul remboursé)", "draw_no_bet", None, a / ha),
        ]
    return opts


def _totals_options(p: MatchProbs, include_half_line: bool = False) -> list[tuple]:
    """Over/Under sur les quatre lignes que le modèle calcule.

    Toutes sont proposées, y compris celles qu'AUCUN book ne cote (la ligne 1,5
    est absente du flux The Odds API, et Betclic n'y propose aucun total). C'est
    précisément le point : l'indisponibilité au pari ne dit rien sur la
    probabilité que l'événement se produise.
    """
    out = []
    rows = (
        ("O05", "Plus de 0.5 but", 0.5, "over_05"),
        ("U05", "Moins de 0.5 but", 0.5, "under_05"),
        ("O15", "Plus de 1.5 buts", 1.5, "over_15"),
        ("U15", "Moins de 1.5 buts", 1.5, "under_15"),
        ("O25", "Plus de 2.5 buts", 2.5, "over_25"),
        ("U25", "Moins de 2.5 buts", 2.5, "under_25"),
        ("O35", "Plus de 3.5 buts", 3.5, "over_35"),
        ("U35", "Moins de 3.5 buts", 3.5, "under_35"),
    )
    # LA LIGNE 0,5 EST ÉCARTÉE PAR DÉFAUT — jugement, et il peut le renverser
    # avec BLIND_INCLUDE_HALF_LINE=1.
    #
    # « Plus de 0,5 but » vaut ~0,91 dans PRESQUE TOUS les matchs. Ce n'est pas
    # une prédiction sur ce match-ci, c'est une propriété du football : elle ne
    # distingue rien. Mesuré au premier scan réel, elle remportait la quasi-
    # totalité des pronostics émis et le vainqueur n'apparaissait jamais.
    #
    # C'est bien l'événement le plus probable, donc au sens strict de sa
    # consigne elle a sa place. Mais un bulletin qui répète la même évidence
    # sur chaque match ne lui apprend rien, et il ne peut pas la jouer non plus.
    if not include_half_line:
        rows = tuple(r for r in rows if r[2] != 0.5)
    for code, lab, pt, attr in rows:
        v = getattr(p, attr, 0.0) or 0.0
        if v > 0:
            out.append((code, lab, "totals", pt, v))
    return out


def _btts_options(p: MatchProbs) -> list[tuple]:
    """Les deux équipes marquent. Le modèle le calcule depuis toujours et ses
    cotes n'ont JAMAIS été demandées à l'API — le marché était donc inexploitable
    dans tous les autres canaux. Ici, l'absence de cote n'empêche rien."""
    y, n = p.btts_yes or 0.0, p.btts_no or 0.0
    if y <= 0 and n <= 0:
        return []
    return [("BTTS_O", "Les deux équipes marquent", "btts", None, y),
            ("BTTS_N", "Une équipe au moins ne marque pas", "btts", None, n)]


def detect_blind_picks(
    events_by_sport: dict[str, list[dict]],
    prebuilt_stats_by_sport: dict[str, dict] | None = None,
    min_prob: float = 0.0,
    max_per_match: int = 4,
    include_half_line: bool = False,
    top_matches: int = 0,
) -> list[ValueBet]:
    """
    Pronostics purement statistiques, un par match par défaut.

    UN PRONOSTIC PAR MATCH, TOUJOURS — la couverture prime sur la sélection.

    Sa précision du 2026-09-13 : « s'il y a 100 matchs à jouer, tu devrais
    pouvoir me donner 100 pronostics (avec au moins 3 ou 4 options de paris par
    match). À la rigueur, tu pourrais sélectionner les 100 matchs les plus
    privilégiés. »

    D'où `min_prob=0.0` PAR DÉFAUT : aucun plancher de probabilité ne doit
    pouvoir faire disparaître un match de la liste. Un plancher, même bas,
    supprime des affiches entières au lieu de supprimer des options — et c'est
    exactement ce qui rendait le bulletin inutilisable. Le tri par confiance
    n'ordonne plus que les options À L'INTÉRIEUR d'un match.

    Réduire la liste, quand il le faut, se fait par `top_matches` : on garde les
    N matchs les plus sûrs ENTIERS, avec toutes leurs options. Couper dans le
    nombre de pronostics plutôt que dans le nombre de matchs rendrait des
    affiches à moitié analysées.

    Sa demande du 2026-09-13 : « ce qui m'aurait plus accroché, c'est de te voir
    donner les pronostics pour chacun des matchs de la journée dont tu disposes,
    ainsi pour chaque match tu pourras donner 3 ou 4 options. J'ai pas besoin de
    combinaison, je ferai mes combinaisons moi-même dans la liste. »

    D'où un plancher BAS (0,50 : une option moins probable que son contraire
    n'est pas un pronostic) plutôt qu'un plancher sélectif à 0,70. Le tri par
    confiance ne sert plus à écarter des matchs, seulement à ordonner les
    options À L'INTÉRIEUR d'un match. Un match dont aucune option ne dépassait
    0,70 disparaissait entièrement de la liste — c'est précisément ce qui
    rendait le bulletin inutilisable pour composer soi-même.

    max_per_match limite le nombre d'options retenues sur un même match : sinon
    un seul match trusterait la liste avec ses variantes de totals, toutes
    corrélées entre elles — ce ne sont pas huit pronostics, c'est le même énoncé
    huit fois.
    """
    picks: list[ValueBet] = []
    n_consensus = 0
    n_modelled = 0

    for sport_key, events in (events_by_sport or {}).items():
        entry = (prebuilt_stats_by_sport or {}).get(sport_key) or {}
        teams = entry.get("teams", {})
        home_avg = entry.get("home_avg", DEFAULT_HOME_AVG)
        away_avg = entry.get("away_avg", DEFAULT_AWAY_AVG)
        h2h_lookup = entry.get("h2h", {})
        league_label = _sport_key_to_label(sport_key)

        for event in events:
            home = event.get("home_team", "") or ""
            away = event.get("away_team", "") or ""
            event_id = event.get("id", home + "_" + away)

            probs = _compute_probs(home, away, event, teams, home_avg, away_avg,
                                   sport_key=sport_key, h2h_lookup=h2h_lookup)
            if probs is None:
                continue

            # Infiltration n°2 : un repli consensus recopie la cote. On préfère
            # ne rien dire sur ce match plutôt que de faire passer le prix du
            # bookmaker pour un pronostic statistique.
            if str(probs.model or "").startswith("consensus"):
                n_consensus += 1
                continue
            n_modelled += 1

            options = _h2h_options(probs, home, away)
            if not (sport_key.startswith("tennis_")
                    or sport_key.startswith("basketball_")):
                options += (_totals_options(probs, include_half_line)
                            + _btts_options(probs))

            options = [o for o in options if float(o[4]) >= min_prob]

            # UN PRONOSTIC PAR FAMILLE DE MARCHÉ, pas un par match.
            #
            # Classer toutes les options d'un match par probabilité brute fait
            # gagner « Plus de 0,5 but » partout : il vaut ~0,91 dans TOUS les
            # matchs. Ce n'est pas une prédiction sur ce match-ci, c'est un fait
            # sur le football — et il écrasait 1X2, BTTS et le reste. Mesuré au
            # premier scan réel : la quasi-totalité des picks émis étaient O05.
            #
            # Chaque famille concourt donc contre elle-même. On obtient un avis
            # sur le vainqueur, un sur les buts, un sur le BTTS — un vrai
            # bulletin — au lieu de la même évidence répétée.
            best_per_family: dict[str, tuple] = {}
            for o in options:
                fam = o[2]
                if fam not in best_per_family or float(o[4]) > float(best_per_family[fam][4]):
                    best_per_family[fam] = o
            options = sorted(
                best_per_family.values(),
                key=lambda o: (_FAMILY_ORDER.index(o[2])
                               if o[2] in _FAMILY_ORDER else len(_FAMILY_ORDER),
                               -float(o[4])),
            )

            for code, label, market, point, prob in options[:max_per_match]:
                picks.append(ValueBet(
                    event_id=event_id,
                    sport_key=sport_key,
                    home_team=home,
                    away_team=away,
                    league_label=league_label,
                    market=market if point is None else market + "_" + str(point),
                    selection_code=code,
                    selection_label=label,
                    # float() EXPLICITE, et ce n'est pas de la cosmétique : le
                    # modèle rend des numpy.float64, que psycopg2 ne sait pas
                    # adapter. Il les sérialise en « np.float64(0.91) » dans le
                    # SQL, Postgres y lit un schéma nommé « np » et REFUSE
                    # l'insertion. Mesuré en production le 2026-09-13 : le canal
                    # a tourné et n'a rien enregistré, chaque écriture perdue
                    # dans un log d'erreur. Les autres canaux y échappent parce
                    # que leurs probabilités traversent le calibrateur, qui les
                    # repasse en float Python — celui-ci ne le traverse pas.
                    model_prob=round(min(float(prob), 1.0), 4),
                    # Aucune cote n'est consultée. 0.0 n'est pas un prix
                    # manquant à compléter plus tard : c'est la déclaration que
                    # ce canal n'en dépend pas. Toute statistique de ce canal se
                    # lit en TAUX DE RÉUSSITE, jamais en ROI.
                    best_odds=0.0,
                    best_book="",
                    value_edge=0.0,
                    kelly_stake=0.0,
                    lambda_home=float(probs.lambda_home),
                    lambda_away=float(probs.lambda_away),
                    model_type=probs.model,
                    reliability=1.0,
                    commence_time=event.get("commence_time", "") or "",
                    market_prob=None,
                    channel=CHANNEL,
                ))

    # PLAFOND PAR MATCH, jamais par pronostic. Trancher dans la liste à plat
    # laisserait des affiches avec deux options sur quatre — à moitié
    # analysées, ce qui est pire qu'absentes.
    if top_matches > 0:
        meilleur: dict[str, float] = {}
        for b in picks:
            if b.model_prob > meilleur.get(b.event_id, 0.0):
                meilleur[b.event_id] = b.model_prob
        gardes = {e for e, _ in sorted(meilleur.items(), key=lambda kv: -kv[1])[:top_matches]}
        picks = [b for b in picks if b.event_id in gardes]

    picks.sort(key=lambda b: b.model_prob, reverse=True)
    n_matchs = len({b.event_id for b in picks})
    logger.info(
        "Canal aveugle : %d match(s) modélisé(s), %d écarté(s) (repli consensus) "
        "-> %d match(s) couvert(s), %d option(s) (%.1f par match)",
        n_modelled, n_consensus, n_matchs, len(picks),
        len(picks) / max(n_matchs, 1),
    )
    return picks
