# ADR-0002 : Un processus, deux workers, état en base

## Statut
Accepté (2026-10-22)

## Date
2026-10-22

## Contexte
- Deux traitements longs : téléchargements (heures) et pipeline (vérification de dizaines de Go de pièces, attente de la vérification qBittorrent).
- Décisions de cadrage : **une seule file de téléchargement globale**, un téléchargement à la fois ; reprise après redémarrage.
- La démo mélangeait état en mémoire et JSON réécrits par plusieurs threads.

## Décision
- **Un seul processus** Uvicorn (1 worker), qui démarre au lancement de l'application **deux threads** :
  - `download-worker` : prend le prochain élément `queued` de la file (FIFO), le télécharge, met à jour la progression ;
  - `pipeline-worker` : prend la prochaine release dont une étape est `pending` (et aucune en `error`), exécute cette étape, enregistre le résultat.
- **SQLite est la seule source de vérité** (ADR-0003) : les workers lisent et écrivent la base ; l'API ne fait que lire l'état et poser des intentions (créer, annuler, relancer, supprimer).
- Connexions SQLite : une par thread, mode **WAL**, `busy_timeout` 5 s ; écritures courtes dans des transactions.
- Réveil : un `threading.Event` par worker, déclenché par l'API (nouvel élément, relance) ; sinon attente de 3 s.
- Redémarrage : tout élément ou étape `running` repasse en `pending`/`queued` (AC-CORE-05, AC-DL-04).
- Annulation d'un téléchargement : drapeau en base, lu par le callback de progression qui lève `Cancelled` (mécanisme repris de la démo).
- **Interface rafraîchie par polling** (2 s sur les vues actives) : pas de WebSocket ni de SSE.

## Alternatives envisagées

### Celery / RQ / file externe
- Contre : un broker (Redis) en plus dans la stack pour deux threads.
- Rejeté.

### asyncio pour les workers
- Contre : la logique reprise (hash de pièces, `urllib`, `subprocess`) est bloquante ; il faudrait des `to_thread` partout.
- Rejeté : des threads simples suffisent.

### Plusieurs workers de pipeline
- Pour : une vérification qBittorrent longue ne bloquerait pas l'analyse d'une autre release.
- Contre : verrous et concurrence sur le disque (lectures de pièces en parallèle ralentissent un disque mécanique).
- Rejeté pour la V1 : limite connue : pipeline-worker unique, une étape longue (seed qui attend qBittorrent) retarde les autres releases ; ajouter un 2e worker si l'attente devient gênante.

### SSE / WebSocket
- Contre : gestion des connexions, reconnexions, proxys ; le polling de petites réponses JSON suffit pour un seul utilisateur.
- Rejeté.

## Conséquences
- L'état survit à tout redémarrage, sans logique de sauvegarde spécifique.
- Uvicorn doit tourner avec **un seul worker** (sinon deux jeux de threads) : imposé dans la commande de l'image.
- Les tests pilotent les workers de façon synchrone (appel direct de « traiter le prochain élément ») sans attendre des threads.
