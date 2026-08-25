# Overlay Qt6 pour MTG Arena, sur Linux

Interface PyQt6 posée sur le moteur de
[unrealities/MTGA_Draft_17Lands](https://github.com/unrealities/MTGA_Draft_17Lands),
pensée pour tenir **à côté** d'Arena pendant un draft plutôt que d'être une
seconde application vers laquelle basculer.

Le moteur amont (lecture du log, jeux de données 17lands, conseiller, constructeur
de deck) est importé **sans aucune modification**. Seule l'interface est réécrite.

| | Lignes |
|---|---|
| `overlay_qt/` — notre interface | 2 827 |
| `src/` — leur moteur, réutilisé tel quel | 23 002 |
| `src/ui/` — leur interface Tk, non portée | 12 673 |

---

## Lancer

### Au démarrage de la session, recommandé

```bash
cd ~/mgta/MTGA_Draft_17Lands
.venv/bin/python -m overlay_qt.app --install-service
```

Installe un service utilisateur systemd et une entrée dans le menu des
applications. L'overlay démarre avec la session, **reste masqué tant qu'Arena
n'est pas lancé**, apparaît quand le jeu démarre et disparaît à sa fermeture.

```bash
systemctl --user status mtga-overlay-qt.service     # état
journalctl --user -u mtga-overlay-qt.service -f     # journal
.venv/bin/python -m overlay_qt.app --uninstall-service
```

### À la main

```bash
cd ~/mgta/MTGA_Draft_17Lands
.venv/bin/python -m overlay_qt.app
```

| Option | Effet |
|---|---|
| `--daemon` | Masqué tant qu'Arena n'est pas lancé |
| `-f <chemin>` | Forcer un `Player.log` précis |
| `--debug` | Journalisation détaillée |
| `--x11` | Forcer XWayland, seul moyen de retrouver la position de la fenêtre |
| `--install-service` / `--uninstall-service` | Service systemd et entrée de menu |

> **Ne jamais lancer avec `sudo`.** Root prendrait possession de `Sets/`,
> `Temp/`, `Logs/` et `Debug/`, qui deviendraient inaccessibles à votre compte.
> Réparation : `sudo chown -R "$USER":"$USER" {Debug,Sets,Temp,Logs}`

### Préalable indispensable dans Arena

Roue crantée → **Compte** → cocher **Detailed Logs (Plugin Support)** → quitter
et relancer le jeu. Sans cela Arena n'écrit **aucune** donnée de draft et aucun
outil ne peut rien afficher.

Le réglage **ne suit pas le compte entre installations** : l'activer sur Steam
Flatpak laisse une installation Steam native désactivée. Si vous alternez,
activez-le des deux côtés. L'overlay affiche une bannière violette quand il
détecte le cas.

### Installation des dépendances

```bash
python3 -m venv .venv
.venv/bin/pip install PyQt6 numpy Pillow pydantic requests numba scipy ttkbootstrap==1.20.3
```

`pynput`, déclaré en amont, échoue à compiler sous Linux (`evdev` réclame les
en-têtes du noyau) et n'est **importé nulle part** : dépendance morte, à ignorer.

---

## L'interface

Colonne étroite (340 px par défaut), sans cadre, toujours au-dessus, translucide.

```
┌─ Pack 1 Pick 5 ─────────── HOB QuickDraft  ◐ ⟳ ✕ ┐   barre de titre = poignée
│ 84  We Say Thee Nay!                             │   conseiller : les 3
│     Real Bomb · Improves Deck                    │   meilleurs choix avec
│ 76  Undercover Skrull                            │   les justifications
├──────────────────────────────────────────────────┤
│ U +25 W +23 │ 16-10-10-3 │ 42/21cr │ wheel 1     │   bande permanente
├──────────────────────────────────────────────────┤
│ Booster │ Pool (42) │ Deck                       │
│ Score  Carte              GIH WR   Wheel         │
│    84  We Say Thee Nay!    62.7      7%          │
└──────────────────────────────────────────────────┘
```

| Geste | Effet |
|---|---|
| Cliquer-glisser la barre de titre | Déplacer |
| Molette sur la barre de titre | Transparence, réglage fin |
| **◐** | Transparence par paliers |
| **⟳** | Relire le log, reconstruit un draft déjà en cours (~30 s) |
| Coin bas-droit | Redimensionner |
| Clic droit sur l'entête du tableau | Ajouter **et retirer** des colonnes |
| Survol d'une carte | Aperçu de l'image, disparaît en sortant |

Largeur, hauteur, opacité et **choix des colonnes** sont mémorisés entre deux
lancements, dans `Temp/overlay_qt_prefs.json`.

La **position** de la fenêtre ne peut pas l'être sous Wayland : le compositeur
seul décide du placement, `move()` est ignoré et `pos()` renvoie ce que Qt
croyait faire, pas où la fenêtre se trouve. Nous refusons donc d'enregistrer une
valeur qui serait une fiction. Deux solutions si cela vous gêne :

- une règle KWin, *Paramètres du système → Gestion des fenêtres → Règles*, en
  mémorisant la position pour cette fenêtre ;
- lancer avec `--x11`, où la position est enregistrée et restaurée normalement,
  au prix du rendu Wayland natif.

### Les trois onglets

**Booster** — le booster classé par le score du conseiller. Par défaut score,
carte, winrate GIH et probabilité de wheel ; mana, ALSA, IWD et jouabilité sont
disponibles au clic droit.

**Pool** — les cartes prises, la courbe de mana avec les créatures empilées sous
le total, le coût moyen, l'ouverture des couleurs.

**Deck** — les archétypes complets proposés par le moteur, avec leur note et un
pronostic de résultat. Deux modes :

- **Cible** : liste à cocher groupée par coût de mana, à pointer au fur et à
  mesure que vous ajoutez les cartes dans Arena. Compteurs par groupe et au total.
- **Écart** : une fois le deck soumis, comparaison avec la recommandation du
  moteur, rouge pour les cartes à retirer, vert pour celles à ajouter.

### Sealed

Détecté automatiquement. L'onglet Booster et le conseiller de pick sont masqués,
puisqu'un Sealed ne produit jamais de booster, et l'onglet Deck prend la main sur
le pool des six boosters.

---

## Ce que nous avons changé, et pourquoi

Chaque décision vient d'un test en conditions réelles, pas d'une préférence.

### Le format

Leur outil est une application de bureau : trop large pour rester au-dessus
d'Arena, obligeant à basculer sans cesse. D'où la colonne étroite et
translucide, le conseiller **en haut** parce que c'est la réponse à la seule
question posée, et une bande d'une ligne pour l'ouverture des couleurs, la
courbe et la pool, que leur version enterre sous la ligne de flottaison.

Le deck builder n'apparaît qu'**à la fin du draft**. Pendant les picks il
n'apporte rien et encombre.

### Trois fois la même limitation Wayland

Un motif qui revient et mérite d'être retenu :

1. **Déplacement de fenêtre** — `move()` est sans effet sur une fenêtre de
   premier niveau, le compositeur décide. Résolu par `startSystemMove()`.
2. **Aperçu de carte** — même cause. L'aperçu est devenu un widget **enfant**,
   positionné en coordonnées locales.
3. **Transparence** — `setWindowOpacity` n'est pas implémenté par le plugin
   Wayland. Résolu par le canal alpha des fonds, ce qui est meilleur : le texte
   reste opaque et lisible pendant que les cartes transparaissent.

### Trois défauts de leur interface, corrigés chez nous

- **Image de carte figée** : `CardToolTip.create()` n'a aucun chemin de
  fermeture, la fenêtre ne se referme qu'au clic suivant. Chez nous, survol pour
  afficher, sortie pour masquer.
- **Colonnes non retirables** : leur menu n'expose que l'ajout. Le nôtre est un
  menu à cases à cocher.
- **Import circulaire** entre `card_logic` et `advisor.deck_builder` : atteindre
  `deck_builder` en premier lève une `ImportError`. Leur interface y échappe par
  l'ordre de ses propres imports.

### Détails trouvés en testant

- La liaison du jeu de données doit tenir compte de **l'événement joué**, pas
  seulement de l'extension : sur MSH, choisir au code d'extension prenait
  `ContenderDraft (Top)`, un fichier sans aucun winrate.
- Les statistiques **par archétype** sont creuses. Quand la valeur est nulle, on
  retombe sur les données globales, sinon les deux tiers d'un booster affichent 0.
- Les **terrains de base** ne sont dans aucun jeu de données 17lands : la
  comparaison de decks porte sur les sorts uniquement.

---

## Correctifs proposables en amont

La branche `fix/linux-tcl9-and-log-discovery` ne contient **que** ces deux
correctifs, sans notre code Qt, pour une pull request propre.

- **Démarrage Linux cassé sur Tcl 9** (Fedora 42+, Nobara, Bazzite). Leur garde
  msgcat existe mais patche `msgs.initialize_localities`, alors que
  `Style.__init__` appelle `localization.initialize_localities()`, une référence
  copiée à l'import. Le garde ne couvrait donc jamais le vrai site d'appel.
- **Découverte du `Player.log`** : leur liste Linux ne contient qu'un chemin,
  Steam natif. Ajout de Steam Flatpak (le défaut sur Fedora et dérivés), snap,
  `.steam/root`, bibliothèques secondaires via `libraryfolders.vdf`, Lutris,
  Bottles, et canonicalisation des liens symboliques.

---

## Tests

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -q
```

**728 tests** au total, dont **56** ajoutés par nous : découverte du log Linux,
garde msgcat, chaîne complète du log jusqu'au tableau, liste à cocher,
comparaison de decks, Sealed, détection des logs détaillés.

Les tests amont ne sont pas modifiés : leur moteur étant intact, toute
régression de leur côté signalerait une erreur de la nôtre.

---

## Limites connues

- La **position** de la fenêtre n'est pas mémorisée sous Wayland, limitation du
  protocole et non de l'application. Contournements ci-dessus.
- **Aucun envoi de deck vers Arena** n'est possible : Arena n'offre pas d'import
  en événement limité et ne publie le deck qu'une fois soumis. D'où la liste à
  cocher plutôt qu'une automatisation qui serait de toute façon non autorisée.
- Écrans amont non portés : Sealed Studio, éditeur glisser-déposer, tier lists,
  comparateur de cartes. Leur fenêtre Tk reste lançable en parallèle
  (`.venv/bin/python main.py`).
