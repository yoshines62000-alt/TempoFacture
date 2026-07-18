"""Tests pour invoice.py : calcul des heures/montants (logique pure) et
generation reelle d'un PDF via fpdf2."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database
import invoice as inv


class DurationTestCase(unittest.TestCase):
    def test_computes_hours_between_two_timestamps(self):
        hours = inv.compute_duration_hours("2026-01-01T09:00:00+00:00", "2026-01-01T11:30:00+00:00")
        self.assertAlmostEqual(hours, 2.5)

    def test_negative_duration_is_clamped_to_zero(self):
        # Protection defensive : une entree corrompue (fin avant le debut)
        # ne doit jamais produire des heures negatives sur une facture.
        hours = inv.compute_duration_hours("2026-01-01T11:00:00+00:00", "2026-01-01T09:00:00+00:00")
        self.assertEqual(hours, 0.0)


class BuildLineItemsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "test.sqlite")
        self.addCleanup(self.db.close)
        self.client_id = self.db.add_client("Client Test", hourly_rate=50.0)
        self.project_id = self.db.add_project(self.client_id, "Projet A")

    def test_groups_and_sums_hours_by_project(self):
        self.db.add_manual_time_entry(self.project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.add_manual_time_entry(self.project_id, "2026-01-02T09:00:00+00:00", "2026-01-02T11:00:00+00:00")
        entries = self.db.list_time_entries(project_id=self.project_id)

        line_items = inv.build_line_items(entries, self.db)
        self.assertEqual(len(line_items), 1)
        self.assertAlmostEqual(line_items[0].hours, 3.0)
        self.assertEqual(line_items[0].rate, 50.0)
        self.assertAlmostEqual(line_items[0].amount, 150.0)

    def test_running_entry_without_end_time_is_excluded(self):
        self.db.add_manual_time_entry(self.project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.start_time_entry(self.project_id)  # en cours, pas de end_time
        entries = self.db.list_time_entries(project_id=self.project_id)

        line_items = inv.build_line_items(entries, self.db)
        self.assertEqual(len(line_items), 1)
        self.assertAlmostEqual(line_items[0].hours, 1.0)  # seule l'entree terminee compte

    def test_separate_projects_produce_separate_line_items(self):
        project_b = self.db.add_project(self.client_id, "Projet B", hourly_rate=80.0)
        self.db.add_manual_time_entry(self.project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        self.db.add_manual_time_entry(project_b, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        entries = self.db.list_time_entries(client_id=self.client_id)

        line_items = inv.build_line_items(entries, self.db)
        self.assertEqual(len(line_items), 2)
        rates = sorted(li.rate for li in line_items)
        self.assertEqual(rates, [50.0, 80.0])


class ComputeTotalsTestCase(unittest.TestCase):
    def test_totals_with_tax(self):
        items = [inv.LineItem("Projet A", hours=10.0, rate=50.0)]
        subtotal, tax_amount, total = inv.compute_totals(items, tax_rate=20.0)
        self.assertEqual(subtotal, 500.0)
        self.assertEqual(tax_amount, 100.0)
        self.assertEqual(total, 600.0)

    def test_totals_with_zero_tax(self):
        items = [inv.LineItem("Projet A", hours=5.0, rate=40.0)]
        subtotal, tax_amount, total = inv.compute_totals(items, tax_rate=0.0)
        self.assertEqual(subtotal, 200.0)
        self.assertEqual(tax_amount, 0.0)
        self.assertEqual(total, 200.0)

    def test_totals_sum_multiple_line_items(self):
        items = [
            inv.LineItem("Projet A", hours=10.0, rate=50.0),
            inv.LineItem("Projet B", hours=5.0, rate=80.0),
        ]
        subtotal, tax_amount, total = inv.compute_totals(items, tax_rate=10.0)
        self.assertEqual(subtotal, 900.0)  # 500 + 400
        self.assertEqual(tax_amount, 90.0)
        self.assertEqual(total, 990.0)


class GenerateInvoicePdfTestCase(unittest.TestCase):
    def test_generates_a_readable_pdf_file(self):
        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture.pdf"
        items = [inv.LineItem("Site web", hours=12.5, rate=45.0)]

        inv.generate_invoice_pdf(
            output_path=output_path,
            invoice_number="2026-0001",
            issue_date="2026-01-15",
            due_date="2026-02-15",
            company_name="Mon Entreprise",
            company_info="SIRET 123 456 789",
            client_name="Client SARL",
            client_address="1 rue de la Republique, 75001 Paris",
            line_items=items,
            tax_rate=20.0,
            notes="Merci de votre confiance.",
        )

        self.assertTrue(output_path.exists())
        content = output_path.read_bytes()
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertGreater(len(content), 500)  # un vrai contenu, pas un fichier vide/tronque

    def test_generates_pdf_with_no_line_items_without_crashing(self):
        # Une facture vide (aucune heure selectionnee) ne doit pas planter
        # la generation, meme si elle n'a pas grand sens fonctionnellement.
        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_vide.pdf"
        inv.generate_invoice_pdf(
            output_path=output_path,
            invoice_number="2026-0002",
            issue_date="2026-01-15",
            due_date=None,
            company_name="Mon Entreprise",
            company_info="",
            client_name="Client",
            client_address="",
            line_items=[],
            tax_rate=0.0,
        )
        self.assertTrue(output_path.exists())

    def test_generates_pdf_with_non_latin1_characters_without_crashing(self):
        # Champs libres copies-colles depuis Word/le web : tiret long "—",
        # guillemets courbes, emoji, signe euro... aucun ne fait partie du
        # jeu de caracteres Latin-1 de la police de base "Helvetica". La
        # generation doit degrader (caracteres remplaces), jamais planter.
        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_unicode.pdf"
        items = [inv.LineItem("Projet — spécial 🚀", hours=1.0, rate=50.0)]
        inv.generate_invoice_pdf(
            output_path=output_path,
            invoice_number="2026-0003",
            issue_date="2026-01-15",
            due_date=None,
            company_name="Société “Test” — 100€",
            company_info="Ünïcödé ✨",
            client_name="Client",
            client_address="",
            line_items=items,
            tax_rate=0.0,
            notes="Merci 🙏 — à bientôt",
        )
        self.assertTrue(output_path.exists())
        self.assertTrue(output_path.read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
