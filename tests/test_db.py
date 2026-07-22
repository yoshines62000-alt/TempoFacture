"""Tests pour db.py : schema SQLite, CRUD, calcul du taux horaire effectif,
numerotation des factures, liberation des heures lors de la suppression
d'une facture."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database, TVA_EXEMPTION_TEMPLATE_NAME
from invoice import LineItem, compute_totals


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

    def test_get_all_invoice_line_items_matches_per_invoice_lookups(self):
        # get_all_invoice_line_items() (une seule requete, invoice_id inclus)
        # doit renvoyer, une fois regroupe par invoice_id, exactement les
        # memes lignes que get_invoice_line_items() appelee facture par
        # facture (l'ancienne methode, cause du pattern N+1 dans
        # gui._refresh_invoices) - voir aussi InvoiceRefreshPerformanceTestCase
        # plus bas pour la mesure de performance sur un gros volume.
        client_id = self.db.add_client("Client", hourly_rate=50.0)
        project_id = self.db.add_project(client_id, "Projet")
        invoice_ids = []
        for i in range(3):
            entry_id = self.db.add_manual_time_entry(
                project_id, f"2026-01-0{i + 1}T09:00:00+00:00", f"2026-01-0{i + 1}T1{i}:00:00+00:00"
            )
            line_items = [LineItem(project_name="Projet", hours=float(i + 1), rate=50.0 + i)]
            invoice_ids.append(self.db.create_invoice(client_id, [entry_id], line_items=line_items))
        # Une facture recurrente sans aucune ligne : doit apparaitre comme
        # liste vide, pas comme cle absente.
        empty_invoice_id = self.db.create_invoice(client_id, [], line_items=[])
        invoice_ids.append(empty_invoice_id)

        grouped: dict = {}
        for row in self.db.get_all_invoice_line_items():
            grouped.setdefault(row["invoice_id"], []).append((row["project_name"], row["hours"], row["rate"]))

        for invoice_id in invoice_ids:
            expected = [
                (row["project_name"], row["hours"], row["rate"])
                for row in self.db.get_invoice_line_items(invoice_id)
            ]
            self.assertEqual(grouped.get(invoice_id, []), expected)

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

    def test_save_and_list_note_templates(self):
        self.db.save_note_template("Merci", "Merci pour votre confiance.")
        # Le modele "Franchise en base de TVA" est deja present sur toute
        # base fraichement creee (voir A6 / test_new_database_seeds_the_tva_
        # exemption_note_template ci-dessous) : on l'exclut ici pour garder
        # ce test concentre sur le comportement save/list generique.
        templates = [t for t in self.db.list_note_templates() if t["name"] != TVA_EXEMPTION_TEMPLATE_NAME]
        self.assertEqual(templates, [{"name": "Merci", "text": "Merci pour votre confiance."}])

    def test_save_note_template_with_existing_name_overwrites_it(self):
        self.db.save_note_template("Merci", "Ancien texte")
        self.db.save_note_template("Merci", "Nouveau texte")
        templates = [t for t in self.db.list_note_templates() if t["name"] == "Merci"]
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["text"], "Nouveau texte")

    def test_save_note_template_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            self.db.save_note_template("   ", "Un texte")

    def test_delete_note_template_removes_only_the_named_one(self):
        self.db.save_note_template("A", "texte A")
        self.db.save_note_template("B", "texte B")
        self.db.delete_note_template("A")
        names = [t["name"] for t in self.db.list_note_templates()]
        self.assertIn("B", names)
        self.assertNotIn("A", names)

    def test_delete_unknown_note_template_is_a_no_op(self):
        before = len(self.db.list_note_templates())
        self.db.save_note_template("A", "texte A")
        self.db.delete_note_template("Inconnu")
        self.assertEqual(len(self.db.list_note_templates()), before + 1)

    # -- item audit Phase 4, A6 : mention legale de franchise en base de --
    # -- TVA absente (modele de note preconfigure des la premiere --------
    # -- installation) ------------------------------------------------------

    def test_new_database_seeds_the_tva_exemption_note_template(self):
        # Correctif A6 : un auto-entrepreneur francais exonere de TVA
        # (franchise en base) doit legalement faire figurer une mention
        # specifique sur ses factures (art. 293 B du CGI) - rien ne le lui
        # suggerait avant ce correctif (bug trouve a l'audit). Une base
        # fraichement creee (self.db, voir setUp) est desormais
        # preinitialisee avec un modele de note pret a l'emploi pour cela.
        templates = self.db.list_note_templates()
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["name"], TVA_EXEMPTION_TEMPLATE_NAME)
        self.assertIn("293 B", templates[0]["text"])

    def test_deleting_the_seeded_template_is_not_reseeded_on_reopen(self):
        # Le modele par defaut n'est fourni qu'"a la premiere installation"
        # (cle jamais ecrite en base) : un utilisateur qui le supprime
        # explicitement ne doit jamais le voir reapparaitre a la prochaine
        # ouverture de la meme base - sans quoi il serait impossible de
        # vraiment s'en debarrasser.
        self.db.delete_note_template(TVA_EXEMPTION_TEMPLATE_NAME)
        self.db.close()
        reopened = Database(self.tmp / "test.sqlite")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.list_note_templates(), [])

    def test_settings_roundtrip(self):
        self.db.set_setting("company_name", "Ma Societe")
        self.assertEqual(self.db.get_setting("company_name"), "Ma Societe")
        self.assertEqual(self.db.get_setting("inconnu", default="valeur_par_defaut"), "valeur_par_defaut")

    def test_settings_update_overwrites_previous_value(self):
        self.db.set_setting("company_name", "Ancien Nom")
        self.db.set_setting("company_name", "Nouveau Nom")
        self.assertEqual(self.db.get_setting("company_name"), "Nouveau Nom")

    def test_backup_to_copies_all_data_to_a_new_file(self):
        client_id = self.db.add_client("Acme Corp", hourly_rate=50.0)
        project_id = self.db.add_project(client_id, "Site web")
        entry_id = self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        invoice_id = self.db.create_invoice(client_id, [entry_id])

        backup_path = self.tmp / "backup.sqlite"
        self.db.backup_to(backup_path)

        copy = Database(backup_path)
        self.addCleanup(copy.close)
        self.assertEqual(copy.get_client(client_id)["name"], "Acme Corp")
        self.assertEqual(copy.get_project(project_id)["name"], "Site web")
        self.assertIsNotNone(copy.get_invoice(invoice_id))

    def test_backup_to_is_independent_of_later_writes(self):
        self.db.add_client("Avant la sauvegarde")
        backup_path = self.tmp / "backup.sqlite"
        self.db.backup_to(backup_path)
        self.db.add_client("Apres la sauvegarde")

        copy = Database(backup_path)
        self.addCleanup(copy.close)
        names = {c["name"] for c in copy.list_clients()}
        self.assertIn("Avant la sauvegarde", names)
        self.assertNotIn("Apres la sauvegarde", names)

    def test_backup_to_the_live_path_raises(self):
        with self.assertRaises(ValueError):
            self.db.backup_to(self.db.path)

    def test_backup_to_the_live_path_spelled_differently_still_raises(self):
        aliased = self.db.path.parent / ".." / self.db.path.parent.name / self.db.path.name
        with self.assertRaises(ValueError):
            self.db.backup_to(aliased)

    def test_schema_creates_indexes_on_foreign_keys(self):
        # Sans ces index, chaque filtre/jointure sur une cle etrangere
        # (time_entries.project_id, time_entries.invoice_id,
        # projects.client_id, invoices.client_id) force un scan complet de
        # la table concernee.
        index_names = {
            row["name"]
            for row in self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        expected = {
            "idx_time_entries_project_id",
            "idx_time_entries_invoice_id",
            "idx_projects_client_id",
            "idx_invoices_client_id",
        }
        self.assertTrue(expected.issubset(index_names))

    def test_reopening_a_pre_index_database_file_adds_the_missing_indexes(self):
        # Meme logique de migration additive que pour la colonne currency :
        # une base existante (creee avant l'ajout des index) ne doit ni
        # planter a la reouverture, ni rester sans les nouveaux index.
        self.db.close()
        import sqlite3

        old_style_path = self.tmp / "old_no_index.sqlite"
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
                notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'unpaid', created_at TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'EUR'
            );
            CREATE TABLE invoice_line_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL, project_name TEXT NOT NULL,
                hours REAL NOT NULL, rate REAL NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.commit()
        conn.close()

        reopened = Database(old_style_path)
        self.addCleanup(reopened.close)
        index_names = {
            row["name"]
            for row in reopened.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        self.assertIn("idx_time_entries_project_id", index_names)
        self.assertIn("idx_invoices_client_id", index_names)

    def test_backup_to_a_hard_link_of_the_live_path_raises(self):
        # Regression trouvee a l'audit : resolve() ne detecte jamais un
        # lien physique (hard link) vers le meme fichier, puisqu'un lien
        # physique n'est pas un point de reparse a suivre. Sans la
        # verification d'identite via os.path.samefile, backup_to tentait
        # d'ouvrir une seconde connexion vers le fichier physique deja
        # ouvert et restait bloque indefiniment.
        hardlink = self.db.path.parent / "hardlink.sqlite"
        os.link(self.db.path, hardlink)
        with self.assertRaises(ValueError):
            self.db.backup_to(hardlink)


class InvoiceRefreshPerformanceTestCase(unittest.TestCase):
    """Verrouille la correction du pattern N+1 mesure a l'audit Phase 3 dans
    gui._refresh_invoices() : cette fonction appelait get_invoice_line_items()
    UNE FOIS PAR FACTURE affichee (~750ms avec 3000 factures, mesure a
    l'audit), au lieu d'une seule requete groupee. On reproduit ici le meme
    volume synthetique (3000 factures) et on compare l'ancienne strategie
    (une requete par facture) a la nouvelle (get_all_invoice_line_items(),
    une seule requete puis regroupement Python) : meme resultat, largement
    plus rapide."""

    INVOICE_COUNT = 3000

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "perf.sqlite")
        self.addCleanup(self.db.close)

        client_id = self.db.add_client("Client volumineux", hourly_rate=50.0)
        now = "2026-01-01T00:00:00+00:00"
        # Insertion directe en masse (executemany) plutot que via
        # create_invoice() facture par facture : seul le volume de donnees
        # nous interesse ici, pas la logique de numerotation/snapshot deja
        # testee ailleurs - generer 3000 factures via l'API haut niveau
        # rendrait ce test lui-meme trop lent.
        invoice_rows = [
            (client_id, f"2026-{i:04d}", "2026-01-01", None, 20.0, "", "unpaid", "EUR", now)
            for i in range(self.INVOICE_COUNT)
        ]
        self.db.conn.executemany(
            """INSERT INTO invoices
               (client_id, invoice_number, issue_date, due_date, tax_rate, notes, status, currency, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            invoice_rows,
        )
        self.db.conn.commit()
        self.invoice_ids = [row["id"] for row in self.db.conn.execute("SELECT id FROM invoices").fetchall()]

        line_item_rows = []
        for idx, invoice_id in enumerate(self.invoice_ids):
            # Deux lignes par facture, montants varies, pour un total non
            # trivial (arrondi Decimal inclus).
            line_item_rows.append((invoice_id, "Projet A", 1.0 + (idx % 7) * 0.25, 45.0 + (idx % 5)))
            line_item_rows.append((invoice_id, "Projet B", 2.5, 60.0))
        self.db.conn.executemany(
            "INSERT INTO invoice_line_items (invoice_id, project_name, hours, rate) VALUES (?, ?, ?, ?)",
            line_item_rows,
        )
        self.db.conn.commit()

    def _totals_via_old_n_plus_one_lookup(self) -> dict:
        totals = {}
        for invoice in self.db.list_invoices():
            stored_items = self.db.get_invoice_line_items(invoice["id"])
            line_items = [LineItem(row["project_name"], row["hours"], row["rate"]) for row in stored_items]
            _, _, total = compute_totals(line_items, invoice["tax_rate"])
            totals[invoice["id"]] = total
        return totals

    def _totals_via_batched_lookup(self) -> dict:
        line_items_by_invoice: dict = {}
        for row in self.db.get_all_invoice_line_items():
            line_items_by_invoice.setdefault(row["invoice_id"], []).append(row)
        totals = {}
        for invoice in self.db.list_invoices():
            stored_items = line_items_by_invoice.get(invoice["id"], [])
            line_items = [LineItem(row["project_name"], row["hours"], row["rate"]) for row in stored_items]
            _, _, total = compute_totals(line_items, invoice["tax_rate"])
            totals[invoice["id"]] = total
        return totals

    def test_batched_lookup_gives_identical_totals_to_the_old_n_plus_one_lookup(self):
        self.assertEqual(self._totals_via_old_n_plus_one_lookup(), self._totals_via_batched_lookup())

    def test_batched_lookup_is_much_faster_than_one_query_per_invoice(self):
        start = time.perf_counter()
        self._totals_via_old_n_plus_one_lookup()
        old_duration = time.perf_counter() - start

        start = time.perf_counter()
        self._totals_via_batched_lookup()
        new_duration = time.perf_counter() - start

        # Assertion de duree volontairement large (machine de CI potentiellement
        # lente/partagee) : on verifie un gain net, pas un chiffre precis.
        # L'audit mesurait ~750ms pour l'ancienne strategie avec ce volume ;
        # on verifie ici au moins un facteur 5, et un plafond absolu genereux
        # pour la nouvelle strategie.
        self.assertLess(new_duration, old_duration / 5)
        self.assertLess(new_duration, 0.5)


class ProjectRefreshPerformanceTestCase(unittest.TestCase):
    """Verrouille la correction du pattern N+1 mesure a l'audit Phase 4 dans
    gui._refresh_projects() : cette fonction appelait
    effective_hourly_rate(project_id) UNE FOIS PAR PROJET affiche (chaque
    appel refait un get_project(), puis potentiellement un get_client() -
    jusqu'a 2 requetes SQL de plus par ligne, mesure a ~96x plus lent avec
    2000 projets a l'audit), au lieu de precharger une seule fois les
    clients (nom + taux horaire) et de calculer le taux effectif en Python.
    Exactement le meme pattern - et la meme methodologie de test - que
    InvoiceRefreshPerformanceTestCase ci-dessus, deja corrige pour les
    factures."""

    PROJECT_COUNT = 2000

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "perf_projects.sqlite")
        self.addCleanup(self.db.close)

        self.client_ids = [self.db.add_client(f"Client {i}", hourly_rate=40.0 + i) for i in range(20)]
        now = "2026-01-01T00:00:00+00:00"
        project_rows = []
        for i in range(self.PROJECT_COUNT):
            client_id = self.client_ids[i % len(self.client_ids)]
            # Un projet sur deux definit son propre taux (l'autre herite de
            # celui de son client) : les deux branches de
            # effective_hourly_rate doivent etre exercees par ce test.
            project_hourly_rate = 55.0 + (i % 13) if i % 2 == 0 else None
            project_rows.append((client_id, f"Projet {i}", project_hourly_rate, 0, now))
        self.db.conn.executemany(
            "INSERT INTO projects (client_id, name, hourly_rate, archived, created_at) VALUES (?, ?, ?, ?, ?)",
            project_rows,
        )
        self.db.conn.commit()

    def _rates_via_old_n_plus_one_lookup(self) -> dict:
        rates = {}
        for project in self.db.list_projects(include_archived=True):
            rates[project["id"]] = self.db.effective_hourly_rate(project["id"])
        return rates

    def _rates_via_batched_lookup(self) -> dict:
        clients_by_id = {c["id"]: c for c in self.db.list_clients(include_archived=True)}
        rates = {}
        for project in self.db.list_projects(include_archived=True):
            client = clients_by_id.get(project["client_id"])
            if project["hourly_rate"] is not None:
                rates[project["id"]] = float(project["hourly_rate"])
            elif client is not None:
                rates[project["id"]] = float(client["hourly_rate"])
            else:
                rates[project["id"]] = 0.0
        return rates

    def test_batched_lookup_gives_identical_rates_to_the_old_n_plus_one_lookup(self):
        self.assertEqual(self._rates_via_old_n_plus_one_lookup(), self._rates_via_batched_lookup())

    def test_batched_lookup_is_much_faster_than_one_query_per_project(self):
        start = time.perf_counter()
        self._rates_via_old_n_plus_one_lookup()
        old_duration = time.perf_counter() - start

        start = time.perf_counter()
        self._rates_via_batched_lookup()
        new_duration = time.perf_counter() - start

        # Assertion de duree volontairement large (machine de CI potentiellement
        # lente/partagee) : on verifie un gain net, pas un chiffre precis.
        # L'audit mesurait un facteur ~96x avec 2000 projets ; on verifie
        # ici au moins un facteur 5, et un plafond absolu genereux pour la
        # nouvelle strategie.
        self.assertLess(new_duration, old_duration / 5)
        self.assertLess(new_duration, 0.5)


if __name__ == "__main__":
    unittest.main()
