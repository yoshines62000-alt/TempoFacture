"""Tests pour db.py : schema SQLite, CRUD, calcul du taux horaire effectif,
numerotation des factures, liberation des heures lors de la suppression
d'une facture."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database
from invoice import LineItem


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "test.sqlite")
        self.addCleanup(self.db.close)

    def test_add_and_get_client(self):
        client_id = self.db.add_client("Acme Corp", "contact@acme.test", "1 rue de la Paix", 50.0)
        client = self.db.get_client(client_id)
        self.assertEqual(client["name"], "Acme Corp")
        self.assertEqual(client["hourly_rate"], 50.0)

    def test_list_clients_excludes_archived_by_default(self):
        active = self.db.add_client("Actif")
        archived = self.db.add_client("Archive")
        self.db.update_client(archived, archived=1)
        names = [c["name"] for c in self.db.list_clients()]
        self.assertIn("Actif", names)
        self.assertNotIn("Archive", names)
        # Mais consultable explicitement si demande.
        all_names = [c["name"] for c in self.db.list_clients(include_archived=True)]
        self.assertIn("Archive", all_names)

    def test_project_uses_own_rate_when_set(self):
        client_id = self.db.add_client("Client", hourly_rate=40.0)
        project_id = self.db.add_project(client_id, "Site web", hourly_rate=60.0)
        self.assertEqual(self.db.effective_hourly_rate(project_id), 60.0)

    def test_project_falls_back_to_client_rate_when_unset(self):
        client_id = self.db.add_client("Client", hourly_rate=40.0)
        project_id = self.db.add_project(client_id, "Site web")  # pas de taux propre
        self.assertEqual(self.db.effective_hourly_rate(project_id), 40.0)

    def test_project_rate_of_zero_overrides_client_rate(self):
        # Un taux de 0 (prestation offerte) doit etre distingue de "non
        # defini" (None) - il ne doit jamais retomber sur le taux du client.
        client_id = self.db.add_client("Client", hourly_rate=40.0)
        project_id = self.db.add_project(client_id, "Projet gratuit", hourly_rate=0.0)
        self.assertEqual(self.db.effective_hourly_rate(project_id), 0.0)

    def test_start_and_stop_time_entry(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.start_time_entry(project_id, "Travail en cours")
        running = self.db.get_running_entry()
        self.assertIsNotNone(running)
        self.assertEqual(running["id"], entry_id)

        self.db.stop_time_entry(entry_id)
        self.assertIsNone(self.db.get_running_entry())

    def test_start_time_entry_rejects_second_concurrent_timer(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        self.db.start_time_entry(project_id)
        with self.assertRaises(RuntimeError):
            self.db.start_time_entry(project_id)

    def test_stop_time_entry_rejects_end_before_start(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.start_time_entry(project_id)
        with self.assertRaises(ValueError):
            self.db.stop_time_entry(entry_id, end_time_iso="2000-01-01T00:00:00+00:00")

    def test_add_manual_time_entry_rejects_end_before_start(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        with self.assertRaises(ValueError):
            self.db.add_manual_time_entry(
                project_id, "2026-01-01T11:00:00+00:00", "2026-01-01T09:00:00+00:00"
            )

    def test_delete_time_entry_rejects_already_invoiced_entry(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.create_invoice(client_id, [entry_id])
        with self.assertRaises(ValueError):
            self.db.delete_time_entry(entry_id)

    def test_update_time_entry_rejects_time_change_on_invoiced_entry(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.create_invoice(client_id, [entry_id])
        with self.assertRaises(ValueError):
            self.db.update_time_entry(entry_id, end_time="2026-01-01T12:00:00+00:00")

    def test_update_time_entry_allows_description_change_on_invoiced_entry(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.create_invoice(client_id, [entry_id])
        self.db.update_time_entry(entry_id, description="Note ajoutee apres coup")
        self.assertEqual(self.db.get_time_entry(entry_id)["description"], "Note ajoutee apres coup")

    def test_get_time_entry_returns_none_for_unknown_id(self):
        self.assertIsNone(self.db.get_time_entry(999))

    def test_list_projects_for_active_clients_excludes_archived_client(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        active_ids = [p["id"] for p in self.db.list_projects_for_active_clients()]
        self.assertIn(project_id, active_ids)

        self.db.update_client(client_id, archived=1)
        active_ids = [p["id"] for p in self.db.list_projects_for_active_clients()]
        self.assertNotIn(project_id, active_ids)

    def test_only_one_running_entry_is_reported(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        self.db.start_time_entry(project_id)
        self.assertIsNotNone(self.db.get_running_entry())

    def test_list_time_entries_filters_by_project_and_client(self):
        client_a = self.db.add_client("Client A")
        client_b = self.db.add_client("Client B")
        project_a = self.db.add_project(client_a, "Projet A")
        project_b = self.db.add_project(client_b, "Projet B")
        self.db.add_manual_time_entry(project_a, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.add_manual_time_entry(project_b, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")

        by_project = self.db.list_time_entries(project_id=project_a)
        self.assertEqual(len(by_project), 1)

        by_client = self.db.list_time_entries(client_id=client_b)
        self.assertEqual(len(by_client), 1)
        self.assertEqual(by_client[0]["project_name"], "Projet B")

    def test_invoice_numbering_increments_per_year(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        e1 = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        e2 = self.db.add_manual_time_entry(project_id, "2026-01-02T09:00:00+00:00", "2026-01-02T10:00:00+00:00")

        first_number = self.db.next_invoice_number(year=2026)
        self.assertEqual(first_number, "2026-0001")

        invoice_id_1 = self.db.create_invoice(client_id, [e1], tax_rate=20.0)
        invoice_1 = self.db.get_invoice(invoice_id_1)
        self.assertEqual(invoice_1["invoice_number"], "2026-0001")

        invoice_id_2 = self.db.create_invoice(client_id, [e2], tax_rate=20.0)
        invoice_2 = self.db.get_invoice(invoice_id_2)
        self.assertEqual(invoice_2["invoice_number"], "2026-0002")

    def test_create_invoice_marks_time_entries_as_invoiced(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")

        uninvoiced_before = self.db.list_time_entries(uninvoiced_only=True)
        self.assertEqual(len(uninvoiced_before), 1)

        self.db.create_invoice(client_id, [entry_id], tax_rate=0.0)

        uninvoiced_after = self.db.list_time_entries(uninvoiced_only=True)
        self.assertEqual(len(uninvoiced_after), 0)

    def test_delete_invoice_releases_time_entries_for_reinvoicing(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        invoice_id = self.db.create_invoice(client_id, [entry_id], tax_rate=0.0)

        self.assertEqual(len(self.db.list_time_entries(uninvoiced_only=True)), 0)

        self.db.delete_invoice(invoice_id)

        # Le temps redevient facturable, il n'est pas perdu.
        uninvoiced = self.db.list_time_entries(uninvoiced_only=True)
        self.assertEqual(len(uninvoiced), 1)
        self.assertEqual(uninvoiced[0]["id"], entry_id)
        self.assertIsNone(self.db.get_invoice(invoice_id))

    def test_create_invoice_rejects_time_entries_from_other_client(self):
        client_a = self.db.add_client("Client A")
        client_b = self.db.add_client("Client B")
        project_a = self.db.add_project(client_a, "Projet A")
        entry_id = self.db.add_manual_time_entry(project_a, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")

        with self.assertRaises(ValueError):
            self.db.create_invoice(client_b, [entry_id])  # l'entree appartient a client_a, pas client_b

        # L'entree ne doit pas avoir ete marquee facturee malgre l'echec.
        self.assertEqual(len(self.db.list_time_entries(uninvoiced_only=True)), 1)

    def test_invoice_number_not_reused_after_deleting_an_invoice(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        e1 = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        e2 = self.db.add_manual_time_entry(project_id, "2026-01-02T09:00:00+00:00", "2026-01-02T10:00:00+00:00")
        e3 = self.db.add_manual_time_entry(project_id, "2026-01-03T09:00:00+00:00", "2026-01-03T10:00:00+00:00")

        invoice_1 = self.db.create_invoice(client_id, [e1])  # 2026-0001
        invoice_2 = self.db.create_invoice(client_id, [e2])  # 2026-0002
        self.db.delete_invoice(invoice_1)  # libere le numero 0001, mais ne doit pas etre reemis

        invoice_3 = self.db.create_invoice(client_id, [e3])
        number_3 = self.db.get_invoice(invoice_3)["invoice_number"]
        number_2 = self.db.get_invoice(invoice_2)["invoice_number"]
        self.assertNotEqual(number_3, number_2)
        self.assertEqual(number_3, "2026-0003")

    def test_set_invoice_status_rejects_invalid_value(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        invoice_id = self.db.create_invoice(client_id, [entry_id])
        with self.assertRaises(ValueError):
            self.db.set_invoice_status(invoice_id, "statut_invalide")

    def test_invoice_line_items_are_frozen_after_rate_change(self):
        client_id = self.db.add_client("Client", hourly_rate=50.0)
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00")

        line_items = [LineItem(project_name="Projet", hours=2.0, rate=50.0)]
        invoice_id = self.db.create_invoice(client_id, [entry_id], line_items=line_items)

        # Le taux du client augmente APRES la creation de la facture.
        self.db.update_client(client_id, hourly_rate=90.0)

        stored = self.db.get_invoice_line_items(invoice_id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["rate"], 50.0)  # jamais 90.0 : fige a la creation

    def test_create_invoice_with_no_time_entries_supports_recurring_invoices(self):
        # Une facture "dupliquee" (facturation recurrente) ne consomme aucune
        # heure suivie reelle - seules les lignes recopiees comptent.
        client_id = self.db.add_client("Client")
        line_items = [LineItem(project_name="Forfait mensuel", hours=10.0, rate=100.0)]
        invoice_id = self.db.create_invoice(client_id, [], tax_rate=20.0, line_items=line_items)
        invoice = self.db.get_invoice(invoice_id)
        self.assertEqual(invoice["client_id"], client_id)
        stored = self.db.get_invoice_line_items(invoice_id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["project_name"], "Forfait mensuel")
        self.assertEqual(stored[0]["hours"], 10.0)

    def test_create_invoice_freezes_the_current_currency_setting(self):
        self.db.set_setting("currency", "USD")
        client_id = self.db.add_client("Client")
        line_items = [LineItem(project_name="Forfait", hours=1.0, rate=100.0)]
        invoice_id = self.db.create_invoice(client_id, [], line_items=line_items)
        self.assertEqual(self.db.get_invoice(invoice_id)["currency"], "USD")

    def test_changing_the_currency_setting_does_not_retroactively_relabel_old_invoices(self):
        # Trouve a l'audit : sans devise figee par facture, changer le
        # parametre global apres coup relabellisait silencieusement toutes
        # les factures deja emises, sans aucune conversion reelle du montant.
        self.db.set_setting("currency", "EUR")
        client_id = self.db.add_client("Client")
        line_items = [LineItem(project_name="Forfait", hours=1.0, rate=100.0)]
        invoice_id = self.db.create_invoice(client_id, [], line_items=line_items)
        self.db.set_setting("currency", "USD")
        self.assertEqual(self.db.get_invoice(invoice_id)["currency"], "EUR")

    def test_create_invoice_defaults_to_eur_when_no_currency_setting_exists(self):
        client_id = self.db.add_client("Client")
        line_items = [LineItem(project_name="Forfait", hours=1.0, rate=100.0)]
        invoice_id = self.db.create_invoice(client_id, [], line_items=line_items)
        self.assertEqual(self.db.get_invoice(invoice_id)["currency"], "EUR")

    def test_create_invoice_accepts_an_explicit_currency_override(self):
        client_id = self.db.add_client("Client")
        line_items = [LineItem(project_name="Forfait", hours=1.0, rate=100.0)]
        invoice_id = self.db.create_invoice(client_id, [], line_items=line_items, currency="GBP")
        self.assertEqual(self.db.get_invoice(invoice_id)["currency"], "GBP")

    def test_reopening_a_pre_currency_database_file_adds_the_missing_column(self):
        # Simule une base creee avant l'ajout de currency : la migration
        # additive ne doit ni planter, ni casser les factures existantes.
        self.db.close()
        import sqlite3

        old_style_path = self.tmp / "old.sqlite"
        conn = sqlite3.connect(str(old_style_path))
        conn.executescript("""
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '', hourly_rate REAL NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL, name TEXT NOT NULL,
                hourly_rate REAL, archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, start_time TEXT NOT NULL,
                end_time TEXT, description TEXT NOT NULL DEFAULT '', invoice_id INTEGER
            );
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL, invoice_number TEXT NOT NULL UNIQUE,
                issue_date TEXT NOT NULL, due_date TEXT, tax_rate REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'unpaid', created_at TEXT NOT NULL
            );
            CREATE TABLE invoice_line_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL, project_name TEXT NOT NULL,
                hours REAL NOT NULL, rate REAL NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.execute(
            "INSERT INTO clients (name, created_at) VALUES ('Ancien client', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO invoices (client_id, invoice_number, issue_date, created_at) "
            "VALUES (1, '2025-0001', '2025-01-01', '2025-01-01')"
        )
        conn.commit()
        conn.close()

        reopened = Database(old_style_path)
        self.addCleanup(reopened.close)
        old_invoice = reopened.list_invoices()[0]
        self.assertEqual(old_invoice["currency"], "EUR")
        new_id = reopened.create_invoice(1, [], line_items=[LineItem("Forfait", 1.0, 50.0)])
        self.assertEqual(reopened.get_invoice(new_id)["currency"], "EUR")

    def test_list_overdue_invoices_finds_unpaid_past_due_date(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        invoice_id = self.db.create_invoice(client_id, [entry_id], due_date="2026-01-15")
        overdue = self.db.list_overdue_invoices(as_of="2026-02-01")
        self.assertEqual([row["id"] for row in overdue], [invoice_id])

    def test_list_overdue_invoices_excludes_invoices_not_yet_due(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.create_invoice(client_id, [entry_id], due_date="2026-03-01")
        overdue = self.db.list_overdue_invoices(as_of="2026-02-01")
        self.assertEqual(overdue, [])

    def test_list_overdue_invoices_excludes_paid_invoices(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        invoice_id = self.db.create_invoice(client_id, [entry_id], due_date="2026-01-15")
        self.db.set_invoice_status(invoice_id, "paid")
        overdue = self.db.list_overdue_invoices(as_of="2026-02-01")
        self.assertEqual(overdue, [])

    def test_list_overdue_invoices_excludes_invoices_without_due_date(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.create_invoice(client_id, [entry_id])  # pas de due_date
        overdue = self.db.list_overdue_invoices(as_of="2026-02-01")
        self.assertEqual(overdue, [])

    def test_list_overdue_invoices_defaults_to_today_when_as_of_omitted(self):
        client_id = self.db.add_client("Client")
        project_id = self.db.add_project(client_id, "Projet")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.create_invoice(client_id, [entry_id], due_date="2000-01-01")  # tres passe
        overdue = self.db.list_overdue_invoices()
        self.assertEqual(len(overdue), 1)

    def test_settings_roundtrip(self):
        self.db.set_setting("company_name", "Ma Societe")
        self.assertEqual(self.db.get_setting("company_name"), "Ma Societe")
        self.assertEqual(self.db.get_setting("inconnu", default="valeur_par_defaut"), "valeur_par_defaut")

    def test_settings_update_overwrites_previous_value(self):
        self.db.set_setting("company_name", "Ancien Nom")
        self.db.set_setting("company_name", "Nouveau Nom")
        self.assertEqual(self.db.get_setting("company_name"), "Nouveau Nom")


if __name__ == "__main__":
    unittest.main()
