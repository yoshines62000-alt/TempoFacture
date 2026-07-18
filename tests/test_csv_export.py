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


if __name__ == "__main__":
    unittest.main()
