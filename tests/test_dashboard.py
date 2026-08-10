"""Tests cibles de l'onglet Tableau de bord (vue d'accueil de synthese).

Meme approche que test_gui_smoke.py : vraie fenetre Tkinter, vraie base
SQLite temporaire, seul gui._data_dir est redirige vers un dossier jetable.
"""

import sys
import tempfile
import tkinter
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui
from gui import TempoFactureApp


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._data_dir_patcher = patch.object(gui, "_data_dir", return_value=self.tmp)
        self._data_dir_patcher.start()
        self.addCleanup(self._data_dir_patcher.stop)

        try:
            self.root = tkinter.Tk()
        except tkinter.TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour piloter la GUI Tkinter : {exc}")
            return
        self.root.withdraw()
        self.app = TempoFactureApp(self.root)
        self.addCleanup(self._teardown_app)

    def _teardown_app(self):
        self.app.db.close()
        self.root.destroy()

    # -- structure -----------------------------------------------------------

    def test_dashboard_is_the_first_tab(self):
        self.assertEqual(self.app.notebook.tabs()[0], str(self.app.dashboard_tab))
        self.assertEqual(self.app.notebook.tab(0, "text"), "Tableau de bord")

    def test_empty_database_shows_zeroed_and_up_to_date_state(self):
        self.app._refresh_dashboard()
        self.assertEqual(self.app.dash_week_var.get(), "0,00 h")
        self.assertEqual(self.app.dash_month_var.get(), "0,00 h")
        self.assertEqual(self.app.dash_uninvoiced_sub_var.get(), "Tout est facture")
        self.assertEqual(self.app.dash_overdue_var.get(), "A jour")
        # Etat vide : le tableau des clients non factures est vide et le
        # message d'etat vide est affiche.
        self.assertEqual(len(self.app.dash_top_tree.get_children()), 0)
        self.assertNotEqual(self.app.dash_top_empty_var.get(), "")

    # -- heures semaine / mois -----------------------------------------------

    def test_week_and_month_hours_only_count_recent_completed_entries(self):
        client_id = self.app.db.add_client("Client", hourly_rate=50.0)
        project_id = self.app.db.add_project(client_id, "Projet")
        now = datetime.now(timezone.utc)
        # 1h suivie il y a une heure -> compte dans la semaine ET le mois.
        self.app.db.add_manual_time_entry(project_id, _iso(now - timedelta(hours=1)), _iso(now))
        # 2h l'an dernier -> ne compte ni dans la semaine ni dans le mois.
        old = now - timedelta(days=400)
        self.app.db.add_manual_time_entry(project_id, _iso(old - timedelta(hours=2)), _iso(old))

        self.app._refresh_dashboard()
        self.assertEqual(self.app.dash_week_var.get(), "1,00 h")
        self.assertEqual(self.app.dash_month_var.get(), "1,00 h")

    # -- montant non facture + top clients -----------------------------------

    def test_uninvoiced_total_and_top_clients_breakdown(self):
        now = datetime.now(timezone.utc)
        # Client A : 2h a 50/h = 100. Client B : 1h a 40/h = 40.
        a = self.app.db.add_client("Client A", hourly_rate=50.0)
        pa = self.app.db.add_project(a, "Projet A")
        self.app.db.add_manual_time_entry(pa, _iso(now - timedelta(hours=2)), _iso(now))
        b = self.app.db.add_client("Client B", hourly_rate=40.0)
        pb = self.app.db.add_project(b, "Projet B")
        self.app.db.add_manual_time_entry(pb, _iso(now - timedelta(hours=1)), _iso(now))

        self.app._refresh_dashboard()

        self.assertEqual(self.app.dash_uninvoiced_var.get(), "140,00 EUR")
        self.assertEqual(self.app.dash_uninvoiced_sub_var.get(), "2 client(s) concerne(s)")
        # Trie par montant decroissant : Client A (100) avant Client B (40).
        children = self.app.dash_top_tree.get_children()
        self.assertEqual(len(children), 2)
        first = self.app.dash_top_tree.item(children[0], "values")
        self.assertEqual(first[0], "Client A")
        self.assertEqual(first[1], "100,00 EUR")

    def test_invoiced_hours_are_excluded_from_uninvoiced_total(self):
        now = datetime.now(timezone.utc)
        client_id = self.app.db.add_client("Client", hourly_rate=50.0)
        project_id = self.app.db.add_project(client_id, "Projet")
        entry_id = self.app.db.add_manual_time_entry(
            project_id, _iso(now - timedelta(hours=2)), _iso(now)
        )
        entries = self.app.db.list_time_entries(project_id=project_id)
        line_items = gui.build_line_items(entries, self.app.db)
        self.app.db.create_invoice(client_id, [entry_id], line_items=line_items)

        self.app._refresh_dashboard()
        self.assertEqual(self.app.dash_uninvoiced_var.get(), "0,00 EUR")
        self.assertEqual(self.app.dash_uninvoiced_sub_var.get(), "Tout est facture")

    # -- factures en retard --------------------------------------------------

    def test_overdue_invoice_shows_amount_in_danger_colour(self):
        now = datetime.now(timezone.utc)
        client_id = self.app.db.add_client("Client", hourly_rate=50.0)
        project_id = self.app.db.add_project(client_id, "Projet")
        entry_id = self.app.db.add_manual_time_entry(
            project_id, _iso(now - timedelta(hours=2)), _iso(now)
        )
        entries = self.app.db.list_time_entries(project_id=project_id)
        line_items = gui.build_line_items(entries, self.app.db)
        past_due = (now - timedelta(days=5)).date().isoformat()
        invoice_id = self.app.db.create_invoice(
            client_id, [entry_id], due_date=past_due, line_items=line_items
        )
        self.app.db.set_invoice_status(invoice_id, "unpaid")

        self.app._refresh_dashboard()

        self.assertEqual(self.app.dash_overdue_var.get(), "100,00 EUR")
        self.assertEqual(self.app.dash_overdue_sub_var.get(), "1 facture impayee")
        self.assertEqual(
            str(self.app.dash_overdue_value.cget("foreground")),
            gui.opl_theme.couleur("danger"),
        )

    # -- chronometre en cours ------------------------------------------------

    def test_running_timer_is_reflected_on_the_dashboard(self):
        client_id = self.app.db.add_client("Client Chrono")
        project_id = self.app.db.add_project(client_id, "Refonte site")
        self.app._refresh_timer_project_choices()
        self.app.timer_project_var.set(f"{project_id} - Refonte site")
        self.app._start_timer()

        self.app._refresh_dashboard()
        self.assertIn("Refonte site", self.app.dash_timer_var.get())
        self.assertIn("Chronometre en cours", self.app.dash_timer_var.get())

        self.app._stop_timer()
        self.app._refresh_dashboard()
        self.assertEqual(self.app.dash_timer_var.get(), "")


if __name__ == "__main__":
    unittest.main()
