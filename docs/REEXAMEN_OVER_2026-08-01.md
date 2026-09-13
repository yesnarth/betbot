# Coupe des Over sur le marché totals — verdict

*Sources : 27 constats issus de 4 analystes, chacun soumis à contre-vérification adversariale (14 CONFIRMÉ, 7 FRAGILE, 6 NON VÉRIFIÉ, 1 réfuté et retiré). Les constats FRAGILE et NON VÉRIFIÉ sont signalés à chaque usage.*

---

## 1. La réponse en 4 lignes

**NUANCER — la coupe reste en vigueur à court terme, mais pour une raison différente et sur un périmètre différent.** Sa justification officielle (« le modèle surestime les buts dans UNE direction ») est morte : l'asymétrie Over/Under n'est significative sous aucun découpage testé (p=0,076 brut, 0,243 après le plafond de cote, 0,936 en poolant à l'intérieur des ligues). Ce qui reste établi, c'est que **le marché totals entier est sans avantage** — le modèle y est battu par la cote brute, dans les deux sens, et les Under post-filtre ne rapportent pas non plus (-7,5 %, n=21). La bonne décision n'est donc pas « interdire les Over » mais « réduire l'exposition aux totals », en gardant les Over bloqués tant que le bug #3 vit, et en conservant un contingent résiduel mesurable — faute de quoi la coupe est infalsifiable par construction.

---

## 2. Ce que j'avais loupé

**Le propriétaire a raison sur le fond du raisonnement, tort sur le mécanisme qu'il invoque.**

### Là où il a raison

| # | Erreur de la décision initiale | Preuve |
|---|---|---|
| 1 | **La directionnalité n'a jamais été démontrée.** Un λ gonflé ferait perdre les Over ET gagner les Under. Les Under perdent aussi (-10,8 %, et sous H0 « le marché a raison » ils réalisent 16 gains pour 17,68 attendus). L'écart Over−Under vaut -0,2005, z=-1,78, **p=0,076** — sous le seuil de 5 %. Stratifié par bande de cote : p=0,096. Poolé à l'intérieur de chaque ligue : p=0,936. | CONFIRMÉ, n=67 |
| 2 | **Le chiffre affiché est faux d'étiquette.** Le ROI agrégé des 32 Over est **-51,6 %**, pas -54,5 %. Le -54,5 % est la cellule O2.5 seule (n=20). C'est bien -51,6 % qui redonne le t=-3,68 et l'IC95 [-79,1 ; -24,1] cités. | CONFIRMÉ |
| 3 | **6 des 32 Over étaient déjà morts.** MAX_BOOK_ODDS=2,22 élimine 6 Over (cotes 2,38 à 2,75), **0 gagnant sur 6**. Le chiffre pertinent pour la configuration actuelle est **-40,5 % sur n=26**, pas -51,6 % sur n=32. | CONFIRMÉ |
| 4 | **Le libellé couvre 4 lignes, la réalité 3.** O05 n'a jamais existé et ne peut pas exister : P(O0.5)=0,924 → cote juste 1,08, très sous le plancher MIN_BOOK_ODDS=1,50. 0/1752 matchs simulés produiraient une cote O0.5 éligible. | CONFIRMÉ |
| 5 | **n=32 est pile au seuil de détection.** Le plus petit écart détectable à 80 % de puissance vaut 48 pts à n=32. Simulation (400 000 tirages) : si le vrai déficit était -25 %, l'estimation moyenne *parmi les significatives* serait -44,5 % — exagération ×1,8. Et le n effectif est < 32 : seulement **28 événements distincts** (4 matchs portent deux Over) sur **6 journées de création**. | CONFIRMÉ |
| 6 | **La règle est permanente, la population ne l'est pas.** 0 des 47 picks Over ne vient d'une grande ligue européenne. Fenêtre : 9,1 jours, 12 ligues d'été, max 9 observations dans une même ligue. La règle s'appliquera d'abord à la reprise d'août — une population qui ne recouvre pas celle de validation. | CONFIRMÉ |
| 7 | **La coupe s'auto-scelle.** Elle met le volume Over à zéro, donc aucune donnée ne viendra jamais la confirmer ni l'infirmer, alors qu'il faudrait n=327 pour trancher à 15 pts près. Une décision qui supprime sa propre condition de falsification n'est pas une décision de mesure. | CONFIRMÉ |
| 8 | **31 % de l'échantillon est manquant, et pas au hasard.** 30 des 97 picks totals ne sont pas résolus (15 Over / 15 Under), certains depuis plus de 10 jours. Les Over non résolus sont **15 consensus / 0 blended**, et concentrés sur les qualifications de Ligue des Champions — exactement la zone (gros favoris, chemin consensus, O3.5) où la preuve manque. | CONFIRMÉ |

### Là où il a tort

**Les bugs #1 (tri chronologique) et #2 (prior Elo) n'ont touché aucun des 32 Over.** Ce point est le plus solide du dossier :

- Les 32 Over sont répartis sur 11 ligues portant des codes `AF-xxx`, donc le chemin `stats_inseason.py → api_football.get_finished_matches`, qui trie déjà `reverse=True`. 0 des 97 picks totals ne vient des 9 ligues de `LEAGUE_MAP`, seul chemin passant par `parse_match_results` (l'ordre ascendant bugué). Vérifié par git : `api_football.py` est né au commit `e33d684` du 2026-07-26 08:20 **avec le tri déjà présent**, et le premier pick totals blended est du 2026-07-26 09:21.
- Bug #2 : dans `models.py::blended_match_probs`, `over_25/over_15/over_35` sont copiés depuis `poisson_probs` **avant** l'étape Elo, qui ne réécrit que `home_win/draw/away_win`. Impact rigoureusement nul sur les totals. (CONFIRMÉ)

**« Le modèle qui a perdu n'existe plus » est faux.** La partie totals du pipeline est aujourd'hui **inchangée**. Les correctifs du 01/08 sont même des modifications non commitées portant uniquement sur le tri, le prior Elo et la whitelist.

---

## 3. La part attribuable aux bugs — chiffrée

Écart à expliquer : prédit 0,586 → réalisé 0,281 = **30,5 points** (le « 31 points » du dossier).

| Source | Part de l'écart | Statut | Preuve |
|---|---|---|---|
| **Bug #1 — tri chronologique inversé** | **0 pt** (et de signe négatif : -0,7 pt) | CONFIRMÉ, n=1139 puis n=1551 reproduit | Biais SIGNÉ Δλ_total bugué−corrigé = **-0,0203 but** (négatif). ΔP(O2.5) = -0,69 pt. Le bruit est énorme (|Δ| moyen 0,31 but, extrêmes -37 / +39 pts) mais **symétrique** : les ratios attaque/défense sont normalisés par la moyenne de ligue, donc réordonner redistribue la force entre équipes sans déplacer le total. Passé dans le pipeline complet, le ROI est identique (Over -9,2 % bugué vs -9,7 % corrigé). |
| **Bug #2 — prior Elo** | **0 pt**, structurellement | CONFIRMÉ (vérification de code) | N'atteint jamais les totals. |
| **Bug #3 — heuristique consensus (NON CORRIGÉ)** | **≈ 0,1 pt en moyenne globale ; jusqu'à 6,5 pts sur le sous-ensemble « gros favori »** | CONFIRMÉ, n=1752 | λ_total moyen 2,806 vs 2,765 buts réels ; P(O2.5) 0,525 modèle / 0,524 marché / 0,530 réel → **biais moyen +0,14 pt**. Le biais est entièrement conditionnel : p_nul<0,18 → **+6,45 pts** (n=148) ; il **s'inverse** au centre (-2,56 pts, n=539). Coût mesuré en value bets : O25 n=268 ROI **-4,9 %**. |
| ↳ *amplification par la sélection* | +0,36 but sur les matchs sélectionnés vs +0,04 global (×8,7) | CONFIRMÉ, n=1752 | La règle qui maximise l'edge va chercher précisément les matchs où l'heuristique se trompe le plus. Réel, non corrigé — mais 16 des 32 Over venaient de **blended** (donc hors consensus) avec un ROI de **-57,9 %**, pire que les 16 consensus à -45,4 %. Le bug #3 n'est pas le seul en cause. |
| **Sélection sur le bruit (malédiction du vainqueur)** | **≈ 5 à 10 pts**, **symétrique Over/Under** | FRAGILE (points estimés non reproductibles, signe et symétrie confirmés) | Backtest fidèle au pipeline de production : Over n=222 prédit 0,590 / implicite 0,543 / réalisé 0,523 ; Under n=208 ROI -0,6 %. Reproduction indépendante : Over -9,7 % / Under -11,9 %. **Lire « environ -8 à -12 % des deux côtés », pas « -7 à -9 % ».** |
| **Écart MARCHÉ ↔ RÉEL** | **24,9 pts, soit 81,6 %** | CONFIRMÉ, n=32 | model_prob − 1/best_odds = **+0,056 seulement**. Le modèle ne revendiquait que 5,6 pts au-dessus du marché ; les 25 points restants séparent le **prix affiché** de la réalité. 38 des 67 meilleures cotes venaient de Matchbook (exchange quasi sans vig), donc 1/best_odds est proche de la probabilité juste. Aucun bug de λ ne peut creuser ça : une mauvaise sélection contre un marché efficient coûte la marge (1-4 %), pas 50 %. |

**Bilan.** Sur 30,5 points : **0 point attribuable aux bugs corrigés**. Au plus 5-7 points sur le sous-ensemble à gros favori via le bug #3, encore vivant. Environ 5-10 points de sélection-sur-bruit, structurelle et **présente des deux côtés**. **Il reste ~20 points inexpliqués**, entièrement logés entre le prix du marché et le résultat.

Ces 20 points sont soit un échantillon extrême, soit une propriété réelle des ligues d'été effectivement pariées. Les tests selon l'hypothèse nulle retenue :

- H0 « le marché a raison » : P(X≤9 | n=32) = **0,0034**
- H0b « espérance structurelle du pipeline (-4,6 pts vs marché) » : P(X≤9 | n=32) = **0,0147**
- H0c « taux réalisé du backtest fidèle (0,523) », post-plafond : P(X≤9 | n=26) = **0,053** — *à la limite, non significatif à 5 %*

C'est le point le plus favorable au propriétaire : sous l'hypothèse nulle la plus réaliste, les 32 Over ne sont pas significativement pires que ce que le pipeline produit normalement.

**Vérifications d'artefact fermées** (pour couper court) : le notateur `resolver.py::_decide_totals_outcome` est symétrique et correct, les taux de résolution sont équilibrés (Over 68 %, Under 70 %) ; l'Over n'est **pas** le côté à cote longue (médiane 1,82 contre 1,97 pour l'Under, Mann-Whitney p=0,19), donc « les cotes longues perdent » ne peut pas fabriquer le déficit.

---

## 4. Par ligne et par filtre

### Production, picks tranchés — avant / après MAX_BOOK_ODDS=2,22

| Ligne | n | prédit | implicite | réalisé | ROI | t | IC95 bootstrap | → après plafond 2,22 |
|---|---|---|---|---|---|---|---|---|
| **O1.5** | 9 | 0,681 | 0,617 | 0,444 | **-29,1 %** | -1,04 | [-82,8 ; +24,6] | n=9, -29,1 % *(inchangé)* |
| **O2.5** | 20 | 0,562 | 0,507 | 0,250 | **-54,5 %** | -2,99 | [-84,5 ; -17,4] | **n=16, -43,1 %, t=-1,96** |
| **O3.5** | 3 | — | — | 0,000 | **-100 %** | — | *n<20, indicatif* | n=1, -100 % |
| **Over total** | **32** | 0,586 | 0,530 | 0,281 | **-51,6 %** | **-3,68** | [-79,1 ; -24,1] | **n=26, -40,5 %, t=-2,45, IC95 [-71,5 ; -7,6]** |
| U1.5 | 2 | — | — | — | -100 % | — | *indicatif* | — |
| U2.5 | 18 | — | — | — | -6,7 % | — | — | — |
| U3.5 | 15 | — | — | — | -3,9 % | — | — | — |
| **Under total** | **35** | — | — | — | **-10,8 %** | — | [-43,0 ; +22,8] | **n=21, -7,5 %, t=-0,38** |

*Note de cohérence : en appliquant **tous** les seuils cumulativement (et pas seulement le plafond), l'Under tombe à n=20 / -2,9 %. Le n=21 / -7,5 % ci-dessus est l'effet du plafond seul. Les deux chiffres sont vrais, ils ne répondent pas à la même question.*

**Lecture critique.** O2.5 est **la seule ligne qui porte une preuve** (binomial exact P=0,0166). O1.5 est indistinguable du hasard (P=0,231) — **mais 7 de ses 9 picks sont argentins**, ce n'est pas un échantillon « O1.5 », c'est un échantillon Argentine : le rouvrir serait une inférence mono-ligue. O3.5 n'a pas d'échantillon. Et dans le régime qui s'applique réellement, O2.5 tombe à t=-1,96 : la preuve est elle-même marginale.

### Effet de chaque filtre actuel sur les 32 Over (application rétroactive cumulative)

| Filtre | Valeur prod (vérifiée dans l'env du worker) | Over éliminés | Under éliminés | Effet |
|---|---|---|---|---|
| **MAX_BOOK_ODDS** | 2,22 | **6 / 32 (18,8 %)**, **0 gagnant sur 6** | **14 / 35 (40 %)** | ROI Over -51,6 % → **-40,5 %** ; Under -10,8 % → -7,5 % |
| MIN_BOOK_ODDS | 1,50 | 0 | — | aucun |
| MIN_MODEL_PROB | 0,40 | 0 | — | aucun |
| MIN_VALUE_EDGE | 0,04 | 0 | — | aucun |
| MIN_EDGE_VS_NOVIG | 0,03 | 0 | — | **inerte** : le dévig proportionnel rend l'edge no-vig toujours > edge brut (moyenne +0,174, **minimum +0,082**) |
| DERIVE_DNB | 0 | 0 | 0 | no-op (aucun pick totals n'est un DNB) |
| BOOKMAKER_WHITELIST | betclic,bet365 | **non ré-applicable** | — | 0/97 picks totals y étaient cotés (Matchbook 51, 1xBet 15, Unibet 13…) |
| MIN_BOOK_ODDS (sur O0.5) | 1,50 | — | — | exclut O0.5 **structurellement**, coupe ou pas |

**Deux avertissements sur ce tableau.**

1. **Le plafond 2,22 est un filtre directionnel caché qui *favorise* les Over.** Le marché cote structurellement l'Over plus court (médiane B365 1,83 vs 2,00). Part survivant au plafond : Over 88,1 % vs Under 74,5 %. En production : 37/47 Over (78,7 %) contre 32/50 Under (64,0 %). Superposer la coupe à ce plafond crée **deux biais directionnels de sens opposé, non séparables** : toute mesure future de l'un sera contaminée par l'autre. (CONFIRMÉ)
2. **Le -40,5 % est une BORNE HAUTE, pas basse.** Les prix historiques venaient d'exchanges et de books offshore ~3 % au-dessus de ce que betclic/bet365 auraient offert (ratio médian Betfair/B365 sur Over 2.5 = 1,0314 ; overround exchange 1,013 vs B365 1,051). Avec la whitelist réellement en place, le même jeu de paris aurait rendu **moins**. On ne peut pas invoquer « de meilleures cotes désormais » pour lever la coupe. (CONFIRMÉ ; réserve mineure : Betfair est hors commission et 51/97 picks étaient chez Matchbook, donc l'écart réel est un peu plus étroit, mais de même signe.)

---

## 5. Recommandation opérationnelle

### À appliquer aujourd'hui

**A. Garder `ALLOW_TOTALS_OVER=0` — mais pour le motif corrigé, et à durée limitée.**
Justification retenue : (i) les survivants au plafond perdent avec un IC95 **entièrement sous zéro** (-40,5 %, [-71,5 ; -7,6], n=26) ; (ii) le bug #3 est vivant et non corrigé ; (iii) le -40,5 % est une borne haute. Justification **abandonnée** : « le modèle surestime les buts dans une direction ».
**Corriger le commentaire de `analysis.py`** : le périmètre réel est O1.5/O2.5/O3.5 (O05 n'existe pas), et le chiffre à citer est **-51,6 % sur n=32 / -40,5 % sur n=26**, pas -54,5 %.

**B. Symétriser sur l'Under.** C'est le changement de fond. Le modèle est battu par la cote brute sur les totals (Brier 0,2356 contre 0,2242 en global, et l'heuristique consensus elle-même est à 0,2442 contre 0,2417 pour le marché). Une coupe unilatérale laisse tourner un Under **tout aussi dépourvu d'avantage** (-7,5 % post-plafond, backtest -8 à -12 %). Concrètement : relever la barre sur le marché totals dans son ensemble (mise réduite ou edge minimal spécifique aux totals). *Il n'existe pas de variable d'environnement par marché — c'est une ligne dans `analysis.py`, pas un réglage.*

**C. NE PAS durcir `MIN_EDGE_VS_NOVIG`.** Piste tentante et **réfutée** : durcir le seuil **dégrade** le ROI Over dans les deux reconstructions (+4,8 % à 3 % → +1,1 % à 12 % → -1,3 % à 15 % → -15,2 % à 20 %). C'est cohérent avec le mécanisme du bug #3 : l'écart au marché maximal désigne précisément les matchs où l'heuristique se trompe le plus. Le seuil no-vig durci est **anti-sélectif**.

**D. Ne pas déployer la garde ciblée « interdire les Over consensus à p_nul bas ».** *(FRAGILE.)* Le biais est bien localisé sur les matchs déséquilibrés (+7,7 pts sur O2.5 à p_nul<0,15, +11,1 pts sur O3.5), mais il n'est significatif **que sur O3.5** (t=+2,82) ; sur O2.5 il ne l'est pas (t=+0,98). Et passé dans le pipeline complet, les paris effectivement sélectionnés dans cette bande **gagnent** (+11,9 %, n=53 — indicatif). La garde supprimerait le sous-ensemble le mieux performant.

### Les deux vrais leviers

**E. Corriger le bug #3 — `models.py:473`, `_prob_to_lambda`.** C'est la seule correction qui touche réellement les picks incriminés. λ_total = -2·ln(p_nul) exactement : l'espérance de buts ne regarde **jamais** le marché des totals. Deux correctifs propres : ancrer λ sur le marché des totals lui-même, ou appliquer un abattement conditionnel croissant avec p_fav (0 à -0,75 but). Coût mesuré du bug : ~-5 % de ROI, pas -55 % — mais c'est un défaut réel et non corrigé, et **la coupe ne doit pas être levée avant sa réparation**.

**F. Réparer le resolver sur les compétitions à qualification — gratuit et immédiat.** 30 picks totals non tranchés (15 Over / 15 Under), certains créés le 20-22 juillet. Cela représente **+45 % d'échantillon déjà acquis mais non lu**, et il porte exactement sur la zone où la preuve manque : chemin consensus, gros favoris, O3.5 (résolue à 3/7 seulement). C'est le point le mieux fondé de tout le dossier, et il ne coûte rien.

**G. Persister λ_home / λ_away.** 0/310 picks ont un λ persisté. Sans cela, aucune décision future sur les totals ne sera auditable rétroactivement.

### Contingent résiduel (obligatoire pour que la décision soit testable)

**H. Laisser passer 20-30 % des Over, tirés au sort.** Sinon la coupe est infalsifiable : elle porte le volume à zéro, donc aucune donnée ne viendra jamais la trancher. Un contingent de 25 % coûte au pire ~0,25 × 40 % = 10 % de ROI sur 4 % du flux de picks, soit un coût borné et faible.

### Critère observable

Après (E) et (F), sur le contingent résiduel, avec la whitelist betclic/bet365 en place :

| Jalon | Décision |
|---|---|
| **n = 30 Over** | Contrôle de fumée seulement. Si le taux réalisé est ≥ 10 pts sous l'implicite marché → suspendre le contingent. *(n<20 par ligne : indicatif.)* |
| **n = 60 Over** | Si la borne haute de l'IC95 du ROI reste **< 0** → coupe permanente, dossier clos. Si l'IC95 contient 0 **et** l'écart réalisé−implicite est dans ±5 pts → lever la coupe, revenir au régime symétrique totals. Sinon → prolonger. *(À n=60 la puissance ne détecte que ~35 pts d'écart : c'est un test grossier, à assumer comme tel.)* |
| **n = 327 Over** | Seul jalon permettant de trancher à ±15 pts près. |

**Le chiffre à battre est -40,5 % sur n=26, face à -7,5 % sur n=21 côté Under** — et non -54,5 % face à -10,8 %.

---

## 6. Ce qu'on ne peut pas savoir

**Structurellement impossible avec les données actuelles :**

1. **Le contrefactuel exact.** 0/310 picks ont un λ persisté (0 aussi sur les 97 totals). On ne peut pas relire ce que le modèle annonçait. Le `model_prob` stocké n'est pas non plus la sortie du modèle : c'est `shrink_toward_market ∘ isotonique ∘ renormalisation`. Et l'isotonique appliquée aux totals est en réalité **la carte 1X2** — le segment `football_totals` est absent du calibrateur de production, qui retombe sur la carte globale ajustée sur 4 647 échantillons h2h.
2. **Les ligues effectivement pariées.** Aucune donnée locale scores+cotes n'existe pour l'Argentine, le Brésil, la Suède, la Corée, les qualifs LdC. **Zéro des 47 picks Over ne vient des 5 ligues des CSV.** Tous les backtests testent donc le **mécanisme** du modèle sur des ligues denses et bien cotées, pas les mêmes matchs.
3. **Les lignes autres que 2.5.** Les CSV football-data.co.uk ne cotent que O/U 2.5. **O1.5 et O3.5 ne sont testables hors production par aucun backtest** — or O3.5 est précisément la seule ligne où un biais significatif du bug #3 a été localisé (+11,1 pts, t=+2,82). 12 des 32 Over et 17 des 35 Under sont sur ces lignes : plus d'un tiers des paris concernés n'est simulé nulle part.
4. **L'effet de la whitelist.** Non ré-applicable rétroactivement (0/97 picks chez betclic/bet365). On sait seulement le **sens** du biais (~3 % en défaveur de la whitelist).
5. **La valeur ponctuelle du vrai déficit.** L'argument d'erreur de magnitude (Type M) montre que l'estimation est gonflée *conditionnellement à l'existence d'un effet réel* ; il ne fournit pas de valeur. L'énoncé honnête est : le point d'estimation est inflaté, et l'IC95 lui-même s'étend jusqu'à **-24 %**.

**Volume de données nécessaire (écart-type du P&L par pari : 0,968) :**

| Précision voulue | n requis | Délai à 100 % du flux (4,6 Over/jour) | à 25 % de survie | à 10 % |
|---|---|---|---|---|
| ±15 pts | **327** | 72 jours | 287 jours | 717 jours |
| ±10 pts | **736** | 161 jours | 645 jours | 1 613 jours |
| ±5 pts | **2 941** | ~640 jours | — | — |

Rappel : le flux survivant après la whitelist est **très incertain** (99,4 % des picks étaient sur des books inaccessibles). Et le n effectif est inférieur au n nominal (28 événements distincts pour 32 paris, 6 journées de création).

**Conclusion sur l'incertitude : il faut entre 2,5 mois et plus de 2 ans de flux pour trancher définitivement.** D'ici là, le seul gain d'information gratuit et immédiat est de **réparer le resolver et de lire les 30 résultats déjà acquis** — ils augmentent l'échantillon de 45 % et portent précisément sur la zone où la preuve manque.