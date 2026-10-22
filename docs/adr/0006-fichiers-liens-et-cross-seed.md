# ADR-0006 : Fichiers, liens et compatibilité cross-seed

## Statut
Accepté (2026-10-22)

## Date
2026-10-22

## Contexte
- Règle de cadrage : **un fichier téléchargé ne quitte jamais son dossier** (ni déplacé, ni renommé, ni copié) ; la release reconstituée et, plus tard, l'import Sonarr/Radarr se font par liens, sans doublon.
- Exception validée : retrait sur place du padding nul (un hardlink a la taille de l'original).
- Le rangement doit être celui de cross-seed pour partager ses dossiers : `linkDir/<tracker Prowlarr>/<release>` (ou à plat si `flatLinking`), `linkDir` choisi sur le même système de fichiers, `linkType` hardlink / symlink / reflink.
- Le `config.js` de cross-seed est du JavaScript exécutable.

## Décision

### Arborescence
```
<downloads>/<source>/<titre>/S01/        ← fichiers téléchargés, noms IPTV, jamais déplacés
<linkDir>/<tracker>/<nom de la release>/  ← liens sous les noms d'origine (un dossier par référence)
/config/torrents/<release>-<candidat>.torrent
```
- `<tracker>` = nom de l'indexeur Prowlarr nettoyé des caractères interdits dans un nom de fichier (pas le « slug » de la démo), comme cross-seed.
- `linkDir` retenu : le premier de `linkDirs` dont `st_dev` est celui du dossier de téléchargement ; aucun → étape `restore` en erreur `restore.no_linkdir_same_fs` (pour hardlink ; symlink : premier `linkDir`, comme cross-seed).

### Restauration (par référence)
1. Revérifier, pour chaque fichier à lier, que l'excédent éventuel n'est que des octets nuls ; tronquer sur place à la longueur d'origine (`os.truncate`). C'est la seule écriture sur un fichier téléchargé.
2. Créer chaque lien : `os.link` (hardlink), `os.symlink` (chemin absolu), ou reflink (`ioctl FICLONE` ; repli en erreur explicite si le système de fichiers ne le permet pas).
3. Cible existante : si c'est déjà un lien vers le même inode, on la garde ; sinon on ne touche à rien et le fichier est signalé.
4. qBittorrent reçoit `savepath = <linkDir vu par qBittorrent>/<tracker>` (le nom de la release vient du torrent) et télécharge lui-même les fichiers manquants (annexes, « compléter depuis ») dans ce dossier.

### Suppression d'une release (AC-PIPE-05)
Torrents retirés de qBittorrent avec `deleteFiles=true` (ne supprime que leurs fichiers, c'est-à-dire nos liens et les fichiers complétés par qBittorrent), puis les dossiers de liens vides sont retirés, puis la release en base. Fichiers téléchargés supprimés seulement si `delete_files=true`. Aucun autre chemin n'est jamais supprimé (vérification `is_relative_to` du dossier de téléchargement ou du `link_root` de la release).

### Import de `config.js`
- Lecture du fichier (≤ 256 Kio) sans l'exécuter ; recherche des clés `linkDirs`, `linkDir` (ancien nom), `linkType`, `flatLinking`, `linkCategory`.
- Valeur acceptée seulement si c'est un **littéral** : chaîne entre guillemets simples/doubles sans `${`, booléen, ou liste de chaînes littérales. Commentaires `//` et `/* */` ignorés. Tout le reste (variable, appel, concaténation, `process.env`) → champ marqué « à saisir à la main ».
- Le résultat pré-remplit le formulaire ; rien n'est enregistré sans validation (AC-SET-06).

## Alternatives envisagées

### Déplacer les fichiers (démo)
- Rejeté : règle de cadrage ; les autres services ne doivent pas voir disparaître les fichiers, et Sonarr/Radarr (V1.1) lieront aussi vers eux.

### Copier puis tronquer la copie
- Contre : doublon de dizaines de Go.
- Rejeté (seule alternative à la troncature sur place).

### Exécuter `config.js` avec Node ou un interpréteur JS
- Contre : exécution de code arbitraire monté depuis un autre conteneur, et Node dans l'image.
- Rejeté.

### Interroger l'API de cross-seed
- Contre : cross-seed n'expose pas sa configuration (vérifié dans sa doc, octobre 2026).
- Rejeté.

## Conséquences
- Le dossier de téléchargement de VODarr doit être sur le même volume que le `linkDir` (documenté, vérifié à l'enregistrement des réglages, AC-SET-04).
- Supprimer un fichier téléchargé hors de VODarr ne casse pas un hardlink (les données restent tant qu'un lien existe) ; avec `symlink`, si — documenté.
- Le module `torrent.py` reprend `bdecode`, `match`, `check_padding`, `verify` de la démo ; la logique de déplacement de `st_restore` est remplacée par la création de liens.
