"""Tests pour invoice.py : calcul des heures/montants (logique pure) et
generation reelle d'un PDF via fpdf2."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database
import invoice as inv

try:
    import fitz  # PyMuPDF : utilise uniquement pour extraire le texte REELLEMENT
    # rendu dans le PDF genere (voir NonLatin1ClientNameTestCase), pour verrouiller
    # que les caracteres non-Latin1 apparaissent lisiblement plutot que comme "?".
except ImportError:
    fitz = None


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

    def test_aggregated_hours_round_half_up_on_a_known_binary_float_tie(self):
        # Regression (audit Phase 4, A1) : build_line_items() appelait
        # round(hours, 2) (arrondi natif Python, round-half-to-even sur la
        # representation BINAIRE) sur la somme des heures d'un projet, au
        # lieu du meme arrondi Decimal/ROUND_HALF_UP deja applique aux
        # montants (voir LineItemAmountRoundingTestCase ci-dessus). Deux
        # entrees reelles totalisant exactement 2h40m30s = 2.675h
        # illustrent le piege : round(2.675, 2) natif donne 2.67 (car
        # 2.675 est en realite stocke en binaire comme
        # 2.67499999999999982...), alors que l'arrondi commercial correct
        # est 2.68 - un ecart silencieux de 1 EUR sur cette seule ligne a
        # 100/h, jamais visible a l'ecran (apercu, PDF, CSV restent tous
        # coherents entre eux car tous derives de LineItem.hours deja
        # fige).
        client_id = self.db.add_client("Client Arrondi", hourly_rate=100.0)
        project_id = self.db.add_project(client_id, "Projet")
        self.db.add_manual_time_entry(project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:30:15+00:00")  # 1h30m15s
        self.db.add_manual_time_entry(project_id, "2026-01-01T11:00:00+00:00", "2026-01-01T12:10:15+00:00")  # 1h10m15s
        entries = self.db.list_time_entries(project_id=project_id)

        # Sanity check : la somme brute des heures reproduit bien le cas
        # limite binaire vise par ce test, avant tout arrondi.
        raw_total = sum(
            inv.compute_duration_hours(e["start_time"], e["end_time"]) for e in entries
        )
        self.assertEqual(str(raw_total), "2.675")
        self.assertEqual(round(raw_total, 2), 2.67)  # comportement natif (faux, ce que ce test verrouille comme corrige)

        line_items = inv.build_line_items(entries, self.db)
        self.assertEqual(len(line_items), 1)
        self.assertEqual(line_items[0].hours, 2.68)  # arrondi commercial correct, pas 2.67
        self.assertEqual(line_items[0].amount, 268.0)  # pas 267.0


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


class FormatAmountTestCase(unittest.TestCase):
    """Regression (audit Phase 6, B7) : format_amount() convertissait deja
    la virgule des milliers anglo-saxonne en espace, mais laissait le
    point decimal tel quel - un melange incoherent des deux conventions a
    la fois (ex : "12 345.67 EUR"), alors que l'integralite de
    l'interface, le vocabulaire (TVA, SIRET) et le public cible (voir
    README) sont francophones. La convention francaise attendue est une
    espace insecable pour les milliers et une virgule pour la decimale."""

    def test_uses_comma_as_the_decimal_separator(self):
        self.assertEqual(inv.format_amount(40.0), "40,00 EUR")
        self.assertEqual(inv.format_amount(3.13), "3,13 EUR")

    def test_uses_a_non_breaking_space_as_the_thousands_separator(self):
        # Espace INSECABLE ( ), pas une espace normale : evite qu'un
        # total ne se retrouve coupe en deux lignes au milieu d'un nombre
        # par un retour a la ligne automatique.
        self.assertEqual(inv.format_amount(1234.5), "1 234,50 EUR")
        self.assertEqual(inv.format_amount(1_234_567.89), "1 234 567,89 EUR")

    def test_a_currency_code_containing_a_period_is_left_untouched(self):
        # La devise est un champ 100% libre (voir Parametres/onglet
        # Factures) qui peut lui-meme contenir un point (ex. "U.S.
        # DOLLARS") : la conversion point -> virgule ne doit porter QUE sur
        # la partie numerique, jamais sur le code devise accole.
        self.assertEqual(inv.format_amount(10.0, "U.S. DOLLARS"), "10,00 U.S. DOLLARS")

    def test_default_currency_is_still_eur(self):
        self.assertEqual(inv.format_amount(0.0), "0,00 EUR")


class FormatDateFrTestCase(unittest.TestCase):
    """Regression (audit Phase 6, B6) : les dates imprimees sur la facture
    ("Date d'emission : 2026-07-22") utilisaient le format ISO 8601 brut
    (AAAA-MM-JJ, ideal pour le stockage - voir db.py) au lieu du format
    usuel francais JJ/MM/AAAA, inhabituel sur un document remis a un
    client francais."""

    def test_converts_iso_date_to_french_dd_mm_yyyy(self):
        self.assertEqual(inv._format_date_fr("2026-07-22"), "22/07/2026")
        self.assertEqual(inv._format_date_fr("2026-01-05"), "05/01/2026")

    def test_a_datetime_with_time_component_is_truncated_to_the_date(self):
        self.assertEqual(inv._format_date_fr("2026-07-22T00:00:00+00:00"), "22/07/2026")

    def test_falls_back_to_the_original_string_when_unparsable(self):
        # Filet de securite : une date corrompue/mal importee ne doit
        # jamais faire planter toute la generation de la facture pour un
        # simple probleme d'affichage.
        self.assertEqual(inv._format_date_fr("pas-une-date"), "pas-une-date")
        self.assertEqual(inv._format_date_fr(""), "")


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

    def test_generates_pdf_with_extreme_totals_and_long_currency_without_truncating_the_amount(self):
        # Regression (audit Phase 4, B1) : le correctif precedent (voir le
        # test ci-dessus) ne couvrait que les 4 cellules de la ligne
        # d'article, jamais le bloc Sous-total/TVA/Total. Avec un montant
        # tres eleve (faute de frappe plausible sur le taux horaire, aucune
        # borne superieure n'existe) combine a une devise longue, la
        # colonne Montant du bloc de totaux (30mm) tronquait les CHIFFRES
        # du total lui-meme, pas seulement le libelle de devise - rendant
        # le montant a payer illisible sur le document remis au client
        # (ex : "1 481 481 88..." au lieu du total complet). Ce test
        # verrouille que chacune des 3 lignes du bloc de totaux affiche
        # desormais son montant integral, sans troncature ni depassement
        # de sa cellule.
        from fpdf import FPDF
        from unittest import mock

        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_totaux_extremes.pdf"
        items = [inv.LineItem("Projet A", hours=12345.67, rate=99999999.99)]
        subtotal, tax_amount, total = inv.compute_totals(items, tax_rate=20.0)

        recorded = []
        original_cell = FPDF.cell

        def spying_cell(self, w=0, h=0, text="", *args, **kwargs):
            recorded.append((w, text, self.get_string_width(text)))
            return original_cell(self, w, h, text, *args, **kwargs)

        with mock.patch.object(FPDF, "cell", spying_cell):
            inv.generate_invoice_pdf(
                output_path=output_path,
                invoice_number="2026-0006",
                issue_date="2026-01-15",
                due_date=None,
                company_name="Ma societe",
                company_info="",
                client_name="Client",
                client_address="",
                line_items=items,
                tax_rate=20.0,
                currency="DOLLARS AMERICAINS",
            )

        self.assertTrue(output_path.exists())
        self.assertTrue(output_path.read_bytes().startswith(b"%PDF"))

        expected_subtotal_text = inv.format_amount(subtotal, "DOLLARS AMERICAINS")
        expected_tax_label = "TVA (20 %)"
        expected_tax_text = inv.format_amount(tax_amount, "DOLLARS AMERICAINS")
        expected_total_text = inv.format_amount(total, "DOLLARS AMERICAINS")

        def _amount_cell_following(label_text):
            idx = next(i for i, (_, text, _) in enumerate(recorded) if text == label_text)
            return recorded[idx + 1]  # (largeur, texte dessine, largeur du texte) de la cellule montant

        for label_text, expected_amount_text in [
            ("Sous-total", expected_subtotal_text),
            (expected_tax_label, expected_tax_text),
            ("Total", expected_total_text),
        ]:
            width, drawn_text, drawn_width = _amount_cell_following(label_text)
            self.assertEqual(
                drawn_text, expected_amount_text,
                f"le montant de la ligne '{label_text}' a ete tronque : '{drawn_text}' "
                f"au lieu de '{expected_amount_text}'",
            )
            self.assertFalse(drawn_text.endswith("..."))
            self.assertLessEqual(
                drawn_width, width,
                f"le texte '{drawn_text}' (largeur {drawn_width}mm) deborde de sa cellule ({width}mm)",
            )

    def test_prints_dates_in_french_dd_mm_yyyy_format_not_raw_iso(self):
        # Regression (audit Phase 6, B6) : verrouille le texte REELLEMENT
        # dessine dans le PDF (pas seulement _format_date_fr() en
        # isolation, voir FormatDateFrTestCase ci-dessus) : "Date
        # d'emission : 22/07/2026" / "Echeance : 21/08/2026", jamais le
        # format ISO brut ("2026-07-22").
        from fpdf import FPDF
        from unittest import mock

        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_dates_fr.pdf"
        items = [inv.LineItem("Projet A", hours=1.0, rate=50.0)]

        recorded_texts = []
        original_cell = FPDF.cell

        def spying_cell(self, w=0, h=0, text="", *args, **kwargs):
            recorded_texts.append(text)
            return original_cell(self, w, h, text, *args, **kwargs)

        with mock.patch.object(FPDF, "cell", spying_cell):
            inv.generate_invoice_pdf(
                output_path=output_path,
                invoice_number="2026-0007",
                issue_date="2026-07-22",
                due_date="2026-08-21",
                company_name="Ma societe",
                company_info="",
                client_name="Client",
                client_address="",
                line_items=items,
                tax_rate=0.0,
            )

        self.assertIn("Date d'emission : 22/07/2026", recorded_texts)
        self.assertIn("Echeance : 21/08/2026", recorded_texts)
        self.assertNotIn("Date d'emission : 2026-07-22", recorded_texts)
        self.assertNotIn("Echeance : 2026-08-21", recorded_texts)

    def test_prints_amounts_with_french_comma_decimal_separator(self):
        # Regression (audit Phase 6, B7) : verrouille le texte REELLEMENT
        # dessine dans le PDF pour les montants (lignes d'articles ET bloc
        # de totaux) : virgule decimale et espace insecable pour les
        # milliers, jamais le point decimal anglo-saxon d'origine.
        from fpdf import FPDF
        from unittest import mock

        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_montants_fr.pdf"
        items = [inv.LineItem("Projet A", hours=20.0, rate=100.0)]  # sous-total 2000

        recorded_texts = []
        original_cell = FPDF.cell

        def spying_cell(self, w=0, h=0, text="", *args, **kwargs):
            recorded_texts.append(text)
            return original_cell(self, w, h, text, *args, **kwargs)

        with mock.patch.object(FPDF, "cell", spying_cell):
            inv.generate_invoice_pdf(
                output_path=output_path,
                invoice_number="2026-0008",
                issue_date="2026-01-15",
                due_date=None,
                company_name="Ma societe",
                company_info="",
                client_name="Client",
                client_address="",
                line_items=items,
                tax_rate=20.0,
            )

        self.assertIn("2 000,00 EUR", recorded_texts)  # sous-total
        self.assertIn("2 400,00 EUR", recorded_texts)  # total (20 x 100 x 1,2)
        self.assertFalse(
            any("2,000.00" in text or "2000.00" in text for text in recorded_texts),
            "un montant a ete imprime avec le point decimal anglo-saxon au lieu de la virgule",
        )

    def test_repeats_the_table_header_on_every_page_of_a_multi_page_invoice(self):
        # Regression (audit Phase 6, B4) : l'en-tete du tableau
        # (Description/Heures/Taux horaire/Montant, fond grise) n'etait
        # dessine qu'une seule fois avant la boucle d'ecriture des lignes -
        # reproduction de l'audit avec 60 lignes (2 pages) : la page 2
        # commencait directement par "Tache recurrente #29" sans aucun
        # rappel des colonnes. On intercepte les appels reels a
        # FPDF.cell() pour verifier que la cellule d'en-tete "Description"
        # (fill=True, largeur de la 1ere colonne) est bien redessinee sur
        # CHACUNE des pages produites, pas seulement la premiere.
        from fpdf import FPDF
        from unittest import mock

        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture_multi_pages.pdf"
        items = [inv.LineItem(f"Tache recurrente #{i}", hours=1.0, rate=50.0) for i in range(60)]

        recorded = []
        original_cell = FPDF.cell

        def spying_cell(self, w=0, h=0, text="", *args, **kwargs):
            recorded.append((w, text, bool(kwargs.get("fill")), self.page_no()))
            return original_cell(self, w, h, text, *args, **kwargs)

        with mock.patch.object(FPDF, "cell", spying_cell):
            inv.generate_invoice_pdf(
                output_path=output_path,
                invoice_number="2026-0009",
                issue_date="2026-01-15",
                due_date=None,
                company_name="Ma societe",
                company_info="",
                client_name="Client",
                client_address="",
                line_items=items,
                tax_rate=20.0,
            )

        self.assertTrue(output_path.exists())
        header_pages = {
            page for (w, text, fill, page) in recorded
            if text == "Description" and fill and w == 90
        }
        self.assertGreaterEqual(
            len(header_pages), 2,
            "le scenario doit produire au moins 2 pages pour etre pertinent au correctif",
        )
        self.assertIn(1, header_pages, "en-tete absent de la page 1")
        self.assertIn(2, header_pages, "en-tete absent de la page 2 (bug d'origine, voir B4)")

        # Non-regression : les lignes elles-memes restent bien reparties
        # sur les 2 pages (pas de doublon/perte de ligne cause par le
        # saut de page manuel).
        row_labels = {text for (w, text, fill, _page) in recorded if w == 90 and not fill}
        for i in range(60):
            self.assertIn(f"Tache recurrente #{i}", row_labels)


@unittest.skipUnless(fitz is not None, "PyMuPDF (pymupdf) n'est pas installe")
class NonLatin1ClientNameTestCase(unittest.TestCase):
    """Regression (audit Phase 5, B2) : un nom de client redige dans une
    ecriture non latine (chinois, cyrillique...) etait entierement
    remplace par une suite de '?' sur la facture PDF - la police de base
    "Helvetica" de fpdf2 ne supporte que Latin-1 (voir l'ancien
    _latin1_safe()). generate_invoice_pdf() embarque desormais une police
    Unicode (Noto Sans SC, voir fonts/NotoSansSC-Regular.otf et
    _register_unicode_font) pour tous les champs texte. Ces tests
    extraient le texte REELLEMENT rendu dans le PDF (via PyMuPDF), pas
    seulement l'absence de plantage (deja verrouille par
    test_generates_pdf_with_non_latin1_characters_without_crashing
    ci-dessus), pour verrouiller que le nom apparait bien lisiblement."""

    @staticmethod
    def _generate_and_extract_text(client_name: str) -> str:
        tmp = Path(tempfile.mkdtemp())
        output_path = tmp / "facture.pdf"
        items = [inv.LineItem("Projet A", hours=2.0, rate=50.0)]
        inv.generate_invoice_pdf(
            output_path=output_path,
            invoice_number="2026-0010",
            issue_date="2026-01-15",
            due_date=None,
            company_name="Mon Entreprise",
            company_info="",
            client_name=client_name,
            client_address="",
            line_items=items,
            tax_rate=0.0,
        )
        doc = fitz.open(str(output_path))
        try:
            return "".join(page.get_text() for page in doc)
        finally:
            doc.close()

    def test_cjk_client_name_renders_legibly_not_as_question_marks(self):
        client_name = "北京科技有限公司"  # exemple utilise par l'audit
        text = self._generate_and_extract_text(client_name)
        self.assertIn(client_name, text)
        self.assertNotIn("?" * len(client_name), text)

    def test_cyrillic_client_name_renders_legibly_not_as_question_marks(self):
        client_name = "ООО Ромашка"
        text = self._generate_and_extract_text(client_name)
        self.assertIn(client_name, text)
        self.assertNotIn("???", text)

    def test_falls_back_to_latin1_safe_when_the_font_file_is_missing(self):
        # Filet de securite : si la ressource de police venait a manquer
        # (ex. probleme d'empaquetage de l'executable), la generation ne
        # doit jamais planter - elle se degrade vers l'ancien comportement
        # Latin-1 (tout caractere hors Latin-1 remplace par '?'), plutot
        # que d'empecher la facture d'etre emise.
        from unittest import mock

        with mock.patch.object(inv, "_resource_path", return_value=Path("chemin/inexistant.otf")):
            text = self._generate_and_extract_text("北京科技有限公司")
        self.assertNotIn("北京", text)
        self.assertIn("?" * 8, text)

    def test_a_character_absent_even_from_the_unicode_font_degrades_to_question_mark(self):
        # Un emoji n'est couvert ni par Latin-1 ni par la police Unicode
        # embarquee (Noto Sans SC ne vise pas les emoji) : doit degrader
        # proprement en '?' (comme avant, voir _glyph_safe) plutot que de
        # planter ou de rendre un glyphe "manquant" invisible et intracable.
        text = self._generate_and_extract_text("Client \U0001F680")
        self.assertIn("Client ?", text)


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
