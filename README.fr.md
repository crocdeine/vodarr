# VODarr

Français · [English](README.md)

VODarr rejoint votre stack *arr. Il télécharge une saison ou un film depuis votre source VOD
IPTV (Xtream Codes ou M3U), retrouve la release P2P d'origine sur vos trackers via Prowlarr,
vérifie **pièce par pièce** que le contenu est identique, reconstitue la release par des liens
sous ses noms d'origine, puis la met en seed dans qBittorrent : du **cross-seed à partir de
fichiers IPTV**.

- Un conteneur, un port, une interface web en anglais et en français (thèmes sombre et clair).
- Rien n'est envoyé (upload) vers les trackers. Rien ne sort de votre stack hormis les requêtes
  que vous configurez.
- Les fichiers téléchargés ne bougent jamais : la release reconstituée est faite de hardlinks.

## Fonctionnement

```
Explorer ──► téléchargement ──► analyse ──► recherche ──► vérification ──► restauration ──► seed
 (Xtream/M3U)  (.part,          (ffprobe :   (Prowlarr :   (chaque pièce      (liens, rangement  (qBittorrent :
               reprise)         nom de       packs et      du .torrent)       cross-seed)        vérifie puis
                                release)     épisodes)                                           partage)
```

1. **Téléchargement** — une seule file globale, un fichier à la fois, avec reprise (`.part` +
   HTTP Range, 5 essais). Les fichiers arrivent dans `<téléchargements>/<source>/<titre>/Sxx/`
   et y restent.
2. **Analyse** — `ffprobe` lit le nom de release que certains fournisseurs laissent dans les
   tags des pistes (un nom inconnu n'empêche rien) et les caractéristiques techniques affichées
   dans la fiche : résolution, codecs, HDR, langues audio et sous-titres, durée.
3. **Recherche** — Prowlarr cherche le titre sur tous vos indexeurs, puis VODarr retient :
   - les packs de la bonne saison (`S01`, `Season 1`, `Saison 1` ; jamais `S12` ni `Saison 10`) ;
   - les épisodes isolés et les plages (`S01E05`, `S01E05-E06`) que vous avez en local ;
   - l'année pour un film ; une taille à ±15 % de la vôtre.
   Toutes les versions de la même saison sont listées pour information.
4. **Vérification** — le `.torrent` de chaque candidat est téléchargé et **toutes les pièces**
   sont comparées à vos fichiers, fichier par fichier. Par tracker et par portée (le pack, ou
   chaque épisode), la taille la plus proche est essayée d'abord et le premier identique arrête
   la recherche.
5. **Restauration** — pour chaque référence identique, des liens sont créés sous les noms
   d'origine dans `<dossier de liens>/<tracker>/<release>/`, comme
   [cross-seed](https://www.cross-seed.org/).
6. **Seed** — le torrent est ajouté à qBittorrent sur ce dossier, avec vérification forcée, dans
   sa propre catégorie. VODarr attend la fin de la vérification.

Une release finit **terminée**, **action requise** (correspondance partielle : certains
fichiers diffèrent, voir plus bas) ou **en erreur** avec un motif traduit. Chaque étape peut être
relancée.

## Installation

Prérequis : Docker, Prowlarr, qBittorrent ≥ 4.6 (API WebUI v2) et **un seul volume `/data`
partagé avec qBittorrent** (les hardlinks ne fonctionnent qu'à l'intérieur d'un même système
de fichiers).

1. Copiez [`docker-compose.example.yml`](docker-compose.example.yml) à côté de votre stack et
   adaptez les valeurs signalées : `PUID`/`PGID` (l'utilisateur de qBittorrent), le montage
   `/data`, le réseau et `VODARR_ALLOWED_HOSTS`.
2. `docker compose up -d vodarr`
3. Lisez le **code d'installation** dans le journal : `docker logs vodarr` →
   `First run: open VODarr and enter the setup code xxxx-xxxx-xxxx`.
4. Ouvrez `http://<hôte>:8863` et créez le compte administrateur avec ce code.
5. Dans **Réglages** : ajoutez votre source, Prowlarr et qBittorrent (chacun a un bouton
   *Tester*), le dossier de téléchargement (par exemple `/data/vod`) et le dossier de liens (par
   exemple `/data/cross-seed`). Si vous utilisez cross-seed, *Importer le config.js de
   cross-seed* reprend ses dossiers et son type de lien.

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `PUID` / `PGID` | `1000` | Utilisateur qui exécute l'application (jamais root) |
| `TZ` | — | Fuseau horaire |
| `VODARR_ALLOWED_HOSTS` | `localhost`, nom et IP du conteneur | Noms d'hôte que vous tapez dans le navigateur, séparés par des virgules ; les autres reçoivent HTTP 421 (anti DNS-rebinding) |
| `VODARR_PORT` | `8863` | Port d'écoute |
| `VODARR_SETUP_TOKEN` | aléatoire | Code d'installation fixe au lieu de celui affiché dans le journal |
| `VODARR_DEV` | — | `1` active `/docs` (OpenAPI) |

Volumes : `/config` (base, cache du catalogue, `.torrent` de référence, fichiers M3U sous
`/config/playlists`) et `/data` (vos médias, partagés avec qBittorrent).

### Réglages à connaître

- **TMDB (facultatif)** — votre propre clé gratuite sur [themoviedb.org](https://www.themoviedb.org/settings/api) (clé API v3 ou jeton de lecture v4) ajoute synopsis, affiche, genres, note et distribution dans l'Explorer et les releases. Les appels à l'API TMDB partent de VODarr ; les affiches sont chargées par votre navigateur depuis `image.tmdb.org`.

- **Chemin vu par qBittorrent** — si qBittorrent monte le même dossier sous un autre chemin,
  renseignez *Racine des données (VODarr)* et *Même racine vue par qBittorrent* (par exemple
  `/data` → `/downloads`).
- **Type de lien** — `hardlink` (défaut), `symlink` ou `reflink` (btrfs/xfs). Les hardlinks
  exigent téléchargements et dossier de liens sur le même volume ; VODarr le vérifie à
  l'enregistrement.
- **Rangement à plat** — `<dossier de liens>/<release>` au lieu de
  `<dossier de liens>/<tracker>/<release>`.
- **Catégorie** — `cross-seed-link` par défaut.

## Comportements automatiques

VODarr agit pour vous de ces façons ; chacune est voulue.

- **Téléchargement de `.torrent` depuis vos trackers.** Pour vérifier un candidat, VODarr
  télécharge son `.torrent` via Prowlarr — seulement les candidats filtrés, avec arrêt au premier
  identique par tracker et par portée. Sur certains trackers privés, cela compte comme un
  *snatch* : vérifiez leurs règles de hit-and-run avant d'activer Prowlarr. Les `.torrent` de
  référence restent dans `/config/torrents` (ils contiennent votre passkey) et ne sont jamais
  servis par l'API.
- **Tolérance de taille de ±15 %.** Les résultats trop éloignés de votre taille sont listés mais
  pas vérifiés ; dans la tolérance, la vérification des pièces tranche. Dans le doute, VODarr
  vérifie.
- **Les fichiers téléchargés ne bougent jamais.** Ils ne sont ni déplacés, ni renommés, ni
  copiés. La release reconstituée est faite de liens qui pointent vers eux : rien n'est dupliqué
  sur le disque.
- **Retrait sur place du padding nul — la seule modification jamais apportée à un fichier
  téléchargé.** Certains fournisseurs IPTV ajoutent quelques octets nuls en fin de fichier. Un
  hardlink *est* le même fichier, donc de même taille ; pour que le lien ait la taille exacte de
  la release d'origine, VODarr tronque le fichier téléchargé à sa longueur d'origine. Le fichier
  reste à sa place avec son nom ; seuls des octets nuls sont retirés, et seulement après avoir
  vérifié qu'il n'y a rien d'autre (sinon le fichier est déclaré *différent* et laissé intact).
  La vidéo est inchangée bit à bit. La seule alternative serait une copie, donc un doublon sur
  le disque.
- **Packs de saison et épisodes isolés.** Les deux sont recherchés et vérifiés ; un même épisode
  peut être en seed sur plusieurs trackers, dans un pack sur l'un et seul sur l'autre.
- **Annexes complétées par qBittorrent.** Quand il ne manque que de petits fichiers (`.nfo`,
  samples, fichiers non vidéo de moins de 1 Mio au total), la correspondance est *identique,
  annexes à compléter* et qBittorrent les télécharge lui-même (impact négligeable sur le ratio).
- **Les correspondances partielles vous attendent.** Si une vidéo diffère (par exemple un
  épisode fourni dans un autre format), la release passe en *action requise* avec le détail
  fichier par fichier et un bouton **Compléter depuis \<tracker\>** : seuls les fichiers
  identiques sont liés, qBittorrent télécharge le reste dans le dossier de liens.
- **Catégorie qBittorrent.** Les torrents utilisent la catégorie `cross-seed-link` par défaut :
  excluez-la des outils de nettoyage comme Cleanuparr, sinon ils pourraient supprimer vos seeds.
- **Compatibilité cross-seed.** Même rangement que cross-seed : les deux peuvent partager leurs
  dossiers. L'import de `config.js` ne lit que des valeurs littérales ; le fichier n'est **jamais
  exécuté**.
- **Supprimer une release** retire ce que VODarr a créé : ses torrents dans qBittorrent (avec
  leurs fichiers, c'est-à-dire les liens et ce que qBittorrent a complété), les liens et la fiche.
  Les fichiers téléchargés ne sont supprimés que si vous cochez la case dans la confirmation. Un
  torrent déjà présent dans qBittorrent avant VODarr n'est jamais retiré.

## Sécurité

- **Compte administrateur** créé au premier lancement avec le code à usage unique du journal
  (personne d'autre sur votre réseau ne peut s'approprier une instance neuve). Mots de passe :
  scrypt, 12 caractères minimum.
- **Sessions** dans un cookie `HttpOnly`, `SameSite=Strict`, 30 jours glissants. **Clé API**
  pour les scripts (en-tête `X-Api-Key`) : masquée dans les réglages, affichée après saisie du
  mot de passe admin, régénérable.
- **Force brute :** 5 échecs en 5 minutes bloquent l'adresse concernée pendant 5 minutes ;
  50 échecs toutes adresses confondues bloquent tout le monde. Derrière un reverse proxy, tous les
  clients partagent l'adresse du proxy.
- **Aucune exemption pour le réseau local**, liste blanche des noms d'hôte (`Host`), contrôle de
  même origine sur chaque modification, Content Security Policy stricte, requêtes limitées à
  1 Mio.
- **Secrets** (mots de passe des sources, URL M3U, clé Prowlarr, mot de passe qBittorrent)
  stockés dans `/config/vodarr.db` (droits 600, dossier 700), jamais renvoyés au navigateur
  (affichés `••••`), jamais écrits dans les journaux. Un secret masqué n'est réutilisé que si
  l'adresse de son serveur n'a pas changé.
- **Affiches** de l'Explorer chargées par votre navigateur directement depuis les serveurs
  d'images du fournisseur (qui voient donc votre adresse IP), comme dans toute application IPTV.
- **Pas de TLS** dans le conteneur : pour un accès distant, placez VODarr derrière votre reverse
  proxy ou votre VPN. Le proxy doit conserver l'en-tête `Host`, et ce nom doit figurer dans
  `VODARR_ALLOWED_HOSTS`.

## Développement

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                                   # ~700 tests, sans réseau
ruff check . && ruff format --check .
python -m tests.fakes.serve                 # faux Xtream, Prowlarr, qBittorrent (données synthétiques)
VODARR_CONFIG=.dev-config VODARR_ALLOWED_HOSTS=localhost \
  uvicorn vodarr.app:create_app --factory --port 8863
```

Décisions de conception dans [`docs/adr/`](docs/adr/).
L'interface est en HTML, CSS et modules JavaScript, sans étape de build ; la page de référence
du design system est `#/design`.

## Licence

[MIT](LICENSE). VODarr n'est affilié ni à Prowlarr, ni à qBittorrent, ni à cross-seed, ni à
aucun fournisseur IPTV. Utilisez-le avec des contenus auxquels vous avez légitimement accès et
respectez les règles de vos trackers.
