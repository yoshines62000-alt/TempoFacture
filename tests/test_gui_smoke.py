"""Smoke tests de bout en bout pour gui.py : pilotent la VRAIE interface
Tkinter (vraie fenetre Tk, vrais widgets, vraie base SQLite temporaire) -
seuls tkinter.messagebox, tkinter.filedialog et tkinter.simpledialog sont
mockes, comme dans le reste de la suite, pour ne jamais faire dependre un
test automatise d'un clic humain sur une boite de dialogue modale."""

import sys
import tempfile
import tkinter
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui
from gui import TempoFactureApp


class GuiSmokeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Redirige la base de donnees vers un dossier temporaire : la vraie
        # fenetre Tkinter et la vraie couche SQLite tournent normalement,
        # mais sans jamais toucher au vrai dossier %APPDATA%\TempoFacture de
        # la machine qui execute les tests.
        self._data_dir_patcher = patch.object(gui, "_data_dir", return_value=self.tmp)
        self._data_dir_patcher.start()
        self.addCleanup(self._data_dir_patcher.stop)

        try:
            self.root = tkinter.Tk()
        except tkinter.TclError as exc:
            self.skipTest(f"Pas d'affichage disponible pour piloter la vraie GUI Tkinter : {exc}")
            return
        self.root.withdraw()
        self.app = TempoFactureApp(self.root)
        self.addCleanup(self._teardown_app)

    def _teardown_app(self):
        self.app.db.close()
        self.root.destroy()

    def _select_client_row(self, client_id: int):
        self.app.clients_tree.selection_set(str(client_id))

    # -- item 1 : avertissement avant d'archiver un client avec des heures --
    # -- non facturees --------------------------------------------------------

    def test_archiving_client_without_uninvoiced_hours_needs_no_confirmation(self):
        client_id = self.app.db.add_client("Client sans heures")
        self.app._refresh_clients()
        self._select_client_row(client_id)

        with patch("tkinter.messagebox.askyesno") as askyesno:
            self.app._toggle_client_archived()

        askyesno.assert_not_called()
        self.assertEqual(self.app.db.get_client(client_id)["archived"], 1)

    def test_archiving_client_with_uninvoiced_hours_prompts_for_confirmation(self):
        client_id = self.app.db.add_client("Client avec heures")
        project_id = self.app.db.add_project(client_id, "Projet")
        self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        self.app._refresh_clients()
        self._select_client_row(client_id)

        with patch("tkinter.messagebox.askyesno", return_value=True) as askyesno:
            self.app._toggle_client_archived()

        askyesno.assert_called_once()
        self.assertEqual(self.app.db.get_client(client_id)["archived"], 1)

    def test_cancelling_the_confirmation_leaves_the_client_active(self):
        client_id = self.app.db.add_client("Client avec heures")
        project_id = self.app.db.add_project(client_id, "Projet")
        self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        self.app._refresh_clients()
        self._select_client_row(client_id)

        with patch("tkinter.messagebox.askyesno", return_value=False):
            self.app._toggle_client_archived()

        self.assertEqual(self.app.db.get_client(client_id)["archived"], 0)

    def test_unarchiving_never_prompts_even_with_uninvoiced_hours(self):
        # Le desarchivage ne fait jamais disparaitre d'heures : seul
        # l'archivage doit etre soumis a confirmation.
        client_id = self.app.db.add_client("Client")
        project_id = self.app.db.add_project(client_id, "Projet")
        self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        self.app.db.update_client(client_id, archived=1)
        self.app._refresh_clients()
        self._select_client_row(client_id)

        with patch("tkinter.messagebox.askyesno") as askyesno:
            self.app._toggle_client_archived()

        askyesno.assert_not_called()
        self.assertEqual(self.app.db.get_client(client_id)["archived"], 0)

    # -- item 4 : seuil d'inactivite configurable -----------------------------

    def test_saving_settings_persists_custom_idle_threshold(self):
        self.app.setting_idle_threshold_var.set("10")
        with patch("tkinter.messagebox.showinfo"):
            self.app._save_settings()

        self.assertEqual(self.app.db.get_setting("idle_threshold_minutes"), "10")
        self.assertEqual(self.app._idle_threshold_seconds(), 600)

    def test_idle_threshold_defaults_to_the_hardcoded_constant(self):
        self.assertEqual(
            self.app._idle_threshold_seconds(), gui.IDLE_THRESHOLD_SECONDS
        )

    def test_saving_settings_rejects_idle_threshold_below_one_minute(self):
        self.app.setting_idle_threshold_var.set("0")
        with patch("tkinter.messagebox.showwarning") as showwarning:
            self.app._save_settings()

        showwarning.assert_called_once()
        # La valeur invalide n'a pas ete persistee.
        self.assertEqual(self.app.db.get_setting("idle_threshold_minutes"), "")

    def test_saving_settings_rejects_non_numeric_idle_threshold(self):
        self.app.setting_idle_threshold_var.set("abc")
        with patch("tkinter.messagebox.showwarning") as showwarning:
            self.app._save_settings()

        showwarning.assert_called_once()

    # -- item 5 : recherche/filtre sur les listes -----------------------------

    def test_client_search_filters_the_treeview_in_real_time(self):
        self.app.db.add_client("Acme Corp")
        self.app.db.add_client("Beta SARL")
        self.app._refresh_clients()
        self.assertEqual(len(self.app.clients_tree.get_children()), 2)

        self.app.client_search_var.set("acme")
        self.assertEqual(len(self.app.clients_tree.get_children()), 1)
        remaining_id = self.app.clients_tree.get_children()[0]
        self.assertEqual(self.app.clients_tree.item(remaining_id, "values")[1], "Acme Corp")

        self.app.client_search_var.set("")
        self.assertEqual(len(self.app.clients_tree.get_children()), 2)

    def test_project_search_filters_by_project_or_client_name(self):
        client_id = self.app.db.add_client("Client Recherche")
        self.app.db.add_project(client_id, "Site vitrine")
        self.app.db.add_project(client_id, "Application mobile")
        self.app._refresh_projects()
        self.assertEqual(len(self.app.projects_tree.get_children()), 2)

        self.app.project_search_var.set("vitrine")
        self.assertEqual(len(self.app.projects_tree.get_children()), 1)

        self.app.project_search_var.set("client recherche")
        self.assertEqual(len(self.app.projects_tree.get_children()), 2)

        self.app.project_search_var.set("introuvable")
        self.assertEqual(len(self.app.projects_tree.get_children()), 0)

    def test_invoice_search_filters_by_number_client_or_status(self):
        client_id = self.app.db.add_client("Client Facture")
        project_id = self.app.db.add_project(client_id, "Projet")
        entry_id = self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        invoice_id = self.app.db.create_invoice(client_id, [entry_id])
        self.app._refresh_invoices()
        self.assertEqual(len(self.app.invoices_tree.get_children()), 1)

        number = self.app.db.get_invoice(invoice_id)["invoice_number"]
        self.app.invoice_search_var.set(number)
        self.assertEqual(len(self.app.invoices_tree.get_children()), 1)

        self.app.invoice_search_var.set("client facture")
        self.assertEqual(len(self.app.invoices_tree.get_children()), 1)

        self.app.invoice_search_var.set("introuvable")
        self.assertEqual(len(self.app.invoices_tree.get_children()), 0)


if __name__ == "__main__":
    unittest.main()
