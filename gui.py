"""Interface Tkinter de TempoFacture : clients, projets, chronometre,
factures et parametres, tous relies a la meme base SQLite locale."""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, TOP, X, Y, StringVar, Tk, ttk, messagebox, simpledialog

from db import Database
from invoice import LineItem, build_line_items, compute_totals, format_amount, generate_invoice_pdf
from timer import Timer, get_idle_seconds
from csv_export import export_time_entries_csv, export_invoices_csv

APP_TITLE = "TempoFacture"
DONATE_URL = "https://ko-fi.com/yoshines62000"
IDLE_THRESHOLD_SECONDS = 5 * 60  # au-dela, on propose de retirer le temps d'inactivite
IDLE_CHECK_INTERVAL_MS = 15_000


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def _data_dir() -> Path:
    appdata = Path.home() / "AppData" / "Roaming" / "TempoFacture"
    return appdata


class TempoFactureApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x640")

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
        donate_label = ttk.Label(bottom_bar, text="☕ Soutenir le projet", foreground="#0645AD", cursor="hand2")
        donate_label.pack(side=RIGHT, padx=8, pady=4)
        donate_label.bind("<Button-1>", lambda event: webbrowser.open(DONATE_URL))

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

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_timer()
        self._check_idle()

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
        ttk.Entry(form, textvariable=self.client_name_var, width=30).grid(row=0, column=1, padx=5)
        ttk.Label(form, text="Email").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=self.client_email_var, width=25).grid(row=0, column=3, padx=5)
        ttk.Label(form, text="Taux horaire").grid(row=0, column=4, sticky="w")
        ttk.Entry(form, textvariable=self.client_rate_var, width=8).grid(row=0, column=5, padx=5)

        ttk.Label(form, text="Adresse").grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Entry(form, textvariable=self.client_address_var, width=60).grid(
            row=1, column=1, columnspan=4, sticky="we", pady=(5, 0)
        )
        ttk.Button(form, text="Ajouter le client", command=self._add_client).grid(row=1, column=5, pady=(5, 0))

        columns = ("id", "name", "email", "rate", "archived")
        self.clients_tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        for col, label, width in [
            ("id", "ID", 40), ("name", "Nom", 200), ("email", "Email", 200),
            ("rate", "Taux horaire", 100), ("archived", "Archive", 70),
        ]:
            self.clients_tree.heading(col, text=label)
            self.clients_tree.column(col, width=width, anchor="w")
        self.clients_tree.pack(fill=BOTH, expand=True, padx=10, pady=(0, 5))

        actions = ttk.Frame(frame)
        actions.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Button(actions, text="Archiver / desarchiver", command=self._toggle_client_archived).pack(side=LEFT)

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

    def _refresh_clients(self):
        self.clients_tree.delete(*self.clients_tree.get_children())
        for client in self.db.list_clients(include_archived=True):
            self.clients_tree.insert("", END, iid=str(client["id"]), values=(
                client["id"], client["name"], client["email"],
                format_amount(client["hourly_rate"], self._currency()), "Oui" if client["archived"] else "Non",
            ))

    def _toggle_client_archived(self):
        selection = self.clients_tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Selectionnez un client d'abord.")
            return
        client_id = int(selection[0])
        client = self.db.get_client(client_id)
        self.db.update_client(client_id, archived=0 if client["archived"] else 1)
        self._refresh_clients()
        # Un client archive/desarchive doit disparaitre/reapparaitre partout
        # ou sa liste est utilisee (projets, chronometre, factures), pas
        # seulement dans son propre onglet.
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
        ttk.Entry(form, textvariable=self.project_name_var, width=25).grid(row=0, column=3, padx=5)
        ttk.Label(form, text="Taux (optionnel)").grid(row=0, column=4, sticky="w")
        ttk.Entry(form, textvariable=self.project_rate_var, width=10).grid(row=0, column=5, padx=5)
        ttk.Button(form, text="Ajouter le projet", command=self._add_project).grid(row=0, column=6, padx=5)

        columns = ("id", "client", "name", "rate", "archived")
        self.projects_tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        for col, label, width in [
            ("id", "ID", 40), ("client", "Client", 160), ("name", "Projet", 200),
            ("rate", "Taux effectif", 110), ("archived", "Archive", 70),
        ]:
            self.projects_tree.heading(col, text=label)
            self.projects_tree.column(col, width=width, anchor="w")
        self.projects_tree.pack(fill=BOTH, expand=True, padx=10, pady=(0, 5))

        actions = ttk.Frame(frame)
        actions.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Button(actions, text="Archiver / desarchiver", command=self._toggle_project_archived).pack(side=LEFT)

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

    def _refresh_projects(self):
        clients, client_labels = self._client_choices()
        self.project_client_combo["values"] = client_labels
        self.projects_tree.delete(*self.projects_tree.get_children())
        clients_by_id = {c["id"]: c["name"] for c in self.db.list_clients(include_archived=True)}
        for project in self.db.list_projects(include_archived=True):
            self.projects_tree.insert("", END, iid=str(project["id"]), values=(
                project["id"], clients_by_id.get(project["client_id"], "?"), project["name"],
                format_amount(self.db.effective_hourly_rate(project["id"]), self._currency()),
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
        from datetime import date as _date
        self.manual_date_var = StringVar(value=_date.today().isoformat())
        self.manual_hours_var = StringVar()
        ttk.Label(manual, text="Date de fin (AAAA-MM-JJ)").pack(side=LEFT, padx=5, pady=8)
        ttk.Entry(manual, textvariable=self.manual_date_var, width=12).pack(side=LEFT, padx=5)
        ttk.Label(manual, text="Nombre d'heures").pack(side=LEFT, padx=(10, 5))
        ttk.Entry(manual, textvariable=self.manual_hours_var, width=8).pack(side=LEFT, padx=5)
        ttk.Button(manual, text="Ajouter au projet selectionne", command=self._add_manual_entry).pack(side=LEFT, padx=10)

        columns = ("id", "project", "start", "end", "hours", "description")
        self.entries_tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        for col, label, width in [
            ("id", "ID", 40), ("project", "Projet", 140), ("start", "Debut", 150),
            ("end", "Fin", 150), ("hours", "Heures", 70), ("description", "Description", 200),
        ]:
            self.entries_tree.heading(col, text=label)
            self.entries_tree.column(col, width=width, anchor="w")
        self.entries_tree.pack(fill=BOTH, expand=True, padx=10, pady=(0, 5))
        self.entries_tree.bind("<Double-1>", self._edit_time_entry)

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
        from invoice import compute_duration_hours
        for entry in self.db.list_time_entries():
            hours = compute_duration_hours(entry["start_time"], entry["end_time"]) if entry["end_time"] else None
            self.entries_tree.insert("", END, iid=str(entry["id"]), values=(
                entry["id"], entry["project_name"], entry["start_time"][:19].replace("T", " "),
                entry["end_time"][:19].replace("T", " ") if entry["end_time"] else "(en cours)",
                f"{hours:.2f}" if hours is not None else "-", entry["description"],
            ))

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
        import time as _time
        from datetime import datetime
        started = datetime.fromisoformat(running["start_time"])
        elapsed = (datetime.now(started.tzinfo) - started).total_seconds()
        self.timer._start_monotonic = _time.monotonic() - elapsed
        self.timer._start_wall = _time.time() - elapsed
        self.timer_start_button.config(state="disabled")
        self.timer_stop_button.config(state="normal")
        self.timer_project_combo.config(state="disabled")

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
            if idle >= IDLE_THRESHOLD_SECONDS:
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

    def _prompt_time_entry_fields(self, title: str, initial_hours: str, initial_description: str):
        """Petit dialogue (heures, description) - renvoie (hours_text,
        description) ou None si annule."""
        from tkinter import Toplevel

        dialog = Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        hours_var = StringVar(value=initial_hours)
        desc_var = StringVar(value=initial_description)
        result = {}

        ttk.Label(dialog, text="Heures").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))
        ttk.Entry(dialog, textvariable=hours_var, width=15).grid(row=0, column=1, padx=10, pady=(10, 5))
        ttk.Label(dialog, text="Description").grid(row=1, column=0, sticky="w", padx=10)
        ttk.Entry(dialog, textvariable=desc_var, width=30).grid(row=1, column=1, padx=10, pady=(0, 10))

        def on_ok():
            result["hours"] = hours_var.get().strip()
            result["description"] = desc_var.get().strip()
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, columnspan=2, pady=(0, 10))
        ttk.Button(buttons, text="Enregistrer", command=on_ok).pack(side=LEFT, padx=5)
        ttk.Button(buttons, text="Annuler", command=dialog.destroy).pack(side=LEFT, padx=5)

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
        result = self._prompt_time_entry_fields(
            "Modifier l'entree de temps", f"{current_hours:.2f}", entry["description"]
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

        from datetime import datetime, timedelta
        end = datetime.fromisoformat(entry["end_time"])
        start = end - timedelta(hours=hours)
        try:
            self.db.update_time_entry(
                entry_id, start_time=start.isoformat(), end_time=end.isoformat(), description=result["description"]
            )
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
        ttk.Entry(top, textvariable=self.invoice_tax_var, width=6).pack(side=LEFT, padx=5)
        ttk.Button(top, text="Generer la facture (PDF)", command=self._generate_invoice).pack(side=LEFT, padx=10)

        preview_columns = ("project", "hours", "rate", "amount")
        self.preview_tree = ttk.Treeview(frame, columns=preview_columns, show="headings", height=6)
        for col, label, width in [
            ("project", "Projet", 200), ("hours", "Heures non facturees", 150),
            ("rate", "Taux", 100), ("amount", "Montant", 100),
        ]:
            self.preview_tree.heading(col, text=label)
            self.preview_tree.column(col, width=width, anchor="w")
        self.preview_tree.pack(fill=X, padx=10, pady=(0, 10))

        ttk.Separator(frame).pack(fill=X, padx=10)

        columns = ("id", "number", "client", "date", "total", "status")
        self.invoices_tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        for col, label, width in [
            ("id", "ID", 40), ("number", "Numero", 100), ("client", "Client", 160),
            ("date", "Date", 110), ("total", "Total", 100), ("status", "Statut", 100),
        ]:
            self.invoices_tree.heading(col, text=label)
            self.invoices_tree.column(col, width=width, anchor="w")
        self.invoices_tree.pack(fill=BOTH, expand=True, padx=10, pady=10)

        actions = ttk.Frame(frame)
        actions.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Button(actions, text="Marquer payee", command=lambda: self._set_invoice_status("paid")).pack(side=LEFT)
        ttk.Button(actions, text="Marquer annulee", command=lambda: self._set_invoice_status("cancelled")).pack(side=LEFT, padx=5)
        ttk.Button(actions, text="Marquer non payee", command=lambda: self._set_invoice_status("unpaid")).pack(side=LEFT)
        ttk.Button(actions, text="Dupliquer (facture recurrente)...", command=self._duplicate_invoice).pack(side=LEFT, padx=5)
        ttk.Button(actions, text="Supprimer (libere les heures)", command=self._delete_invoice).pack(side=RIGHT)
        ttk.Button(actions, text="Exporter en CSV...", command=self._export_invoices_csv).pack(side=RIGHT, padx=(0, 6))

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

    def _refresh_invoices(self):
        clients, labels = self._client_choices()
        self.invoice_client_combo["values"] = labels
        self.invoices_tree.delete(*self.invoices_tree.get_children())
        clients_by_id = {c["id"]: c["name"] for c in self.db.list_clients(include_archived=True)}
        for invoice in self.db.list_invoices():
            # Reconstruit les lignes a partir du snapshot fige a la creation
            # de la facture (pas des taux horaires actuels des projets), pour
            # qu'un total affiche ici ne change jamais apres coup.
            stored_items = self.db.get_invoice_line_items(invoice["id"])
            line_items = [LineItem(row["project_name"], row["hours"], row["rate"]) for row in stored_items]
            _, _, total = compute_totals(line_items, invoice["tax_rate"])
            self.invoices_tree.insert("", END, iid=str(invoice["id"]), values=(
                invoice["id"], invoice["invoice_number"], clients_by_id.get(invoice["client_id"], "?"),
                invoice["issue_date"][:10], format_amount(total, self._currency()), invoice["status"],
            ))
        self._refresh_uninvoiced_preview()

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

        from datetime import date as _date, timedelta as _timedelta
        try:
            payment_terms_days = int(self.db.get_setting("payment_terms_days", "30") or 0)
        except ValueError:
            payment_terms_days = 30
        due_date = (_date.today() + _timedelta(days=payment_terms_days)).isoformat()

        try:
            invoice_id = self.db.create_invoice(
                client_id, [e["id"] for e in entries], tax_rate=tax_rate, line_items=line_items, due_date=due_date,
            )
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible de creer la facture : {exc}")
            return
        invoice = self.db.get_invoice(invoice_id)

        try:
            generate_invoice_pdf(
                output_path=Path(output_path),
                invoice_number=invoice["invoice_number"],
                issue_date=invoice["issue_date"][:10],
                due_date=invoice["due_date"],
                company_name=self.db.get_setting("company_name", "Mon entreprise"),
                company_info=self.db.get_setting("company_info", ""),
                client_name=client["name"],
                client_address=client["address"],
                line_items=line_items,
                tax_rate=tax_rate,
                currency=self._currency(),
            )
        except OSError as exc:
            # Le PDF n'a pas pu etre ecrit (permission refusee, fichier
            # ouvert ailleurs, disque plein...) : on annule la facture pour
            # ne jamais laisser une facture "fantome" sans PDF correspondant,
            # avec des heures verrouillees pour rien.
            self.db.delete_invoice(invoice_id)
            messagebox.showerror(APP_TITLE, f"Le PDF n'a pas pu etre enregistre, facture annulee : {exc}")
            return

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
        if not messagebox.askyesno(APP_TITLE, "Supprimer cette facture ? Les heures redeviendront facturables."):
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

        from datetime import date as _date, timedelta as _timedelta
        try:
            payment_terms_days = int(self.db.get_setting("payment_terms_days", "30") or 0)
        except ValueError:
            payment_terms_days = 30
        due_date = (_date.today() + _timedelta(days=payment_terms_days)).isoformat()

        try:
            new_invoice_id = self.db.create_invoice(
                source_invoice["client_id"], [], tax_rate=source_invoice["tax_rate"],
                due_date=due_date, line_items=line_items,
            )
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible de creer la facture : {exc}")
            return
        new_invoice = self.db.get_invoice(new_invoice_id)

        try:
            generate_invoice_pdf(
                output_path=Path(output_path),
                invoice_number=new_invoice["invoice_number"],
                issue_date=new_invoice["issue_date"][:10],
                due_date=new_invoice["due_date"],
                company_name=self.db.get_setting("company_name", "Mon entreprise"),
                company_info=self.db.get_setting("company_info", ""),
                client_name=client["name"],
                client_address=client["address"],
                line_items=line_items,
                tax_rate=source_invoice["tax_rate"],
                currency=self._currency(),
            )
        except OSError as exc:
            self.db.delete_invoice(new_invoice_id)
            messagebox.showerror(APP_TITLE, f"Le PDF n'a pas pu etre enregistre, facture annulee : {exc}")
            return

        self._refresh_invoices()
        if messagebox.askyesno(APP_TITLE, f"Facture {new_invoice['invoice_number']} generee.\nOuvrir le PDF maintenant ?"):
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

        ttk.Label(form, text="Nom de l'entreprise (affiche sur les factures)").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.setting_company_name_var, width=50).grid(row=0, column=1, padx=5)
        ttk.Label(form, text="Informations (SIRET, adresse...)").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.setting_company_info_var, width=50).grid(row=1, column=1, padx=5)
        ttk.Label(form, text="Delai de paiement par defaut (jours)").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.setting_payment_terms_var, width=10).grid(row=2, column=1, sticky="w", padx=5)
        ttk.Label(form, text="Devise (code, ex: EUR, USD, CHF)").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.setting_currency_var, width=10).grid(row=3, column=1, sticky="w", padx=5)
        ttk.Button(form, text="Enregistrer", command=self._save_settings).grid(row=4, column=1, sticky="e", pady=10)

        ttk.Label(
            frame,
            text="Toutes les donnees sont stockees localement sur cet ordinateur,\n"
                 "aucune connexion internet ni compte n'est necessaire.",
            justify=LEFT,
        ).pack(anchor="w", padx=10, pady=20)

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
        self.db.set_setting("company_name", self.setting_company_name_var.get().strip())
        self.db.set_setting("company_info", self.setting_company_info_var.get().strip())
        self.db.set_setting("payment_terms_days", str(payment_terms))
        self.db.set_setting("currency", currency)
        self._refresh_clients()
        self._refresh_projects()
        self._refresh_time_entries()
        self._refresh_invoices()
        messagebox.showinfo(APP_TITLE, "Parametres enregistres.")

    def _currency(self) -> str:
        return self.db.get_setting("currency", "EUR")

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
