"""Interface Tkinter de TempoFacture : clients, projets, chronometre,
factures et parametres, tous relies a la meme base SQLite locale."""

from __future__ import annotations

import logging
import queue
import sys
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, TOP, X, Y, BooleanVar, StringVar, Tk, ttk, messagebox, simpledialog

import update_checker
from db import Database, TVA_EXEMPTION_NOTE
from invoice import LineItem, build_line_items, compute_totals, format_amount, generate_invoice_pdf, reexport_invoice_pdf
from timer import Timer, get_idle_seconds, get_uptime_seconds
from csv_export import export_time_entries_csv, export_invoices_csv

APP_TITLE = "TempoFacture"
DONATE_URL = "https://ko-fi.com/yoshines62000"
APP_VERSION = "1.0.18"
UPDATE_REPO = "yoshines62000-alt/TempoFacture"
RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"
IDLE_THRESHOLD_SECONDS = 5 * 60  # valeur par defaut si aucun reglage n'existe encore en base
IDLE_CHECK_INTERVAL_MS = 15_000
# Marge de tolerance sous laquelle un ecart entre l'uptime machine et le temps
# ecoule depuis le demarrage du chronometre est ignore (delai de demarrage de
# l'app, imprecision des deux horloges) plutot que traite comme une extinction
# complete du PC a signaler a l'utilisateur - voir _offline_gap_seconds.
OFFLINE_GAP_THRESHOLD_SECONDS = 60
SEARCH_DEBOUNCE_MS = 250  # attend une pause dans la frappe avant de rafraichir une liste (factures/clients/projets)
LOG_FILE_NAME = "tempofacture.log"
# Nombre max d'entrees de temps affichees d'un coup dans le tableau de l'onglet
# Chronometre (constat d'audit, 2026-07-28) : sans limite, un historique long
# (des annees de suivi) ralentissait le rafraichissement de l'UI a chaque
# demarrage/arret du chronometre, alors que _refresh_time_entries() est
# appelee tres frequemment. Seules les entrees les plus recentes sont
# affichees ; le total reel reste visible via le libelle sous le tableau, et
# l'export CSV (_export_time_entries_csv) continue de porter sur TOUTES les
# entrees, jamais seulement celles affichees. Meme principe que
# HISTORY_DISPLAY_LIMIT dans DownloadOrganizer.
ENTRIES_DISPLAY_LIMIT = 200

_logger = logging.getLogger("tempofacture")


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def _data_dir() -> Path:
    appdata = Path.home() / "AppData" / "Roaming" / "TempoFacture"
    return appdata


def _log_path() -> Path:
    return _data_dir() / "logs" / LOG_FILE_NAME


def _configure_logging() -> Path:
    """Journalise les erreurs dans un fichier du dossier de donnees de
    l'app plutot que de les laisser filer sur stderr : l'executable
    empaquete est construit avec console=False (voir TempoFacture.spec),
    donc aucun terminal n'est attache et stderr n'est visible nulle part
    pour l'utilisateur final. Sans ce fichier, un bug non prevu echouait
    entierement en silence - aucune trace exploitable pour un rapport de
    bug (bug trouve a l'audit, voir D1).

    Contenu journalise : uniquement le type/message de l'exception et la
    pile d'appels (fichiers, lignes, code source) telle que produite par
    traceback.format_exception - jamais un contenu de la base de donnees.
    Aucun message d'erreur "metier" de ce projet n'embarque de donnee
    client/financiere (voir les `raise` de db.py/invoice.py/timer.py : tous
    des messages fixes ou parametres par de simples identifiants/statuts,
    jamais un nom, un email, une adresse ou un montant).
    """
    log_dir = _data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_FILE_NAME
    _logger.setLevel(logging.INFO)
    # Retire d'abord d'eventuels anciens handlers : cette fonction peut etre
    # appelee plusieurs fois dans le meme processus (ex: plusieurs instances
    # successives de TempoFactureApp dans les tests, chacune redirigeant
    # _data_dir() vers un dossier temporaire different) - sans ce nettoyage,
    # chaque appel ajouterait un handler de plus, dupliquant chaque ligne
    # journalisee et gardant ouvert un fichier d'un ancien dossier obsolete.
    for handler in list(_logger.handlers):
        handler.close()
        _logger.removeHandler(handler)
    handler = logging.FileHandler(str(path), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    _logger.addHandler(handler)
    _logger.propagate = False
    return path


def _handle_uncaught_exception(exc_type, exc_value, exc_tb) -> None:
    """Gestionnaire d'exception Tk global (branche sur
    root.report_callback_exception dans TempoFactureApp.__init__).

    Par defaut, quand un callback Tkinter (clic de bouton, etc.) leve une
    exception non geree, Tkinter se contente d'imprimer la trace sur stderr
    puis CONTINUE l'execution sans rien signaler : l'utilisateur clique et
    il ne se passe rigoureusement rien de visible, sans aucune trace
    exploitable dans l'exe empaquete (console=False) (bug trouve a l'audit,
    voir D1). Ce gestionnaire journalise systematiquement l'exception puis
    informe clairement l'utilisateur, avec le chemin du fichier ou la trace
    complete a ete enregistree."""
    _logger.error("Exception non geree dans un callback Tk", exc_info=(exc_type, exc_value, exc_tb))
    try:
        messagebox.showerror(
            APP_TITLE,
            "Une erreur inattendue s'est produite et l'action demandee n'a "
            "pas pu aboutir.\n\n"
            f"Les details techniques ont ete enregistres dans :\n{_log_path()}\n\n"
            "Vous pouvez joindre ce fichier a un rapport de bug.",
        )
    except Exception:
        # L'important est que la journalisation ci-dessus ait deja eu lieu :
        # un gestionnaire d'exceptions ne doit jamais lui-meme lever, ce qui
        # remplacerait un echec silencieux par un plantage plus silencieux
        # encore.
        pass


class TempoFactureApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        # 640px de hauteur coupait le bas de l'onglet Factures (le plus
        # charge : ligne de boutons "Marquer payee"/"Dupliquer"/etc.) qui a
        # besoin de 649px pour s'afficher entierement (mesure via
        # winfo_reqheight() - bug trouve a l'audit). 760px laisse une marge
        # confortable au-dessus de ce minimum sur un ecran 1920x1080.
        self.root.geometry("980x760")

        # Doit etre en place avant tout le reste : c'est ce qui garantit
        # qu'une exception non prevue dans n'importe quel callback (clic de
        # bouton, etc.) sera journalisee et signalee a l'utilisateur au lieu
        # d'echouer silencieusement (voir D1 de l'audit).
        _configure_logging()
        self.root.report_callback_exception = _handle_uncaught_exception

        self.db = Database(_data_dir() / "tempofacture.sqlite")
        self.timer = Timer(self.db)
        self._idle_prompt_open = False

        icon_path = _resource_path("icon.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except Exception:
                pass

        bottom_bar = ttk.Frame(self.root)
        bottom_bar.pack(fill=X, side="bottom")
        ttk.Label(bottom_bar, text=f"v{APP_VERSION}", foreground="#666").pack(side=LEFT, padx=(8, 0), pady=4)
        self.update_status_var = StringVar(value="")
        self.update_status_label = ttk.Label(bottom_bar, textvariable=self.update_status_var, foreground="#666")
        self.update_status_label.pack(side=LEFT, padx=(6, 0), pady=4)
        donate_label = ttk.Label(bottom_bar, text="☕ Soutenir le projet", foreground="#0645AD", cursor="hand2")
        donate_label.pack(side=RIGHT, padx=8, pady=4)
        donate_label.bind("<Button-1>", lambda event: webbrowser.open(DONATE_URL))

        self._update_check_queue = queue.Queue()
        self._maybe_start_update_check()

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=BOTH, expand=True, padx=8, pady=8)

        self.clients_tab = ttk.Frame(notebook)
        self.projects_tab = ttk.Frame(notebook)
        self.timer_tab = ttk.Frame(notebook)
        self.invoices_tab = ttk.Frame(notebook)
        self.settings_tab = ttk.Frame(notebook)

        notebook.add(self.clients_tab, text="Clients")
        notebook.add(self.projects_tab, text="Projets")
        notebook.add(self.timer_tab, text="Chronometre")
        notebook.add(self.invoices_tab, text="Factures")
        notebook.add(self.settings_tab, text="Parametres")

        self._build_clients_tab()
        self._build_projects_tab()
        self._build_timer_tab()
        self._build_invoices_tab()
        self._build_settings_tab()

        self._refresh_clients()
        self._refresh_projects()
        self._refresh_timer_project_choices()
        self._refresh_invoices()
        self._restore_running_timer()

        # Empeche l'utilisateur de redimensionner la fenetre en dessous de la
        # taille requise par l'onglet Factures (le plus charge), pour ne
        # jamais recouper sa rangee de boutons d'action (bug trouve a
        # l'audit, voir le commentaire sur geometry() ci-dessus).
        self.root.update_idletasks()
        self.root.minsize(max(700, self.root.winfo_reqwidth()), max(700, self.root.winfo_reqheight()))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_timer()
        self._check_idle()

    def _maybe_start_update_check(self):
        """Lance la verification de mise a jour au demarrage, sauf si
        l'utilisateur l'a desactivee depuis l'onglet Parametres. Activee
        par defaut ("1") pour ne pas changer le comportement existant :
        cette verification (simple GET vers l'API publique GitHub, aucune
        donnee personnelle ni identifiant machine transmis, voir
        update_checker.py) etait auparavant inconditionnelle, sans aucun
        moyen de la desactiver - ce qui nuancait legerement l'affirmation
        du README/de l'onglet Parametres selon laquelle aucune connexion
        internet n'est necessaire (bug trouve a l'audit, voir H1). Extrait
        de __init__() dans sa propre methode pour rester testable
        isolement (voir tests/test_gui_smoke.py)."""
        if self.db.get_setting("check_updates_on_startup", "1") == "1":
            update_checker.start_update_check(APP_VERSION, UPDATE_REPO, self._update_check_queue)
            self.root.after(500, self._poll_update_check)

    def _poll_update_check(self):
        try:
            status, tag = self._update_check_queue.get_nowait()
        except queue.Empty:
            self.root.after(500, self._poll_update_check)
            return
        if status == "update_available":
            self.update_status_var.set(f"Mise a jour disponible : {tag} - Telecharger")
            self.update_status_label.configure(foreground="#0645AD", cursor="hand2")
            self.update_status_label.bind("<Button-1>", lambda event: webbrowser.open(RELEASES_URL))
        elif status == "up_to_date":
            self.update_status_var.set("A jour")
            self.update_status_label.configure(foreground="#1B7A1B", cursor="")
        # "check_failed" (hors ligne, GitHub inaccessible...) : on ne
        # revendique rien plutot que d'afficher a tort "a jour".

    # -- utilitaires communs ------------------------------------------------

    def _client_choices(self):
        clients = self.db.list_clients()
        return clients, [f"{c['id']} - {c['name']}" for c in clients]

    def _project_choices(self, client_id=None):
        projects = self.db.list_projects(client_id=client_id)
        return projects, [f"{p['id']} - {p['name']}" for p in projects]

    @staticmethod
    def _parse_id(combo_value: str):
        if not combo_value:
            return None
        return int(combo_value.split(" - ", 1)[0])

    @staticmethod
    def _configure_tree_columns(tree, columns_spec, growable_columns=()):
        """Configure les colonnes d'un Treeview a partir de
        columns_spec = [(col, label, largeur_initiale), ...].

        Par defaut, ttk etend TOUTES les colonnes (stretch=1) pour occuper
        l'espace supplementaire quand la fenetre s'agrandit - mais de facon
        egale, y compris des colonnes qui n'ont pas vocation a grandir (ID,
        montant, statut...), ce qui gaspille l'espace disponible sur des
        colonnes numeriques etroites au lieu de l'offrir a la colonne de
        texte libre qui en beneficierait reellement (ex. Description/
        Adresse - bug trouve a l'audit, voir E4). Seules les colonnes
        listees dans growable_columns recoivent stretch=True ; toutes les
        autres sont fixees a leur largeur initiale (stretch=False), qui ne
        bougent donc plus au redimensionnement."""
        for col, label, width in columns_spec:
            tree.heading(col, text=label)
            tree.column(col, width=width, anchor="w", stretch=col in growable_columns)

    # -- onglet Clients -------------------------------------------------------

    def _build_clients_tab(self):
        frame = self.clients_tab
        form = ttk.Frame(frame)
        form.pack(fill=X, padx=10, pady=10)

        self.client_name_var = StringVar()
        self.client_email_var = StringVar()
        self.client_address_var = StringVar()
        self.client_rate_var = StringVar(value="0")

        ttk.Label(form, text="Nom").grid(row=0, column=0, sticky="w")
        self.client_name_entry = ttk.Entry(form, textvariable=self.client_name_var, width=30)
        self.client_name_entry.grid(row=0, column=1, padx=5)
        ttk.Label(form, text="Email").grid(row=0, column=2, sticky="w")
        self.client_email_entry = ttk.Entry(form, textvariable=self.client_email_var, width=25)
        self.client_email_entry.grid(row=0, column=3, padx=5)
        ttk.Label(form, text="Taux horaire").grid(row=0, column=4, sticky="w")
        self.client_rate_entry = ttk.Entry(form, textvariable=self.client_rate_var, width=8)
        self.client_rate_entry.grid(row=0, column=5, padx=5)

        ttk.Label(form, text="Adresse").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.client_address_entry = ttk.Entry(form, textvariable=self.client_address_var, width=60)
        self.client_address_entry.grid(
            row=1, column=1, columnspan=4, sticky="we", pady=(5, 0)
        )
        ttk.Button(form, text="Ajouter le client", command=self._add_client).grid(row=1, column=5, pady=(5, 0))
        # Valider le formulaire au clavier (touche Entree) plutot que de
        # forcer un clic souris sur "Ajouter le client" : l'usage courant de
        # l'app implique des saisies frequentes et rapides, et aucun
        # formulaire de l'application ne se soumettait au clavier avant ce
        # correctif (bug trouve a l'audit, voir E2).
        for entry in (self.client_name_entry, self.client_email_entry, self.client_rate_entry, self.client_address_entry):
            entry.bind("<Return>", lambda event: self._add_client())

        search_row = ttk.Frame(frame)
        search_row.pack(fill=X, padx=10, pady=(0, 5))
        ttk.Label(search_row, text="Rechercher :").pack(side=LEFT)
        self.client_search_var = StringVar()
        ttk.Entry(search_row, textvariable=self.client_search_var, width=30).pack(side=LEFT, padx=5)
        self._client_search_after_id = None
        # Debounce (voir SEARCH_DEBOUNCE_MS) : ce mecanisme n'existait
        # auparavant que pour la recherche Factures, pas pour Clients/
        # Projets, qui rafraichissaient donc la liste entiere a CHAQUE
        # frappe - une incoherence de robustesse entre les 3 recherches de
        # l'application, aggravee par le pattern N+1 corrige en C1 (bug
        # trouve a l'audit, voir C3).
        self.client_search_var.trace_add("write", lambda *_: self._on_client_search_changed())

        columns = ("id", "name", "email", "rate", "archived")
        self.clients_tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        self._configure_tree_columns(
            self.clients_tree,
            [
                ("id", "ID", 40), ("name", "Nom", 200), ("email", "Email", 200),
                ("rate", "Taux horaire", 100), ("archived", "Archive", 70),
            ],
            growable_columns=("name", "email"),
        )
        self.clients_tree.pack(fill=BOTH, expand=True, padx=10, pady=(0, 5))
        self.clients_tree.bind("<Double-1>", self._edit_client)

        actions = ttk.Frame(frame)
        actions.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Button(actions, text="Archiver / desarchiver", command=self._toggle_client_archived).pack(side=LEFT)
        ttk.Label(actions, text="Double-cliquez sur une ligne pour la modifier.", foreground="#666").pack(
            side=LEFT, padx=(10, 0)
        )

    def _add_client(self):
        name = self.client_name_var.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "Le nom du client est obligatoire.")
            return
        try:
            rate = float(self.client_rate_var.get().replace(",", ".") or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Le taux horaire doit etre un nombre.")
            return
        if rate < 0:
            messagebox.showwarning(APP_TITLE, "Le taux horaire ne peut pas etre negatif.")
            return
        self.db.add_client(name, self.client_email_var.get().strip(), self.client_address_var.get().strip(), rate)
        self.client_name_var.set("")
        self.client_email_var.set("")
        self.client_address_var.set("")
        self.client_rate_var.set("0")
        self._refresh_clients()
        self._refresh_timer_project_choices()

    def _on_client_search_changed(self):
        # Meme mecanisme de debounce que _on_invoice_search_changed (voir
        # SEARCH_DEBOUNCE_MS) : annule un rafraichissement encore en
        # attente et en reprogramme un seul, apres une courte pause dans la
        # frappe, plutot que de rafraichir a chaque caractere tape (bug
        # trouve a l'audit, voir C3).
        if self._client_search_after_id is not None:
            self.root.after_cancel(self._client_search_after_id)
        self._client_search_after_id = self.root.after(SEARCH_DEBOUNCE_MS, self._run_debounced_client_refresh)

    def _run_debounced_client_refresh(self):
        self._client_search_after_id = None
        self._refresh_clients()

    def _refresh_clients(self):
        self.clients_tree.delete(*self.clients_tree.get_children())
        query = self.client_search_var.get().strip().lower() if hasattr(self, "client_search_var") else ""
        # self._currency() interroge la table "settings" (voir _currency()
        # ci-dessous) : hissee hors de la boucle pour n'etre appelee qu'une
        # seule fois au lieu d'une fois par client affiche - meme anti-
        # pattern (accessoire du N+1 deja corrige, voir C1) que celui deja
        # traite dans _refresh_projects() (bug trouve a l'audit, voir C2).
        currency = self._currency()
        for client in self.db.list_clients(include_archived=True):
            if query and query not in client["name"].lower() and query not in client["email"].lower():
                continue
            self.clients_tree.insert("", END, iid=str(client["id"]), values=(
                client["id"], client["name"], client["email"],
                format_amount(client["hourly_rate"], currency), "Oui" if client["archived"] else "Non",
            ))

    def _toggle_client_archived(self):
        selection = self.clients_tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Selectionnez un client d'abord.")
            return
        client_id = int(selection[0])
        client = self.db.get_client(client_id)
        archiving = not client["archived"]
        if archiving:
            # Archiver un client le fait disparaitre du combo de l'onglet
            # Factures (_refresh_invoices/_generate_invoice utilisent
            # list_clients(), qui exclut les clients archives par defaut) :
            # ses heures non facturees resteraient alors invisibles alors
            # qu'elles existent toujours en base. Meme piege que celui deja
            # documente pour les projets (voir list_projects_for_active_clients
            # ci-dessus), mais pour l'archivage direct d'un client.
            uninvoiced = self.db.list_time_entries(client_id=client_id, uninvoiced_only=True)
            if uninvoiced:
                count = len(uninvoiced)
                plural = "s" if count > 1 else ""
                if not messagebox.askyesno(
                    APP_TITLE,
                    f"Ce client a {count} heure{plural} non facturee{plural}.\n"
                    "Une fois archive, il n'apparaitra plus dans l'onglet Factures et ces heures "
                    "resteront invisibles tant qu'il n'est pas desarchive.\n\n"
                    "Archiver quand meme ?",
                ):
                    return
        self.db.update_client(client_id, archived=1 if archiving else 0)
        self._refresh_clients()
        # Un client archive/desarchive doit disparaitre/reapparaitre partout
        # ou sa liste est utilisee (projets, chronometre, factures), pas
        # seulement dans son propre onglet.
        self._refresh_projects()
        self._refresh_timer_project_choices()
        self._refresh_invoices()

    def _prompt_client_fields(self, title: str, initial_name: str, initial_email: str, initial_address: str, initial_rate):
        """Petit dialogue (nom, email, adresse, taux horaire) - renvoie un
        dict de chaines brutes (non validees) ou None si annule. Meme
        pattern que _prompt_time_entry_fields, reutilise ici pour l'edition
        d'un client existant (voir _edit_client)."""
        from tkinter import Toplevel

        dialog = Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        name_var = StringVar(value=initial_name)
        email_var = StringVar(value=initial_email)
        address_var = StringVar(value=initial_address)
        rate_var = StringVar(value="" if initial_rate is None else str(initial_rate))
        result = {}

        ttk.Label(dialog, text="Nom").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))
        ttk.Entry(dialog, textvariable=name_var, width=30).grid(row=0, column=1, padx=10, pady=(10, 5))
        ttk.Label(dialog, text="Email").grid(row=1, column=0, sticky="w", padx=10)
        ttk.Entry(dialog, textvariable=email_var, width=30).grid(row=1, column=1, padx=10)
        ttk.Label(dialog, text="Adresse").grid(row=2, column=0, sticky="w", padx=10)
        ttk.Entry(dialog, textvariable=address_var, width=30).grid(row=2, column=1, padx=10)
        ttk.Label(dialog, text="Taux horaire").grid(row=3, column=0, sticky="w", padx=10)
        ttk.Entry(dialog, textvariable=rate_var, width=30).grid(row=3, column=1, padx=10, pady=(0, 10))
        ttk.Label(
            dialog, text="Le nouveau taux ne s'applique qu'aux futures factures :\nles factures deja emises gardent leur montant d'origine.",
            foreground="#666", justify=LEFT,
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 10))

        def on_ok():
            result["name"] = name_var.get().strip()
            result["email"] = email_var.get().strip()
            result["address"] = address_var.get().strip()
            result["hourly_rate"] = rate_var.get().strip()
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=5, column=0, columnspan=2, pady=(0, 10))
        ttk.Button(buttons, text="Enregistrer", command=on_ok).pack(side=LEFT, padx=5)
        ttk.Button(buttons, text="Annuler", command=dialog.destroy).pack(side=LEFT, padx=5)

        # Entree valide le dialogue (equivalent clavier de "Enregistrer"),
        # Echap l'annule (equivalent clavier de "Annuler") - lie sur le
        # Toplevel lui-meme plutot que sur chaque champ individuellement :
        # un evenement clavier non consomme par le widget avec le focus
        # (aucune des Entry ci-dessus ne lie <Return>/<Escape>) remonte
        # naturellement jusqu'au binding du Toplevel qui les contient (bug
        # trouve a l'audit, voir E2).
        dialog.bind("<Return>", lambda event: on_ok())
        dialog.bind("<Escape>", lambda event: dialog.destroy())

        dialog.wait_window()
        return result or None

    def _edit_client(self, event=None):
        """Double-clic sur une ligne de clients_tree : ouvre un dialogue
        pre-rempli permettant de corriger nom/email/adresse/taux horaire
        (bug trouve a l'audit, voir E3 : la couche db supportait deja
        pleinement cette edition via update_client(), mais aucun chemin
        d'interface graphique ne l'exposait - le seul contournement
        possible etait d'archiver le client et d'en recreer un nouveau,
        scindant artificiellement son historique)."""
        selection = self.clients_tree.selection()
        if not selection:
            return
        client_id = int(selection[0])
        client = self.db.get_client(client_id)
        if client is None:
            return
        result = self._prompt_client_fields(
            "Modifier le client", client["name"], client["email"], client["address"], client["hourly_rate"]
        )
        if result is None:
            return
        if not result["name"]:
            messagebox.showwarning(APP_TITLE, "Le nom du client est obligatoire.")
            return
        try:
            rate = float(result["hourly_rate"].replace(",", ".") or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Le taux horaire doit etre un nombre.")
            return
        if rate < 0:
            messagebox.showwarning(APP_TITLE, "Le taux horaire ne peut pas etre negatif.")
            return
        self.db.update_client(
            client_id, name=result["name"], email=result["email"], address=result["address"], hourly_rate=rate
        )
        # Le nom/taux d'un client apparait dans plusieurs onglets (Projets,
        # Chronometre, Factures) : les rafraichir tous, comme pour
        # l'archivage (voir _toggle_client_archived ci-dessus).
        self._refresh_clients()
        self._refresh_projects()
        self._refresh_timer_project_choices()
        self._refresh_invoices()

    # -- onglet Projets -------------------------------------------------------

    def _build_projects_tab(self):
        frame = self.projects_tab
        form = ttk.Frame(frame)
        form.pack(fill=X, padx=10, pady=10)

        self.project_client_var = StringVar()
        self.project_name_var = StringVar()
        self.project_rate_var = StringVar()

        ttk.Label(form, text="Client").grid(row=0, column=0, sticky="w")
        self.project_client_combo = ttk.Combobox(form, textvariable=self.project_client_var, width=25, state="readonly")
        self.project_client_combo.grid(row=0, column=1, padx=5)
        ttk.Label(form, text="Nom du projet").grid(row=0, column=2, sticky="w")
        self.project_name_entry = ttk.Entry(form, textvariable=self.project_name_var, width=25)
        self.project_name_entry.grid(row=0, column=3, padx=5)
        ttk.Label(form, text="Taux (optionnel)").grid(row=0, column=4, sticky="w")
        self.project_rate_entry = ttk.Entry(form, textvariable=self.project_rate_var, width=10)
        self.project_rate_entry.grid(row=0, column=5, padx=5)
        ttk.Button(form, text="Ajouter le projet", command=self._add_project).grid(row=0, column=6, padx=5)
        # Voir le commentaire equivalent dans _build_clients_tab (bug trouve
        # a l'audit, voir E2) : soumission du formulaire au clavier.
        for entry in (self.project_name_entry, self.project_rate_entry):
            entry.bind("<Return>", lambda event: self._add_project())

        search_row = ttk.Frame(frame)
        search_row.pack(fill=X, padx=10, pady=(0, 5))
        ttk.Label(search_row, text="Rechercher :").pack(side=LEFT)
        self.project_search_var = StringVar()
        ttk.Entry(search_row, textvariable=self.project_search_var, width=30).pack(side=LEFT, padx=5)
        self._project_search_after_id = None
        # Voir le commentaire equivalent dans _build_clients_tab (bug
        # trouve a l'audit, voir C3) : debounce de la recherche.
        self.project_search_var.trace_add("write", lambda *_: self._on_project_search_changed())

        columns = ("id", "client", "name", "rate", "archived")
        self.projects_tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        self._configure_tree_columns(
            self.projects_tree,
            [
                ("id", "ID", 40), ("client", "Client", 160), ("name", "Projet", 200),
                ("rate", "Taux effectif", 110), ("archived", "Archive", 70),
            ],
            growable_columns=("client", "name"),
        )
        self.projects_tree.pack(fill=BOTH, expand=True, padx=10, pady=(0, 5))
        self.projects_tree.bind("<Double-1>", self._edit_project)

        actions = ttk.Frame(frame)
        actions.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Button(actions, text="Archiver / desarchiver", command=self._toggle_project_archived).pack(side=LEFT)
        ttk.Label(actions, text="Double-cliquez sur une ligne pour la modifier.", foreground="#666").pack(
            side=LEFT, padx=(10, 0)
        )

    def _add_project(self):
        client_id = self._parse_id(self.project_client_var.get())
        name = self.project_name_var.get().strip()
        if client_id is None or not name:
            messagebox.showwarning(APP_TITLE, "Choisissez un client et un nom de projet.")
            return
        rate_text = self.project_rate_var.get().strip().replace(",", ".")
        rate = None
        if rate_text:
            try:
                rate = float(rate_text)
            except ValueError:
                messagebox.showwarning(APP_TITLE, "Le taux horaire doit etre un nombre.")
                return
            if rate < 0:
                messagebox.showwarning(APP_TITLE, "Le taux horaire ne peut pas etre negatif.")
                return
        self.db.add_project(client_id, name, hourly_rate=rate)
        self.project_name_var.set("")
        self.project_rate_var.set("")
        self._refresh_projects()
        self._refresh_timer_project_choices()

    def _on_project_search_changed(self):
        # Voir le commentaire equivalent dans _on_client_search_changed
        # (bug trouve a l'audit, voir C3).
        if self._project_search_after_id is not None:
            self.root.after_cancel(self._project_search_after_id)
        self._project_search_after_id = self.root.after(SEARCH_DEBOUNCE_MS, self._run_debounced_project_refresh)

    def _run_debounced_project_refresh(self):
        self._project_search_after_id = None
        self._refresh_projects()

    def _refresh_projects(self):
        clients, client_labels = self._client_choices()
        self.project_client_combo["values"] = client_labels
        self.projects_tree.delete(*self.projects_tree.get_children())
        # Precharge nom ET taux horaire de chaque client en une seule
        # requete, puis calcule le taux effectif de chaque projet en Python
        # (meme logique que Database.effective_hourly_rate) plutot que
        # d'appeler self.db.effective_hourly_rate(project["id"]) A
        # L'INTERIEUR de la boucle - cet appel refait un get_project() (la
        # ligne est pourtant deja disponible ici) puis potentiellement un
        # get_client(), soit jusqu'a 2 requetes SQL de plus par projet
        # affiche. Exactement le meme pattern N+1 deja corrige pour les
        # factures (voir get_all_invoice_line_items()/_refresh_invoices()),
        # mesure a l'audit a ~96x plus lent avec 2000 projets (bug trouve a
        # l'audit, voir C1). self._currency() est egalement sorti de la
        # boucle (une seule requete "settings" au lieu d'une par ligne).
        clients_by_id = {c["id"]: c for c in self.db.list_clients(include_archived=True)}
        currency = self._currency()
        query = self.project_search_var.get().strip().lower() if hasattr(self, "project_search_var") else ""
        for project in self.db.list_projects(include_archived=True):
            client = clients_by_id.get(project["client_id"])
            client_name = client["name"] if client is not None else "?"
            if query and query not in project["name"].lower() and query not in client_name.lower():
                continue
            if project["hourly_rate"] is not None:
                effective_rate = float(project["hourly_rate"])
            elif client is not None:
                effective_rate = float(client["hourly_rate"])
            else:
                effective_rate = 0.0
            self.projects_tree.insert("", END, iid=str(project["id"]), values=(
                project["id"], client_name, project["name"],
                format_amount(effective_rate, currency),
                "Oui" if project["archived"] else "Non",
            ))

    def _toggle_project_archived(self):
        selection = self.projects_tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Selectionnez un projet d'abord.")
            return
        project_id = int(selection[0])
        project = self.db.get_project(project_id)
        self.db.update_project(project_id, archived=0 if project["archived"] else 1)
        self._refresh_projects()
        self._refresh_timer_project_choices()

    def _prompt_project_fields(self, title: str, initial_name: str, initial_rate):
        """Petit dialogue (nom, taux horaire optionnel) - renvoie un dict de
        chaines brutes (non validees) ou None si annule. initial_rate peut
        etre None (le projet herite alors du taux de son client) ; laisser
        le champ vide a la validation revient au meme comportement."""
        from tkinter import Toplevel

        dialog = Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        name_var = StringVar(value=initial_name)
        rate_var = StringVar(value="" if initial_rate is None else str(initial_rate))
        result = {}

        ttk.Label(dialog, text="Nom du projet").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))
        ttk.Entry(dialog, textvariable=name_var, width=30).grid(row=0, column=1, padx=10, pady=(10, 5))
        ttk.Label(dialog, text="Taux horaire (vide = taux du client)").grid(row=1, column=0, sticky="w", padx=10)
        ttk.Entry(dialog, textvariable=rate_var, width=30).grid(row=1, column=1, padx=10, pady=(0, 10))

        def on_ok():
            result["name"] = name_var.get().strip()
            result["hourly_rate"] = rate_var.get().strip()
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, columnspan=2, pady=(0, 10))
        ttk.Button(buttons, text="Enregistrer", command=on_ok).pack(side=LEFT, padx=5)
        ttk.Button(buttons, text="Annuler", command=dialog.destroy).pack(side=LEFT, padx=5)

        # Voir le commentaire equivalent dans _prompt_client_fields (bug
        # trouve a l'audit, voir E2).
        dialog.bind("<Return>", lambda event: on_ok())
        dialog.bind("<Escape>", lambda event: dialog.destroy())

        dialog.wait_window()
        return result or None

    def _edit_project(self, event=None):
        """Double-clic sur une ligne de projects_tree : ouvre un dialogue
        pre-rempli permettant de corriger le nom ou le taux horaire propre
        du projet (bug trouve a l'audit, voir E3 - meme lacune que pour les
        clients : update_project() supportait deja cette edition cote
        base, mais aucun chemin GUI ne l'exposait)."""
        selection = self.projects_tree.selection()
        if not selection:
            return
        project_id = int(selection[0])
        project = self.db.get_project(project_id)
        if project is None:
            return
        result = self._prompt_project_fields("Modifier le projet", project["name"], project["hourly_rate"])
        if result is None:
            return
        name = result["name"]
        if not name:
            messagebox.showwarning(APP_TITLE, "Le nom du projet est obligatoire.")
            return
        rate_text = result["hourly_rate"].replace(",", ".")
        rate = None
        if rate_text:
            try:
                rate = float(rate_text)
            except ValueError:
                messagebox.showwarning(APP_TITLE, "Le taux horaire doit etre un nombre.")
                return
            if rate < 0:
                messagebox.showwarning(APP_TITLE, "Le taux horaire ne peut pas etre negatif.")
                return
        # rate=None remet explicitement le projet en heritage du taux de
        # son client (meme semantique qu'a la creation, voir _add_project) :
        # utile pour annuler un taux specifique saisi par erreur.
        self.db.update_project(project_id, name=name, hourly_rate=rate)
        self._refresh_projects()
        self._refresh_timer_project_choices()
        self._refresh_invoices()

    # -- onglet Chronometre ---------------------------------------------------

    def _build_timer_tab(self):
        frame = self.timer_tab
        top = ttk.Frame(frame)
        top.pack(fill=X, padx=10, pady=10)

        self.timer_project_var = StringVar()
        self.timer_description_var = StringVar()

        ttk.Label(top, text="Projet").pack(side=LEFT)
        self.timer_project_combo = ttk.Combobox(top, textvariable=self.timer_project_var, width=30, state="readonly")
        self.timer_project_combo.pack(side=LEFT, padx=5)
        ttk.Label(top, text="Description").pack(side=LEFT, padx=(10, 0))
        ttk.Entry(top, textvariable=self.timer_description_var, width=35).pack(side=LEFT, padx=5)

        self.timer_display_var = StringVar(value="00:00:00")
        display = ttk.Label(frame, textvariable=self.timer_display_var, font=("Segoe UI", 42, "bold"))
        display.pack(pady=20)

        buttons = ttk.Frame(frame)
        buttons.pack()
        self.timer_start_button = ttk.Button(buttons, text="Demarrer", command=self._start_timer)
        self.timer_start_button.pack(side=LEFT, padx=5)
        self.timer_stop_button = ttk.Button(buttons, text="Arreter", command=self._stop_timer, state="disabled")
        self.timer_stop_button.pack(side=LEFT, padx=5)

        manual = ttk.LabelFrame(frame, text="Ajouter une entree manuelle (heures)")
        manual.pack(fill=X, padx=10, pady=20)
        from datetime import datetime as _datetime, timezone as _timezone
        # date.today() (heure LOCALE) melangerait deux fuseaux differents :
        # _add_manual_entry() construit ensuite l'horodatage effectif de
        # l'entree a partir de datetime.now(timezone.utc) (voir plus bas),
        # dont seuls year/month/day sont remplaces par cette date - entre
        # minuit UTC et minuit local, la date locale du jour suggeree par
        # defaut ne correspond alors plus au jour calendaire UTC courant,
        # decalant silencieusement l'entree d'un jour pour un utilisateur
        # hors du fuseau UTC (meme piege deja identifie et corrige pour
        # l'echeance des factures, voir _compute_due_date - bug trouve a
        # l'audit, voir F1).
        self.manual_date_var = StringVar(value=_datetime.now(_timezone.utc).date().isoformat())
        self.manual_hours_var = StringVar()
        ttk.Label(manual, text="Date de fin (AAAA-MM-JJ)").pack(side=LEFT, padx=5, pady=8)
        self.manual_date_entry = ttk.Entry(manual, textvariable=self.manual_date_var, width=12)
        self.manual_date_entry.pack(side=LEFT, padx=5)
        ttk.Label(manual, text="Nombre d'heures").pack(side=LEFT, padx=(10, 5))
        self.manual_hours_entry = ttk.Entry(manual, textvariable=self.manual_hours_var, width=8)
        self.manual_hours_entry.pack(side=LEFT, padx=5)
        ttk.Button(manual, text="Ajouter au projet selectionne", command=self._add_manual_entry).pack(side=LEFT, padx=10)
        # Voir le commentaire equivalent dans _build_clients_tab (bug trouve
        # a l'audit, voir E2).
        for entry in (self.manual_date_entry, self.manual_hours_entry):
            entry.bind("<Return>", lambda event: self._add_manual_entry())

        columns = ("id", "project", "start", "end", "hours", "description")
        self.entries_tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        self._configure_tree_columns(
            self.entries_tree,
            [
                ("id", "ID", 40), ("project", "Projet", 140), ("start", "Debut", 150),
                ("end", "Fin", 150), ("hours", "Heures", 70), ("description", "Description", 200),
            ],
            growable_columns=("project", "description"),
        )
        self.entries_tree.pack(fill=BOTH, expand=True, padx=10, pady=(0, 5))
        self.entries_tree.bind("<Double-1>", self._edit_time_entry)

        self.entries_summary_var = StringVar(value="")
        ttk.Label(frame, textvariable=self.entries_summary_var, foreground="#666").pack(
            anchor="w", padx=10, pady=(0, 4)
        )

        entries_actions = ttk.Frame(frame)
        entries_actions.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Label(entries_actions, text="Double-cliquez sur une ligne pour la modifier.", foreground="#666").pack(side=LEFT)
        ttk.Button(entries_actions, text="Supprimer l'entree selectionnee", command=self._delete_time_entry).pack(side=RIGHT)
        ttk.Button(entries_actions, text="Exporter en CSV...", command=self._export_time_entries_csv).pack(side=RIGHT, padx=(0, 6))

    def _refresh_timer_project_choices(self):
        # N'affiche que les projets dont le client est actif : un client
        # archive redevient invisible dans l'onglet Factures, donc du temps
        # suivi sur l'un de ses projets y resterait bloque, invisible.
        projects = self.db.list_projects_for_active_clients()
        labels = [f"{p['id']} - {p['name']}" for p in projects]
        self.timer_project_combo["values"] = labels
        self._refresh_time_entries()

    def _refresh_time_entries(self):
        self.entries_tree.delete(*self.entries_tree.get_children())
        from datetime import datetime
        from invoice import compute_duration_hours

        entries = self.db.list_time_entries()
        # Constat d'audit (2026-07-28) : tableau non borne (voir
        # ENTRIES_DISPLAY_LIMIT plus haut). list_time_entries() trie par
        # start_time croissant, donc les plus recentes sont en fin de liste -
        # on ne tronque que l'affichage, jamais les donnees elles-memes
        # (export CSV, factures, etc. continuent de porter sur tout).
        displayed = entries[-ENTRIES_DISPLAY_LIMIT:] if len(entries) > ENTRIES_DISPLAY_LIMIT else entries
        for entry in displayed:
            hours = compute_duration_hours(entry["start_time"], entry["end_time"]) if entry["end_time"] else None
            # Constat d'audit (2026-07-28) : les heures sont stockees en UTC
            # (voir l'entete de db.py), mais etaient jusqu'ici affichees
            # brutes (sans conversion) - .astimezone() sans argument convertit
            # vers le fuseau local du poste avant affichage, comme le
            # documente (mais ne faisait pas) l'entete de db.py.
            start_local = datetime.fromisoformat(entry["start_time"]).astimezone()
            start_display = start_local.strftime("%Y-%m-%d %H:%M:%S")
            if entry["end_time"]:
                end_display = datetime.fromisoformat(entry["end_time"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            else:
                end_display = "(en cours)"
            self.entries_tree.insert("", END, iid=str(entry["id"]), values=(
                entry["id"], entry["project_name"], start_display, end_display,
                f"{hours:.2f}" if hours is not None else "-", entry["description"],
            ))

        total = len(entries)
        if total > len(displayed):
            self.entries_summary_var.set(
                f"Affichage des {len(displayed)} entrees les plus recentes sur {total} au total "
                "(exportez en CSV pour tout consulter)."
            )
        else:
            self.entries_summary_var.set(f"{total} entree(s) de temps.")

    def _export_time_entries_csv(self):
        entries = self.db.list_time_entries()
        if not entries:
            messagebox.showinfo(APP_TITLE, "Aucune entree de temps a exporter.")
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="Exporter les entrees de temps", initialfile="entrees_de_temps.csv",
            defaultextension=".csv", filetypes=[("Fichier CSV", "*.csv")],
        )
        if not path:
            return
        try:
            export_time_entries_csv(entries, Path(path))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible d'exporter le CSV : {exc}")
            return
        messagebox.showinfo(APP_TITLE, f"Export termine : {Path(path).name}")

    def _start_timer(self):
        if self.timer.is_running:
            return
        project_id = self._parse_id(self.timer_project_var.get())
        if project_id is None:
            messagebox.showwarning(APP_TITLE, "Choisissez un projet avant de demarrer le chronometre.")
            return
        self.timer.start(project_id, self.timer_description_var.get().strip())
        self.timer_start_button.config(state="disabled")
        self.timer_stop_button.config(state="normal")
        self.timer_project_combo.config(state="disabled")

    def _stop_timer(self):
        if not self.timer.is_running:
            return
        self.timer.stop()
        self.timer_start_button.config(state="normal")
        self.timer_stop_button.config(state="disabled")
        self.timer_project_combo.config(state="readonly")
        self.timer_display_var.set("00:00:00")
        self._refresh_time_entries()
        self._refresh_invoices()

    def _restore_running_timer(self):
        running = self.db.get_running_entry()
        if not running:
            return
        self.timer.active_entry_id = running["id"]
        self.timer.project_id = running["project_id"]
        # Reflete l'etat reellement restaure dans les champs visibles : sans
        # cela, l'utilisateur voyait le temps defiler et le bouton "Arreter"
        # actif juste apres une fermeture/reouverture avec un chronometre en
        # cours, mais les champs Projet et Description restaient vides (le
        # combo Projet est verrouille juste en dessous, donc impossible de
        # les re-saisir soi-meme) - aucune indication de ce qui est
        # effectivement chronometre sans aller consulter la ligne "(en
        # cours)" dans le tableau des entrees plus bas (bug trouve a
        # l'audit, voir E1).
        project = self.db.get_project(running["project_id"])
        if project:
            self.timer_project_var.set(f"{project['id']} - {project['name']}")
        self.timer_description_var.set(running["description"] or "")
        import time as _time
        from datetime import datetime, timedelta
        started = datetime.fromisoformat(running["start_time"])
        elapsed = (datetime.now(started.tzinfo) - started).total_seconds()
        # Bug trouve a l'audit (2026-07-28) : si le PC a ete completement
        # eteint (pas juste l'application fermee proprement) pendant que ce
        # chronometre tournait, "elapsed" ci-dessus inclut silencieusement
        # tout le temps hors-ligne (l'ecart horloge murale entre le
        # demarrage et maintenant), qui se retrouverait facture comme du
        # temps de travail reel. Contrairement a une simple veille geree en
        # continu par sleep_gap_seconds() pendant que l'app tourne, une
        # extinction complete est invisible ici puisque le processus lui-
        # meme s'est arrete - on la detecte donc a la restauration via
        # l'uptime machine (voir _offline_gap_seconds) et on laisse
        # l'utilisateur decider, au lieu de l'inclure sans rien dire.
        offline_gap = self._offline_gap_seconds(elapsed)
        if offline_gap > 0:
            minutes = int(offline_gap // 60)
            remove = messagebox.askyesno(
                APP_TITLE,
                "Le chronometre etait toujours en cours au dernier arret de "
                "l'ordinateur : une extinction complete (pas une simple "
                f"fermeture de l'application) d'environ {minutes} minute(s) "
                "a ete detectee pendant qu'il tournait.\n\n"
                "Retirer ce temps hors-ligne du chronometre en cours ? "
                "(vous pourrez toujours affiner l'heure de debut ensuite, "
                "via un double-clic sur l'entree en cours dans le tableau "
                "des heures)",
            )
            if remove:
                started = started + timedelta(seconds=offline_gap)
                self.db.update_time_entry(running["id"], start_time=started.isoformat())
                elapsed -= offline_gap
        self.timer._start_monotonic = _time.monotonic() - elapsed
        self.timer._start_wall = _time.time() - elapsed
        self.timer_start_button.config(state="disabled")
        self.timer_stop_button.config(state="normal")
        self.timer_project_combo.config(state="disabled")

    def _offline_gap_seconds(self, elapsed: float) -> float:
        """Estime le temps que le PC a passe completement eteint (pas juste
        l'application fermee) depuis "elapsed" secondes, pour
        _restore_running_timer. Repose sur l'uptime machine (GetTickCount64,
        via get_uptime_seconds) : un uptime inferieur a "elapsed" prouve que
        la machine a redemarre entre-temps (extinction ou hibernation), la
        difference etant alors du temps necessairement hors-ligne. Renvoie
        0.0 si l'uptime est indisponible (jamais de faux positif) ou si
        l'ecart reste sous OFFLINE_GAP_THRESHOLD_SECONDS (imprecision
        normale, pas une vraie extinction)."""
        uptime = get_uptime_seconds()
        if uptime is None:
            return 0.0
        gap = elapsed - uptime
        return gap if gap > OFFLINE_GAP_THRESHOLD_SECONDS else 0.0

    def _tick_timer(self):
        if self.timer.is_running:
            self.timer_display_var.set(Timer.format_duration(self.timer.elapsed_seconds()))
        self.root.after(1000, self._tick_timer)

    def _check_idle(self):
        if self.timer.is_running and not self._idle_prompt_open:
            # Ajoute l'ecart eventuel horloge murale / compteur moniteur : ce
            # dernier (comme GetTickCount64, utilise par get_idle_seconds) se
            # fige pendant une veille Windows, donc une mise en veille du
            # poste pendant que le chronometre tourne ne serait autrement
            # jamais detectee comme de l'inactivite.
            idle = get_idle_seconds() + self.timer.sleep_gap_seconds()
            if idle >= self._idle_threshold_seconds():
                self._idle_prompt_open = True
                minutes = int(idle // 60)
                answer = messagebox.askyesno(
                    APP_TITLE,
                    f"Vous semblez inactif depuis environ {minutes} minute(s).\n"
                    "Retirer ce temps d'inactivite du chronometre en cours ?",
                )
                if answer:
                    self.timer.stop_removing_idle_time(idle)
                    self.timer_start_button.config(state="normal")
                    self.timer_stop_button.config(state="disabled")
                    self.timer_project_combo.config(state="readonly")
                    self.timer_display_var.set("00:00:00")
                    self._refresh_time_entries()
                    self._refresh_invoices()
                self._idle_prompt_open = False
        self.root.after(IDLE_CHECK_INTERVAL_MS, self._check_idle)

    def _add_manual_entry(self):
        project_id = self._parse_id(self.timer_project_var.get())
        if project_id is None:
            messagebox.showwarning(APP_TITLE, "Choisissez un projet.")
            return
        try:
            hours = float(self.manual_hours_var.get().replace(",", "."))
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Entrez un nombre d'heures valide.")
            return
        if hours <= 0:
            messagebox.showwarning(APP_TITLE, "Le nombre d'heures doit etre positif.")
            return
        if hours > 24:
            if not messagebox.askyesno(
                APP_TITLE,
                f"{hours:g} heures en une seule entree, c'est inhabituel (plus d'une journee complete).\n"
                "Confirmez-vous que ce nombre est correct ?",
            ):
                return
        from datetime import date as _date, datetime, timedelta, timezone
        try:
            target_date = _date.fromisoformat(self.manual_date_var.get().strip())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "La date de fin doit etre au format AAAA-MM-JJ.")
            return
        # Conserve l'heure actuelle mais sur la date choisie : permet de
        # saisir "3 heures faites hier" sans avoir a viser une heure precise.
        now = datetime.now(timezone.utc)
        end = now.replace(year=target_date.year, month=target_date.month, day=target_date.day)
        start = end - timedelta(hours=hours)
        self.db.add_manual_time_entry(project_id, start.isoformat(), end.isoformat(), self.timer_description_var.get().strip())
        self.manual_hours_var.set("")
        self._refresh_time_entries()
        self._refresh_invoices()

    def _prompt_time_entry_fields(
        self, title: str, initial_hours: str, initial_description: str,
        current_project_id=None, project_choices=None,
    ):
        """Petit dialogue (heures, description, et optionnellement projet) -
        renvoie un dict ou None si annule. project_choices (liste de
        (id, label)) n'est fourni QUE par _edit_time_entry : quand present,
        un selecteur de projet est ajoute au dialogue et le resultat
        contient une cle "project_id" en plus de "hours"/"description" -
        auparavant aucun chemin GUI ne permettait de reassigner une entree
        de temps a un autre projet (la seule solution etait de la supprimer
        et de la ressaisir), alors que update_time_entry() supportait deja
        pleinement ce champ cote base (bug trouve a l'audit, voir E3b)."""
        from tkinter import Toplevel

        dialog = Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        hours_var = StringVar(value=initial_hours)
        desc_var = StringVar(value=initial_description)
        result = {}

        row = 0
        project_var = None
        if project_choices is not None:
            project_var = StringVar()
            for project_id, label in project_choices:
                if project_id == current_project_id:
                    project_var.set(label)
                    break
            ttk.Label(dialog, text="Projet").grid(row=row, column=0, sticky="w", padx=10, pady=(10, 5))
            project_combo = ttk.Combobox(dialog, textvariable=project_var, width=28, state="readonly")
            project_combo["values"] = [label for _, label in project_choices]
            project_combo.grid(row=row, column=1, padx=10, pady=(10, 5))
            row += 1

        ttk.Label(dialog, text="Heures").grid(row=row, column=0, sticky="w", padx=10, pady=(10 if row == 0 else 0, 5))
        ttk.Entry(dialog, textvariable=hours_var, width=15).grid(row=row, column=1, padx=10, pady=(10 if row == 0 else 0, 5))
        row += 1
        ttk.Label(dialog, text="Description").grid(row=row, column=0, sticky="w", padx=10)
        ttk.Entry(dialog, textvariable=desc_var, width=30).grid(row=row, column=1, padx=10, pady=(0, 10))
        row += 1

        def on_ok():
            result["hours"] = hours_var.get().strip()
            result["description"] = desc_var.get().strip()
            if project_var is not None:
                result["project_id"] = self._parse_id(project_var.get())
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=row, column=0, columnspan=2, pady=(0, 10))
        ttk.Button(buttons, text="Enregistrer", command=on_ok).pack(side=LEFT, padx=5)
        ttk.Button(buttons, text="Annuler", command=dialog.destroy).pack(side=LEFT, padx=5)

        # Voir le commentaire equivalent dans _prompt_client_fields (bug
        # trouve a l'audit, voir E2) : Entree valide, Echap annule.
        dialog.bind("<Return>", lambda event: on_ok())
        dialog.bind("<Escape>", lambda event: dialog.destroy())

        dialog.wait_window()
        return result or None

    def _edit_time_entry(self, event=None):
        selection = self.entries_tree.selection()
        if not selection:
            return
        entry_id = int(selection[0])
        entry = self.db.get_time_entry(entry_id)
        if entry is None:
            return
        if entry["end_time"] is None:
            messagebox.showinfo(APP_TITLE, "Le chronometre en cours ne peut pas etre modifie ici ; arretez-le d'abord.")
            return

        from invoice import compute_duration_hours
        current_hours = compute_duration_hours(entry["start_time"], entry["end_time"])
        # Liste tous les projets (y compris archives) : une entree existante
        # peut porter sur un projet depuis archive, qui doit rester
        # selectionnable (au moins pour le laisser tel quel) meme s'il
        # n'apparait plus dans le combo du chronometre (voir
        # _refresh_timer_project_choices, qui ne liste que les projets
        # facturables).
        projects = self.db.list_projects(include_archived=True)
        project_choices = [(p["id"], f"{p['id']} - {p['name']}") for p in projects]
        result = self._prompt_time_entry_fields(
            "Modifier l'entree de temps", f"{current_hours:.2f}", entry["description"],
            current_project_id=entry["project_id"], project_choices=project_choices,
        )
        if result is None:
            return
        try:
            hours = float(result["hours"].replace(",", "."))
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Entrez un nombre d'heures valide.")
            return
        if hours <= 0:
            messagebox.showwarning(APP_TITLE, "Le nombre d'heures doit etre positif.")
            return

        update_kwargs = {"description": result["description"]}
        # N'envoyer start_time/end_time que si les heures ont reellement ete
        # modifiees : update_time_entry refuse tout changement d'horaires
        # sur une entree deja facturee, mais une simple correction de
        # description doit rester possible meme sur une entree facturee -
        # renvoyer ces deux champs a l'identique (meme non modifies)
        # bloquerait donc a tort cette correction (bug trouve a l'audit).
        if abs(hours - current_hours) > 0.005:
            from datetime import datetime, timedelta
            end = datetime.fromisoformat(entry["end_time"])
            start = end - timedelta(hours=hours)
            update_kwargs["start_time"] = start.isoformat()
            update_kwargs["end_time"] = end.isoformat()
        # Meme logique que pour les heures ci-dessus : n'envoyer project_id
        # que s'il a reellement change, pour ne jamais declencher a tort le
        # garde-fou "entree deja facturee" de update_time_entry() sur une
        # simple correction de description (bug trouve a l'audit, voir E3b
        # : reassigner une entree de temps a un autre projet necessitait
        # auparavant de la supprimer et de la ressaisir, aucun chemin GUI
        # n'exposant ce champ pourtant deja supporte cote base).
        new_project_id = result.get("project_id")
        if new_project_id is not None and new_project_id != entry["project_id"]:
            update_kwargs["project_id"] = new_project_id
        try:
            self.db.update_time_entry(entry_id, **update_kwargs)
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        self._refresh_time_entries()
        self._refresh_invoices()

    def _delete_time_entry(self):
        selection = self.entries_tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Selectionnez une entree d'abord.")
            return
        entry_id = int(selection[0])
        if not messagebox.askyesno(APP_TITLE, "Supprimer cette entree de temps ?"):
            return
        try:
            self.db.delete_time_entry(entry_id)
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        self._refresh_time_entries()
        self._refresh_invoices()

    # -- onglet Factures ------------------------------------------------------

    def _build_invoices_tab(self):
        frame = self.invoices_tab
        top = ttk.Frame(frame)
        top.pack(fill=X, padx=10, pady=10)

        self.invoice_client_var = StringVar()
        self.invoice_tax_var = StringVar(value="0")

        ttk.Label(top, text="Client").pack(side=LEFT)
        self.invoice_client_combo = ttk.Combobox(top, textvariable=self.invoice_client_var, width=25, state="readonly")
        self.invoice_client_combo.pack(side=LEFT, padx=5)
        self.invoice_client_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_uninvoiced_preview())
        ttk.Label(top, text="TVA (%)").pack(side=LEFT, padx=(10, 0))
        self.invoice_tax_entry = ttk.Entry(top, textvariable=self.invoice_tax_var, width=6)
        self.invoice_tax_entry.pack(side=LEFT, padx=5)
        ttk.Button(top, text="Generer la facture (PDF)", command=self._generate_invoice).pack(side=LEFT, padx=10)
        # Voir le commentaire equivalent dans _build_clients_tab (bug trouve
        # a l'audit, voir E2) : Entree dans le champ TVA declenche la meme
        # action que le bouton "Generer la facture (PDF)".
        self.invoice_tax_entry.bind("<Return>", lambda event: self._generate_invoice())

        # Rappel contextuel de la mention legale de franchise en base de
        # TVA (art. 293 B du CGI) : le champ TVA est preinitialise a "0",
        # exactement le cas d'un auto-entrepreneur francais exonere (public
        # cible principal de l'app, voir README) - mais rien ne suggerait
        # auparavant cette mention obligatoire, une simple ligne "TVA (0 %)"
        # n'etant pas la mention legale attendue (bug trouve a l'audit, voir
        # A6). Visible uniquement quand la TVA saisie est 0, pour ne pas
        # gener les utilisateurs assujettis.
        tva_hint_row = ttk.Frame(frame)
        tva_hint_row.pack(fill=X, padx=10, pady=(0, 5))
        self.tva_hint_var = StringVar()
        ttk.Label(
            tva_hint_row, textvariable=self.tva_hint_var, foreground="#666",
            wraplength=560, justify=LEFT,
        ).pack(side=LEFT)
        self.tva_hint_button = ttk.Button(
            tva_hint_row, text="Ajouter la mention legale aux notes", command=self._insert_tva_exemption_note
        )
        self.invoice_tax_var.trace_add("write", lambda *_: self._update_tva_hint())

        notes_row = ttk.Frame(frame)
        notes_row.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Label(notes_row, text="Notes (imprimees sur la facture)").pack(side=LEFT)
        self.invoice_notes_var = StringVar()
        self.invoice_notes_entry = ttk.Entry(notes_row, textvariable=self.invoice_notes_var, width=45)
        self.invoice_notes_entry.pack(side=LEFT, padx=5)
        self.invoice_notes_entry.bind("<Return>", lambda event: self._generate_invoice())
        self.note_template_var = StringVar()
        self.note_template_combo = ttk.Combobox(notes_row, textvariable=self.note_template_var, width=20, state="readonly")
        self.note_template_combo.pack(side=LEFT, padx=(10, 5))
        self.note_template_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_note_template())
        ttk.Button(notes_row, text="Enregistrer comme modele...", command=self._save_note_template).pack(side=LEFT, padx=5)
        ttk.Button(notes_row, text="Supprimer le modele", command=self._delete_note_template).pack(side=LEFT)
        self._refresh_note_templates()

        preview_columns = ("project", "hours", "rate", "amount")
        self.preview_tree = ttk.Treeview(frame, columns=preview_columns, show="headings", height=6)
        self._configure_tree_columns(
            self.preview_tree,
            [
                ("project", "Projet", 200), ("hours", "Heures non facturees", 150),
                ("rate", "Taux", 100), ("amount", "Montant", 100),
            ],
            growable_columns=("project",),
        )
        self.preview_tree.pack(fill=X, padx=10, pady=(0, 10))

        ttk.Separator(frame).pack(fill=X, padx=10)

        invoice_search_row = ttk.Frame(frame)
        invoice_search_row.pack(fill=X, padx=10, pady=(10, 5))
        ttk.Label(invoice_search_row, text="Rechercher :").pack(side=LEFT)
        self.invoice_search_var = StringVar()
        ttk.Entry(invoice_search_row, textvariable=self.invoice_search_var, width=30).pack(side=LEFT, padx=5)
        self._invoice_search_after_id = None
        self.invoice_search_var.trace_add("write", lambda *_: self._on_invoice_search_changed())

        columns = ("id", "number", "client", "date", "total", "status")
        self.invoices_tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        self._configure_tree_columns(
            self.invoices_tree,
            [
                ("id", "ID", 40), ("number", "Numero", 100), ("client", "Client", 160),
                ("date", "Date", 110), ("total", "Total", 100), ("status", "Statut", 100),
            ],
            growable_columns=("client",),
        )
        self.invoices_tree.tag_configure("overdue", foreground="#c0392b")
        self.invoices_tree.pack(fill=BOTH, expand=True, padx=10, pady=10)

        self.overdue_summary_var = StringVar(value="")
        ttk.Label(frame, textvariable=self.overdue_summary_var, foreground="#c0392b").pack(
            anchor="w", padx=10, pady=(0, 6)
        )

        actions = ttk.Frame(frame)
        actions.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Button(actions, text="Marquer payee", command=lambda: self._set_invoice_status("paid")).pack(side=LEFT)
        ttk.Button(actions, text="Marquer annulee", command=lambda: self._set_invoice_status("cancelled")).pack(side=LEFT, padx=5)
        ttk.Button(actions, text="Marquer non payee", command=lambda: self._set_invoice_status("unpaid")).pack(side=LEFT)
        ttk.Button(actions, text="Dupliquer (facture recurrente)...", command=self._duplicate_invoice).pack(side=LEFT, padx=5)
        ttk.Button(actions, text="Reexporter le PDF...", command=self._reexport_invoice_pdf).pack(side=LEFT)
        ttk.Button(actions, text="Supprimer (libere les heures)", command=self._delete_invoice).pack(side=RIGHT)
        ttk.Button(actions, text="Exporter en CSV...", command=self._export_invoices_csv).pack(side=RIGHT, padx=(0, 6))

        # self.invoice_notes_var doit deja exister (voir notes_row
        # ci-dessus) avant ce premier appel, puisque _insert_tva_exemption_
        # note() (declenche par le bouton) l'utilise.
        self._update_tva_hint()

    def _update_tva_hint(self):
        try:
            tax_rate = float(self.invoice_tax_var.get().replace(",", ".") or 0)
        except ValueError:
            tax_rate = None
        if tax_rate == 0:
            self.tva_hint_var.set(
                "Si vous etes exonere de TVA (franchise en base), pensez a ajouter la "
                "mention legale obligatoire dans les notes de la facture :"
            )
            self.tva_hint_button.pack(side=LEFT, padx=(6, 0))
        else:
            self.tva_hint_var.set("")
            self.tva_hint_button.pack_forget()

    def _insert_tva_exemption_note(self):
        current = self.invoice_notes_var.get().strip()
        if TVA_EXEMPTION_NOTE in current:
            return  # deja presente, ne pas la dupliquer
        self.invoice_notes_var.set(f"{current} {TVA_EXEMPTION_NOTE}".strip())

    def _refresh_note_templates(self):
        templates = self.db.list_note_templates()
        self.note_template_combo["values"] = [t["name"] for t in templates]

    def _apply_note_template(self):
        name = self.note_template_var.get()
        for template in self.db.list_note_templates():
            if template["name"] == name:
                self.invoice_notes_var.set(template["text"])
                return

    def _save_note_template(self):
        text = self.invoice_notes_var.get().strip()
        if not text:
            messagebox.showinfo(APP_TITLE, "Saisissez d'abord un texte de note a enregistrer comme modele.")
            return
        name = simpledialog.askstring(APP_TITLE, "Nom du modele :", parent=self.root)
        if not name or not name.strip():
            return
        self.db.save_note_template(name, text)
        self._refresh_note_templates()
        self.note_template_var.set(name.strip())

    def _delete_note_template(self):
        name = self.note_template_var.get()
        if not name:
            messagebox.showinfo(APP_TITLE, "Selectionnez d'abord un modele dans la liste.")
            return
        if not messagebox.askyesno(APP_TITLE, f"Supprimer le modele de note '{name}' ?"):
            return
        self.db.delete_note_template(name)
        self.note_template_var.set("")
        self._refresh_note_templates()

    def _export_invoices_csv(self):
        invoices = self.db.list_invoices()
        if not invoices:
            messagebox.showinfo(APP_TITLE, "Aucune facture a exporter.")
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="Exporter les factures", initialfile="factures.csv",
            defaultextension=".csv", filetypes=[("Fichier CSV", "*.csv")],
        )
        if not path:
            return
        try:
            export_invoices_csv(invoices, self.db, Path(path))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible d'exporter le CSV : {exc}")
            return
        messagebox.showinfo(APP_TITLE, f"Export termine : {Path(path).name}")

    def _on_invoice_search_changed(self):
        # Debounce : une frappe rapide de N caracteres ne doit pas declencher
        # N rafraichissements complets de la liste (chacun re-scanne toutes
        # les factures) - on annule le rafraichissement encore en attente et
        # on en reprogramme un seul, apres une courte pause dans la frappe.
        if self._invoice_search_after_id is not None:
            self.root.after_cancel(self._invoice_search_after_id)
        self._invoice_search_after_id = self.root.after(SEARCH_DEBOUNCE_MS, self._run_debounced_invoice_refresh)

    def _run_debounced_invoice_refresh(self):
        self._invoice_search_after_id = None
        self._refresh_invoices()

    def _refresh_invoices(self):
        clients, labels = self._client_choices()
        self.invoice_client_combo["values"] = labels
        self.invoices_tree.delete(*self.invoices_tree.get_children())
        clients_by_id = {c["id"]: c["name"] for c in self.db.list_clients(include_archived=True)}
        overdue_ids = {row["id"] for row in self.db.list_overdue_invoices()}
        query = self.invoice_search_var.get().strip().lower() if hasattr(self, "invoice_search_var") else ""
        # Recupere les lignes de TOUTES les factures en une seule requete
        # (au lieu d'un get_invoice_line_items() par facture affichee, qui
        # provoquait un pattern N+1 mesure a ~750ms de gel d'interface avec
        # 3000 factures - cette methode est appelee apres quasiment toute
        # action de l'app, y compris a chaque frappe dans la recherche) puis
        # les regroupe par invoice_id cote Python. Le calcul du total en
        # lui-meme (compute_totals sur le snapshot fige) est inchange.
        line_items_by_invoice: dict = {}
        for row in self.db.get_all_invoice_line_items():
            line_items_by_invoice.setdefault(row["invoice_id"], []).append(row)
        for invoice in self.db.list_invoices():
            client_name = clients_by_id.get(invoice["client_id"], "?")
            status_label = "en retard" if invoice["id"] in overdue_ids else invoice["status"]
            if query and query not in invoice["invoice_number"].lower() and query not in client_name.lower() \
                    and query not in status_label.lower():
                continue
            # Reconstruit les lignes a partir du snapshot fige a la creation
            # de la facture (pas des taux horaires actuels des projets), pour
            # qu'un total affiche ici ne change jamais apres coup.
            stored_items = line_items_by_invoice.get(invoice["id"], [])
            line_items = [LineItem(row["project_name"], row["hours"], row["rate"]) for row in stored_items]
            _, _, total = compute_totals(line_items, invoice["tax_rate"])
            tags = ("overdue",) if invoice["id"] in overdue_ids else ()
            self.invoices_tree.insert("", END, iid=str(invoice["id"]), values=(
                invoice["id"], invoice["invoice_number"], client_name,
                invoice["issue_date"][:10], format_amount(total, invoice["currency"]), status_label,
            ), tags=tags)
        self._refresh_overdue_summary(overdue_ids)
        self._refresh_uninvoiced_preview()

    def _refresh_overdue_summary(self, overdue_ids):
        if not overdue_ids:
            self.overdue_summary_var.set("")
            return
        count = len(overdue_ids)
        plural = "s" if count > 1 else ""
        self.overdue_summary_var.set(
            f"{count} facture{plural} en retard de paiement (echeance depassee)"
        )

    def _refresh_uninvoiced_preview(self):
        self.preview_tree.delete(*self.preview_tree.get_children())
        client_id = self._parse_id(self.invoice_client_var.get())
        if client_id is None:
            return
        entries = [e for e in self.db.list_time_entries(client_id=client_id, uninvoiced_only=True)]
        for item in build_line_items(entries, self.db):
            self.preview_tree.insert("", END, values=(
                item.project_name, f"{item.hours:.2f} h",
                format_amount(item.rate, self._currency()), format_amount(item.amount, self._currency()),
            ))

    def _compute_due_date(self) -> str:
        """Echeance par defaut (delai de paiement configure en Parametres,
        ajoute a la date du jour) - factorise entre _generate_invoice et
        _duplicate_invoice, qui dupliquaient auparavant integralement ce
        meme bloc de code (meme lecture du parametre, meme commentaire) -
        bug trouve a l'audit, voir L1. date.today() (heure LOCALE)
        melangerait deux fuseaux differents avec list_overdue_invoices, qui
        compare toujours a la date UTC du jour (voir db.py : "Toutes les
        dates/heures sont stockees en UTC") - une facture pourrait alors
        etre jugee en retard un jour trop tot ou trop tard selon le fuseau
        de l'utilisateur (bug trouve a l'audit)."""
        from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone
        try:
            payment_terms_days = int(self.db.get_setting("payment_terms_days", "30") or 0)
        except ValueError:
            payment_terms_days = 30
        return (_datetime.now(_timezone.utc).date() + _timedelta(days=payment_terms_days)).isoformat()

    def _write_invoice_pdf_or_rollback(self, invoice_id, invoice, client, line_items, tax_rate, output_path) -> bool:
        """Genere le PDF d'une facture qui vient d'etre creee en base ; en
        cas d'echec (quel qu'il soit, pas seulement OSError), annule la
        facture (rollback complet) pour ne jamais laisser de facture
        "fantome" sans PDF correspondant, avec des heures verrouillees pour
        rien, et affiche un message d'erreur. Renvoie True si le PDF a bien
        ete ecrit (l'appelant peut continuer normalement), False si la
        facture a du etre annulee (l'appelant doit alors s'arreter la, sans
        rien faire de plus). Factorise entre _generate_invoice et
        _duplicate_invoice, qui dupliquaient auparavant integralement ce
        meme bloc (bug trouve a l'audit, voir L1). On capture toute
        exception plausible, pas seulement OSError, car
        generate_invoice_pdf peut aussi lever d'autres types d'erreurs
        (encodage, etc.) - bug trouve a l'audit : une devise contenant un
        caractere hors Latin-1 (ex : "€") faisait planter la generation
        avec une exception non OSError, laissant une facture fantome en
        base sans rollback."""
        try:
            generate_invoice_pdf(
                output_path=output_path,
                invoice_number=invoice["invoice_number"],
                issue_date=invoice["issue_date"][:10],
                due_date=invoice["due_date"],
                company_name=self.db.get_setting("company_name", "Mon entreprise"),
                company_info=self.db.get_setting("company_info", ""),
                client_name=client["name"],
                client_address=client["address"],
                line_items=line_items,
                tax_rate=tax_rate,
                notes=invoice["notes"],
                currency=invoice["currency"],
            )
        except Exception as exc:
            self.db.delete_invoice(invoice_id)
            messagebox.showerror(APP_TITLE, f"Le PDF n'a pas pu etre enregistre, facture annulee : {exc}")
            return False
        return True

    def _generate_invoice(self):
        client_id = self._parse_id(self.invoice_client_var.get())
        if client_id is None:
            messagebox.showwarning(APP_TITLE, "Choisissez un client.")
            return
        try:
            tax_rate = float(self.invoice_tax_var.get().replace(",", ".") or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Le taux de TVA doit etre un nombre.")
            return
        if tax_rate < 0:
            messagebox.showwarning(APP_TITLE, "Le taux de TVA ne peut pas etre negatif.")
            return
        if tax_rate > 100:
            # Aucune borne superieure n'existait auparavant (contrairement
            # au nombre d'heures manuelles, voir plus bas) : une simple
            # erreur de frappe d'un zero en trop (ex. 200 au lieu de 20)
            # produisait silencieusement une facture avec un montant de TVA
            # absurde envoye tel quel au client (bug trouve a l'audit, voir
            # A7). Meme pattern de confirmation que pour les heures > 24 :
            # on n'empeche rien (un taux > 100% reste legal dans de rares
            # cas), on demande juste de confirmer que ce n'est pas une
            # faute de frappe.
            if not messagebox.askyesno(
                APP_TITLE,
                f"Un taux de TVA de {tax_rate:g} % est inhabituel (superieur a 100 %).\n"
                "Confirmez-vous que ce taux est correct ?",
            ):
                return
        entries = self.db.list_time_entries(client_id=client_id, uninvoiced_only=True)
        if not entries:
            messagebox.showinfo(APP_TITLE, "Aucune heure non facturee pour ce client.")
            return
        client = self.db.get_client(client_id)
        line_items = build_line_items(entries, self.db)

        # On demande l'emplacement du PDF AVANT de creer la facture en base :
        # si l'utilisateur annule ce dialogue, aucune facture "fantome" ne
        # doit rester en base a consommer un numero et verrouiller des heures
        # sans qu'aucun PDF n'ait ete produit.
        from tkinter import filedialog
        next_number = self.db.next_invoice_number()
        default_name = f"Facture_{next_number}.pdf"
        output_path = filedialog.asksaveasfilename(
            title="Enregistrer la facture PDF", initialfile=default_name, defaultextension=".pdf",
            filetypes=[("Fichier PDF", "*.pdf")],
        )
        if not output_path:
            return

        due_date = self._compute_due_date()

        try:
            invoice_id = self.db.create_invoice(
                client_id, [e["id"] for e in entries], tax_rate=tax_rate, line_items=line_items, due_date=due_date,
                currency=self._currency(), notes=self.invoice_notes_var.get(),
            )
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible de creer la facture : {exc}")
            return
        invoice = self.db.get_invoice(invoice_id)

        if not self._write_invoice_pdf_or_rollback(
            invoice_id, invoice, client, line_items, tax_rate, Path(output_path)
        ):
            return

        # Efface les notes une fois la facture generee : un texte specifique
        # a CE client (ex: une remise negociee) resterait sinon affiche si
        # l'utilisateur change de client et genere une nouvelle facture
        # sans remarquer que le champ contient encore l'ancien texte (bug
        # trouve a l'audit).
        self.invoice_notes_var.set("")
        self.note_template_var.set("")
        self._refresh_invoices()
        self._refresh_time_entries()
        if messagebox.askyesno(APP_TITLE, f"Facture {invoice['invoice_number']} generee.\nOuvrir le PDF maintenant ?"):
            webbrowser.open(Path(output_path).resolve().as_uri())

    def _selected_invoice_id(self):
        selection = self.invoices_tree.selection()
        return int(selection[0]) if selection else None

    def _set_invoice_status(self, status: str):
        invoice_id = self._selected_invoice_id()
        if invoice_id is None:
            messagebox.showinfo(APP_TITLE, "Selectionnez une facture d'abord.")
            return
        self.db.set_invoice_status(invoice_id, status)
        self._refresh_invoices()

    def _delete_invoice(self):
        invoice_id = self._selected_invoice_id()
        if invoice_id is None:
            messagebox.showinfo(APP_TITLE, "Selectionnez une facture d'abord.")
            return
        invoice = self.db.get_invoice(invoice_id)
        if invoice is None:
            # Facture supprimee entre son affichage dans la liste et ce clic
            # (meme garde-fou que _reexport_invoice_pdf/_duplicate_invoice).
            messagebox.showwarning(APP_TITLE, "Cette facture n'existe plus.")
            return
        # Contrairement a "Marquer annulee" (qui conserve la facture et son
        # numero dans l'historique), supprimer efface definitivement la
        # ligne "invoices" ET son invoice_number - un numero deja attribue
        # ne doit normalement jamais disparaitre d'une sequence de
        # facturation officielle (numerotation chronologique continue,
        # obligation legale dans plusieurs juridictions dont la France). Le
        # message d'origine ne mentionnait que la liberation des heures,
        # jamais cette consequence sur la numerotation - un utilisateur qui
        # "nettoie" par reflexe une facture deja emise/communiquee creerait
        # ainsi un trou de sequence sans le savoir (bug trouve a l'audit,
        # voir A5). "Marquer annulee" produit le meme resultat pratique
        # (heures a nouveau facturables) sans jamais creer ce trou.
        if not messagebox.askyesno(
            APP_TITLE,
            f"Supprimer definitivement la facture {invoice['invoice_number']} ?\n\n"
            "Les heures associees redeviendront facturables, mais le numero "
            f"{invoice['invoice_number']} disparaitra definitivement de votre "
            "historique de facturation.\n\n"
            "Si cette facture a deja ete communiquee a un client, cela peut poser "
            "probleme en cas de controle fiscal (numerotation chronologique "
            "continue exigee) : preferez 'Marquer annulee' dans ce cas, qui libere "
            "aussi les heures mais conserve la facture et son numero dans "
            "l'historique.\n\n"
            "Supprimer quand meme ?",
        ):
            return
        self.db.delete_invoice(invoice_id)
        self._refresh_invoices()
        self._refresh_time_entries()

    def _duplicate_invoice(self):
        """Cree une nouvelle facture reprenant les memes lignes qu'une facture
        existante (memes projets/heures/taux figes), pour un client en
        facturation recurrente (ex : forfait mensuel identique chaque mois).
        Contrairement a une facture normale, aucune heure suivie n'est
        consommee - les lignes sont simplement recopiees telles quelles."""
        invoice_id = self._selected_invoice_id()
        if invoice_id is None:
            messagebox.showinfo(APP_TITLE, "Selectionnez une facture a dupliquer d'abord.")
            return
        source_invoice = self.db.get_invoice(invoice_id)
        if source_invoice is None:
            # Meme garde-fou que _reexport_invoice_pdf, pour la meme raison
            # (facture supprimee entre l'affichage de la liste et ce clic) :
            # peu probable en usage normal, mais moins cher a verifier
            # explicitement que d'en debattre.
            messagebox.showwarning(APP_TITLE, "Cette facture n'existe plus.")
            return
        stored_items = self.db.get_invoice_line_items(invoice_id)
        if not stored_items:
            messagebox.showwarning(APP_TITLE, "Cette facture n'a aucune ligne a dupliquer.")
            return
        line_items = [LineItem(row["project_name"], row["hours"], row["rate"]) for row in stored_items]
        client = self.db.get_client(source_invoice["client_id"])
        if client is None:
            messagebox.showerror(APP_TITLE, "Le client de cette facture n'existe plus.")
            return

        if not messagebox.askyesno(
            APP_TITLE,
            f"Creer une nouvelle facture pour '{client['name']}' avec les memes lignes que "
            f"{source_invoice['invoice_number']} ({len(line_items)} ligne(s)) ?",
        ):
            return

        from tkinter import filedialog
        next_number = self.db.next_invoice_number()
        output_path = filedialog.asksaveasfilename(
            title="Enregistrer la nouvelle facture PDF", initialfile=f"Facture_{next_number}.pdf",
            defaultextension=".pdf", filetypes=[("Fichier PDF", "*.pdf")],
        )
        if not output_path:
            return

        due_date = self._compute_due_date()

        try:
            new_invoice_id = self.db.create_invoice(
                source_invoice["client_id"], [], tax_rate=source_invoice["tax_rate"],
                due_date=due_date, line_items=line_items, currency=self._currency(),
                notes=source_invoice["notes"],
            )
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible de creer la facture : {exc}")
            return
        new_invoice = self.db.get_invoice(new_invoice_id)

        if not self._write_invoice_pdf_or_rollback(
            new_invoice_id, new_invoice, client, line_items, source_invoice["tax_rate"], Path(output_path)
        ):
            return

        self._refresh_invoices()
        if messagebox.askyesno(APP_TITLE, f"Facture {new_invoice['invoice_number']} generee.\nOuvrir le PDF maintenant ?"):
            webbrowser.open(Path(output_path).resolve().as_uri())

    def _reexport_invoice_pdf(self):
        invoice_id = self._selected_invoice_id()
        if invoice_id is None:
            messagebox.showinfo(APP_TITLE, "Selectionnez une facture d'abord.")
            return
        invoice = self.db.get_invoice(invoice_id)
        if invoice is None:
            # La facture selectionnee a ete supprimee entre le moment ou
            # elle a ete affichee dans la liste et ce clic (bug trouve a
            # l'audit : rare en usage normal mais atteignable, par ex. une
            # seconde instance de l'app pointant sur le meme fichier de
            # donnees) - sans cette verification, invoice['invoice_number']
            # ci-dessous plantait avec un TypeError au lieu d'un message
            # clair (reexport_invoice_pdf, lui, gere deja proprement ce cas
            # via un ValueError, mais on ne l'atteint jamais : le plantage
            # a lieu avant, en construisant le nom de fichier par defaut).
            messagebox.showwarning(APP_TITLE, "Cette facture n'existe plus.")
            return

        from tkinter import filedialog
        output_path = filedialog.asksaveasfilename(
            title="Reexporter la facture PDF",
            initialfile=f"Facture_{invoice['invoice_number']}.pdf",
            defaultextension=".pdf", filetypes=[("Fichier PDF", "*.pdf")],
        )
        if not output_path:
            return

        try:
            reexport_invoice_pdf(
                self.db, invoice_id, Path(output_path),
                company_name=self.db.get_setting("company_name", "Mon entreprise"),
                company_info=self.db.get_setting("company_info", ""),
            )
        except (OSError, ValueError) as exc:
            # Contrairement a _generate_invoice, aucune facture n'a ete creee
            # en base : il n'y a donc rien a annuler en cas d'echec.
            messagebox.showerror(APP_TITLE, f"Le PDF n'a pas pu etre enregistre : {exc}")
            return

        messagebox.showinfo(
            APP_TITLE,
            f"Facture {invoice['invoice_number']} reexportee.\n"
            "Montants, dates et notes sont ceux figes a l'emission ;\n"
            "seules les coordonnees actuelles du client sont utilisees.",
        )
        if messagebox.askyesno(APP_TITLE, "Ouvrir le PDF maintenant ?"):
            webbrowser.open(Path(output_path).resolve().as_uri())

    # -- onglet Parametres ----------------------------------------------------

    def _build_settings_tab(self):
        frame = self.settings_tab
        form = ttk.Frame(frame)
        form.pack(fill=X, padx=10, pady=10)

        self.setting_company_name_var = StringVar(value=self.db.get_setting("company_name"))
        self.setting_company_info_var = StringVar(value=self.db.get_setting("company_info"))
        self.setting_payment_terms_var = StringVar(value=self.db.get_setting("payment_terms_days", "30"))
        self.setting_currency_var = StringVar(value=self.db.get_setting("currency", "EUR"))
        self.setting_idle_threshold_var = StringVar(
            value=self.db.get_setting("idle_threshold_minutes", str(IDLE_THRESHOLD_SECONDS // 60))
        )
        # La verification de mise a jour au demarrage n'etait auparavant
        # jamais desactivable (appelee inconditionnellement dans __init__)
        # - la requete elle-meme est minimale et non intrusive (simple GET
        # vers l'API publique GitHub Releases, aucune donnee personnelle ni
        # identifiant machine, voir update_checker.py), mais cela nuancait
        # legerement l'affirmation ci-dessous ("aucune connexion internet
        # n'est necessaire") pour un utilisateur soucieux d'un fonctionnement
        # strictement hors-ligne (bug trouve a l'audit, voir H1). Active par
        # defaut pour ne pas changer le comportement existant.
        self.setting_check_updates_var = BooleanVar(
            value=self.db.get_setting("check_updates_on_startup", "1") == "1"
        )

        ttk.Label(form, text="Nom de l'entreprise (affiche sur les factures)").grid(row=0, column=0, sticky="w", pady=5)
        self.settings_company_name_entry = ttk.Entry(form, textvariable=self.setting_company_name_var, width=50)
        self.settings_company_name_entry.grid(row=0, column=1, padx=5)
        ttk.Label(form, text="Informations (SIRET, adresse...)").grid(row=1, column=0, sticky="w", pady=5)
        self.settings_company_info_entry = ttk.Entry(form, textvariable=self.setting_company_info_var, width=50)
        self.settings_company_info_entry.grid(row=1, column=1, padx=5)
        ttk.Label(form, text="Delai de paiement par defaut (jours)").grid(row=2, column=0, sticky="w", pady=5)
        self.settings_payment_terms_entry = ttk.Entry(form, textvariable=self.setting_payment_terms_var, width=10)
        self.settings_payment_terms_entry.grid(row=2, column=1, sticky="w", padx=5)
        ttk.Label(form, text="Devise (code, ex: EUR, USD, CHF)").grid(row=3, column=0, sticky="w", pady=5)
        self.settings_currency_entry = ttk.Entry(form, textvariable=self.setting_currency_var, width=10)
        self.settings_currency_entry.grid(row=3, column=1, sticky="w", padx=5)
        ttk.Label(form, text="Seuil d'inactivite du chronometre (minutes)").grid(row=4, column=0, sticky="w", pady=5)
        self.settings_idle_threshold_entry = ttk.Entry(form, textvariable=self.setting_idle_threshold_var, width=10)
        self.settings_idle_threshold_entry.grid(row=4, column=1, sticky="w", padx=5)
        ttk.Checkbutton(
            form, text="Verifier les mises a jour au demarrage (seule activite reseau de l'application)",
            variable=self.setting_check_updates_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Button(form, text="Enregistrer", command=self._save_settings).grid(row=6, column=1, sticky="e", pady=10)
        # Voir le commentaire equivalent dans _build_clients_tab (bug trouve
        # a l'audit, voir E2).
        for entry in (
            self.settings_company_name_entry, self.settings_company_info_entry, self.settings_payment_terms_entry,
            self.settings_currency_entry, self.settings_idle_threshold_entry,
        ):
            entry.bind("<Return>", lambda event: self._save_settings())

        ttk.Label(
            frame,
            text="Toutes les donnees sont stockees localement sur cet ordinateur,\n"
                 "aucune connexion internet ni compte n'est necessaire.",
            justify=LEFT,
        ).pack(anchor="w", padx=10, pady=20)

        backup_frame = ttk.LabelFrame(frame, text="Sauvegarde", padding=10)
        backup_frame.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Label(
            backup_frame,
            text="Toutes les factures et heures tiennent dans un seul fichier : sauvegardez-le\n"
                 "regulierement pour ne rien perdre en cas de probleme disque.",
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(backup_frame)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="Sauvegarder les donnees...", command=self._backup_database).pack(side=LEFT)
        ttk.Button(buttons, text="Ouvrir le dossier de donnees", command=self._open_data_dir).pack(side=LEFT, padx=(6, 0))

    def _backup_database(self):
        from datetime import date
        from tkinter import filedialog
        default_name = f"tempofacture-sauvegarde-{date.today().isoformat()}.sqlite"
        path = filedialog.asksaveasfilename(
            title="Sauvegarder les donnees", initialfile=default_name,
            defaultextension=".sqlite", filetypes=[("Base SQLite", "*.sqlite")],
        )
        if not path:
            return
        import sqlite3
        try:
            self.db.backup_to(Path(path))
        except (OSError, ValueError, sqlite3.Error) as exc:
            # sqlite3.Error en plus d'OSError/ValueError (bug trouve a
            # l'audit) : sqlite3.connect() sur une destination inaccessible
            # (dossier disparu, lecteur demonte, chemin verrouille) leve un
            # sqlite3.OperationalError, qui n'est PAS une sous-classe
            # d'OSError - sans ce clause, ce cas pourtant tres plausible
            # (cle USB retiree entre l'ouverture du dialogue et le clic)
            # remontait comme un plantage non gere au lieu d'un message clair.
            messagebox.showerror(APP_TITLE, f"Impossible d'enregistrer la sauvegarde : {exc}")
            return
        messagebox.showinfo(
            APP_TITLE,
            f"Sauvegarde enregistree :\n{path}\n\n"
            "Pour restaurer : fermez TempoFacture, puis remplacez le fichier de donnees "
            "actif par cette copie.",
        )

    def _open_data_dir(self):
        import os
        os.startfile(_data_dir())  # nosec - ouverture Explorateur Windows d'un dossier local

    def _save_settings(self):
        try:
            payment_terms = int(self.setting_payment_terms_var.get().strip() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Le delai de paiement doit etre un nombre entier de jours.")
            return
        if payment_terms < 0:
            messagebox.showwarning(APP_TITLE, "Le delai de paiement ne peut pas etre negatif.")
            return
        currency = self.setting_currency_var.get().strip().upper()
        if not currency:
            messagebox.showwarning(APP_TITLE, "La devise ne peut pas etre vide.")
            return
        try:
            idle_threshold_minutes = int(self.setting_idle_threshold_var.get().strip() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Le seuil d'inactivite doit etre un nombre entier de minutes.")
            return
        if idle_threshold_minutes < 1:
            messagebox.showwarning(APP_TITLE, "Le seuil d'inactivite doit etre d'au moins 1 minute.")
            return
        self.db.set_setting("company_name", self.setting_company_name_var.get().strip())
        self.db.set_setting("company_info", self.setting_company_info_var.get().strip())
        self.db.set_setting("payment_terms_days", str(payment_terms))
        self.db.set_setting("currency", currency)
        self.db.set_setting("idle_threshold_minutes", str(idle_threshold_minutes))
        # Ne s'applique qu'au PROCHAIN demarrage (voir __init__, ou la
        # verification est lancee juste apres l'ouverture de la base, avant
        # meme la construction de cet onglet) : decocher ici n'annule pas
        # une verification deja en cours dans ce processus, mais empeche la
        # suivante (bug trouve a l'audit, voir H1).
        self.db.set_setting("check_updates_on_startup", "1" if self.setting_check_updates_var.get() else "0")
        self._refresh_clients()
        self._refresh_projects()
        self._refresh_time_entries()
        self._refresh_invoices()
        messagebox.showinfo(APP_TITLE, "Parametres enregistres.")

    def _currency(self) -> str:
        return self.db.get_setting("currency", "EUR")

    def _idle_threshold_seconds(self) -> int:
        # Meme mecanisme de stockage/lecture que les autres reglages
        # (payment_terms_days, currency...) : une valeur texte dans la table
        # settings, relue a chaque appel plutot que mise en cache, pour
        # refleter immediatement un changement enregistre depuis l'onglet
        # Parametres.
        try:
            minutes = int(self.db.get_setting("idle_threshold_minutes", str(IDLE_THRESHOLD_SECONDS // 60)) or 0)
        except ValueError:
            minutes = IDLE_THRESHOLD_SECONDS // 60
        return max(1, minutes) * 60

    # -- fermeture ------------------------------------------------------------

    def _on_close(self):
        self.db.close()
        self.root.destroy()


def main():
    root = Tk()
    TempoFactureApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
