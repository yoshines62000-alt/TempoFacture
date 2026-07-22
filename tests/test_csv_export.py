"""Tests pour csv_export.py : export CSV des entrees de temps et des
factures, sur une vraie base SQLite temporaire."""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database
from invoice import build_line_items
import csv_export


class CsvExportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "test.sqlite")
        self.addCleanup(self.db.close)
        self.client_id = self.db.add_client("Client Test", hourly_rate=50.0)
        self.project_id = self.db.add_project(self.client_id, "Projet Test")

    def _read_csv(self, path):
        with open(path, encoding="utf-8-sig") as f:
            return list(csv.reader(f))

    def test_export_time_entries_csv_includes_computed_hours(self):
        self.db.add_manual_time_entry(
            self.project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:30:00+00:00", "Travail"
        )
        entries = self.db.list_time_entries()
        output = self.tmp / "entries.csv"
        csv_export.export_time_entries_csv(entries, output)

        rows = self._read_csv(output)
        self.assertEqual(rows[0], ["ID", "Projet", "Debut", "Fin", "Heures", "Description", "Facture"])
        self.assertEqual(rows[1][1], "Projet Test")
        self.assertEqual(rows[1][4], "2.50")
        self.assertEqual(rows[1][5], "Travail")

    def test_export_time_entries_csv_running_entry_has_no_hours(self):
        self.db.start_time_entry(self.project_id, "En cours")
        entries = self.db.list_time_entries()
        output = self.tmp / "entries.csv"
        csv_export.export_time_entries_csv(entries, output)
        rows = self._read_csv(output)
        self.assertEqual(rows[1][3], "")  # fin
        self.assertEqual(rows[1][4], "")  # heures

    def test_export_invoices_csv_uses_frozen_line_item_totals(self):
        entry_id = self.db.add_manual_time_entry(
            self.project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00"
        )
        entries = self.db.list_time_entries(uninvoiced_only=True)
        line_items = build_line_items(entries, self.db)
        invoice_id = self.db.create_invoice(self.client_id, [entry_id], tax_rate=20.0, line_items=line_items)

        # Le taux change APRES la facturation : le CSV doit refleter le
        # montant fige a la creation, pas le nouveau taux.
        self.db.update_client(self.client_id, hourly_rate=999.0)

        invoices = self.db.list_invoices()
        output = self.tmp / "invoices.csv"
        csv_export.export_invoices_csv(invoices, self.db, output)

        rows = self._read_csv(output)
        self.assertEqual(rows[0], ["Numero", "Client", "Date d'emission", "Echeance", "TVA (%)", "Total", "Devise", "Statut"])
        self.assertEqual(rows[1][1], "Client Test")
        self.assertEqual(rows[1][5], "120.00")  # 2h * 50 = 100, + 20% TVA = 120
        self.assertEqual(rows[1][6], "EUR")
        self.assertEqual(rows[1][7], "unpaid")

    def test_export_invoices_csv_with_no_invoices_writes_header_only(self):
        output = self.tmp / "invoices.csv"
        csv_export.export_invoices_csv([], self.db, output)
        rows = self._read_csv(output)
        self.assertEqual(len(rows), 1)


class CsvSafeTestCase(unittest.TestCase):
    def test_prefixes_formula_characters_with_apostrophe(self):
        for dangerous in ["=SUM(A1:A9)", "+1+1", "-1+1", "@SUM(1)"]:
            self.assertEqual(csv_export._csv_safe(dangerous), "'" + dangerous)

    def test_leaves_ordinary_text_untouched(self):
        self.assertEqual(csv_export._csv_safe("Client Normal"), "Client Normal")
        self.assertEqual(csv_export._csv_safe(""), "")

    def test_leaves_non_string_values_untouched(self):
        self.assertEqual(csv_export._csv_safe(42), 42)
        self.assertIsNone(csv_export._csv_safe(None))


class CsvExportFormulaInjectionTestCase(unittest.TestCase):
    """Verrouille le correctif CWE-1236 : une colonne texte libre exportee en
    CSV commencant par =, +, - ou @ (nom de projet/description/client
    importe depuis une source non fiable) ne doit jamais etre interpretable
    comme une formule par Excel/LibreOffice a l'ouverture du fichier."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "test.sqlite")
        self.addCleanup(self.db.close)

    def _read_csv(self, path):
        with open(path, encoding="utf-8-sig") as f:
            return list(csv.reader(f))

    def test_time_entries_csv_escapes_malicious_project_and_description(self):
        client_id = self.db.add_client("Client Test", hourly_rate=50.0)
        project_id = self.db.add_project(client_id, "=cmd|'/c calc'!A1")
        self.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00",
            "+HYPERLINK(\"http://evil\")",
        )
        entries = self.db.list_time_entries()
        output = self.tmp / "entries.csv"
        csv_export.export_time_entries_csv(entries, output)

        rows = self._read_csv(output)
        self.assertTrue(rows[1][1].startswith("'="))
        self.assertTrue(rows[1][5].startswith("'+"))

    def test_invoices_csv_escapes_malicious_client_name_and_currency(self):
        client_id = self.db.add_client("=cmd|'/c calc'!A1", hourly_rate=50.0)
        project_id = self.db.add_project(client_id, "Projet Test")
        entry_id = self.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00"
        )
        entries = self.db.list_time_entries(uninvoiced_only=True)
        line_items = build_line_items(entries, self.db)
        self.db.create_invoice(
            client_id, [entry_id], tax_rate=0.0, line_items=line_items, currency="-1+2"
        )
        invoices = self.db.list_invoices()
        output = self.tmp / "invoices.csv"
        csv_export.export_invoices_csv(invoices, self.db, output)

        rows = self._read_csv(output)
        self.assertTrue(rows[1][1].startswith("'="))
        self.assertTrue(rows[1][6].startswith("'-"))


class AtomicCsvWriteTestCase(unittest.TestCase):
    """Verrouille le correctif C8 : un echec en cours d'ecriture ne doit
    jamais laisser de fichier CSV tronque au chemin de destination final -
    voir _atomic_csv_write (ecriture dans un fichier temporaire puis
    os.replace())."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "test.sqlite")
        self.addCleanup(self.db.close)
        self.client_id = self.db.add_client("Client Test", hourly_rate=50.0)
        self.project_id = self.db.add_project(self.client_id, "Projet Test")
        self.db.add_manual_time_entry(
            self.project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00", "Travail"
        )

    def test_no_partial_file_is_left_when_writing_fails_partway_through(self):
        entries = list(self.db.list_time_entries()) * 5  # plusieurs lignes, pour ecrire "en cours de route"
        output = self.tmp / "entries.csv"

        class _FlakyFile:
            """Enveloppe le vrai fichier temporaire ouvert par
            _atomic_csv_write : laisse passer les premieres ecritures (donc
            un fichier temporaire partiel existe bel et bien sur le disque
            a ce stade), puis simule une panne (disque plein...) en cours
            de route, comme le ferait un vrai OSError d'ecriture."""

            def __init__(self, real_file):
                self._real_file = real_file
                self._write_count = 0

            def write(self, data):
                self._write_count += 1
                if self._write_count == 3:
                    raise OSError("disque plein (simule)")
                return self._real_file.write(data)

            def __getattr__(self, name):
                return getattr(self._real_file, name)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                self._real_file.close()
                return False

        original_fdopen = csv_export.os.fdopen

        def patched_fdopen(fd, *args, **kwargs):
            return _FlakyFile(original_fdopen(fd, *args, **kwargs))

        from unittest import mock

        with mock.patch("csv_export.os.fdopen", side_effect=patched_fdopen):
            with self.assertRaises(OSError):
                csv_export.export_time_entries_csv(entries, output)

        # Aucun fichier - ni tronque au chemin final, ni fichier temporaire
        # orphelin - ne doit subsister.
        self.assertFalse(output.exists())
        leftover = list(self.tmp.glob(f".{output.name}.*"))
        self.assertEqual(leftover, [])

    def test_export_does_not_overwrite_the_destination_until_fully_written(self):
        from unittest import mock

        output = self.tmp / "entries.csv"
        output.write_text("ancien contenu complet\n", encoding="utf-8")

        with mock.patch("csv_export.os.replace", side_effect=OSError("permission refusee (simule)")):
            with self.assertRaises(OSError):
                csv_export.export_time_entries_csv(list(self.db.list_time_entries()), output)

        # Le fichier de destination existant n'a jamais ete touche : os.replace()
        # est le tout dernier appel, une fois l'ecriture complete deja
        # reussie dans le fichier temporaire.
        self.assertEqual(output.read_text(encoding="utf-8"), "ancien contenu complet\n")
        leftover = list(self.tmp.glob(f".{output.name}.*"))
        self.assertEqual(leftover, [])

    def test_successful_export_leaves_no_temporary_file_behind(self):
        output = self.tmp / "entries.csv"
        csv_export.export_time_entries_csv(list(self.db.list_time_entries()), output)
        self.assertTrue(output.exists())
        leftover = list(self.tmp.glob(f".{output.name}.*"))
        self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
