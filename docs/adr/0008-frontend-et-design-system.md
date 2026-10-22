# ADR-0008 : Frontend sans build et design system en CSS natif

## Statut
Accepté (2026-10-22). Direction visuelle choisie le 2026-10-22 : **A · « inspirée d'iPadOS, fidèle »** (voir « Direction visuelle » ci-dessous).

## Date
2026-10-22

## Contexte
- 4 écrans (Explorer, Releases, Réglages, tiroir Téléchargements), un utilisateur, rafraîchissement par polling.
- Exigence de cadrage : un **design system propre** pour éviter une interface générique ; pas de framework CSS ni de bibliothèque de composants.
- Leçons UX de la démo : liste filtrable + détail en onglets, détail fichier par fichier, ne pas écraser une saisie pendant le rafraîchissement.

## Décision

### Frontend
- `web/index.html` + modules ES (`app.js`, `api.js`, `i18n.js`, une vue par écran), **sans build ni dépendance**.
- Routage par `location.hash` (`#/explorer`, `#/releases/<id>`, `#/settings`, `#/design`).
- `api.js` : `fetch` JSON, gestion uniforme du format d'erreur (ADR-0004) et redirection vers la connexion sur 401.
- Rendu : fonctions qui construisent le DOM (`document.createElement` / `textContent`, **jamais `innerHTML` avec des données** : les titres viennent de sources non fiables).
- Polling 2 s sur la vue active ; une vue ne remplace pas un champ qui a le focus ou une valeur modifiée (AC-UI-04).

### Design system
- `web/styles/tokens.css` : toutes les valeurs en variables CSS — couleurs (rôles : fond, surface, texte, bordure, accent, états `pending/running/done/skipped/error/action`), typographie (familles, échelle), espacements (échelle 4 px), rayons, ombres, durées d'animation.
- Thème sombre par défaut, thème clair via `[data-theme="light"]` ; `prefers-color-scheme` respecté en mode `auto`.
- `web/styles/components.css` : boutons, champs, pastilles d'état, tableaux, barres de progression, tiroir, onglets, toasts — **n'utilise que des tokens** (test : aucune couleur ni taille en dur hors `tokens.css`, AC-UI-06).
- Page `#/design` : tous les composants, dans les deux thèmes et les deux langues ; c'est la référence de validation visuelle et de vérification dans le navigateur.
- Polices : système, ou une police libre **embarquée dans l'image** (pas d'appel à un CDN : CSP `default-src 'self'`).
- Accessibilité : contraste AA vérifié par un test sur les paires de tokens (AC-UI-08), focus visible, navigation clavier, `aria-live` pour les toasts et la progression.

### Direction visuelle : inspirée d'iPadOS (variante A)
- **Mise en page en vue partagée** : barre latérale translucide (navigation, sources, carte de téléchargement), liste au centre, détail à droite ; réglages en listes groupées à coins arrondis.
- **Typographie** : police système (`system-ui, -apple-system, "Segoe UI", Roboto, …`) — SF Pro s'affiche sur les appareils Apple sans être embarquée ; grands titres 34 px gras, corps 15 px, chiffres tabulaires.
- **Formes** : rayons 16 / 12 / 9 px, boutons en pilule, ombres douces sans bordure, surfaces translucides (`backdrop-filter`) pour la barre latérale et l'en-tête du détail.
- **Contrôles** : sélecteurs segmentés (filtres de liste, onglets d'étapes), frise des 6 étapes du pipeline, pastilles d'état rondes, alertes teintées avec action principale.
- **Couleurs** : accent turquoise propre à VODarr (`#2fb8a3` sombre, `#14937f` clair), pas le bleu système d'Apple ; états ok `#34c77b`, en cours `#4a9cff`, action `#f5a524`, erreur `#ff5a5f`, ignoré `#8e8e93` (contrastes AA à vérifier par test, ajustables).
  Ajusté le 2026-10-22 après le test de contraste AA (`tests/test_design_tokens.py`) : en clair, accent `#0f7a6a`, ok `#136b3a`, en cours `#1d64c2`, action `#864e00`, erreur `#ad252b`, ignoré `#62626a`, textes secondaires `#4f5663` / `#636a76` ; en sombre, ignoré `#9d9da3` et texte tertiaire `#8a929e`. Les valeurs de référence sont dans `web/styles/tokens.css`.
  **Palette du logo (2026-10-22, demande de l'auteur)** : le logo (téléviseur rétro en bois sur fond marine, `web/icon.svg`, dessin original sous licence MIT — l'esquisse fournie reprenait l'emoji Apple 📺 et un Mii de Nintendo, non réutilisables) donne la palette : fonds marine (`#08141f`, surfaces `#0f2132` / `#16293c`), accent **cuivre** (`#e08a5f` en sombre, `#a2462a` en clair) à la place du turquoise, thème clair crème (`#f5f1ea`). En sombre, en cours `#5aa8ff` et erreur `#ff6b6f` pour rester AA sur le marine. Référence : `web/styles/tokens.css`.
- **Icônes** : jeu au trait **dessiné pour VODarr** (`web/icons.js`, 24 × 24, trait 1,8, même style que Lucide), sous la licence MIT du projet : pas de dépendance ni de licence tierce à joindre ; aucune icône SF Symbols.

### Ressources Apple : inspiration seulement
La licence « Apple Design Resources » (version du 2023-07-13) réserve les kits UI à des **maquettes** d'applications fonctionnant **uniquement** sur les systèmes Apple et interdit leur usage pour un logiciel fonctionnant sur un autre système ; la police San Francisco a sa propre licence. VODarr (application web, conteneur Linux) **n'utilise donc ni les kits, ni SF Pro embarquée, ni SF Symbols** : il s'inspire des principes publics des Human Interface Guidelines, sans copier l'identité ni les composants d'Apple.

## Alternatives envisagées

### Variante B « iPadOS, version outil » (plus dense, bordures, police à chasse fixe)
- Pour : plus d'information à l'écran.
- Rejetée par l'auteur au profit de la variante A.

### React / Vue / Svelte + build
- Contre : chaîne de build, dépendances npm, image et CI plus lourdes pour 4 écrans.
- Rejeté.

### htmx / Alpine.js
- Pour : moins de JavaScript.
- Contre : une dépendance, et le serveur devrait rendre du HTML traduit (contraire à l'ADR-0007).
- Rejeté.

### Tailwind / Bootstrap / bibliothèque de composants
- Contre : exclu par le cadrage ; rendu générique.
- Rejeté.

## Conséquences
- Aucun `node_modules`, aucun build : l'image copie `web/` tel quel.
- Le choix de direction visuelle est la prochaine étape d'architecture, avant tout écran.
- limite connue : rendu DOM manuel sans composant réactif ; envisager un micro-framework si les vues dépassent ~300 lignes chacune.
