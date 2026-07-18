"""Couche donnees de TempoFacture (SQLite, sans dependance externe).

Toutes les dates/heures sont stockees en UTC, au format ISO 8601, pour
eviter toute ambiguite liee au fuseau horaire ou a l'heure d'ete/hiver.
La conversion vers l'heure locale ne se fait qu'a l'affichage (gui.py).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Enveloppe fine autour de sqlite3 : une connexion, un schema, des
    methodes CRUD explicites. Pas d'ORM : le schema est simple et les
    requetes restent lisibles telles quelles."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    def _create_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            hourly_rate REAL NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES clients(id),
            name TEXT NOT NULL,
            hourly_rate REAL,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS time_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            start_time TEXT NOT NULL,
            end_time TEXT,
            description TEXT NOT NULL DEFAULT '',
            invoice_id INTEGER REFERENCES invoices(id)
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES clients(id),
            invoice_number TEXT NOT NULL UNIQUE,
            issue_date TEXT NOT NULL,
            due_date TEXT,
            tax_rate REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unpaid',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS invoice_line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            project_name TEXT NOT NULL,
            hours REAL NOT NULL,
            rate REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        self.conn.commit()
        # currency a ete ajoutee apres la sortie initiale : les bases SQLite
        # existantes ne sont pas recreees par CREATE TABLE IF NOT EXISTS,
        # d'ou cette migration additive explicite (idempotente). Sans elle,
        # changer la devise dans les Parametres relabelliserait
        # retroactivement toutes les factures deja emises (meme montant,
        # mauvaise devise affichee) - chaque facture doit au contraire
        # figer la devise en vigueur au moment de sa creation.
        self._add_column_if_missing("invoices", "currency", "TEXT NOT NULL DEFAULT 'EUR'")

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self.conn.commit()

    # -- clients ------------------------------------------------------------

    def add_client(self, name: str, email: str = "", address: str = "", hourly_rate: float = 0.0) -> int:
        cur = self.conn.execute(
            "INSERT INTO clients (name, email, address, hourly_rate, created_at) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), email.strip(), address.strip(), hourly_rate, _now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_client(self, client_id: int, **fields) -> None:
        allowed = {"name", "email", "address", "hourly_rate", "archived"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(f"UPDATE clients SET {set_clause} WHERE id = ?", (*updates.values(), client_id))
        self.conn.commit()

    def get_client(self, client_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()

    def list_clients(self, include_archived: bool = False) -> list:
        query = "SELECT * FROM clients"
        if not include_archived:
            query += " WHERE archived = 0"
        query += " ORDER BY name COLLATE NOCASE"
        return self.conn.execute(query).fetchall()

    # -- projets --------------------------------------------------------------

    def add_project(self, client_id: int, name: str, hourly_rate: Optional[float] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO projects (client_id, name, hourly_rate, created_at) VALUES (?, ?, ?, ?)",
            (client_id, name.strip(), hourly_rate, _now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_project(self, project_id: int, **fields) -> None:
        allowed = {"name", "hourly_rate", "archived"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", (*updates.values(), project_id))
        self.conn.commit()

    def get_project(self, project_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()

    def list_projects(self, client_id: Optional[int] = None, include_archived: bool = False) -> list:
        query = "SELECT * FROM projects WHERE 1=1"
        params = []
        if client_id is not None:
            query += " AND client_id = ?"
            params.append(client_id)
        if not include_archived:
            query += " AND archived = 0"
        query += " ORDER BY name COLLATE NOCASE"
        return self.conn.execute(query, params).fetchall()

    def list_projects_for_active_clients(self) -> list:
        """Projets facturables : ni le projet ni son client ne sont archives.
        Utilise pour la liste du chronometre, afin qu'on ne puisse pas
        continuer a suivre du temps sur un client archive (ou ses heures
        resteraient invisibles dans l'onglet Factures, qui n'affiche que les
        clients actifs)."""
        return self.conn.execute(
            """SELECT projects.* FROM projects
               JOIN clients ON clients.id = projects.client_id
               WHERE projects.archived = 0 AND clients.archived = 0
               ORDER BY projects.name COLLATE NOCASE"""
        ).fetchall()

    def effective_hourly_rate(self, project_id: int) -> float:
        """Taux horaire d'un projet : celui du projet si defini, sinon celui
        de son client par defaut."""
        project = self.get_project(project_id)
        if project is None:
            return 0.0
        if project["hourly_rate"] is not None:
            return float(project["hourly_rate"])
        client = self.get_client(project["client_id"])
        return float(client["hourly_rate"]) if client else 0.0

    # -- suivi du temps -------------------------------------------------------

    def start_time_entry(self, project_id: int, description: str = "") -> int:
        # Un seul chronometre actif a la fois : sans ce garde-fou, un second
        # demarrage laisserait une entree "en cours" orpheline qui accumule
        # du temps indefiniment sans jamais apparaitre comme facturable tant
        # qu'elle n'est pas explicitement arretee.
        if self.get_running_entry() is not None:
            raise RuntimeError("Un chronometre est deja en cours ; arretez-le avant d'en demarrer un autre.")
        cur = self.conn.execute(
            "INSERT INTO time_entries (project_id, start_time, description) VALUES (?, ?, ?)",
            (project_id, _now_iso(), description.strip()),
        )
        self.conn.commit()
        return cur.lastrowid

    def stop_time_entry(self, entry_id: int, end_time_iso: Optional[str] = None) -> None:
        end_time_iso = end_time_iso or _now_iso()
        entry = self.conn.execute("SELECT start_time FROM time_entries WHERE id = ?", (entry_id,)).fetchone()
        if entry and end_time_iso < entry["start_time"]:
            raise ValueError("L'heure de fin ne peut pas etre anterieure a l'heure de debut.")
        self.conn.execute(
            "UPDATE time_entries SET end_time = ? WHERE id = ?",
            (end_time_iso, entry_id),
        )
        self.conn.commit()

    def add_manual_time_entry(self, project_id: int, start_iso: str, end_iso: str, description: str = "") -> int:
        if end_iso < start_iso:
            raise ValueError("L'heure de fin ne peut pas etre anterieure a l'heure de debut.")
        cur = self.conn.execute(
            "INSERT INTO time_entries (project_id, start_time, end_time, description) VALUES (?, ?, ?, ?)",
            (project_id, start_iso, end_iso, description.strip()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_time_entry(self, entry_id: int, **fields) -> None:
        allowed = {"start_time", "end_time", "description", "project_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        current = self.conn.execute(
            "SELECT start_time, end_time, invoice_id FROM time_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if current and current["invoice_id"] is not None and (
            "start_time" in updates or "end_time" in updates or "project_id" in updates
        ):
            # Les lignes de facture sont figees a la creation (voir
            # create_invoice), mais changer les horaires ou le projet d'une
            # entree deja facturee desynchroniserait ce que cette entree
            # affiche desormais de ce que la facture a reellement enregistre.
            raise ValueError("Cette entree a deja ete facturee ; annulez d'abord la facture correspondante.")
        if "start_time" in updates or "end_time" in updates:
            if current:
                start = updates.get("start_time", current["start_time"])
                end = updates.get("end_time", current["end_time"])
                if start and end and end < start:
                    raise ValueError("L'heure de fin ne peut pas etre anterieure a l'heure de debut.")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(f"UPDATE time_entries SET {set_clause} WHERE id = ?", (*updates.values(), entry_id))
        self.conn.commit()

    def delete_time_entry(self, entry_id: int) -> None:
        entry = self.conn.execute("SELECT invoice_id FROM time_entries WHERE id = ?", (entry_id,)).fetchone()
        if entry and entry["invoice_id"] is not None:
            raise ValueError("Cette entree a deja ete facturee ; annulez d'abord la facture correspondante.")
        self.conn.execute("DELETE FROM time_entries WHERE id = ?", (entry_id,))
        self.conn.commit()

    def get_time_entry(self, entry_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM time_entries WHERE id = ?", (entry_id,)).fetchone()

    def get_running_entry(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM time_entries WHERE end_time IS NULL ORDER BY id LIMIT 1"
        ).fetchone()

    def list_time_entries(
        self,
        project_id: Optional[int] = None,
        client_id: Optional[int] = None,
        uninvoiced_only: bool = False,
        include_running: bool = True,
    ) -> list:
        query = """
            SELECT time_entries.*, projects.name AS project_name, projects.client_id AS client_id
            FROM time_entries
            JOIN projects ON projects.id = time_entries.project_id
            WHERE 1=1
        """
        params = []
        if project_id is not None:
            query += " AND time_entries.project_id = ?"
            params.append(project_id)
        if client_id is not None:
            query += " AND projects.client_id = ?"
            params.append(client_id)
        if uninvoiced_only:
            query += " AND time_entries.invoice_id IS NULL AND time_entries.end_time IS NOT NULL"
        if not include_running:
            query += " AND time_entries.end_time IS NOT NULL"
        query += " ORDER BY time_entries.start_time"
        return self.conn.execute(query, params).fetchall()

    # -- factures ---------------------------------------------------------------

    def next_invoice_number(self, year: Optional[int] = None) -> str:
        # Derive from MAX(suffix), not COUNT(*): if an invoice was deleted,
        # COUNT would drop and reissue a number that still exists among the
        # remaining invoices, causing a UNIQUE constraint violation.
        year = year or datetime.now(timezone.utc).year
        row = self.conn.execute(
            "SELECT MAX(CAST(SUBSTR(invoice_number, 6) AS INTEGER)) FROM invoices WHERE invoice_number LIKE ?",
            (f"{year}-%",),
        ).fetchone()
        max_number = row[0] or 0
        return f"{year}-{max_number + 1:04d}"

    def create_invoice(
        self,
        client_id: int,
        time_entry_ids: list,
        tax_rate: float = 0.0,
        due_date: Optional[str] = None,
        notes: str = "",
        line_items: Optional[list] = None,
        currency: Optional[str] = None,
    ) -> int:
        issue_date = _now_iso()
        # Use the same (UTC) year as issue_date, so the invoice number never
        # disagrees with the date printed on the invoice itself.
        invoice_number = self.next_invoice_number(year=datetime.fromisoformat(issue_date).year)
        # Fige la devise en vigueur au moment de la creation : elle ne doit
        # plus jamais changer ensuite, meme si le parametre global de devise
        # est modifie par la suite (voir _add_column_if_missing ci-dessus).
        currency = currency or self.get_setting("currency", "EUR")
        try:
            cur = self.conn.execute(
                """INSERT INTO invoices (client_id, invoice_number, issue_date, due_date, tax_rate, notes, currency, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (client_id, invoice_number, issue_date, due_date, tax_rate, notes.strip(), currency, _now_iso()),
            )
            invoice_id = cur.lastrowid
            if time_entry_ids:
                placeholders = ",".join("?" * len(time_entry_ids))
                # Restrict to entries whose project actually belongs to this
                # client: guards against accidentally billing another client's
                # hours onto this invoice if the caller passes a bad id list.
                updated = self.conn.execute(
                    f"""UPDATE time_entries SET invoice_id = ?
                        WHERE id IN ({placeholders})
                        AND project_id IN (SELECT id FROM projects WHERE client_id = ?)""",
                    (invoice_id, *time_entry_ids, client_id),
                ).rowcount
                if updated != len(time_entry_ids):
                    raise ValueError("Certaines entrees de temps n'appartiennent pas a ce client.")
            # Snapshot the rate/hours used at creation time: if the client's or
            # project's hourly_rate is edited later, this invoice's total must
            # stay exactly what was actually billed, not silently change.
            for item in (line_items or []):
                self.conn.execute(
                    "INSERT INTO invoice_line_items (invoice_id, project_name, hours, rate) VALUES (?, ?, ?, ?)",
                    (invoice_id, item.project_name, item.hours, item.rate),
                )
        except Exception:
            # Toute erreur en cours de creation (id invalide, item malforme...)
            # annule integralement la facture : jamais de ligne partielle ou
            # d'heures marquees facturees sans que la facture soit complete.
            self.conn.rollback()
            raise
        self.conn.commit()
        return invoice_id

    def get_invoice(self, invoice_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()

    def list_invoices(self, client_id: Optional[int] = None) -> list:
        query = "SELECT * FROM invoices WHERE 1=1"
        params = []
        if client_id is not None:
            query += " AND client_id = ?"
            params.append(client_id)
        query += " ORDER BY invoice_number DESC"
        return self.conn.execute(query, params).fetchall()

    def invoice_time_entries(self, invoice_id: int) -> list:
        return self.conn.execute(
            """SELECT time_entries.*, projects.name AS project_name
               FROM time_entries JOIN projects ON projects.id = time_entries.project_id
               WHERE time_entries.invoice_id = ? ORDER BY time_entries.start_time""",
            (invoice_id,),
        ).fetchall()

    def get_invoice_line_items(self, invoice_id: int) -> list:
        """Lignes de facture telles que figees au moment de la creation de la
        facture (voir create_invoice) - ne reflete jamais un taux horaire
        modifie apres coup."""
        return self.conn.execute(
            "SELECT project_name, hours, rate FROM invoice_line_items WHERE invoice_id = ?",
            (invoice_id,),
        ).fetchall()

    def set_invoice_status(self, invoice_id: int, status: str) -> None:
        if status not in ("unpaid", "paid", "cancelled"):
            raise ValueError(f"statut de facture invalide : {status}")
        self.conn.execute("UPDATE invoices SET status = ? WHERE id = ?", (status, invoice_id))
        self.conn.commit()

    def delete_invoice(self, invoice_id: int) -> None:
        # Libere les entrees de temps associees (elles redeviennent facturables)
        # plutot que de les supprimer : annuler une facture ne doit jamais
        # faire perdre du temps deja suivi.
        self.conn.execute("UPDATE time_entries SET invoice_id = NULL WHERE invoice_id = ?", (invoice_id,))
        self.conn.execute("DELETE FROM invoice_line_items WHERE invoice_id = ?", (invoice_id,))
        self.conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
        self.conn.commit()

    # -- parametres (informations de l'entreprise, etc.) -----------------------

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()
