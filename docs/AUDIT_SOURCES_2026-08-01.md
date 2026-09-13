# Sources de données du bot — audit sans complaisance

*4 analystes, 28 constats, chacun contre-vérifié par un pair. Verdicts : 17 confirmés, 10 nuancés, 4 réfutés, 1 non vérifié.*

---

## 1. Réponse en 5 lignes

Non, les sources actuelles ne suffisent pas — mais pas au sens où on l'espère : **cinq d'entre elles sont mortes ou débranchées** (xG Understat, FBref, Club Elo écrasé, blessures, fatigue), et les autres n'apportent rien parce que le marché les lit aussi.
Oui, il existe mieux, et **c'est déjà payé** : le plan api-football Pro donne accès à Pinnacle (overround médian 5,20 % contre 8,56 % chez Bet365) pour 33 requêtes par jour sur un quota de 7 500, pendant que le bot s'étrangle avec 500 requêtes/mois chez The Odds API.
Mais Pinnacle n'apporte **aucune information nouvelle** : c'est le marché exprimé avec 3,4 points de bruit en moins. Le gain chiffré est de passer de −17,9 % à environ 0 %, pas de gagner.
Le seul test honnête réalisé (prix pris en avance, jugés contre la clôture) donne un **CLV de −0,15 %** : aucun edge extractible.
Priorité absolue et non négociable : **le CLV n'est pas mesurable aujourd'hui** (colonne `commence_time` absente en base, 0 pick sur 297 avec une cote de clôture). Tant que ce n'est pas réparé, ajouter une source revient à changer de carburant sans tableau de bord.

---

## 2. Tableau de l'existant

| Source | État | Couverture réelle | Le marché l'a-t-il déjà ? | Verdict |
|---|---|---|---|---|
| **api-football `/odds` (Pinnacle, id=4)** | Jamais appelée — `grep odds` sur `api_football.py` = 0 occurrence | Pinnacle sur **53/60** fixtures (88,3 %) ; 33 pages/jour couvrent ~330 matchs mondiaux | C'EST le marché — mais sa version la plus propre | **À BRANCHER. Seule source du dossier qui change quelque chose** |
| **api-football (scores, stats été)** | Vivante, plan Pro, expire **2026-08-19**, 637/7 500 req. consommées | 33 ligues mappées, résolution de scores + `stats_inseason` | — | Sous-exploitée à 99 % |
| **The Odds API (plan gratuit)** | Vivante mais asphyxiée : 172 crédits/scan sur 500/mois = **2,9 scans/mois** | 43 ligues ; `/events` est **gratuit** ; **seule source de Betclic** | Prix affiché | À reléguer au rôle de « dernier mètre Betclic » |
| **football-data.co.uk** | Vivante, gratuite, déjà câblée (`football_data_co_uk.py:49-54`) | 8 384 matchs avec clôtures Pinnacle ET Bet365 ; `/new/*.csv` = 1X2 seul, **ni totals ni double chance** | Cotes passées par définition | **Banc d'essai. Meilleur rapport valeur/effort du lot** |
| **Understat (xG)** | **MORTE** — HTTP 200 mais 17 480 octets, `teamsData` absent, cassé depuis déc. 2025 | 0 | Oui, Opta/FBref, Pinnacle l'intègre depuis 10 ans | Débrancher |
| **FBref (xG)** | **MORTE** — HTTP 403 ; Stats Perform a résilié Opta, retrait annoncé le 20/01/2026 | 0 | Oui | Ne pas y toucher |
| **Repli xG api-football** | **MORT ce matin** — `_current_season_year()` renvoie 2026 depuis le 1er août ; `/fixtures?league=39&season=2026&status=FT` → **0 résultat** (380 en 2025) | 119 équipes sur 426 (27,9 %), gelées au 2026-07-19 | Oui | Débrancher, ou corriger le repli saison N−1 |
| **Club Elo** | Vivante à la source (591 clubs) mais **écrasée à chaque boot** : moyenne `elo_rating` = exactement 1500,0 dans les **22 ligues** de la base | 100 % européen ; 0 occurrence de Flamengo / Palmeiras / River / Boca / Galaxy | Oui, l'indicateur de force le plus public qui soit | Ne rien faire |
| **Elo local** | Vivant, écrase Club Elo (`main.py:445` au boot) | 426/426 équipes | — | Structurellement inutilisable en compétition inter-ligues (Real Madrid en C1 = 1548 < Bournemouth = 1553) |
| **`injuries.py` / `fatigue.py`** | **CODE MORT** — `FETCH_INJURIES` et `FETCH_FATIGUE` absents du `.env`, défaut `"0"` → facteur 1.0 permanent | Carte figée à 9 ligues européennes, toutes hors saison | Oui, annonces de club, flux dédiés chez les traders | **Ne pas rallumer** (bug dormant : facteur constant 0,825) |
| **`/fixtures/lineups`** | **MORTE en pratique** — `results=0` à 4 h 18 du coup d'envoi | Publiées ~40 min avant, quand Pinnacle a déjà bougé | Oui, avant tout le monde | Abandonner |
| **Open-Meteo (météo)** | Active par défaut (`weather.py:175`, défaut `"1"`) | `_STADIUM_COORDS` = ~33 clubs → **no-op dans 92 % des cas** | Oui, lu par tous les books | Sans effet |
| **Tavily (news)** | Clé présente | Aucun usage mesuré dans le flux de pari | Oui | Sans intérêt |
| **`sources_updated_at`** | Écrit, **jamais lu** — aucun lecteur dans tout le dépôt | 192/426 lignes, **toutes horodatées à la même seconde** (2026-07-19T14:53:47) | — | **Garde-fou manquant : les lignes se déclarent fraîches en portant des valeurs de mai** |
| **`tennis_sackmann.py` / `bbref_scraper.py`** | Non audités | 5 picks tennis, 5 picks basket sur 297 | — | Hors périmètre, volume négligeable |
| **`football_api.py` (football-data.org)** | Non audité par les 4 analystes | Inconnue | — | À trancher : doublon probable d'api-football |

---

## 3. Le gisement inexploité : le plan api-football Pro déjà payé

**C'est la section qui compte.** Le propriétaire paie un plan Pro dont le bot n'utilise qu'une fraction, pendant qu'il se rationne sur une API gratuite.

### 3.1 Le rapport de débit : 450×

| | The Odds API (gratuit) | api-football Pro (payé) |
|---|---|---|
| Quota | 500 requêtes / **MOIS** | 7 500 requêtes / **JOUR** |
| Coût d'un balayage complet | 172 crédits (43 ligues × 2 régions × 2 marchés) | 33 requêtes pour ~330 matchs mondiaux |
| Fréquence possible | **1 scan tous les 10 jours** | 227 scans/jour |
| Part du quota | 34 % par scan | **0,44 % par scan** |

Consommation réelle mesurée le 2026-08-01 : 637 requêtes sur 7 500, soit 8,5 %. **La contrainte de quota n'existe pas** — elle est purement auto-infligée.

### 3.2 Ce que Pinnacle débloque exactement

- **Présence** : 53/60 fixtures testées (88,3 %). Ce n'est pas 100 % — il faut prévoir un repli explicite sur ~1 match sur 9.
- **Marges médianes mesurées sur 60 fixtures, marché Match Winner** : Pinnacle **5,20 %** | Betano 8,10 % | Unibet 8,55 % | **Bet365 8,56 %** | Marathonbet 9,46 % | 1xBet 9,96 %. Écart réel Pinnacle/Bet365 : **3,36 points**.
- **Par ligue** : MLS 3,48 % | Norvège 2. Division 7,46 % | Damallsvenskan féminine 8,64 %. La vraie fracture n'est pas « majeur contre mineur » mais **« grandes ligues 3,5 % contre tout le reste 7,5-8,6 % »**.
- **Coût** : 1 requête par fixture, ou 1 requête par page de 10 fixtures. 10 paris/jour = 10 requêtes.

### 3.3 Le vrai Double Chance : correction importante

Le brief supposait que le plan débloque « le vrai Double Chance ». **Il faut nuancer** : sur les 19 marchés servis par Pinnacle (Match Winner, Asian Handicap, Goals O/U, mi-temps, score exact, Total Home/Away, corners, cartons), **ni Double Chance ni Both Teams Score**. Ce sont Bet365 et les autres qui les cotent, à leur marge.

Or le double chance est **le seul marché calibré du système** (0,683 prédit → 0,680 réalisé). Conséquence pratique : la référence Pinnacle sur le DC doit être **dérivée du 1X2 dévigué** (somme de deux issues), ce qui est trivial et probablement plus propre qu'une cote DC brute d'un book à 8,5 % de marge. Et rien dans les CSV football-data.co.uk ne contient de cote DC : ce marché n'est pas directement falsifiable hors production.

### 3.4 L'historique de cotes : fenêtre glissante de 7 à 9 jours

Mesuré par sondage : `/odds?date=2026-07-25` → 50 pages ; `date=2026-07-23` → **0 résultat**. La coupure tombe entre J−7 et J−9.

**Conséquence dure : le CLV rétroactif sur les 297 picks existants est définitivement impossible.** La fenêtre est fermée. Chaque jour sans instantané quotidien est un jour perdu pour toujours.

Fraîcheur mesurée : dernière mise à jour à **T−1,98 h** en médiane avant le coup d'envoi, et la cadence s'accélère à l'approche (relevés à T−0,49 h et T−0,90 h observés). C'est une **quasi-clôture**, pas une clôture — à écrire tel quel dans le code, et à refuser au-delà de 2 h d'écart.

### 3.5 Compositions et blessures : ce que ça ne débloque pas

- **Compositions** : `/fixtures/lineups?fixture=1490369` → `results=0` à 4 h 18 du coup d'envoi. Elles n'existent qu'à l'annonce officielle, ~40 min avant, quand les books ont déjà repositionné. **Zéro valeur.**
- **Blessures** : abondantes là où le bot parie (MLS 3 168 lignes, Brésil Serie A 2 374) et **absentes là où le code regarde** (EPL 0). Mais c'est le signal public le plus surveillé du pari sportif. Et le code est cassé : `api_football.py:103` interroge la saison entière → Man United 2024 = 182 absences → plafonné à 5 → **facteur constant 0,825 des deux côtés**, ce qui ne déplace pas le 1X2 et comprime arbitrairement les totaux vers l'under.

### 3.6 Le point dur : Betclic n'existe pas chez api-football

Les 33 bookmakers d'api-football ont été listés exhaustivement. **Betclic n'y figure pas.** Or `.env:62` porte `BOOKMAKER_WHITELIST=betclic,bet365`.

Ce que ça coûte, mesuré : sur 159 lignes 1X2, en comparant le meilleur prix Bet365+Unibet+Betano au juste Pinnacle, 4,4 % des lignes dépassent +2 % d'edge. **Restreint à Bet365 seul : 1,3 %, et le maximum tombe de +22,5 % à +5,3 %.** Le gisement est divisé par 3,4.

Betclic reste accessible via The Odds API — d'où le plan de la section 5.

---

## 4. Ce qui ne vaut pas la peine — où ne PAS aller

Le critère n'est pas la richesse de la source, c'est **son antériorité sur le prix**. Une source que le marché lit aussi peut au mieux réduire l'erreur du modèle vers zéro, ce qui ramène le modèle au prix, jamais au-dessus.

| Piste | Pourquoi c'est une impasse | Verdict |
|---|---|---|
| **Réparer le xG** | Le xG agrégé sur 6 matchs est publié par Opta/FBref et pricé par Pinnacle depuis dix ans. Pire ici : les valeurs en base sont calculées sur mai 2026 et appliquées à août, après un mercato complet. Et le garde-fou « pas de xG → poids 0 » **existe déjà** (`models.py:844-845`) : il n'y a pas de surconfiance mécanique à corriger de ce côté. | Ne pas réparer. Débrancher |
| **Étendre le xG aux ligues d'été** | ~140 appels par ligue → ~2 100/jour pour 15 ligues (28 % du quota) pour un signal que le marché a déjà | Non |
| **Activer `FETCH_INJURIES=1`** | Ajouterait du déjà-coté à un modèle qu'il faut retirer. Et en l'état, applique un facteur constant 0,825 | Laisser à 0 |
| **Compositions probables** | Publiées après le mouvement de cote | Abandonner |
| **Transfermarkt / PhysioRoom** | Republient les mêmes annonces de club | Non |
| **Sofascore / FBref / StatsBomb / arbitres** | Intrants des modèles des bookmakers, publiés avant l'ouverture des cotes | Aucun abonnement |
| **Météo** | Déjà active, no-op sur 92 % des équipes, et Open-Meteo est lu par tous | Sans effet |
| **Club Elo** | 100 % européen, aveugle sur la quasi-totalité du portefeuille d'été ; et là où il voit, c'est dans la cote d'ouverture | Ne rien faire |
| **« Ligues mineures = inefficience »** | **Faux.** Pinnacle cote la Norvège 2. Division, la Chine League Two, la Hongrie NB II — 23 pages de fixtures. Il y facture simplement **deux fois plus de vig** (7,46 % contre 3,48 % en MLS). Cette marge est payée par le parieur, pas par le book | Si on y va, exiger un seuil d'edge **plus élevé**, pas plus bas |
| **Détecteur de valeur inter-bookmakers** | Testé sur 21 852 issues : EV>0 donne ROI +11,45 % mais **CLV −0,15 %** et t=1,52. Pire, la courbe s'inverse quand on concentre : EV>2 % → +1,41 %, EV>5 % → **−17,82 %**. Un signal réel se renforce, il ne s'inverse pas. C'est du bruit | **Ne pas construire** |
| **Betfair Exchange API** | 299-499 GBP d'activation | Hors échelle |
| **Historiques commerciaux (OpticOdds, SportsDataIO, The Odds API payant)** | Tarifs professionnels | Hors échelle |

**Piège à éviter dans le plan** : « couper le chemin consensus » (229 picks sur 297) semble évident puisqu'il dévigue le prix pour parier contre ce même prix. Mais `analysis.py:1106` en fait **le fallback terminal** quand `team_stats` manque — c'est-à-dire exactement les ligues d'été, les seules jouables en août. Le couper sèchement, c'est arrêter de produire pendant deux mois. Choix légitime, mais à assumer comme tel.

---

## 5. Plan de migration — 6 actions ordonnées

### Action 0 — Trancher le renouvellement (avant le 2026-08-19)

| | |
|---|---|
| Coût | **À payer : ~25-29 EUR/mois** (tarif non vérifié, page de tarifs en 403) |
| Effort | 0 soirée |
| Gain | Conditionne les actions 3, 4 et 5 |
| Critère | Décision écrite avant le 19 août |

**Le calcul brutal** : 29 EUR/mois sur une bankroll de 100 EUR, c'est **29 % de la bankroll consommée chaque mois en frais fixes**. À raison de mises de 2-3 EUR, il faudrait un ROI net de +35 % juste pour payer l'abonnement. Aucun chiffre de cet audit ne rend cela plausible. **Si le plan n'est pas renouvelé, arrêter ici** : les actions 3 et 5 tombent, seules 1, 2 et 6 restent (toutes gratuites).

---

### Action 1 — Rendre le CLV possible (bloquant absolu)

| | |
|---|---|
| Coût | **Gratuit** |
| Effort | **2 à 3 soirées** (pas 1) |
| Gain | Sans ça, rien de ce qui suit n'est mesurable |

Diagnostic vérifié : `alembic_version` = `l6a9c3e5b8d2`, la migration `m7b0d4f6a8c3_add_commence_time` n'est pas appliquée, `\d predictions` ne contient pas `commence_time`, et **0 pick sur 297 a une `closing_odds`**. Zéro CLV depuis le premier jour.

Ce qu'il faut faire, dans cet ordre :
1. `alembic upgrade head` — **avant** de déployer l'image du working tree. Le conteneur actuel tourne un `Prediction` à 24 colonnes ; déployer le code qui en mappe 25 sans migrer fait tomber **tous** les `select(Prediction)` (dashboard, API, resolver), pas seulement la tâche CLV.
2. Ajouter le champ `commence_time` au dataclass `ValueBet` (`analysis.py:47-70`) — la donnée existe dans l'event, elle est utilisée en `analysis.py:1079/1082`, mais **jetée** au moment de construire le pick.
3. Le propager aux 4 appelants de `save_prediction` : `main.py:331-347`, `routers/recommend.py:52`, `routers/predictions.py:81-96`, `mcp/server.py:369`. Aucun ne le fournit aujourd'hui.
4. Réactiver `SCAN_HOURS` — actuellement vide, auto-scan désactivé, **dernier scan le 2026-07-19**.

**Critère observable** : après le prochain scan, `select count(*) from predictions where commence_time is not null` > 0.

---

### Action 2 — Poser le garde-fou de fraîcheur et débrancher le xG

| | |
|---|---|
| Coût | **Gratuit** |
| Effort | **1 soirée** |
| Gain | Le modèle cesse de parier avec conviction sur des valeurs de mai |

Deux corrections en une passe :
- Dans `db.py:228-241`, n'écrire `sources_updated_at` que si **au moins une valeur réelle** a été écrite. Aujourd'hui `enrichment.py:108` le passe toujours, donc une ligne vide se rafraîchit en gardant sa vieille valeur.
- Dans `blended_match_probs`, **ignorer `xg_for` et `elo_rating` si `sources_updated_at` a plus de 10 jours ou est NULL** — le modèle retombe alors sur Dixon-Coles seul, ce qui est honnête.
- Supprimer l'appel `xg.get_league_xg` d'`enrichment.py:46` (économie : ~140 requêtes/semaine qui reviennent vides).
- Corriger au passage `backtest.py:205` et `tuning.py:59` qui appellent `understat.get_league_xg` **en direct**, court-circuitant la façade : le calibrateur est entraîné sur un modèle sans xG alors que la production en a. Incohérence en deux lignes.

**Critère observable** : un compteur « signaux gelés » dans le rapport hebdo, à 0.

---

### Action 3 — Client `af_odds.py` : Pinnacle dévigué comme référence

| | |
|---|---|
| Coût | **Déjà payé** (sous réserve de l'action 0) |
| Effort | **2 soirées** |
| Gain | La probabilité de référence passe de « consensus pondéré » à « Pinnacle dévigué » |

Écrire `betbot/data_sources/af_odds.py` : `get_odds_for_date(date)` et `get_odds_for_fixture(id)`, **normalisant vers la forme `bookmakers/markets/outcomes` de `betbot/api.py`**. Les 23 sites d'appel passent tous par les deux mêmes méthodes de `OddsAPIClient` : un adaptateur derrière l'interface existante ne demande **aucune modification en aval**.

Deux exigences techniques :
- **Devig puissance ou Shin, pas multiplicatif.** `models.py:384 _remove_margin` est multiplicatif et surestime systématiquement les outsiders : les 7 lignes mesurées à +2 % d'edge étaient **toutes** sur Away (5) ou Draw (2), jamais sur le favori. Une part indéterminée du « gisement » est un artefact de méthode.
- **Prévoir le repli** : Pinnacle absent sur 11,7 % des matchs.

**Critère observable** : `best_book` devient Pinnacle ou Bet365 au lieu de Matchbook. Aujourd'hui sur 297 picks : Matchbook 101, 1xBet 65, Betfair 31, Unibet (SE) 16, **Betclic (FR) 1, Bet365 0**. L'edge de 296 picks sur 297 est calculé sur un prix que le propriétaire ne peut pas obtenir.

---

### Action 4 — The Odds API devient le « dernier mètre Betclic »

| | |
|---|---|
| Coût | **Gratuit** (plan free conservé) |
| Effort | **1 soirée** |
| Gain | L'edge est enfin calculé sur un prix réellement obtenable |

Inverser le flux :
1. api-football sert **tout le catalogue** et la référence Pinnacle (33 requêtes/jour).
2. `/v4/sports/{sport}/events` est **gratuit** (`x-requests-last: 0`, vérifié) et fournit `id`, `commence_time`, `home_team`, `away_team` — il donne aussi gratuitement le `commence_time` dont l'action 1 a besoin. **À faire en même temps.**
3. On ne dépense **1 à 2 crédits que sur les 5-10 candidats retenus** pour lire la vraie cote Betclic (`Betclic (FR)` mesuré à 1.53/4.15/4.85 sur un match MLS).

Arbitrage à trancher : `regions=eu` = 1 crédit/candidat (~16/jour dans 500/mois) **mais on perd Bet365** ; `regions=eu,uk` = 2 crédits (~8/jour) et on garde les deux. Attention, `markets` n'est **pas** une variable d'environnement : c'est un défaut Python en `api.py:216` et un littéral en `api.py:315`. Le passer à `h2h` est une modification de code, pas d'un `.env`.

**Critère observable** : `x-requests-used` augmente de moins de 20 par jour de pari, au lieu de 172 par scan.

---

### Action 5 — Mesurer le CLV contre Pinnacle, pas contre Betclic

| | |
|---|---|
| Coût | **Déjà payé** |
| Effort | **1 soirée** (après l'action 1) |
| Gain | Le seul instrument qui tranche en ~150 paris ce que le ROI met ~7 700 paris à trancher |

- Réécrire `snapshot_closing_odds` pour taper api-football (`/odds?fixture=<id>&bookmaker=4`, 1 requête).
- Stocker `closing_odds` **et** `closing_odds_at`, et **refuser le calcul si l'écart au coup d'envoi dépasse 2 h**. Ce n'est pas une clôture, c'est une ligne T−15 min à T−3 h.
- Utiliser `compute_clv_pct_no_vig` (`clv.py:356`) — qui existe mais **n'est appelé nulle part**, et qui est **défectueuse** : elle retrouve l'issue pariée par heuristique (`min(closing_picks, key=lambda p: abs(p - entry_fair_prob))`, lignes 394-396). Sur un 1X2 où nul et extérieur sont proches (4.33 vs 5.50), elle peut apparier la mauvaise issue et renvoyer un CLV inversé. **Passer l'indice explicitement avant de s'en servir.**
- `/odds/live` existe mais ne porte **aucune attribution de bookmaker** : inutilisable comme référence.

**Critère observable** : couverture CLV > 80 % des paris confirmés sous 30 jours.

---

### Action 6 — Le critère d'arrêt (à écrire maintenant, gratuit)

| | |
|---|---|
| Coût | **Gratuit** |
| Effort | 0 soirée |
| Gain | C'est la seule mesure qui protège réellement les 100 EUR |

**Si le CLV moyen reste nul ou négatif après 200 paris supplémentaires, arrêter de parier.**

Le seuil se pose sur le **CLV moyen avec son t de Student, jamais sur le ROI** — sans quoi une série chanceuse comme le +11,45 % (t=1,52, CLV −0,15 %) relancerait indéfiniment la machine.

Et le critère d'admission de toute source future, unique et falsifiable : **le signal prédit-il le MOUVEMENT de la ligne Pinnacle entre le scan et le coup d'envoi ?** Un signal qui ne prédit pas le mouvement de Pinnacle n'apporte rien, même s'il prédit bien le résultat.

---

### Banc d'essai transversal (gratuit, 1 soirée, à faire en parallèle)

Charger `football-data.co.uk` en base : `/new/*.csv` (NOR, SWE, BRA, USA, MEX, JPN, ARG, FIN, DNK, IRL, AUT, POL, ROU, SWZ — 3 496 lignes pour la seule Norvège) et `/mmz4281/*/*.csv`. **8 384 matchs avec clôtures Pinnacle et Bet365, 0 EUR, 0 requête de quota.**

Toute nouvelle règle passe par là **avant** d'être codée dans le scan. Aujourd'hui, `MAX_BOOK_ODDS=2.22`, `ALLOW_TOTALS_OVER=0` et `OVER_SAMPLING_RATE=0.25` sont calibrés sur 170-227 observations de production ; ils pourraient l'être sur 8 000.

Trois limites à connaître : les fichiers `/new/*.csv` ne contiennent **que le 1X2** (donc les constantes sur les totals ne sont calibrables que sur les grands championnats), **aucun fichier ne contient de cote Double Chance ni BTTS**, et **Betclic est absent** de football-data.co.uk comme des 33 bookmakers d'api-football.

---

## 6. La réponse honnête à la question de fond

**Non. Aucune source, si bonne soit-elle, ne peut rendre ce bot rentable.** Et ce n'est pas une opinion, c'est mesuré à trois endroits distincts.

**Premièrement, le plafond est connu et il est à zéro.** Sur 8 384 matchs avec les vraies clôtures, parier toutes les issues Bet365 donne **−8,32 %** (c'est la vig). Filtrer sur « cote Bet365 > juste Pinnacle dévigué » ramène à **−0,13 % sur 1 003 paris (t = −0,03)** : on annule la vig, on ne gagne rien. Et même ce chiffre est optimiste, parce qu'il compare deux clôtures — or on ne connaît la clôture Pinnacle qu'après le coup d'envoi. C'est un plafond de *look-ahead*, un pari qu'on ne peut pas placer.

**Deuxièmement, la version implémentable donne un CLV nul.** Le seul test qui utilise deux horodatages réels (prix pris en avance, jugés contre la clôture) : 510 paris à EV>0, ROI apparent +11,45 %, **CLV moyen −0,15 %**, bat la clôture 54,9 % du temps, t = 1,52. Et la courbe s'inverse quand on concentre le signal : EV>2 % → +1,41 %, EV>5 % → **−17,82 %**. Un signal réel se renforce quand on le concentre. Celui-ci s'effondre. C'est du bruit de cotation et de devig, pas de l'information.

**Troisièmement, le problème n'est pas la qualité de l'entrée, c'est la direction du modèle.** `w* = −0,20` signifie que le modèle doit être **retiré** du prix, pas mélangé avec lui. Améliorer ses entrées le rapproche du prix ; le rapprocher du prix le ramène à zéro edge moins la vig. Le chemin `consensus`, qui produit **229 des 297 picks**, dévigue les cotes puis parie contre ces mêmes cotes : par construction, il ne peut contenir que l'erreur d'approximation de sa propre transformation. On ne peut pas ajouter de données à un miroir.

**Ce qui reste vrai, et c'est le gain réel :** basculer sur Pinnacle dévigué fait passer d'environ **−18 % à environ 0 %**. Sur 100 EUR et 227 paris, cela représente ~18 EUR sauvés. C'est le seul chiffre positif de tout ce dossier, et ce n'est pas un profit, c'est l'arrêt d'une hémorragie.

**Et il faut y opposer trois coûts.** Le renouvellement Pro à ~29 EUR/mois consomme **29 % de la bankroll par mois** en frais fixes. Le gisement exploitable, une fois restreint à Bet365 seul (Betclic n'existe pas chez api-football), tombe à **1,3 % des lignes avec un maximum de +5,3 %** — et une part indéterminée de ce reste est un artefact du devig multiplicatif. Enfin, Bet365 restreint les comptes qui battent régulièrement la clôture, souvent en quelques semaines : même un edge réel serait consommé avant d'être rentabilisé sur 100 EUR.

**Deux réserves sur ce qui pourrait sembler être une sortie de secours.** Le double chance est bien calibré (0,683 prédit → 0,680 réalisé), mais **calibration n'est pas rentabilité** : une probabilité parfaitement calibrée à 0,68 perd de l'argent si le prix offert implique 0,72 — cas courant sur un marché où le book empile deux issues et élargit sa marge. Et `n=50` ne tranche rien : l'intervalle à 95 % autour de 0,68 va d'environ 0,55 à 0,80. Se replier sur le double chance est une **hypothèse à tester par le CLV**, pas un acquis. Quant au seul avantage théoriquement accessible à un particulier — la lenteur relative de Betclic/Bet365 face à Pinnacle sur les petites ligues — il se compte **en heures**, ce qui est incompatible avec un placement manuel le soir.

**L'objectif réaliste n'est donc pas « gagner ». C'est : ne plus perdre 17,9 %, et le savoir.** L'ordre correct est de réparer l'instrument de mesure (actions 1, 2, 6 — toutes gratuites), de brancher Pinnacle si et seulement si le plan est renouvelé (actions 3, 4, 5), puis de laisser le CLV trancher sur 150-200 paris. S'il est négatif, aucune source ne sauvera le système, et la bonne décision sera d'arrêter.