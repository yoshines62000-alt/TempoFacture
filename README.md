# TempoFacture

[![Dernière version](https://img.shields.io/github/v/release/yoshines62000-alt/TempoFacture?label=derni%C3%A8re%20version)](https://github.com/yoshines62000-alt/TempoFacture/releases/latest)
[![Téléchargements](https://img.shields.io/github/downloads/yoshines62000-alt/TempoFacture/total?label=t%C3%A9l%C3%A9chargements)](https://github.com/yoshines62000-alt/TempoFacture/releases/latest)

**[⬇️ Télécharger l'exécutable (.exe) — aucune installation requise](https://github.com/yoshines62000-alt/TempoFacture/releases/latest)**

Suivi du temps, gestion de clients/projets et facturation PDF pour
freelances — gratuit, open source, et 100 % local. Alternative libre à des
outils comme [Harvest](https://www.getharvest.com/),
[Toggl](https://toggl.com/) ou [RescueTime](https://www.rescuetime.com/),
qui facturent un abonnement mensuel pour ces mêmes fonctions de base (et dont
les tarifs peuvent grimper brutalement après un rachat — Harvest a été
racheté par Bending Spoons en 2025, avec des hausses de prix rapportées
allant jusqu'à x10).

Créez vos clients et projets, chronométrez votre travail (ou saisissez des
heures manuellement), puis générez une facture PDF professionnelle en un
clic à partir des heures non encore facturées.

## Fonctionnalités

- **Clients et projets** : chaque client a un taux horaire par défaut,
  chaque projet peut définir son propre taux (sinon celui du client
  s'applique). Archivage sans perte de l'historique — archiver un client
  qui a encore des heures non facturées déclenche un avertissement
  explicite, pour ne jamais les perdre de vue par inadvertance.
- **Recherche instantanée** : un champ de recherche au-dessus des listes
  de clients, de projets et de factures filtre les lignes affichées en
  temps réel (nom, email, client associé, numéro de facture, statut...).
- **Chronomètre en un clic** : démarrez/arrêtez le suivi du temps par
  projet, avec description libre. Le chronomètre en cours survit à une
  fermeture/réouverture de l'application (il repart de l'heure de
  démarrage réelle enregistrée en base).
- **Détection d'inactivité** : si vous restez inactif pendant qu'un
  chronomètre tourne, l'application le signale et propose de retirer ce
  temps mort — pour ne jamais facturer un client pendant une pause. Le
  seuil de déclenchement (5 minutes par défaut) se règle depuis l'onglet
  **Paramètres**. Comme pour les autres outils de cette suite, **aucune
  frappe clavier n'est jamais enregistrée** : seule la durée d'inactivité
  est mesurée (une simple horloge système), jamais le contenu tapé.
- **Saisie manuelle** : ajoutez directement un nombre d'heures à un projet,
  sans passer par le chronomètre.
- **Facturation PDF** : sélectionnez un client, générez en un clic une
  facture PDF regroupant toutes les heures non encore facturées par
  projet, avec calcul automatique de la TVA et du total.
- **Modèles de notes réutilisables** : enregistrez un texte de note (ex :
  conditions de paiement, message de remerciement) sous un nom, puis
  réappliquez-le en un clic sur une future facture au lieu de le
  retaper.
- **Facturation récurrente** : dupliquez une facture existante pour
  générer une nouvelle facture PDF reprenant exactement les mêmes lignes
  (mêmes projets, heures et taux) — pratique pour un forfait mensuel
  identique d'un mois sur l'autre, sans consommer de nouvelles heures
  suivies.
- **Réexport PDF** : régénérez le PDF d'une facture déjà émise (fichier
  perdu, à renvoyer...) sans rien changer aux montants, dates ou notes
  figés à l'émission ; seules les coordonnées actuelles du client sont
  reprises.
- **Alerte factures en retard** : les factures non payées dont l'échéance
  est dépassée sont mises en évidence (couleur + compteur récapitulatif)
  dans l'onglet Factures.
- **Suivi des statuts** : marquez une facture comme payée, non payée ou
  annulée. Annuler une facture ne fait jamais perdre du temps déjà suivi :
  les heures redeviennent facturables.
- **Export CSV** : exportez les entrées de temps ou l'historique des
  factures au format CSV (compatible Excel, avec BOM UTF-8), pour les
  transmettre à un comptable ou les analyser dans un tableur.
- **100 % local, zéro cloud** : toutes les données (clients, projets,
  temps, factures) sont stockées dans une base SQLite locale. Aucun compte,
  aucune connexion internet requise.
- **Indicateur de mise à jour** : au démarrage, un petit indicateur en bas
  de fenêtre signale si une nouvelle version est disponible (simple requête
  vers l'API publique GitHub Releases, aucune donnée personnelle transmise)
  et propose un lien direct vers la page de téléchargement. Peut être
  désactivé à tout moment depuis l'onglet **Paramètres** pour un usage
  strictement hors ligne.
- **Gratuit et open source, pour toujours** : pas de version payante, pas
  de fonctionnalité verrouillée derrière un abonnement.

## Démarrage rapide

1. [**Téléchargez `TempoFacture.exe`**](https://github.com/yoshines62000-alt/TempoFacture/releases/latest)
   depuis la dernière release.
2. Double-cliquez dessus : la fenêtre de l'application s'ouvre directement,
   sans installation, sans Python.

L'exécutable n'étant pas signé numériquement, Windows SmartScreen peut
afficher un avertissement au premier lancement : cliquez sur **Informations
complémentaires** puis **Exécuter quand même**.

## Lancer depuis le code source

Alternative à l'exécutable, pour les développeurs ou par souci de
transparence (voir [Installation](#installation) pour les dépendances) :
double-cliquez sur **[`Lancer.vbs`](Lancer.vbs)** — la fenêtre s'ouvre
directement, sans console. Vous pouvez créer un raccourci sur le Bureau (clic
droit sur `Lancer.vbs` → Envoyer vers → Bureau) pour un accès en un clic.

## Installation

Nécessite Python 3.9+ avec Tkinter (inclus dans les installations standard de
Python sous Windows), plus une dépendance légère :

```bash
python -m pip install -r requirements.txt
```

- **[fpdf2](https://py-pdf.github.io/fpdf2/)** : génération des factures PDF,
  pure Python, sans binaire externe requis.

## Utilisation

1. Onglet **Clients** : ajoutez vos clients (nom, email, adresse, taux
   horaire par défaut). Double-cliquez sur une ligne existante pour modifier
   ses informations (par exemple une revalorisation de tarif) - les
   factures déjà émises conservent toujours leurs montants d'origine.
2. Onglet **Projets** : créez un projet rattaché à un client, avec un taux
   horaire propre si besoin (sinon celui du client s'applique).
   Double-cliquez sur une ligne existante pour modifier son nom ou son taux
   (laissez le taux vide pour revenir à celui du client).
3. Onglet **Chronomètre** : choisissez un projet, cliquez sur **Démarrer**,
   travaillez, cliquez sur **Arrêter**. Vous pouvez aussi ajouter des heures
   manuellement.
4. Onglet **Factures** : choisissez un client, la liste des heures non
   facturées s'affiche automatiquement avec le montant estimé. Renseignez
   la TVA, cliquez sur **Générer la facture (PDF)**, choisissez où
   l'enregistrer. Si vous êtes exonéré de TVA (franchise en base), un
   rappel s'affiche automatiquement quand le taux est à 0 % pour ajouter la
   mention légale obligatoire (« TVA non applicable, art. 293 B du CGI »)
   dans les notes de la facture ; un modèle de note prêt à l'emploi portant
   ce nom est aussi fourni dès la première installation.
5. Onglet **Paramètres** : renseignez le nom et les informations de votre
   entreprise (affichés sur chaque facture générée), le délai de paiement
   par défaut, la devise, et le seuil d'inactivité du chronomètre. Une case
   à cocher permet aussi de désactiver la vérification de mise à jour au
   démarrage (activée par défaut).

## Confidentialité

- Aucune donnée ne quitte votre machine : pas de compte, pas de serveur, pas
  de télémétrie.
- La détection d'inactivité mesure uniquement une durée (via l'horloge
  système Windows) — elle n'enregistre jamais ce qui est tapé ou cliqué.
- Les données sont stockées dans `%APPDATA%\TempoFacture\tempofacture.sqlite`.
- Seule activité réseau de l'application : au démarrage, une requête GET
  optionnelle vers l'API publique GitHub Releases pour signaler une
  nouvelle version disponible (aucune donnée personnelle ni identifiant
  machine transmis). Désactivable depuis l'onglet **Paramètres** pour un
  usage strictement hors ligne.

## Sauvegarde et restauration

- Onglet **Paramètres > Sauvegarde** : le bouton « Sauvegarder les
  données... » enregistre une copie complète du fichier de données à
  l'emplacement de votre choix.
- Pour restaurer une sauvegarde : fermez TempoFacture, puis remplacez le
  fichier `%APPDATA%\TempoFacture\tempofacture.sqlite` par la copie de
  sauvegarde (le bouton « Ouvrir le dossier de données » y accède
  directement).

## Mes données — si l'application ne se lance plus

Tout est rangé dans un seul fichier SQLite (`tempofacture.sqlite`), un format public que
Python sait lire sans rien installer. Si un jour l'exécutable refuse de démarrer
— Windows qui bloque, un antivirus, une machine changée —, vos données restent
récupérables :

```bash
python secours_donnees.py "%APPDATA%\TempoFacture\tempofacture.sqlite" --dossier export
```

Cela écrit, dans le dossier `export`, **un CSV par table** (ouvrable dans un
tableur) et un `donnees.json` de l'ensemble. Le fichier d'origine est ouvert
**en lecture seule** : il n'est jamais modifié. Le script n'utilise que la
bibliothèque standard de Python, et un test le prouve sur une vraie base
(`tests/test_secours_donnees.py`).

Le script ne connaît pas le sens des colonnes — c'est l'application qui le
connaît — : il exporte toutes les tables telles quelles, à vous d'y retrouver
les vôtres.

## Créer un exécutable autonome (.exe)

Pour distribuer l'outil sans que le destinataire ait besoin d'installer
Python ni les dépendances, un exécutable Windows autonome peut être généré
avec [PyInstaller](https://pyinstaller.org/) :

```bash
python -m pip install pyinstaller
python -m PyInstaller TempoFacture.spec
```

L'exécutable est produit dans `dist/TempoFacture.exe` (fichier unique, sans
console). Le fichier `.spec` du dépôt fixe la configuration de build pour un
résultat reproductible. Les dossiers `build/` et `dist/` ne sont pas suivis
par Git.

## Tests

Une suite de tests automatisés couvre la logique pure (base de données,
calcul des lignes de facture, génération de PDF, chronomètre) avec des
scénarios réels (vraie base SQLite temporaire, vrai PDF généré sur disque).

```bash
python -m unittest discover tests -v
```

## Structure du projet

```
db.py                 # couche donnees SQLite : clients, projets, temps, factures
invoice.py            # calcul des lignes de facture et generation du PDF (fpdf2)
csv_export.py         # export CSV des heures et de l'historique des factures
timer.py              # moteur de chronometre + detection d'inactivite (ctypes)
update_checker.py     # verification de mise a jour via l'API GitHub Releases
gui.py                # interface graphique Tkinter
tests/                 # tests automatises
requirements.txt      # dependances (fpdf2)
Lancer.vbs            # raccourci de lancement double-clic (sans console)
Lancer.bat            # raccourci de lancement double-clic (avec console, pour debug)
TempoFacture.spec     # configuration de build PyInstaller (.exe autonome)
version_info.txt      # metadonnees de version Windows embarquees dans l'exe
icon.ico              # icone de l'application et de l'executable
fonts/                 # police Unicode embarquee dans les PDF (Noto Sans SC)
.gitignore
LICENSE               # licence MIT
README.md
```

## Licence

Ce projet est publié sous licence [MIT](LICENSE) : gratuit, open source, et
libre de réutilisation, modification et redistribution.

## Soutenir le projet

<div align="center">

**Cet outil est gratuit, open source, et le restera toujours.**
Pas de version payante, pas de fonctionnalité cachée derrière un paywall.

Si TempoFacture vous fait gagner du temps sur votre suivi d'activité et
votre facturation, un petit café est toujours très apprécié. 🙌

[![Offrez-moi un café sur Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/yoshines62000)

</div>
