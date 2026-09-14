"""
Combinés du canal aveugle — assemblés sur les COTES JUSTES DU MODÈLE.

Sa demande du 2026-09-14 : « dans le lot, donne toujours trois combinés ×100
avec les matchs et options que tu auras ».

LA TENSION, ET COMMENT ELLE EST RÉSOLUE. Un « ×100 » est défini par des cotes,
et ce canal n'en consulte aucune — c'est sa raison d'être. On empile donc sur
la cote JUSTE du modèle, 1/p : une jambe à 75 % vaut 1,33. Aucun prix de
bookmaker n'intervient, ni pour choisir les jambes, ni pour mesurer le
multiplicateur.

CE QUE ÇA IMPLIQUE, ET QU'IL FAUT DIRE. Si le produit des cotes justes vaut
100, alors le produit des probabilités vaut exactement 1/100. **Un ×100 honnête
gagne une fois sur cent, par construction.** Aucune qualité de modèle ne change
cette arithmétique — c'est la définition du multiplicateur, pas une faiblesse du
bot. Et le multiplicateur réellement payé chez le bookmaker sera INFÉRIEUR au
nôtre : il prend sa marge sur chaque jambe, donc un ×100 juste se paie plutôt
×60-80 sur le ticket.

POURQUOI DES JAMBES À HAUTE PROBABILITÉ PLUTÔT QUE PEU DE JAMBES RISQUÉES. Les
deux donnent le même produit de probabilités — 1 % dans les deux cas, c'est
mathématiquement indifférent. Ce qui les départage est la CONFIANCE qu'on peut
accorder à chaque jambe : mesuré sur ce projet, le modèle est calibré au-dessus
de 0,70 (annonce 73,5 %, réalise 74,7 %) et s'effondre en dessous de 0,60
(annonce 63,7 %, réalise 43,7 %). Un ticket de vingt jambes sûres est donc plus
fiable qu'un ticket de huit jambes douteuses, à multiplicateur égal. D'où le
tri par probabilité décroissante et le plancher par jambe.

UN MATCH N'APPARAÎT QU'UNE FOIS, dans un seul combiné. Deux options du même
match se recouvrent — « victoire domicile » et « 1X » gagnent souvent ensemble —
donc les mettre sur le même ticket concentre le risque sur un seul résultat tout
en payant deux marges. Et réutiliser un match d'un combiné à l'autre ferait
tomber ou gagner les trois tickets ensemble, ce qui annule la diversification
qu'on croyait acheter en en prenant trois.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from betbot.analysis import ValueBet

logger = logging.getLogger("betbot.blind_parlays")

MIN_LEG_PROB = 0.60      # sous ce seuil le modèle s'effondre (43,7 % réalisés)
MAX_LEGS = 20
MIN_LEGS = 2


@dataclass
class BlindParlay:
    legs: list[ValueBet]
    fair_odds: float          # produit des 1/p — aucun prix de bookmaker
    win_prob: float           # produit des p ; vaut 1/fair_odds par construction
    reached_target: bool


def build_blind_parlays(
    picks: list[ValueBet],
    target_odds: float = 100.0,
    n_combos: int = 3,
    max_legs: int = MAX_LEGS,
    min_leg_prob: float = MIN_LEG_PROB,
) -> list[BlindParlay]:
    """Assemble `n_combos` tickets disjoints visant `target_odds` en cote juste.

    Renvoie moins de combinés — ou aucun — quand le vivier ne permet pas d'en
    former d'honnêtes. Il n'est jamais complété en descendant le plancher par
    jambe : c'est exactement ce qu'avait fait l'ancienne échelle de relâchement
    des combinés, et elle a coûté de l'argent réel pendant la trêve de septembre.
    """
    # Une seule option par match, la plus probable : deux options d'un même
    # match se recouvrent et ne sont pas deux paris.
    meilleure: dict[str, ValueBet] = {}
    for b in picks:
        if b.model_prob < min_leg_prob:
            continue
        cur = meilleure.get(b.event_id)
        if cur is None or b.model_prob > cur.model_prob:
            meilleure[b.event_id] = b

    vivier = sorted(meilleure.values(), key=lambda b: b.model_prob, reverse=True)
    combos: list[BlindParlay] = []
    utilises: set[str] = set()

    for _ in range(max(n_combos, 0)):
        legs: list[ValueBet] = []
        cote = 1.0
        for b in vivier:
            if b.event_id in utilises or b.model_prob <= 0:
                continue
            legs.append(b)
            cote *= 1.0 / b.model_prob
            if cote >= target_odds or len(legs) >= max_legs:
                break
        if len(legs) < MIN_LEGS:
            break
        utilises.update(b.event_id for b in legs)
        prob = 1.0
        for b in legs:
            prob *= b.model_prob
        combos.append(BlindParlay(
            legs=legs,
            fair_odds=round(cote, 2),
            win_prob=round(prob, 6),
            reached_target=cote >= target_odds,
        ))

    logger.info(
        "Combinés aveugles : %d ticket(s) sur un vivier de %d match(s) "
        "(cible ×%.0f, plancher par jambe %.0f%%)",
        len(combos), len(vivier), target_odds, min_leg_prob * 100,
    )
    return combos
