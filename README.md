# Tessfood

Extension Home Assistant pour savoir **ce qu'il y a dans le frigo, le congélateur et les placards**, faire la **liste de courses** (reliée à Alexa) et préparer le **drive Intermarché** avec Claude.

## Installation

1. Home Assistant → Paramètres → Modules complémentaires → Boutique → ⋮ → **Dépôts** → ajouter `https://github.com/Tessman404/tessfood`
2. Installer **Tessfood**, cocher « Afficher dans la barre latérale », démarrer.
3. Ouvrir Tessfood → **Réglages** : service de notification du téléphone, liste Alexa (`todo.…`), capteur de porte du frigo (optionnel).

## Comment ça marche

- **Scanner au-dessus de la poubelle** : chaque emballage vide passé devant = sorti du stock.
- **Cartes de mode** (Réglages → Imprimer les cartes) : AJOUTER, OUVERT, JETÉ, ANNULER. On scanne la carte puis les produits ; au bout de 90 s sans scan, on revient à « sortie ».
- **Produit inconnu** : le nom et la photo viennent tout seuls d'Open Food Facts.
- **Minimum** sur une fiche produit : en dessous, le produit part sur la liste de courses (et sur Alexa).
- **Liste Alexa** : « Alexa, ajoute du café » arrive dans Tessfood, et inversement.
- **Drive** : « Copier la demande pour Claude » → Claude remplit le panier Intermarché (tu valides et tu paies toi-même) → « Commande passée » → au retour « Courses récupérées » : tout entre dans le stock, ce qui manquait reste sur la liste.
- **Anti-oubli** : chaque semaine, Tessfood te demande de confirmer les produits « sûrement finis » d'après tes habitudes ; alerte chaque matin pour ce qui périme ; alerte si la porte du frigo reste ouverte.

## Scanner

Voir `esphome/tessfood-scanner.yaml` (module GM861 + ESP32 en Wi-Fi). Le scanner envoie le code à `http://<Home Assistant>:8098/scan` avec le jeton affiché dans Réglages.

Chaque scan crée aussi un événement Home Assistant `tessfood_scan` (pour un bip, une lumière, une annonce…).

## Capteurs créés

`sensor.tessfood_stock`, `sensor.tessfood_courses`, `sensor.tessfood_a_consommer`, `sensor.tessfood_a_verifier`.
