# ADR-0003 : Modèle de données SQLite

## Statut
Accepté (2026-10-22)

## Date
2026-10-22

## Contexte
- La démo identifiait les sources par leur position (instable) et stockait les projets dans un gros JSON.
- Besoins : identifiants stables, reprise après redémarrage, détail fichier par fichier et candidat par candidat, aucune URL contenant un secret envoyée au navigateur.

## Décision
Base `/config/vodarr.db` (fichier `600`). Migrations : liste ordonnée de scripts SQL dans `core.py`, version suivie par `PRAGMA user_version`.

```sql
settings      (key TEXT PRIMARY KEY, value TEXT NOT NULL)          -- JSON par clé : services, paths, crossseed, ui
admin         (id INTEGER PRIMARY KEY CHECK (id = 1),
               username TEXT NOT NULL, password_hash TEXT NOT NULL, -- scrypt, sel inclus
               api_key TEXT NOT NULL)                               -- en clair, réaffichable après mot de passe (ADR-0005)
sessions      (token_hash TEXT PRIMARY KEY, created_at INTEGER, expires_at INTEGER)

sources       (id TEXT PRIMARY KEY,                                -- uuid4 hex, stable
               name TEXT NOT NULL, type TEXT NOT NULL CHECK (type IN ('xtream','m3u')),
               config TEXT NOT NULL,                               -- JSON : server/user/password, ou url/path
               split_prefix INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL)

releases      (id TEXT PRIMARY KEY,
               source_id TEXT NOT NULL REFERENCES sources(id),
               kind TEXT NOT NULL CHECK (kind IN ('series','movie')),
               item_id TEXT NOT NULL, season INTEGER,               -- NULL pour un film
               title TEXT NOT NULL, year TEXT, dir TEXT NOT NULL,
               created_at INTEGER NOT NULL,
               UNIQUE (source_id, kind, item_id, season))           -- une sélection ajoutée plus tard rejoint la même release ; implémenté par un index unique sur coalesce(season, -1), les NULL des films étant distincts pour UNIQUE

files         (id INTEGER PRIMARY KEY, release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
               stream_id TEXT NOT NULL, ext TEXT NOT NULL,         -- l'URL est reconstruite au téléchargement
               season INTEGER, episode INTEGER, filename TEXT NOT NULL,
               size INTEGER, release_name TEXT,                     -- remplis par l'analyse
               dl_status TEXT NOT NULL DEFAULT 'queued',            -- queued|running|done|error|cancelled
               dl_done INTEGER NOT NULL DEFAULT 0, dl_total INTEGER,
               dl_error TEXT, queued_at INTEGER NOT NULL,           -- ordre FIFO de la file globale
               in_queue INTEGER NOT NULL DEFAULT 1,                 -- 0 = retiré de la file par « nettoyer les terminés » (le fichier reste dans la release)
               UNIQUE (release_id, stream_id))

stages        (release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
               name TEXT NOT NULL,                                  -- download|analyse|search|verify|restore|seed
               state TEXT NOT NULL DEFAULT 'pending',               -- pending|running|done|skipped|error
               pct INTEGER NOT NULL DEFAULT 0,
               msg_code TEXT, msg_params TEXT,                      -- message traduisible (ADR-0007)
               PRIMARY KEY (release_id, name))

candidates    (id INTEGER PRIMARY KEY, release_id TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
               indexer TEXT NOT NULL, title TEXT NOT NULL, size INTEGER,
               scope TEXT NOT NULL,                                 -- 'pack', 'S01E05', 'S01E05-E06', 'movie'
               is_candidate INTEGER NOT NULL,                       -- 0 = version listée pour info seulement
               download_url TEXT,                                   -- contient la clé Prowlarr : jamais exposé
               info_url TEXT, seeders INTEGER,
               status TEXT NOT NULL DEFAULT 'pending',              -- pending|identical|identical_annex|partial|different|skipped|error
               problem_code TEXT, file_report TEXT,                 -- JSON : [{name, size, status}]
               torrent_path TEXT, infohash TEXT,
               link_root TEXT,                                      -- racine des liens créés (restauration)
               complete_requested INTEGER NOT NULL DEFAULT 0,       -- clic « compléter depuis »
               added_to_qbit INTEGER NOT NULL DEFAULT 0)            -- 1 = ajouté par VODarr : seul ce torrent est retiré à la suppression
```

- **La file de téléchargement, c'est la table `files`** : prochain élément = `dl_status='queued'` trié par `queued_at`. Pas de table de jobs séparée.
- **Une référence, c'est un candidat** `identical`/`identical_annex` (ou `partial` avec `complete_requested`) : pas de table séparée.
- Statut agrégé d'une release (AC-PIPE-04) : **calculé** à la lecture à partir de `files` et `stages`, jamais stocké.
- Le catalogue des sources n'est pas en base : cache JSON sur disque, `/config/cache/<source_id>/<action>.json` (`600`), durée de vie 24 h, jamais écrit si la réponse est invalide.

## Alternatives envisagées

### ORM (SQLAlchemy / SQLModel)
- Contre : dépendance et couche d'abstraction pour 8 tables et des requêtes simples.
- Rejeté : `sqlite3` + requêtes SQL écrites à la main.

### Garder des fichiers JSON
- Contre : écritures concurrentes, pas de transactions, pas de contraintes d'unicité.
- Rejeté (problème connu de la démo).

### Catalogue en base
- Pour : recherche SQL.
- Contre : dizaines de milliers d'entrées réécrites toutes les 24 h, schéma qui dépend des fournisseurs.
- Rejeté : JSON sur disque + filtrage en mémoire. limite connue : recherche linéaire en mémoire sur le catalogue, passer en table FTS5 si une source dépasse ~200 000 entrées et que la recherche rame.

## Conséquences
- `download_url` et la config des sources contiennent des secrets : ces colonnes ne sont **jamais** sérialisées vers l'API (sérialiseurs explicites, test AC-UI-09).
- `UNIQUE (source_id, kind, item_id, season)` rend la création d'une release idempotente : re-télécharger une saison ou ajouter des épisodes rejoint la release existante et relance son pipeline.
- `ON DELETE CASCADE` simplifie la suppression d'une release (AC-PIPE-05) ; liens et torrents sont retirés par le code avant la suppression en base.
