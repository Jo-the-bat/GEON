# Backtesting des détecteurs GEON

Le harness (`ingestors/backtest/runner.py`) rejoue les détecteurs à
horloge injectable sur l'historique déjà présent dans Elasticsearch,
jour par jour, et mesure ce qu'ils auraient détecté — c'est l'outil de
réglage honnête des seuils (`ingestors/config.yaml`).

## Détecteurs rejouables

| Détecteur | Source | Ce qu'il mesure |
|---|---|---|
| `escalation` | Règle 1 (couche géopolitique) | Escalades diplomatiques GDELT validées par la baseline statistique de la paire (z-score). La moitié APT est volontairement exclue : OpenCTI n'a pas d'état historique rejouable. |
| `rhetoric` | Règle 4 complète | Bascules de tonalité médiatique (écart en sigmas à la baseline 30 j). |

## Utilisation

```bash
# Dans le conteneur ingestor — borne les dates à la couverture GDELT réelle
docker exec geon-ingestor python -m backtest.runner \
    --start 2026-04-15 --end 2026-06-10 --output /tmp/backtest.json

# Un seul détecteur, pas de 2 jours
docker exec geon-ingestor python -m backtest.runner \
    --start 2026-04-15 --end 2026-06-10 \
    --detectors rhetoric --step-days 2
```

Le rapport regroupe les détections quotidiennes en **épisodes**
(même sujet, jours consécutifs — mêmes sémantiques que les situations
du moteur), puis :

- compare les épisodes à la **vérité terrain**
  (`ingestors/backtest/ground_truth.yaml`) : événement détecté ou non,
  avance/retard en jours (`lead_days` négatif = détection AVANT
  l'événement), via quel détecteur ;
- compte le **volume d'épisodes** total et par détecteur — c'est le
  proxy du bruit : un seuil plus sensible doit se justifier par un
  meilleur rappel SANS exploser ce volume.

## Vérité terrain

`ground_truth.yaml` est vide à la création : il faut y consigner des
crises documentées qui tombent dans la plage de données réellement
indexée (la couverture GDELT du déploiement actuel commence en avril
2026). Schéma par événement : `name`, `date`, `countries` (noms
canoniques GEON), `window_before`/`window_after` (jours).

## Boucle de réglage recommandée

1. Renseigner 5-10 événements dans la vérité terrain.
2. Lancer le backtest, noter rappel / avance moyenne / volume.
3. Ajuster UN seuil dans `config.yaml`
   (`zscore_threshold`, `goldstein_threshold`, `stddev_threshold`…).
4. Relancer, comparer. Garder le réglage seulement si le rappel
   progresse sans explosion du volume.
