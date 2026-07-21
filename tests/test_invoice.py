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


class LineItemAmountRoundingTestCase(unittest.TestCase):
    def test_amount_rounds_half_up_on_a_known_binary_float_tie(self):
        # round() natif de Python arrondirait ce cas precis a 3.12 (round-
        # half-to-even sur la representation binaire de 0.05*62.5), alors
        # que le montant correct pour une facture est 3.13.
        item = inv.LineItem("Projet", hours=0.05, rate=62.5)
        self.assertEqual(item.amount, 3.13)


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

    def test_generates_pdf_with_very_long_project_name_without_overflow_crash(self):
        # Un nom de projet trop long pour sa colonne doit etre tronque, pas
        # provoquer un depassement silencieux ou une exception fpdf2.
        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_long_nom.pdf"
        items = [inv.LineItem("Un nom de projet extremement long " * 5, hours=1.0, rate=50.0)]
        inv.generate_invoice_pdf(
            output_path=output_path,
            invoice_number="2026-0004",
            issue_date="2026-01-15",
            due_date=None,
            company_name="Mon Entreprise",
            company_info="",
            client_name="Client",
            client_address="",
            line_items=items,
            tax_rate=0.0,
        )
        self.assertTrue(output_path.exists())
        self.assertTrue(output_path.read_bytes().startswith(b"%PDF"))

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

    def test_generates_pdf_with_non_latin1_currency_without_crashing(self):
        # Regression : le champ "Devise" des Parametres n'est valide que
        # "non vide" cote GUI, donc un utilisateur peut y saisir "€" au lieu
        # de "EUR". Avant correctif, currency n'etait jamais passe par
        # _latin1_safe() (contrairement aux autres champs texte) alors qu'il
        # est injecte tel quel dans format_amount() puis pdf.cell() pour 5
        # lignes du tableau : cela levait une exception d'encodage (pas un
        # OSError) qui echappait au garde-fou de gui.py et laissait une
        # facture fantome en base. La generation doit degrader (caractere
        # remplace), jamais planter.
        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_devise.pdf"
        items = [inv.LineItem("Projet A", hours=2.0, rate=50.0)]
        inv.generate_invoice_pdf(
            output_path=output_path,
            invoice_number="2026-0004",
            issue_date="2026-01-15",
            due_date=None,
            company_name="Ma societe",
            company_info="",
            client_name="Client",
            client_address="",
            line_items=items,
            tax_rate=0.0,
            currency="€",
        )
        self.assertTrue(output_path.exists())
        self.assertTrue(output_path.read_bytes().startswith(b"%PDF"))

    def test_generates_pdf_with_long_currency_without_overflowing_columns(self):
        # Regression (audit Phase 2) : une devise longue (ex. "DOLLARS
        # AMERICAINS") gonfle le texte formate des colonnes Heures/Taux/
        # Montant au point de deborder de leur cellule et de chevaucher la
        # colonne voisine, puisque ces 3 colonnes (contrairement a
        # Description) n'etaient jamais tronquees avec ellipse avant
        # correctif. On intercepte les appels reels a FPDF.cell() pour
        # verifier que le texte effectivement dessine dans chaque cellule
        # tient desormais dans la largeur de sa colonne (avant correctif, ce
        # test echoue : le texte brut deborde).
        from fpdf import FPDF
        from unittest import mock

        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_devise_longue.pdf"
        items = [inv.LineItem("Projet A", hours=12345.67, rate=99999999.99)]

        recorded = []
        original_cell = FPDF.cell

        def spying_cell(self, w=0, h=0, text="", *args, **kwargs):
            recorded.append((w, text, self.get_string_width(text)))
            return original_cell(self, w, h, text, *args, **kwargs)

        with mock.patch.object(FPDF, "cell", spying_cell):
            inv.generate_invoice_pdf(
                output_path=output_path,
                invoice_number="2026-0005",
                issue_date="2026-01-15",
                due_date=None,
                company_name="Ma societe",
                company_info="",
                client_name="Client",
                client_address="",
                line_items=items,
                tax_rate=0.0,
                currency="DOLLARS AMERICAINS",
            )

        self.assertTrue(output_path.exists())
        self.assertTrue(output_path.read_bytes().startswith(b"%PDF"))

        col_widths = [90, 25, 30, 30]
        # Localise la cellule "Description" de la ligne de prestation (seule
        # cellule de largeur 90 dont le texte est "Projet A") et prend les 3
        # cellules qui la suivent immediatement (Heures/Taux/Montant) :
        # les autres cellules de largeur 30 plus loin dans le document
        # (sous-total/TVA/total) sont hors du perimetre de ce correctif.
        start = next(i for i, (w, text, _) in enumerate(recorded) if w == col_widths[0] and text == "Projet A")
        row_cells = recorded[start:start + 4]
        self.assertEqual(len(row_cells), 4)
        for width, text, drawn_width in row_cells:
            self.assertLessEqual(
                drawn_width, width,
                f"le texte '{text}' (largeur {drawn_width}mm) deborde de sa colonne ({width}mm)",
            )

        # Verifie aussi qu'une ellipse a bien ete appliquee sur les colonnes
        # Taux/Montant, ou la devise longue rend le texte trop large pour la
        # colonne (la colonne Heures n'est pas affectee par la devise et
        # n'a donc pas besoin d'etre tronquee dans ce scenario - sinon le
        # test ne verrouillerait rien de nouveau par rapport a la colonne
        # Description deja tronquee).
        rate_cell, amount_cell = row_cells[2], row_cells[3]
        self.assertTrue(rate_cell[1].endswith("..."))
        self.assertTrue(amount_cell[1].endswith("..."))


class ReexportInvoicePdfTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "test.sqlite")
        self.addCleanup(self.db.close)
        self.client_id = self.db.add_client("Client Test", address="1 rue de la Paix", hourly_rate=50.0)
        self.project_id = self.db.add_project(self.client_id, "Projet A")
        entry_id = self.db.add_manual_time_entry(
            self.project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00"
        )
        entries = self.db.list_time_entries(project_id=self.project_id)
        line_items = inv.build_line_items(entries, self.db)
        self.invoice_id = self.db.create_invoice(
            self.client_id, [entry_id], tax_rate=20.0, line_items=line_items
        )

    def test_reexport_produces_a_pdf_with_totals_frozen_despite_rate_change(self):
        # Le taux du projet change APRES l'emission de la facture : le PDF
        # reexporte doit refleter les montants d'origine (lignes figees en
        # base), jamais le nouveau taux.
        self.db.update_project(self.project_id, hourly_rate=90.0)
        output_path = self.tmp / "reexport.pdf"

        inv.reexport_invoice_pdf(self.db, self.invoice_id, output_path, "Mon Entreprise", "SIRET 123")

        self.assertTrue(output_path.exists())
        self.assertTrue(output_path.read_bytes().startswith(b"%PDF"))
        stored = self.db.get_invoice_line_items(self.invoice_id)
        items = [inv.LineItem(row["project_name"], row["hours"], row["rate"]) for row in stored]
        invoice = self.db.get_invoice(self.invoice_id)
        subtotal, tax_amount, total = inv.compute_totals(items, invoice["tax_rate"])
        self.assertEqual(subtotal, 100.0)  # 2 h x 50, jamais 2 h x 90
        self.assertEqual(total, 120.0)

    def test_reexport_writes_nothing_to_the_database(self):
        # Reexporter ne doit ni consommer un numero de facture, ni creer de
        # ligne, ni toucher aux entrees de temps.
        invoices_before = [dict(row) for row in self.db.list_invoices()]
        entries_before = [dict(row) for row in self.db.list_time_entries()]
        next_number_before = self.db.next_invoice_number(year=2026)

        inv.reexport_invoice_pdf(self.db, self.invoice_id, self.tmp / "r.pdf", "Mon Entreprise", "")

        self.assertEqual([dict(row) for row in self.db.list_invoices()], invoices_before)
        self.assertEqual([dict(row) for row in self.db.list_time_entries()], entries_before)
        self.assertEqual(self.db.next_invoice_number(year=2026), next_number_before)

    def test_reexport_unknown_invoice_raises_value_error(self):
        with self.assertRaises(ValueError):
            inv.reexport_invoice_pdf(self.db, 999, self.tmp / "absent.pdf", "Mon Entreprise", "")


if __name__ == "__main__":
    unittest.main()
