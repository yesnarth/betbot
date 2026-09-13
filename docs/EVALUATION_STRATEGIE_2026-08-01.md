# BetBot — Ce système peut-il gagner de l'argent ?

**Rapport au propriétaire — 2026-08-01**

---

## 1. La réponse franche, en 5 lignes

**Non, pas en l'état, et pas avec le modèle comme moteur de décision.** Le fait central est confirmé deux fois indépendamment, hors échantillon, sur ~1 500 matchs non sélectionnés des 5 grandes ligues : le Brier du modèle est de 0,6225 contre 0,5874 pour la clôture déviguée, et le poids optimal du modèle dans un mélange modèle/marché est **w\* = −0,20**. Ce n'est ni un artefact de sélection, ni un problème d'échantillon, ni un démarrage à froid : quand le modèle s'écarte du prix, il a en moyenne tort dans le sens de son écart, et cela empire quand on lui donne plus de données. Aucun réglage de seuil ne rend positif un signal dont le contenu informationnel marginal est nul ou négatif — même si, mesuré aux cotes B365 réelles, la discipline actuelle récupère quand même ~9 points de ROI (−12,9 % sans filtre → −3,6 % avec, n=200). **La condition pour que cela change** : abandonner le 1X2 et les totals comme terrain de jeu, restreindre le système aux deux zones où il est déjà démontré calibré (double_chance et h2h à model_prob > 0,70), réparer la CLV — aujourd'hui structurellement fausse sur 89 % des picks — et n'accepter comme preuve d'avantage que cette CLV, mesurée contre une ligne sharp, sur 150 paris.

---

## 2. Ce qui est structurel et ce qui est corrigeable

### Structurel — n'y investis plus une heure

| # | Limite | Pourquoi c'est irréductible |
|---|---|---|
| S1 | **Le désaccord du modèle avec le prix est anti-informatif sur 1X2 et totals** | w\* = −0,20 global, mesuré sur 1 549 matchs, cotes de clôture réelles. Par ligue : Bundesliga −0,55, La Liga −0,25, Serie A −0,20, Ligue 1 −0,20 — seule l'EPL est marginalement positive (+0,10). Se dégrade quand le train grossit. |
| S2 | **Dixon-Coles réduit une équipe à 4 nombres** (`models.py:246-253`) | Aucune notion de composition, minutes, qualité individuelle, rotation, enjeu. Le marché fixe son prix final ~1 h avant, quand les compositions tombent. Le modèle arrive après le marché sans rien qu'il n'ait déjà. Et `football_api.py:89-96` fait qu'au 1er août il tourne sur la saison 2025-26 **close en mai, mercato compris**. |
| S3 | **La marge du book grand public** | Pour afficher +4 % d'edge chez Betclic/Bet365, il faut battre la ligne équitable de ~6 à 8 points sur le segment cote ≤ 2,22. Ni Pinnacle ni les exchanges ne sont accessibles légalement en France. |
| S4 | **Le ROI ne tranchera jamais à cette échelle** | Détecter un edge vrai de 2 % à t=2 demande ~7 700 paris ; 5 % en demande ~1 240. À 1-3 paris/semaine, c'est plusieurs années. La voie du grand échantillon est fermée par construction. |
| S5 | **Demi-saison sans information** | `MIN_MATCHES=4` mais confiance pleine seulement à `SHRINK_FULL_AT=12` matchs à domicile ET 12 à l'extérieur, soit ~24 journées → mi-décembre. Avant, le seul signal différenciant est l'ELO, c'est-à-dire ce que le marché price déjà. L'information n'existe pas ; ce n'est pas un bug. |
| S6 | **Les combinés sont mathématiquement indéfendables ici** | Les k jambes sortent du **même** modèle : leurs erreurs de calibration sont parfaitement corrélées et se multiplient. `CORRELATION_HAIRCUT=0.97` ne s'applique que par jambe partageant un `sport_key` — 12 ligues distinctes = abattement **zéro**. Break-even à 12 jambes : moins de 3,8 % d'erreur relative par jambe ; tu en as ~34 %. |

**Une note importante sur S1** : l'alternative « détecteur d'écart de prix, sans modèle du tout » a été testée sur le même banc et elle est **pire** : Pinnacle dévigué → pari B365 donne −19,1 % (n=71), −45 % à edge ≥ 1 % ; meilleur prix contre consensus dévigué −11,0 % (n=759) à −27,3 % (n=265). Et à deux comptes, il n'y a pas de line-shopping. Ne bascule pas là-dessus en croyant simplifier.

### Corrigeable — c'est là que se trouve ton temps utile

**Priorité absolue — la mesure est cassée**

- **La CLV va écrire des valeurs fausses sur 88,9 % des picks.** `clv.py:53-57` : `_outcome_name` ne connaît que `1`/`X`/`2` ; un pick `O25`, `1X` ou `DNB2` tombe dans le `else` et réclame la cote de l'équipe **extérieure**. `clv.py:204` appelle `extract_best_odds` sans `market_key`, dont le défaut est `h2h` (`models.py:670`). Population : 229 totals + 30 DNB + 5 DC = 264/297. La colonne ne sera pas vide — elle contiendra du bruit signé, et `db.py:724-732` en fera une moyenne sans aucun contrôle.
- **Le snapshot est effacé au règlement.** `db.py:624` : `row.closing_odds = closing_odds`, sans garde, et les quatre appels du résolveur (`resolver.py:229, 352, 407, 468`) passent `update_result` **sans** cet argument. Aller sans retour.
- **La référence de clôture serait ton propre book.** `extract_best_odds` applique la whitelist (`models.py:685`) : tu mesurerais `close_betclic / entrée_betclic`, c'est-à-dire la dérive de ton bookmaker, pas la CLV. Un parieur qui prend des favoris tôt verrait une CLV positive sans aucune compétence. **Il faut une référence déviguée sharp**, via `_novig_fair_prob` (`analysis.py:948`) sur l'événement de clôture.
- **Aucune colonne ne stocke la mise réellement placée.** `db.py:619` `stake = row.kelly_stake`, `db.py:721` `returned = sum(kelly_stake * best_odds)`. Le −17,9 % est un ROI **pondéré par Kelly d'un parieur qui n'existe pas** — et la pondération est corrélée à l'edge affiché, donc elle surpondère précisément les picks les plus surconfiants.
- **Le prix noté n'a jamais été obtenable.** 132/297 picks (44 %) sont cotés Matchbook / Betfair / 1xBet ; 3/297 chez un book français. Le −17,9 % est donc une **borne supérieure** : le vrai chiffre est plus bas. Ne le jette pas — c'est une preuve à charge trop clémente, pas une mesure incertaine.

**Le calibrateur ne calibre presque rien**

- `/app/data/calibrator.json` : `n_samples=4647`, `segments = ['football_h2h']` et rien d'autre. `ml.py:505-508` retombe sur la carte **globale**, qui est le même jeu 1X2. Les 87 picks totals (ROI −36,8 %), le tennis et le basket sont donc corrigés par une courbe ajustée sur des issues 1X2.
- Il est ajusté sur un **autre modèle** que la production : `ml.py:351-355` appelle `blended_match_probs` sans `sport_key` (donc ρ par défaut), sans H2H, sans modificateurs, avec `elo_rating` et `xg_for` à `None`. Carte apprise sur du Dixon-Coles nu, appliquée à du DC+ELO(+xG).
- `brier_after` est **in-sample** (`ml.py:172`) : l'amélioration 0,1986 → 0,1963 ne veut rien dire.
- `_collect_training_data` exclut `_DERIVED_MARKETS` (`ml.py:44`) : **le seul marché calibré et rentable du track record ne nourrit jamais le calibrateur et n'en reçoit jamais de carte.**

**Données mortes ou gelées**

- Le **xG pèse 35 % du λ** (`models.py:772`) et sa source principale est morte (`xg.py:8-10`, Understat cassé depuis fin 2025). Le repli api-football renvoie `[]` car `_current_season_year()` bascule sur 2026 dès le 1er août (`api_football.py:261-264`), saison sans aucun match FT. `db.py:229-231` n'écrit que les valeurs non nulles : les 119 xG en base sont **gelés au 2026-07-19 et ne seront jamais effacés**. `sources_updated_at` est écrit et **lu nulle part**. `/health/sources` affiche « ok » parce qu'il teste la joignabilité, pas la fraîcheur. Silencieux, confiant, pondéré à 35 %.
- **Deux clés orphelines** : le code écrit `soccer_france_ligue1` partout alors que The Odds API émet `soccer_france_ligue_one` — la Ligue 1 est invisible de bout en bout. Et `soccer_efl_champ` (production) contre `soccer_england_championship` (fd.co.uk + api-football) : la Championship ne peut jamais recevoir ni son xG ni ses cotes de clôture.
- **Blessures et fatigue sont du code mort** : `FETCH_INJURIES` et `FETCH_FATIGUE` valent `0` par défaut et n'existent pas dans `.env`. Les deux modificateurs retournent `1.0`. (Et quand ils tourneront, `injuries.py:116` fait que perdre Haaland et perdre le 3e latéral comptent exactement pareil.)
- **`compute_league_averages` n'a aucune garde de taille d'échantillon** (`models.py:256-262`) et ces moyennes multiplient **directement les deux λ**. Dès la 1re journée, elles seront écrasées par une estimation sur ~10 matchs. C'est le seul de ces défauts qui produit une **erreur active** plutôt qu'un repli — il devrait être corrigé avant la reprise.
- `football_data_co_uk.py:168` compte les **années civiles** au lieu des saisons : la boucle casse après une seule saison. Corriger ça double immédiatement l'échantillon d'entraînement, gratuitement.

**Discipline et périmètre**

- **Tous les garde-fous de risque sont inertes.** `AUTO_CONFIRM_PICKS=1` fait naître les picks en `confirmed`, donc `confirm_placement` sort en no-op (`db.py:804`) avant d'atteindre `check_can_place_bet` (`db.py:814`). Pire : `_today_stake_total` (`guards.py:88-101`) compte des lignes `bet_placed` que l'auto-confirm ne crée pas — le compteur lirait zéro même s'il était appelé. Doublement mort, et c'est une **régression introduite aujourd'hui**.
- **77 % des picks (229/297) sont produits sans aucune donnée d'équipe** (`model_type='consensus'`), et 92/297 sur des ligues où `team_stats` est littéralement vide. `SCAN_ALL_SOCCER=1` (`api.py:283-291`) ajoute toute ligue en saison. Sur les 297 picks historiques, **14 (4,7 %) survivent à la discipline d'aujourd'hui**.
- **Le chemin combiné contourne la discipline** : `recommend.py:279-294` ne passe ni `max_book_odds` (donc 2,22 inactif), ni `underdog_odds`, ni `underdog_min_prob` — alors que son docstring affirme « SAME protections as the safe-picks engine ».
- **L'ELO local est Dixon-Coles ré-encodé** : même liste de matchs (`main.py:207/223/227`), corrélation +0,81 avec log(λ_dom/λ_ext), gain réel 0,8 % de Brier pour 30 % du poids. Remis à 1500 à chaque refresh, donc aucune comparabilité inter-ligue ni mémoire inter-saison.

---

## 3. Les atouts réels du système

Sois juste avec toi-même : il y a du bon travail dans ce dépôt.

1. **La dérivation Double Chance / DNB est exacte.** `analysis.py:365-393` — comparée en direct à 30 cotes du **vrai** marché DC de Bet365 : écart médian −0,08 %. Le prix DC n'est pas un artefact algébrique. Et il coûte **zéro crédit** : `analysis.py:766-770` le dérive du groupe h2h déjà payé. Dans un système contraint par 500 requêtes/mois, c'est l'argument le plus décisif en faveur du DC.
2. **La séparation référence/exécution est correcte.** `consensus_match_probs` (`models.py:414`) itère sur **tous** les books avec Pinnacle pondéré 3,0 et Betclic 0,8, tandis que l'edge est calculé sur les seules cotes joignables. Le prix de référence reste adossé au book le plus sharp. Ce n'est pas auto-référentiel.
3. **Le contingent d'échantillonnage déterministe** (`analysis.py:322-362`) est la meilleure idée du dépôt. Le code explique lui-même qu'une coupe sèche supprime les données qui pourraient l'infirmer. C'est exactement la bonne réponse au risque de décision infalsifiable — à généraliser, pas à supprimer.
4. **`MAX_BOOK_ODDS=2.22` est le bon instrument.** Il pilote sur le **prix**, donc il est insensible à l'erreur de calibration, et il vaut ~9 points de ROI sur le banc. Ne le retire pas.
5. **Le repli xG est propre** : `models.py:838-842`, `eff_xg_weight = 0` quand le xG manque et Dixon-Coles récupère 100 % du λ. Pas de dégradation cachée.
6. **Le bankroll_ledger est réel** : `db.py:627-646` conditionne tout mouvement à l'existence d'une écriture `bet_placed`. 48 paris réellement confirmés à la main, 2 dépôts, 1 retrait. C'est le ROI qui est fictif, pas le solde.
7. **`KELLY_EDGE_CAP` est correctement implémenté** : `capped = kelly_edge_cap / b` plafonne l'edge agissant, pas la fraction. L'algèbre est juste.
8. **Le banc de falsification existe déjà, gratuit et hors quota** : `data/fd_couk`, 5 CSV avec résultats **et** cotes de clôture Pinnacle + moyenne marché + O/U 2.5. J'ai obtenu 1 549 points de mesure en quelques minutes, sans clé.
9. **Le plan api-football Pro est déjà payé** : 7 500 requêtes/jour, 414 consommées, Bet365 (88 marchés dont le vrai Double Chance) et Pinnacle. Aujourd'hui totalement inexploité côté cotes.

---

## 4. Ce qu'un parieur sérieux ferait différemment

- **Il mesurerait dans l'unité qui compte : l'euro qu'il a réellement misé, à la cote qu'il a réellement prise.** Une colonne `stake_actual` et une colonne `placed_odds`, saisies à la main à la confirmation. Sans ça, aucune de tes métriques n'est la tienne.
- **Il passerait en mise plate à 1 EUR = 1 unité.** À 100 EUR, la perte de croissance due à une mauvaise taille est du second ordre (rester entre 0,5× et 1,5× de f\* conserve 75-95 % du taux de croissance) ; le **signe** de l'edge est du premier ordre. La mise plate est implémentable à la main et supprime l'amplification de l'erreur de modèle par le multiplicateur Kelly.
- **Il couperait les combinés.** Gratuit, immédiat, et mathématiquement indiscutable à ton échelle.
- **Il ne parierait pas les cinq grands championnats avant la 8e-10e journée.** Zéro code, zéro coût, applicable ce soir.
- **Il relèverait 20 cotes 1X à la main sur Betclic** et les comparerait aux `1/(q1+qX)` calculés sur les mêmes matchs. Coût nul. Ça mesure la décote du prix synthétique sur ton **seul** marché apparemment rentable, et ça décide s'il est exploitable **avant** d'engager un euro. Rappel : les 5 DC en base sont cotés 4/5 sur exchange — le prix DC de ton track record est le plus fictif de tous.
- **Il ouvrirait 2-3 comptes ANJ supplémentaires.** Gratuit, une heure. C'est le seul levier de line-shopping réellement disponible à ton échelle, et il conditionne toute stratégie fondée sur l'écart de prix.
- **Il écrirait sa règle d'arrêt avant de connaître le résultat.** Un critère pré-enregistré qui existe en deux versions n'est plus pré-enregistré : choisis **150 paris avec CLV valide**, écris-le, et tiens-le.
- **Il n'ouvrirait pas l'horizon à J+14** tant que la CLV n'est pas positive à J+0. Une ligne précoce n'est pas une opportunité pour un modèle à w\* ≤ 0 : c'est une ligne plus bruitée autour de la même valeur, et un sélecteur qui prend le max d'edge sur du bruit subit une anti-sélection **plus** forte.

---

## 5. Le plan réaliste

Cinq actions, par ordre de rapport valeur/effort.

### Action 1 — Réparer la CLV, et la financer. *(2 soirées)*

**Quoi :**
- Restreindre le snapshot à `Prediction.market == 'h2h'` (`clv.py:103-107`, une ligne) **ou** router `_outcome_name` sur `(market, selection)` et passer `market_key`/`point`, avec `_derive_dc_dnb_odds` pour DC/DNB.
- `db.py:624` → `if closing_odds is not None: row.closing_odds = closing_odds`. Plus un test qui résout une prédiction pourvue d'un `closing_odds` et vérifie qu'il survit.
- **Référence sharp obligatoire** : appeler `_novig_fair_prob` sur l'événement de clôture plutôt que `extract_best_odds`, sinon tu mesures la dérive de ton propre book.
- Garde-fou dur : refuser d'écrire `closing_odds` si `best_odds/closing_odds` sort de [0,5 ; 2,0].
- **Financement** : passer le cron de `*/10` à `*/30` (`main.py:601-606`) et forcer `ODDS_REGIONS=eu` sur le seul appel de clôture (2 crédits au lieu de 4). Facture divisée par 6-8.

**Gain attendu :** la seule métrique qui tranche en 100-200 observations au lieu de 1 250-7 700. **Critère observable :** `avg_clv_pct` calculée sur des picks dont le marché correspond, avec une erreur-type et un t. Aujourd'hui : 0 mesure sur 297 picks.

### Action 2 — Restreindre le périmètre à ce qui est démontré. *(1 soirée)*

**Quoi :** `SCAN_ALL_SOCCER=0` ; ligues limitées à celles qui ont une ligne `team_stats` **et** une calibration ; marchés limités à `double_chance` + `h2h` avec `model_prob > 0,70` ; garder `MAX_BOOK_ODDS=2.22` et `MIN_VALUE_EDGE=0.04` ; totals fermés jusqu'à l'action 4. Déplacer le plafond de picks/jour de la **confirmation** vers la **génération** (troncature après `rank_value_bets`, `analysis.py:1113`) : 2-3 paris/jour, 1 unité.

**Gain attendu :** ~9 points de ROI sur le banc (−12,9 % → −3,6 %), quota divisé par cinq, et la fin de la surcharge décisionnelle (37 lignes à l'écran le 19/07, pour 99,89 EUR de mise Kelly cumulée).
**Critère observable :** **le flux de picks doit s'effondrer.** S'il ne chute pas fortement, le filtre ne mord pas. Sur l'historique, 14/297 survivent — attends-toi à ~5 %, pas à 310.

### Action 3 — Rendre la mesure celle du propriétaire. *(1 soirée + une migration Alembic)*

**Quoi :** colonnes `stake_actual` et `placed_odds`, saisies à la confirmation ; `db.py:619` et `db.py:721` pointent dessus, jamais sur `kelly_stake`/`best_odds` ; mise plate 1 EUR.

**Gain attendu :** le ROI cesse de décrire un parieur fictif coté sur Matchbook. **Critère observable :** l'écart entre le ROI affiché et le ROI du ledger tombe à zéro.

### Action 4 — Un test de non-régression sur le banc gratuit. *(1 soirée)*

**Quoi :** un script unique lancé avant tout changement de `models.py` : walk-forward sur `data/fd_couk`, critère = **ROI simulé aux cotes B365 réelles avec la discipline de production** (aujourd'hui −3,63 % à edge ≥ 4 %, −2,25 % à edge ≥ 6 %). **Pas** w\* — w\* est mesuré sur le 1X2 de 5 ligues européennes, hors des deux marchés qui ne perdent pas ; l'utiliser comme garde gèlerait tout développement à vie.
Dans le même passage, trois corrections gratuites : le bug `n_seasons` (`football_data_co_uk.py:168`), les clés orphelines Ligue 1 et Championship (table d'alias unique), et un segment `football_totals` — les colonnes `B365>2.5` / `PC>2.5` sont déjà dans les CSV, c'est ~20-30 lignes.

**Gain attendu :** tu cesses de juger tes correctifs sur du raisonnement et 227 paris bruités. **Critère observable :** le ROI simulé bouge quand tu touches le modèle, dans le sens attendu.

### Action 5 — Éteindre le bruit résiduel. *(1 soirée)*

**Quoi :** couper les combinés ; `xg_weight` gouverné par un TTL sur `sources_updated_at` (si > N jours → traiter comme `None`, `models.py:836` gère déjà proprement) ; garde de taille d'échantillon sur `compute_league_averages` ; trois labels distincts `dc` / `dc_elo` / `dc_elo_xg` à `models.py:914` pour que la segmentation redevienne mesurable ; `ml.py:505` renvoie la prob inchangée quand le segment manque au lieu de retomber sur le global ; remplacer l'isotonique par un Platt à deux paramètres, sortie clippée à [0,03 ; 0,95] ; `brier_after` en validation croisée k=5 ; persister la probabilité **brute** du modèle dans une colonne séparée.

**Gain attendu :** suppression des modes de défaillance silencieux. **Critère observable :** `/health/sources` remonte la **fraîcheur** et pas seulement la joignabilité.

---

## 6. La question honnête : combien de temps, quelle probabilité ?

**Le temps de développement** est modeste : les cinq actions ci-dessus représentent 5 à 7 soirées. Rien ici n'est de l'infrastructure de fonds — tout est local, gratuit, hors quota, et pour l'essentiel déjà codé à 80 %.

**Le temps de mesure est le vrai coût.** Après restriction du périmètre, tu produiras de l'ordre de 1 à 3 paris par semaine en saison, avec un quasi-silence en juin-août. Atteindre 150 paris avec une CLV valide demande donc **8 à 15 mois calendaires**, soit deux fenêtres de saison. Et c'est le chemin *rapide* : le ROI, lui, demanderait 1 240 paris pour trancher un edge de 5 %, et 7 700 pour un edge de 2 % — c'est-à-dire une décennie. Il n'y a pas de raccourci ; le quota gratuit plafonne le volume par construction, pas par manque de discipline.

**La probabilité de succès.** Je la situe entre **10 et 20 %**, et voici comment j'y arrive :

- Le modèle est mesurablement anti-informatif sur les deux marchés qui portent 88,9 % des picks. Ça, c'est établi sur 1 549 matchs et reproductible. Il ne reviendra pas.
- Les deux marchés qui survivent sont statistiquement vides : double_chance à n=50 a une SE(ROI) de ~10 points — le +3,3 % couvre −16,7 % à +23,3 % à deux erreurs-types ; h2h à n=21 est encore pire. Le « 0,683 prédit → 0,680 réalisé » ne démontre aucune calibration : la SE du taux de réussite à n=50 est de 6,6 points.
- Le double_chance a néanmoins une **vraie** raison structurelle d'être tolérant : la renormalisation du groupe (`analysis.py:650-652`) garantit que toute erreur de répartition entre p1 et pX s'annule intégralement — seule l'erreur sur p2 survit. C'est une propriété que le plafond de cote ne procure pas, et c'est le meilleur argument du dossier. Mais il est théorique, pas mesuré.
- L'alternative sans modèle (arbitrage de prix contre consensus sharp) a été testée et elle est pire.
- Et le fait central lui-même est marginalement significatif : l'écart de Brier 0,0114 sur n=256 donne t ≈ 1,8. C'est le seul point qui joue *en ta faveur* dans cette estimation — et il ne justifie pas plus d'optimisme que ça, puisque la réplication hors échantillon, elle, est solide.

**Ce que tu risques si ça échoue** : ta bankroll est bornée à 100 EUR et tu mises à la main, donc le risque de ruine mécanique est nul — rien n'est placé automatiquement. Le coût réel est du temps, et l'inconfort de découvrir dans un an que la réponse était déjà dans les données d'aujourd'hui.

**Ce que tu gagnes même si ça échoue** : une réponse propre à une question que la quasi-totalité des parieurs ne se pose jamais, obtenue pour 100 EUR et quelques soirées. C'est un bon prix. Mais il faut écrire la règle d'arrêt **maintenant, avant de connaître le résultat** — sinon dans 150 paris tu trouveras une raison de continuer.