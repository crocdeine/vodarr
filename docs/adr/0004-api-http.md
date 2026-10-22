# ADR-0004 : API HTTP

## Statut
Accepté (2026-10-22)

## Date
2026-10-22

## Contexte
- Une seule interface cliente (la nôtre), mais l'API est publique de fait : clé API (scripts de l'utilisateur), et tout comportement observable finit par être utilisé.
- Les messages doivent être traduisibles (ADR-0007) ; aucun secret ne doit sortir.

## Décision

### Conventions
- REST + JSON sous `/api`, noms au pluriel, **`camelCase` dans le JSON et les paramètres de requête** (`pageSize`, `sourceId`, `deleteFiles`), comme les API *arr : un utilisateur qui scripte Sonarr ou Radarr retrouve ses habitudes. Côté Python et en base : `snake_case`. La conversion est faite une seule fois, par un modèle Pydantic de base (`alias_generator=to_camel`, `populate_by_name=True`, sérialisation `by_alias`) dont héritent tous les schémas.
- Actions qui ne sont pas des créations ou modifications de ressource : sous-ressource au nom explicite en `POST` (`/retry`, `/complete`, `/cancel`, `/test`, `/refresh`).
- Listes paginées : `?page=1&pageSize=50` (max 200) → `{"data": [...], "pagination": {"page", "pageSize", "totalItems", "totalPages"}}`.
- **Format d'erreur unique** :
  ```json
  {"error": {"code": "settings.path_not_writable", "params": {"path": "/data/x"}}}
  ```
  `code` est aussi la clé de traduction. 401 non authentifié, 404 introuvable, 409 conflit, 413 trop gros, 421 hôte refusé, 422 validation, 500 erreur interne (`{"error": {"code": "internal"}}`, jamais de trace).
- Validation Pydantic de chaque corps et paramètre ; réponses construites par des sérialiseurs explicites (liste blanche de champs), jamais en renvoyant une ligne de base telle quelle.

### Ressources

| Méthode et chemin | Rôle |
|---|---|
| `GET /api/health` | Santé (sans auth) |
| `GET /api/auth/status` | `{setup_required, authenticated}` (sans auth) |
| `POST /api/auth/setup` | Création de l'admin (une seule fois, puis 409) |
| `POST /api/auth/login` · `POST /api/auth/logout` | Session |
| `POST /api/auth/api-key` | Régénère la clé (l'ancienne est refusée aussitôt) |
| `POST /api/auth/api-key/reveal` | `{password}` → affiche la clé en clair après saisie du mot de passe admin |
| `GET /api/settings` · `PUT /api/settings` | Services, chemins, cross-seed, langue (secrets masqués, ADR-0005) |
| `POST /api/settings/test` | `{service: "prowlarr" \| "qbittorrent"}` → OK ou code d'erreur |
| `POST /api/settings/cross-seed-import` | `{path}` → valeurs pré-remplies, rien n'est enregistré |
| `GET /api/sources` · `POST /api/sources` | Sources |
| `PATCH /api/sources/{id}` · `DELETE /api/sources/{id}` | Modifier / supprimer |
| `POST /api/sources/{id}/test` · `POST /api/sources/{id}/refresh` | Tester / vider le cache |
| `GET /api/sources/{id}/categories?kind=` | Catégories (groupées par fournisseur si `splitPrefix`) |
| `GET /api/sources/{id}/items?kind=&category=&q=&page=` | Titres (paginé, recherche) |
| `GET /api/sources/{id}/series/{itemId}` · `.../movies/{itemId}` | Détail (sans URL) |
| `POST /api/releases` | `{sourceId, kind, itemId, season?, episodes?}` → crée ou complète la release (idempotent) |
| `GET /api/releases?status=&page=` | Liste + statut agrégé |
| `GET /api/releases/{id}` | Détail : étapes, fichiers, candidats (sans URL de téléchargement), état qBittorrent |
| `POST /api/releases/{id}/retry` | `{stage}` → relance à partir de cette étape |
| `POST /api/releases/{id}/complete` | `{candidateId}` → « compléter depuis » |
| `DELETE /api/releases/{id}?deleteFiles=false` | Supprime tout ; fichiers téléchargés seulement si `true` |
| `GET /api/downloads` | File globale (sans URL) |
| `POST /api/downloads/{fileId}/cancel` · `DELETE /api/downloads/{fileId}` | Annuler / retirer (`.part` supprimé) |
| `POST /api/downloads/clear-finished` | Nettoyer les terminés |

### Idempotence
- `POST /api/releases` : la contrainte `UNIQUE (source_id, kind, item_id, season)` décide atomiquement (insertion, sinon fusion) ; ré-envoyer la même demande ne crée pas de doublon.
- `retry`, `complete`, `cancel`, `clear-finished`, `DELETE` : idempotents par nature (ré-appliquer donne le même état).
- Pas d'en-tête `Idempotency-Key` : un seul client, aucune opération financière.

## Alternatives envisagées

### `snake_case` dans le JSON
- Pour : aucune conversion.
- Contre : diffère des API *arr que les utilisateurs scriptent déjà ; le coût de la conversion se limite à un modèle de base.
- Rejeté (révision du 2026-10-22, à la demande de l'auteur).

### GraphQL
- Rejeté : un seul client, ressources simples.

### Messages d'erreur en texte
- Rejeté : non traduisibles (ADR-0007).

## Conséquences
- Toute nouvelle donnée exposée passe par un sérialiseur : point unique où vérifier l'absence de secret.
- Les champs ajoutés plus tard sont optionnels ; aucun champ n'est retiré sans dépréciation documentée.
- `/docs` et `/openapi.json` désactivés par défaut (ADR-0005), activables en dev.
