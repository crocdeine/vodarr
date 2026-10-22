# ADR-0007 : Langues et codes de message

## Statut
Accepté (2026-10-22)

## Date
2026-10-22

## Contexte
- Tout doit exister en anglais (par défaut) **et** en français dès la V1, interface comme messages du pipeline et erreurs.
- La démo renvoyait des phrases françaises construites côté serveur.

## Décision
- **Le backend n'envoie jamais de phrase** : il envoie un code et des paramètres.
  ```python
  Msg("verify.partial", ok=18, total=19, tracker="TrackerA")
  # stocké : stages.msg_code = "verify.partial", stages.msg_params = '{"ok":18,"total":19,"tracker":"TrackerA"}'
  # erreur d'API : {"error": {"code": "verify.partial", "params": {...}}}
  ```
- **Dictionnaires** : `web/i18n/en.json` et `web/i18n/fr.json`, clés plates à points, valeurs avec paramètres nommés : `"verify.partial": "{ok}/{total} files identical on {tracker}"`. Pluriels : deux clés (`.one` / `.other`) choisies par l'interface quand le paramètre `count` est fourni.
- **Interface** : une fonction `t(code, params)` (≈ 15 lignes) ; langue = réglage utilisateur (`auto` / `en` / `fr`), `auto` = `navigator.language`, repli `en` ; code inconnu → affiché tel quel (visible en test).
- Dates, tailles, nombres : `Intl.DateTimeFormat` / `Intl.NumberFormat` du navigateur.
- **Tests** :
  - `en.json` et `fr.json` ont exactement les mêmes clés (AC-UI-05) ;
  - tout code émis par le backend (`Msg("…")` et codes d'erreur, recensés par expression régulière sur `src/`) existe dans les deux fichiers ;
  - tout paramètre `{x}` d'une valeur anglaise existe dans la valeur française correspondante.
- **README** : `README.md` (anglais) et `README.fr.md`, liés en tête de chacun. Docs internes (`docs/`) en français.

## Alternatives envisagées

### Bibliothèque i18n (i18next, gettext/Babel)
- Contre : dépendance et format (`.po`, build) pour deux langues et quelques centaines de clés.
- Rejeté : deux JSON + une fonction.

### Traduction côté serveur (`Accept-Language`)
- Contre : les messages stockés en base (étapes) seraient figés dans une langue ; changer de langue ne les traduirait pas.
- Rejeté : codes stockés, traduction à l'affichage.

## Conséquences
- Ajouter un message = ajouter la clé dans les deux JSON, sinon les tests échouent : la traduction à 100 % est garantie mécaniquement.
- Une troisième langue = un fichier JSON de plus, sans code.
