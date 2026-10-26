# ADR-0011 : Import dans Sonarr et Radarr par leur API, en mode « Copy »

## Statut
Proposé (2026-10-26), cible 0.4.

## Date
2026-10-26

## Contexte
- Les fichiers téléchargés par VODarr doivent aussi rejoindre la bibliothèque gérée par Sonarr (séries) et Radarr (films) de l'utilisateur.
- Règle du projet : un fichier téléchargé ne quitte jamais son dossier ; il reste en seed sous ses liens.
- Sonarr et Radarr ne remarquent pas un fichier déposé dans leur bibliothèque sans les prévenir : leurs fiches resteraient « manquantes » jusqu'à un rescan.

## Décision
- **API officielle** : `POST /api/v3/command` avec `DownloadedEpisodesScan` (Sonarr) ou `DownloadedMoviesScan` (Radarr), `path` = le dossier à importer, `importMode: "Copy"` (jamais `Move`).
- **Dossier importé** : le dossier de liens de la release (nom d'origine de la release, que leur analyseur reconnaît le mieux) ; à défaut de release identique, le dossier de téléchargement.
- **Pas de déplacement** : en mode Copy, Sonarr et Radarr créent un hardlink quand leur option « Use Hardlinks instead of Copy » est active (défaut) et que les dossiers sont sur le même volume ; VODarr le signale dans les Réglages, car sinon ils copient (doublon sur le disque).
- **Optionnel** : sans URL ni clé, rien ne change. Clés masquées comme les autres secrets ; chemins traduits comme pour qBittorrent si Sonarr/Radarr voient `/data` autrement.
- **Déclenchement** : à la fin du seed (ou de la vérification si rien n'est seedé), puis suivi de la commande jusqu'à son état final, affiché dans la fiche de la release.

## Alternatives envisagées
- Liens créés par VODarr directement dans la bibliothèque : Sonarr/Radarr ne les voient qu'au prochain rescan, sans historique ni renommage ; nommage et dossiers de la bibliothèque à deviner ; rejetée.
- `Move` : déplacerait le fichier téléchargé et casserait le seed ; interdit par la règle du projet.

## Conséquences
- Deux services de plus dans les Réglages (URL, clé API, bouton Tester), avec de faux Sonarr/Radarr pour les tests.
- La release doit correspondre à une série ou un film déjà suivi dans Sonarr/Radarr ; sinon l'import est refusé et l'erreur est affichée telle quelle.
