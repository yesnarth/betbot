# Pourquoi les pronostics de BetBot échouent — rapport de synthèse

**Périmètre** : 310 picks du 20 au 30 juillet 2026, dont 192 notés, 170 tranchés (win/loss), 22 void, 118 encore en attente. Toutes les mesures ci-dessous ont été exécutées en base de production ; les constats issus des cinq analyses ont subi une contre-vérification adversariale et sont étiquetés **CONFIRMÉ** ou **FRAGILE**.

> ⚠️ **La bankroll n'est PAS virtuelle.** L'utilisateur place manuellement sur
> Betclic et Bet365 tout pick issu d'un scan. Les pertes mesurées ici sont
> réelles. Le `bankroll_ledger` ne les reflète pas parce que les mises réelles
> sont variables et ne suivent pas `kelly_stake` — d'où la lecture en **ROI à
> plat (1 unité)**.

---

# CORRECTIFS APPLIQUÉS SUITE À CE RAPPORT — 2026-08-01

Tous déployés en production (`pronos.rboost.link`) et couverts par des tests.

## Sélection — leviers mesurés

| Réglage | Valeur | Justification chiffrée |
|---|---|---|
| `MAX_BOOK_ODDS` | **2.22** | Rupture nette à 45 % de proba implicite. Les 43 paris sous ce seuil rendent **−47,3 %** (t=−2,98, p=0,005) et pèsent **−12,0** des −16,9 pts de ROI. Pilote sur la **cote**, donc insensible à toute erreur de calibration. |
| `ALLOW_TOTALS_OVER` | **0** | Over sur totals : 28,1 % de réussite contre 53,0 % implicite (n=32, ROI −54,5 %, t=−3,68). Perd dans les 2 modèles et les 3 tranches de cote. Les **Under sont conservés** : ils sont au baseline poissonien et ne sont pas démontrés perdants (t=−0,63). |
| `DERIVE_DNB` | **0** | DNB est dérivé par **ratio** → amplifie l'erreur (surconfiance +17,95 pts) ; DC par **somme** → l'amortit (+0,53 pt, seul marché où le modèle bat le prix). Nouveau drapeau distinct de `DERIVE_DC_DNB` : **on coupe DNB, on garde DC**. |
| `MAX_VALUE_EDGE` | **0.0** (désactivé) | ⚠️ **Correction d'une décision antérieure.** J'avais posé un plafond à 0,15 sur la foi d'un tableau par tranche d'edge. L'analyse l'a réfuté : corr(value_edge, P&L) = **−0,036**, et la « malédiction du vainqueur » était un artefact de la confondante cote (corr(edge, cote)=+0,663). Un plafond aurait coupé `h2h` et `double_chance`, qui gagnent à edge élevé. Le mécanisme reste dans le code, opt-in. |
| `KELLY_EDGE_CAP` | **0.10** | `reliability` était structurellement neutralisé : corr(reliability, kelly_stake) = **+0,0034**, parce que corr(reliability, edge) = −0,8311 annulait exactement le numérateur de Kelly. Plafonner l'edge que Kelly utilise **avant** la pondération redonne le dernier mot à `reliability`. Effet mesuré : un pick edge 50 %/fiabilité 0,225 passe de 2,81 € à **0,56 €** ; un pick sain edge 5 %/fiabilité 0,95 est inchangé. |

## Modèle — causes racines corrigées

| # | Défaut | Correctif |
|---|---|---|
| 1 | **Pondération temporelle inversée** : `parse_match_results` renvoie l'ordre chronologique ascendant, `_exp_weight(0)=1.0` donnait donc tout le poids au match le PLUS ANCIEN (écart mesuré 0,148 sur `attack_home`, max 0,438). | Le tri par date décroissante est désormais **imposé dans `build_team_stats`** plutôt que supposé du caller — corrige d'un coup `main.py`, `backtest.py` et `tuning.py`, qui le violaient dans les deux sens. |
| 2 | **Prior ELO mal branché** : `elo_win_probability` renvoie le **score espéré** (P(V)+½·P(N)) mais était consommé comme P(ne pas perdre) (P(V)+P(N)). Le déficit de ½·P(N) était reversé à l'extérieur, sur 100 % des matchs. Corroboration : 14 picks extérieur (4V/8D) contre 9 domicile (4V/0D). | Renommé `elo_expected_score`, ajout de `elo_no_loss_probability`, conversion `E + 0.5·d` au point d'usage avec le nul Poisson. Sur deux équipes égales à 26 % de nuls : 0,50 → **0,63**. |
| 3 | **DNB annulait la marge du book** : `(q1+q2)/q1` est un **ratio** d'implicites, la marge s'y annule exactement — prix true-fair qu'aucun book n'offre. DC somme les implicites et la conserve. | Division par l'overround de la source : `(q1+q2)/(q1·S)`. DNB et DC portent désormais le même facteur de conservatisme `S`. Sans constante magique : un book serré prend une petite décote, un book large une grande. |
| 4 | **Lignes 1X2 placeholder** : 25 lignes DNB à cote exactement 2,000, toutes Betfair, edge moyen 57,8 %, **0/25 résolues**. Le garde-fou médiane×1,20 échoue précisément quand plusieurs books publient le même placeholder. | Rejet **avant dérivation** de tout 1X2 dont `o1` et `o2` diffèrent de moins de 2 %. |

## Instrumentation

| # | Défaut | Correctif |
|---|---|---|
| 5 | **0/310 lambda persistés.** `bet_to_dict` dans `recommend.py` supprimait `lambda_home`/`lambda_away` avant historisation. `main.py` les transmettait, mais avec `SCAN_HOURS` vide ce chemin ne tourne jamais : 100 % des picks passaient par celui qui perd l'information. | Ajoutés aux **3** constructeurs de dict (`manual`, `live`, `agent-local`) et à `_historize_picks`. Les buts attendus du modèle redeviennent auditables. |
| 6 | **Paris annulés comptés en pertes sèches** dans `get_roi_stats` : mise au dénominateur, jamais remboursée. Affichait −18,6 % au lieu de −6,4 %, et 43,2 % de réussite au lieu de 47,6 %. | Population restreinte à win/loss, annulations exposées en `n_void`. |
| 7 | **Couverture de notation invisible** — un ROI sur 15 % de la production se lisait comme un track record. | `/stats/model-performance` renvoie `coverage`, et le dashboard masque/avertit sous 80 %. Agrégations **par marché** et **par type de modèle** ajoutées (le découpage ligue×marché fragmentait en n=1..5, illisible). |
| 8 | **Brier modèle vs marché jamais affiché** — le test le plus sévère n'était nulle part. | Bandeau de verdict en tête du diagnostic : le modèle bat-il `1/best_odds`, marge incluse ? |

## Interface

- L'onglet « Mes picks » décrivait un fonctionnement disparu (« aucun argent n'a été engagé », « archivage à 36 h »). Réécrit selon le mode réel, exposé par `/health` (`auto_confirm_picks`, `bookmaker_whitelist`, `clv_snapshot_enabled`).
- Le message CLV affirmait qu'il « s'activera automatiquement » alors que le snapshot est désactivé. Remplacé par un avertissement explicite sur ce que son absence coûte.

## Non appliqué — décision de l'utilisateur

`SCAN_ALL_SOCCER=1` **conservé** : l'utilisateur dispose de plusieurs comptes Odds API et fait tourner les clés à l'épuisement du quota. La recommandation C6 du rapport était de toute façon étiquetée « par prudence de périmètre, pas parce que c'est prouvé ».

---

## 1. La cause racine, en 5 lignes

1. **`model_prob` ne contient aucune information que la cote ne contienne déjà — elle en contient moins.** Brier apparié modèle − cote = **+0,01140** (n=170, t=+2,25, p≈0,026) : le modèle est *significativement* battu par la simple lecture du prix.
2. **Le poids optimal du modèle dans un mélange modèle/marché est nul, voire négatif** : w=0 → Brier 0,22419 ; w=0,1 → 0,22487 ; w=1,0 → 0,23559 ; et w=−0,2 → **0,22315**, meilleur que w=0. S'éloigner du modèle améliore la prévision.
3. **Le filtre de sélection amplifie cette erreur au lieu de la corriger.** `edge = p × cote − 1` est mécaniquement plus facile à franchir quand la cote est longue : corr(edge, cote) = **+0,437**, corr(edge, model_prob) = **−0,391**. Le bot ne sélectionne pas les paris où il est bon, il sélectionne ceux où il s'écarte le plus d'un prix qui a raison contre lui.
4. **Conséquence mesurée** : les 43 paris sur des sélections que le marché price sous 45 % rendent **−47,3 %** (t=−2,98, p≈0,005) et pèsent −12,0 des −16,92 points de ROI global.
5. **Et le prix de référence n'existe pas pour vous** : 2 picks sur 310 (0,65 %) sont cotés chez Betclic ou Bet365, et il faudrait des cotes **20,36 % plus hautes** (k_breakeven = 1,2036) pour seulement atteindre le point mort. Aucun line-shopping ne sauve cette stratégie.

**En une phrase : le bot ne perd pas parce que ses données sont incomplètes, il perd parce qu'il parie systématiquement contre un signal (la cote) meilleur que le sien, et qu'il le fait d'autant plus fort qu'il a plus tort.**

---

## 2. La chaîne causale

### 2.1 Cause PREMIÈRE (une seule)

**Le modèle est informationnellement vide face au marché.** C'est le seul constat du lot qui soit à la fois significatif, robuste au découpage par `model_type` (blended : Brier 0,2189 vs cote 0,2111 ; consensus : 0,2601 vs 0,2433 — battu dans les deux) et non explicable par une variable de composition. Statut : **CONFIRMÉ**.

Tout le reste en découle.

### 2.2 Premier étage de propagation : le filtre d'edge oriente le flux vers la zone la plus fausse

Puisque `model_prob` est du bruit centré au-dessus du prix, `edge` est maximal là où le bruit est maximal, c'est-à-dire sur les cotes longues et les probabilités basses. La distribution des picks le prouve : 43 des 170 paris tranchés (25,3 %) sont sur des sélections à moins de 45 % implicite, alors que le modèle les jugeait à 46,6 % et que la réalité a livré **20,9 %**. Écart de calibration : **−25,7 points** sur les outsiders contre **+2,6 points** (non significatif, SE 7,1) sur les favoris. La surconfiance globale de +12,6 points est une moyenne qui masque deux régimes opposés. Statut : **CONFIRMÉ** côté outsiders ; **FRAGILE** côté favoris.

### 2.3 Deuxième étage : deux marchés amplifient l'erreur au lieu de l'absorber

- **`totals`, côté Over** : hit réel 28,13 % (n=32) contre 52,98 % annoncés par le marché lui-même. Brier apparié modèle−cote = +0,0261 (t=2,75, p=0,010). t sur le P&L = **−3,68**, IC95 du ROI = **[−79,1 % ; −24,1 %]** — le seul sous-portefeuille dont l'intervalle exclut zéro avec cette marge. Statut : **CONFIRMÉ**.
- **`draw_no_bet`** : la dérivation est un **ratio** `p1/(p1+p2)` alors que `double_chance` est une **somme** `p1+pX`. Les deux partent des mêmes probabilités 1/X/2 et donnent des résultats opposés : surconfiance **+0,53 pt** sur DC contre **+17,95 pts** sur DNB. Statut : **CONFIRMÉ**, et le test décisif a été fait — voir §2.5.

### 2.4 Troisième étage : la plomberie a laissé le système courir sans frein ni instrument

- `reliability` est censé réduire la mise des picks douteux. Il est **neutralisé** : corr(reliability, kelly_stake) = **+0,0034**, parce que corr(reliability, value_edge) = **−0,8311** annule exactement l'effet du numérateur de Kelly. Statut : **CONFIRMÉ** (propriété structurelle mesurée sur 310 picks, sans dépendance à un résultat sportif).
- Les garde-fous de portefeuille existent (`guards.py` : 20 % de mise/jour, 10 paris/jour, 30 % d'exposition) mais ne sont câblés que sur `confirm_placement`, chemin jamais emprunté : **733,22 EUR de notionnel sur 100 EUR de bankroll en 11 jours**, dont **219,30 EUR le seul 27/07**, et un `bankroll_ledger` qui ne contient qu'une ligne (dépôt de 100 EUR, solde inchangé). Statut : **CONFIRMÉ**.
- **0/310** `lambda_home`, **0/310** `closing_odds`, **0/310** `placed_bookmaker`. Aucun CLV mesurable, aucun audit possible du modèle de buts, aucune mesure de l'écart entre prix affiché et prix obtenu. Statut : **CONFIRMÉ**.
- **25 lignes DNB à cote exactement 2,000**, toutes chez Betfair, sur 25 événements distincts, edge moyen 57,8 %, **0/25 résolues**. Signature d'un 1X2 source parfaitement symétrique (ligne placeholder d'un marché non formé). Contrôle intra-jour décisif au 27/07 : DNB chez les books classiques **25/26 notés (96 %)**, DNB chez les exchanges **1/20 (5 %)** — même jour, même marché, même resolver. Statut : **CONFIRMÉ**.

### 2.5 Le piège de la variable confondante — ce qui est vrai et ce qui ne l'est pas

**⚠️ « Les totals perdent » n'est PAS « le consensus perd ».** J'ai exécuté la matrice complète marché × model_type :

| marché | model_type | n | ROI | surconfiance | Brier modèle | Brier cote |
|---|---|---:|---:|---:|---:|---:|
| totals | blended | 28 | **−28,75 %** | +19,43 pts | 0,2513 | 0,2376 |
| totals | consensus | 39 | **−31,41 %** | +20,85 pts | 0,2622 | 0,2420 |
| draw_no_bet | blended | 27 | **−23,26 %** | +16,26 pts | 0,1981 | 0,1754 |
| draw_no_bet | consensus | 22 | **−23,88 %** | +20,02 pts | 0,2970 | 0,2780 |
| double_chance | blended | 31 | −0,44 % | +3,61 pts | 0,2058 | 0,2075 |
| double_chance | consensus | 7 | +24,14 % | −13,12 pts | 0,1394 | 0,1539 |
| h2h | blended | 15 | +16,80 % | +0,59 pt | 0,2226 | 0,2335 |
| h2h | consensus | 1 | −100 % | +45,80 pts | — | — |

**Les deux marchés destructeurs perdent de façon quasi identique dans les DEUX modèles** (écart de 2,7 points sur totals, de 0,6 point sur DNB). L'effet est **marché**, pas **model_type**. Confirmation par l'autre bout : le mauvais ROI global du consensus (−24,37 %, n=69) vient de sa surreprésentation dans totals/DNB, puisque sur DC+h2h le consensus fait **+8,6 %** (n=8). Et le test direct blended (−11,83 %, n=101) contre consensus (−24,37 %, n=69) donne **t=0,889, p=0,375** : **non détectable**.

**En revanche, quatre confondantes mordent réellement et doivent être déclarées :**

1. **« Le modèle est calibré au-dessus de model_prob 0,65 » est un filtre de COTE COURTE, pas un filtre de modèle.** Le segment `model_prob ≥ 0,65` fait +6,86 % (n=58). Mais un filtre purement marché, sans aucun modèle : `best_odds ≤ 1,60` → **+6,41 % (n=49)** ; `implicite ≥ 0,62` → **+7,51 % (n=50)**. Indistinguables. Le modèle n'apporte rien au-delà du prix, ici non plus. Statut du constat d'origine : **FRAGILE**, et sa lecture correcte est « les favoris à cote courte ont sur-performé sur 11 jours ».
2. **« Le ROI décroît avec la cote » et « les sélections sous 45 % implicite perdent » sont le MÊME fait**, à la transformation `implicite = 1/cote` près. Ne pas les additionner.
3. **Le portefeuille en attente n'est pas « structurellement plus toxique »** : hors les 25 placeholders, notés et en attente sont statistiquement jumeaux (edge 13,63 % contre 15,09 %, cotes 1,913 contre 1,943). Les trois constats qui décrivent ce phénomène décrivent un seul défaut : le batch de 25 lignes dégénérées.
4. **Le « régime de gros volume » n'existe pas** : hors 27/07, l'écart tombe de 19,35 à 6,5 points, et le test donne t=1,36, p=0,17. Le 27/07 pèse 71,4 % de la perte, mais c'est une identité comptable ex post, pas un facteur.

**Et une hypothèse populaire qui est réfutée : le trou de couverture `team_stats` n'explique pas l'échec.** Les 143 paris sur des ligues **dotées** de stats perdent **−15,75 %** (contre −23,07 % sans stats, écart t=0,363, p=0,72), et le segment sans stats ne pèse que 27 paris et −6,23 unités sur −28,76. Avoir des stats **ne protège pas**. Statut : **CONFIRMÉ**. Corollaire : investir dans l'ajout de ligues à `team_stats` est un levier démontré non prioritaire.

---

## 3. Tableau des causes, trié par impact ROI

Le ROI global est de **−16,92 %** (−28,759 u sur 170 paris). Les contributions sont exprimées en points de ce ROI (P&L du segment / 170).

### Bloc A — décomposition par marché (additive et exhaustive : la somme fait exactement −16,92)

| # | Cause | Preuve chiffrée | n | Impact ROI | Robustesse |
|---:|---|---|---:|---:|---|
| 1 | **Sélections Over sur `totals`** | hit 28,13 % contre 52,98 % implicite marché ; t=−3,68 ; IC95 [−79,1 ; −24,1] ; perd dans les 2 modèles (−57,9 % blended / −45,4 % consensus) et dans les 3 tranches de cote | 32 | **−9,72 pts** | **CONFIRMÉ** |
| 2 | **Marché `draw_no_bet` (dérivation par ratio)** | surconfiance +17,95 pts ; à probabilité appariée 0,60–0,80, 11,6 des 17,4 points d'écart avec DC survivent ; Brier 0,2425 vs cote 0,2214 | 49 | **−6,78 pts** | **CONFIRMÉ** (mécanisme) ; ROI seul t=−1,86, p≈0,07 |
| 3 | Sélections Under sur `totals` | ROI −10,80 % ; au baseline poissonien de ligue (51,2 % attendu, 55,6 % observé) : pas de biais de signal, la perte vient du **prix** | 35 | −2,22 pts | **FRAGILE** (t=−0,63) |
| 4 | `double_chance` (contribution positive) | ROI +4,09 % | 38 | +0,91 pt | **FRAGILE** (t=0,34) |
| 5 | `h2h` (contribution positive) | ROI +9,50 % | 16 | +0,89 pt | **FRAGILE** (n<20, t=0,32) |

### Bloc B — découpages alternatifs des MÊMES 170 paris (⚠️ non cumulables avec le bloc A ni entre eux)

| # | Cause | Preuve chiffrée | n | Impact ROI | Robustesse |
|---:|---|---|---:|---:|---|
| 6 | **Sélections que le marché price sous 45 %** | réel 20,93 % contre 39,10 % implicite ; t=−2,98, p≈0,005 ; jackknife top-3 → −64,5 % (empire) ; négatif dans 5 des 6 sous-groupes marché×modèle | 43 | **−11,96 pts** | **CONFIRMÉ** |
| 7 | Bloc Amérique du Sud | ROI −27,09 % ; pire dans chaque marché (totals −48,1 % / DNB −47,7 %) ; pas d'outlier | 78 | −12,43 pts | **FRAGILE** (écart contre le reste : t=1,36, p=0,175) |
| 8 | Journée du 27/07 | ROI −33,11 %, 71,4 % de la perte nette | 62 | −12,07 pts | **FRAGILE** (t=−1,83 ; mécanisme proposé contredit par la stratification interne) |
| 9 | Picks empilés (≥2 sélections d'un même marché sur un event) | ROI −41,11 % contre −11,31 % | 32 | −7,74 pts | **FRAGILE** (effet porté à 100 % par 18 picks `totals` ; DC empilés +5,4 %, DNB empilés −13,5 %) |
| 10 | Ligues hors des 10 câblées (`SCAN_ALL_SOCCER`) | 93,87 % du volume ; Brier apparié modèle−cote +0,01672 (t=3,41, p<0,001) | 157 | −21,29 pts | **FRAGILE** (le segment « câblé » de comparaison ne fait que 13 paris, dont 6 d'une seule ligue) |

### Bloc C — causes structurelles sans ROI directement attribuable

| # | Cause | Preuve chiffrée | n | Impact | Robustesse |
|---:|---|---|---:|---|---|
| 11 | **`model_prob` sans information résiduelle** | Brier apparié +0,01140, t=+2,25, p=0,026 ; poids optimal du modèle ≤ 0 | 170 | **cause racine de la totalité des −16,92 pts** | **CONFIRMÉ** |
| 12 | **Prix de référence inaccessible** | 2/310 chez Betclic/Bet365 ; 153/310 chez un exchange ; k_breakeven = 1,2036 | 310 | −4,15 pts sous décote de 5 % (hypothèse, non mesurée) | **CONFIRMÉ** |
| 13 | **`reliability` neutralisé dans le sizing** | corr(reliability, kelly_stake) = +0,0034 ; corr(reliability, edge) = −0,8311 | 310 | n.q. — garde-fou inopérant | **CONFIRMÉ** |
| 14 | **25 lignes DNB placeholder à cote 2,000** | 25 events distincts, edge 57,8 %, 0/25 résolues ; contrôle intra-jour 27/07 : 96 % vs 5 % de notation | 25 | n.q. (0 tranché) — pollue edge moyen ET taux de résolution | **CONFIRMÉ** |
| 15 | **Garde-fous de bankroll non câblés** | 733,22 EUR engagés sur 100 EUR ; 219,30 EUR le 27/07 ; ledger = 1 ligne, solde 100,00 | 310 | risque de ruine, pas de ROI | **CONFIRMÉ** |
| 16 | **Instrumentation morte** | 0/310 lambda, 0/310 closing_odds, 0/310 placed_bookmaker | 310 | n.q. — rend le diagnostic impossible | **CONFIRMÉ** |
| 17 | **Label `blended` trompeur** | 426/426 équipes ont un Elo, 94/426 (22,1 %) ont un xG ; xG concentré sur 6 ligues européennes | 426 | n.q. — a fait croire que 127 picks bénéficiaient d'un modèle riche | **CONFIRMÉ** |
| 18 | Taux de notation à 61,9 % | 118 picks en attente ; API scores plafonnée à 3 jours (`api.py:255`, `resolver.py:138`) | 310 | ROI réel entre −16,0 % et −19,0 % selon hypothèses | **FRAGILE** (quantification) |
| 19 | Void DNB à 30,99 % | 22 void / 71 notés ; z=1,16 contre 25 % de référence, p=0,24 | 71 | **0 pt — fausse piste, le void est ROI-neutre** | **CONFIRMÉ** (constat négatif) |

---

## 4. Ce qui marche — et l'avertissement qui va avec

**Aucun segment de ce portefeuille n'est démontré rentable.** Tous les intervalles de confiance des segments positifs englobent zéro. Voici ce qui est néanmoins exploitable comme point de reconstruction, par ordre de solidité.

| Segment | n | ROI | Calibration | Ce qui est établi | Ce qui ne l'est pas |
|---|---:|---:|---|---|---|
| **`double_chance`** | 38 | +4,09 % | surconfiance **+0,53 pt** (meilleure du portefeuille) ; Brier 0,1936 **< 0,1977** de la cote | **La calibration.** C'est le seul marché où le modèle n'est pas battu par le prix. La dérivation par **somme** `p1+pX` compense les erreurs au lieu de les amplifier. | La rentabilité : t=0,34, IC95 **[−19,3 % ; +27,5 %]**, et jackknife top-3 → **−3,94 %**. Le +4,09 % est porté par 7 picks consensus (+24,1 %) ; DC/blended, qui fait 31 des 38, rend **−0,44 %**. |
| **`h2h`** (marché source) | 16 | +9,50 % | surconfiance **+0,59 pt** en blended ; Brier 0,2226 < 0,2335 | Le marché **source** est calibré, contrairement à ses dérivés. C'est cohérent et informatif. | **n<20, INDICATIF SEULEMENT.** IC95 [−48,8 % ; +67,8 %], jackknife top-3 → −28,77 %. Aucune décision de mise. |
| **Favoris / cotes courtes** | 47 | +7,54 % (`best_odds<1,60`) | réel 73,68 % contre 71,11 % implicite sur `implicite≥65 %` | Le bot **n'y perd pas de façon détectable**. | Qu'il y **gagne** : l'écart +2,6 pts a une erreur-type de ±7,1 pts (t=0,36). Et le balayage de seuil est **plat** entre −6,6 % et +7,5 % : le seuil 1,60 est choisi a posteriori. Surtout, **c'est un signal de marché, pas de modèle**. |
| **Les 10 ligues câblées** | 13 | +57,1 % | Brier 0,1317 contre 0,1846 | Rien. | **FRAGILE / non établi.** 6 des 13 paris sont de l'EFL Championship, gagnants 6/6, portant +7,30 des +7,43 u. Hors EFL : Brier modèle 0,0990 contre cote 0,0997 — l'avantage **disparaît** (t=−0,76, p=0,47). |
| **`reliability ≥ 0,90`** | 36 | −5,51 % | gradient monotone dans le bon sens | La mesure est directionnellement informative. | Elle reste **négative**, et corr(reliability, best_odds) = −0,572 : c'est encore le même axe de prix déguisé. |

**Le meilleur portefeuille filtré que j'aie pu construire et tester** (requête exécutée) : couper les Over sur totals **et** toute sélection sous 45 % d'implicite marché → **n=101, P&L +2,11 u, ROI +2,09 %**, contre −28,76 u et −16,92 % au réel. Mais **t=0,26**, et en retirant les 3 plus gros gains le ROI retombe à **−1,40 %**. Conclusion sans complaisance : **on arrête l'hémorragie, on ne gagne pas.**

Détail utile de ce portefeuille filtré : `double_chance` +13,01 % (n=35), `h2h` +15,75 % (n=8), `draw_no_bet` −5,75 % (n=37), `totals` restants −7,52 % (n=21) ; blended +13,12 % (n=59) contre consensus −13,41 % (n=42) — directionnel, non significatif.

---

## 5. Plan d'amélioration, par gain attendu

### Priorité 1 — Arrêter de perdre (gains mesurés, effort minimal)

**A1. Couper toute sélection Over sur `totals` (O0.5/O1.5/O2.5/O3.5).**
- Gain mesuré : **+8,05 points** (ROI −16,92 % → **−8,87 %**, n=138).
- Effort : une ligne de filtre. Aucun effet de bord.
- Vérification : 0 pick Over émis ; re-mesurer le côté Under à n≥30 avant de décider de son sort (il n'est pas démontré perdant : t=−0,63, et son hit est au baseline poissonien de ligue).

**A2. Rejeter toute sélection dont la probabilité implicite du marché est inférieure à 0,45** (soit `best_odds > 2,22`).
- Gain mesuré **en combinaison avec A1** : n=101, **ROI +2,09 %** — soit **+19,0 points** par rapport au réel. A2 seul : −6,63 % (n=127), soit +10,3 points.
- ⚠️ Le seuil est **45 %, pas 50 %** : la tranche 45–55 % rend −7,9 % (n=37), *mieux* que la tranche 55–65 % (−13,8 %, n=52). Il n'y a pas de dégradation monotone, il y a **une seule rupture**, à 45 %.
- Effort : une ligne. Se pilote sur la **cote**, pas sur le modèle — donc insensible à toute erreur de calibration.
- Vérification : cote maximale émise ≈ 2,20 ; re-mesurer à n≥100 nouveaux paris.

**A3. Suspendre la dérivation `draw_no_bet`, et interdire inconditionnellement la combinaison `draw_no_bet` × `consensus`.**
- Gain à plat : **+2,68 points** (n=121, −14,24 %). Mais le vrai argument est l'exposition : DNB représente **44,8 % des picks**, **49 % du Kelly engagé** (357,2 sur 733,2) et **100 % des 22 void**.
- Preuve du mécanisme : à probabilité appariée (0,60–0,80), DC surconfiance +2,3 pts / ROI +2,6 % (n=29) contre DNB +13,9 pts / −9,9 % (n=30). **11,6 des 17,4 points d'écart survivent à l'appariement** — ce n'est pas un effet de niveau de probabilité, c'est la dérivation elle-même.
- Le garde-fou `not _is_consensus` existe dans `analysis.py` mais **88 DNB consensus** sont en base : le chemin d'écriture le contourne. À corriger, pas à ajouter.
- Vérification : 0 pick DNB émis ; si réintroduit un jour, exiger que `p_X` soit elle-même validée.

**A4. Rejeter les lignes dérivées dégénérées.**
- Refuser tout 1X2 source dont `o1` et `o2` diffèrent de moins de 2 % (placeholder symétrique) **avant** dérivation ; rejeter tout groupe de sélections dont la somme des implicites au meilleur prix descend sous 1,00 ; remplacer le garde-fou `médiane × 1,20` (qui échoue précisément quand plusieurs books publient le même placeholder) par un test de dispersion des prix sources.
- Gain : non quantifiable en ROI (les 25 lignes concernées ne sont pas notées), mais elles polluent l'edge moyen **et** le taux de résolution, et représentent 55 EUR d'exposition fantôme.
- Vérification : 0 pick à cote DNB exactement 2,000 ; 0 groupe à overround < 100 %.

### Priorité 2 — La seule action qui peut rendre le système positif (effort lourd)

**B1. Cesser de traiter `model_prob` comme une probabilité indépendante. Reconstruire autour du prior marché.**
- `p_finale = devig(consensus)` puis **ajustement borné** par le modèle (déplacement maximal ±3 points, jamais de remplacement).
- Justification : c'est le seul remède à la cause racine. Tant que le poids optimal mesuré du modèle est ≤ 0, tout pick engendré par un désaccord modèle/marché est une perte espérée.
- Gain : **non quantifiable ex ante**. Ce qui est certain, c'est que sans cela les filtres A1–A3 plafonnent autour de 0 %.
- **Test d'acceptation** : le Brier apparié (modèle − cote), calculé en glissant sur les 200 derniers picks résolus, doit devenir **négatif**. Il vaut aujourd'hui +0,01140.
- Corollaire immédiat : une **recalibration** doit être refittée en glissant, **jamais en in-sample**. La correction affine `p_corr = 1,76p − 0,60` mesurée sur les 170 picks a un R² de 0,159 et une courbe **non monotone** (le bucket 0,60–0,65, le plus gros à n=43, a un écart de +21,0 pts, deux fois celui du bucket 0,55–0,60 juste en dessous) : c'est un lissage, pas une loi.

### Priorité 3 — Réparer les instruments (effort faible, vérifiable sans attendre de résultats)

**C1. Rebrancher `reliability` sur la mise.** Plafonner l'edge utilisé dans Kelly à `min(value_edge, 0.10)` **avant** de multiplier par `reliability`, pour que la pénalité ne soit plus annulée par le numérateur.
- Test d'acceptation immédiat, sans attendre un seul résultat sportif : `corr(reliability, kelly_stake)` doit passer de **+0,0034** à nettement positive.

**C2. Déplacer les garde-fous de `guards.py` du stade *confirmation* au stade *génération*.** Le scan doit cesser d'émettre dès que la somme des `kelly_stake` du jour atteint 20 % de la bankroll ou 10 picks. Interdire toute mise à jour de `placement_status` hors de `confirm_placement`, et rapprocher le ledger des 310 confirmations existantes.
- Vérification : `sum(kelly_stake)` par jour ≤ 20 EUR ; `bankroll_ledger` contient une ligne `bet_placed` par pick confirmé.

**C3. Rendre le prix auditable.** (a) Persister `lambda_home`/`lambda_away` — vérifier que `evaluate_picks` conserve bien ces clés dans `eval_result['picks']`, le chemin de persistance `_historize_picks` les transmet déjà. (b) Enregistrer `closing_odds` pour mesurer le CLV. (c) **Rendre obligatoire la saisie de `placed_bookmaker` et de la cote réellement obtenue** à la confirmation.
- Sans (c), l'hypothèse de décote de 5 % (−4,15 pts) restera une hypothèse de travail. Vérification : taux de non-nul > 90 % sur les trois colonnes.
- ⚠️ `BOOKMAKER_WHITELIST=betclic,bet365` est **déjà appliquée** en production depuis le 01/08 12:56. Surveiller l'effet de bord : avec 2 books sur 23, le volume de picks va s'effondrer et l'edge moyen chuter mécaniquement. C'est normal et souhaitable.

**C4. Vider les 118 picks zombies.** L'endpoint scores est plafonné à 3 jours (`api.py:255`, `resolver.py:138`) : les 110 picks de plus de 4 jours sont définitivement non notables par ce chemin. Faire tourner le resolver toutes les 12 h et écrire un backfill via une source historique.
- Règle de communication : **ne publier aucun chiffre de ROI tant que le taux de notation reste sous 80 %** (il est à 61,9 %), et toujours afficher l'edge moyen des picks en attente à côté.

**C5. Nettoyer l'instrumentation du modèle** (aucun gain de ROI, mais tout suivi futur en dépend) :
- Réserver le label `blended` aux cas où le **xG est réellement présent** ; introduire `elo_only` pour les 332 équipes (77,9 %) qui n'ont qu'un Elo. Sans cela, aucun suivi par `model_type` n'est interprétable.
- Ajouter une **table d'alias `sport_key`** : `soccer_france_ligue_one` → `soccer_france_ligue1`, `soccer_uefa_champs_league_qualification` → `soccer_uefa_champs_league`. Aujourd'hui les 18 lignes de stats Ligue 1 (dont 16 avec xG, les meilleures données du système) sont inatteignables. Coût nul aujourd'hui (n=1 pick), **coût majeur dès septembre**. Ajouter un test d'intégrité qui échoue si un `sport_key` parié n'existe ni dans `team_stats` ni dans la table d'alias.
- Une seule sélection par `(event_id, market)`, et traiter les jambes d'un même event comme un bloc unique dans le dimensionnement des mises. Justification : **risque de corrélation** (le bot a parié les deux côtés du même marché binaire sur 4 events), pas les 30 points de ROI annoncés, qui sont un artefact.

**C6. `SCAN_ALL_SOCCER=0`** — à faire par **prudence de périmètre**, pas parce que c'est prouvé. Le fait de couverture est solide (93,87 % du volume hors périmètre câblé, Brier apparié +0,01672 avec t=3,41 sur ce segment) ; l'inférence « le modèle n'est pas cassé, il est mal évalué » repose sur 6 paris et ne survit pas au leave-one-league-out. À réévaluer en septembre.

### Ce qu'il ne faut PAS faire

- **Ne pas ajouter de plafond d'edge global.** corr(value_edge, P&L) = **−0,036**, indistinguable de zéro. Le filtre d'edge ≤ 20 % ne rapporte que **+1,94 point** et couperait au passage `h2h` et `double_chance`, qui gagnent à edge élevé. La « malédiction du vainqueur » agrégée est un artefact de la confondante cote (corr(edge, cote)=+0,663).
- **Ne pas plafonner l'edge spécifiquement sur DNB** : la « monotonie sur 3 tertiles » est un gradient de `model_type` déguisé (T1 = 14 consensus/3 blended, T3 = 16 blended/0 consensus ; t=0,83).
- **Ne pas inverser le signal Over.** Parier l'inverse d'un modèle cassé reste du bruit, et le +35,8 % simulé repose sur une marge de 5 % supposée, jamais observée (0/310 `closing_odds`).
- **Ne pas surpondérer l'EFL Championship** sur la foi de 6 paris. Exiger n≥30.
- **Ne pas modifier la planification horaire des scans** : chaque tranche horaire correspond à 1 ou 2 scans précis, la variable est non identifiable. Le −29,47 % de la tranche 19 h *est* le second scan du 27/07 et rien d'autre ; la tranche 08 h passe de −3,89 % à **+30,03 %** dès qu'on retire ce jour.
- **Ne pas attribuer la perte DNB au taux de nul** : 30,99 % contre 25 % de référence donne z=1,16, p=0,24, et le void est **ROI-neutre**. Toujours publier le ROI DNB hors void (−23,54 %) à côté du ROI dilué (−16,24 %).
- **Ne pas investir dans l'extension de `team_stats`** en espérant redresser le ROI : les 143 paris sur des ligues dotées de stats perdent −15,75 %.

---

## 6. Ce que les données ne permettent PAS de conclure

**Le portefeuille est trop petit pour prouver quoi que ce soit de positif.** Avec un écart-type du P&L par pari de ≈0,90 :
- détecter un ROI de **+10 %** au seuil de 5 % exige **n ≈ 310 paris tranchés** ;
- détecter un ROI de **+5 %** exige **n ≈ 1 250** ;
- détecter une **différence de 10 points** entre deux segments exige **n ≈ 620 par segment**.

Nous en avons 170 au total. Toutes les questions ci-dessous restent ouvertes.

1. **Le bot est-il rentable quelque part ?** Non tranché. `double_chance` : IC95 **[−19,3 % ; +27,5 %]** (n=38). `h2h` : **[−48,8 % ; +67,8 %]** (n=16). Portefeuille filtré A1+A2 : t=0,26 (n=101). *Volume nécessaire : ~300 paris tranchés par marché.*
2. **`blended` vaut-il mieux que `consensus` ?** Écart observé 12,5 points, t=0,889, p=0,375. La **MDE** du test est de **27,6 points de ROI** : le test ne pouvait rien détecter. La formulation correcte est « non détectable à ce n », pas « démontré non significatif ». *Volume nécessaire : ~600 par groupe.* Et tant que le label `blended` est déclenché par le seul Elo (C5), le test restera ininterprétable.
3. **Les 10 ligues câblées sont-elles meilleures ?** Non tranché : 13 paris, dont 6 d'une seule ligue. Hors EFL, l'avantage de Brier disparaît (0,0990 contre 0,0997, t=−0,76). *Volume nécessaire : la reprise de septembre, n≥100 sur ce périmètre.*
4. **L'Amérique du Sud est-elle réellement pire ?** Écart de 18,80 points contre le reste, mais **t=1,362, p=0,175**. Le signal directionnel est réel (l'AmSud est pire *dans* chaque marché, ce n'est pas un effet de mélange), l'écart n'est pas établi. *Volume nécessaire : ~250 paris sud-américains.*
5. **Le bug d'heuristique `−2·ln(p_nul)` cause-t-il le biais Over ?** **Inférence non vérifiée.** Le bug est confirmé par lecture du code (`models.py:472-485` : la somme des lambdas ne dépend QUE de la probabilité de nul), mais 0/310 lambda en base interdit de le mesurer. La reconstruction par inversion numérique montre que les lambdas *implicites en sortie* sont proches des moyennes de ligue (écarts de +0,02 à +0,22 but) — donc le bug est **largement amorti par la couche de calibration** et n'est probablement pas l'explication principale. Mais cela ne dit rien des lambdas **bruts en entrée**. *Nécessite C3(a).*
6. **Quel est le coût réel du prix inaccessible ?** Inconnu. 0/310 `placed_bookmaker`, 0/310 `closing_odds`. Le scénario −5 % (−4,15 pts) est une **hypothèse de travail, pas une mesure**. Ce qui est certain, c'est que k_breakeven = 1,2036 : même un accès parfait à Betfair ne comble pas le trou. *Nécessite C3(c) et ~100 paris réellement placés.*
7. **Quel est le ROI réel à maturité ?** Entre **−16,0 % et −19,0 %** selon les hypothèses de résolution du portefeuille en attente. La fourchette varie du simple au quadruple selon le ROI qu'on prête aux 30 picks `consensus × edge ≥ 20 %` (un seul est tranché, à −100 %), et l'oubli du void sur les 68 DNB en attente (≈21 remboursements attendus) déplace l'estimation de 1 à 2,5 points vers le haut. *Nécessite C4.*
8. **Le « book synthétique » à marge négative est-il systématique ?** **n=1.** Une seule paire de jambes complémentaires vraiment simultanée (overround 97,30 %). Les deux autres sont créées à 24 h d'écart et mesurent le mouvement de ligne, pas l'overround. Le mécanisme est arithmétiquement plausible mais ne vaut que ≈1,2 point d'edge apparent par pick, contre 12,26 points revendiqués : le line-shopping n'explique **au mieux que 10 à 15 %** de l'edge fantôme. Les ~10 points restants sont de la miscalibration. *Nécessite d'enregistrer l'overround du marché source à chaque scan.*
9. **L'empilement nuit-il en dehors de `totals` ?** Non. L'effet global de −29,8 points est porté à 100 % par 18 picks `totals` ; `double_chance` empilés font **+5,36 %** (n=10) et `draw_no_bet` empilés **−13,50 %** (n=4). Et le mécanisme invoqué est réfuté : les paires de **même** marché ont une concordance de **50,00 %** (8/16, soit l'indépendance parfaite), ce sont les paires de marchés **différents** qui portent la surconcordance (60,78 %, 62/102). *Volume nécessaire : ~150 events multi-picks.*
10. **Le volume par scan est-il un facteur ?** Non établi : t=1,36, p=0,17, et l'écart tombe de 19,35 à 6,5 points dès qu'on retire le 27/07. De plus corr(volume, nombre de ligues) = **+0,907** : « volume » et « périmètre » sont la même variable, non séparables. *Nécessite un protocole où le volume est fixé indépendamment du périmètre.*
11. **L'heure de scan a-t-elle un effet ?** **Non identifiable**, et ce ne sera pas tranchable tant que chaque créneau ne comptera pas plusieurs dizaines de scans distincts sur plusieurs semaines, à périmètre de ligues gelé.

---

### Le mot de la fin

Trois faits sont établis au-delà du doute raisonnable, et ce sont les seuls sur lesquels décider :

1. **Le modèle est significativement moins bon que la cote** (Brier apparié +0,01140, t=2,25) — et le poids optimal qu'il faudrait lui donner est négatif.
2. **Deux poches détruisent le capital** : les Over sur `totals` (t=−3,68, IC95 excluant zéro) et les sélections que le marché price sous 45 % (t=−2,98).
3. **Le système n'a jamais eu de frein** : 733 EUR engagés sur 100 EUR de bankroll, garde-fous non câblés, ledger vide.

Tout le reste — y compris les segments qui « gagnent » — est du bruit à ce volume. Coupez d'abord (A1–A4, C1–C2), instrumentez ensuite (C3–C5), et ne rebâtissez le modèle que sur un prior marché (B1). Ne recommencez à publier un ROI qu'au-delà de 80 % de notation et de 300 paris tranchés.