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

    def _set_invoice_search(self, text: str):
        """Definit la recherche Factures et force le rafraichissement
        (normalement debounce via root.after) a s'executer immediatement,
        pour garder les tests deterministes sans vrai sleep."""
        self.app.invoice_search_var.set(text)
        if self.app._invoice_search_after_id is not None:
            self.app.root.after_cancel(self.app._invoice_search_after_id)
            self.app._invoice_search_after_id = None
        self.app._refresh_invoices()

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

        # La recherche est debouncee (voir INVOICE_SEARCH_DEBOUNCE_MS) : taper
        # ne rafraichit plus la liste de facon synchrone. On annule le
        # rafraichissement en attente et on le declenche nous-memes pour
        # garder ce test deterministe et rapide (pas de vrai sleep).
        number = self.app.db.get_invoice(invoice_id)["invoice_number"]
        self._set_invoice_search(number)
        self.assertEqual(len(self.app.invoices_tree.get_children()), 1)

        self._set_invoice_search("client facture")
        self.assertEqual(len(self.app.invoices_tree.get_children()), 1)

        self._set_invoice_search("introuvable")
        self.assertEqual(len(self.app.invoices_tree.get_children()), 0)

    # -- item audit Phase 1 : pas de facture fantome avec une devise --------
    # -- contenant un caractere hors Latin-1 ---------------------------------

    def test_generating_invoice_with_non_latin1_currency_creates_no_phantom_invoice(self):
        # Reproduit le scenario de l'audit : la devise en Parametres contient
        # "€" (aucune validation ne l'empeche cote GUI, seulement "non
        # vide"). Avant correctif, generate_invoice_pdf() plantait avec une
        # exception d'encodage qui n'etait pas un OSError, donc echappait au
        # bloc try/except OSError de _generate_invoice : la facture restait
        # en base, les heures restaient verrouillees ("facturees"), et aucun
        # PDF n'etait ecrit ni aucune erreur affichee. Avec le correctif
        # (currency passe par _latin1_safe dans invoice.py, et le
        # try/except elargi a Exception dans gui.py), la generation doit
        # soit reussir proprement (devise nettoyee), soit echouer en
        # annulant vraiment la facture et en deverrouillant les heures.
        self.app.db.set_setting("currency", "€")

        client_id = self.app.db.add_client("Client Devise")
        project_id = self.app.db.add_project(client_id, "Projet")
        entry_id = self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00"
        )

        self.app._refresh_time_entries()
        self.app.invoice_client_var.set(f"{client_id} - Client Devise")

        output_path = self.tmp / "facture_devise.pdf"
        with patch("tkinter.filedialog.asksaveasfilename", return_value=str(output_path)), \
             patch("tkinter.messagebox.showwarning") as mock_warn, \
             patch("tkinter.messagebox.showerror") as mock_error, \
             patch("tkinter.messagebox.showinfo") as mock_info, \
             patch("tkinter.messagebox.askyesno", return_value=False):
            self.app._generate_invoice()

        invoices = self.app.db.list_invoices()
        entry = self.app.db.list_time_entries(project_id=project_id)[0]

        if invoices:
            # Chemin "succes" : la devise a ete nettoyee, un vrai PDF a ete
            # ecrit, et l'heure est bien rattachee a la facture creee (pas
            # d'etat intermediaire).
            self.assertEqual(len(invoices), 1)
            self.assertTrue(output_path.exists())
            self.assertTrue(output_path.read_bytes().startswith(b"%PDF"))
            self.assertIsNotNone(entry["invoice_id"])
            mock_error.assert_not_called()
        else:
            # Chemin "echec propre" : aucune facture fantome, l'heure est
            # redevenue facturable, et l'utilisateur a ete prevenu.
            self.assertIsNone(entry["invoice_id"])
            self.assertFalse(output_path.exists())
            mock_error.assert_called_once()

    # -- item audit Phase 1 : la fenetre par defaut doit afficher tout le ---
    # -- contenu de l'onglet Factures sans redimensionnement manuel ---------

    def test_default_window_height_fits_the_invoices_tab_content(self):
        # Mesure reelle (winfo_reqheight) comme l'a fait l'audit : a 640px
        # de haut, le contenu de l'onglet Factures (le plus charge, avec sa
        # rangee de boutons "Marquer payee"/"Dupliquer"/etc.) demandait
        # 649px et se retrouvait coupe. La fenetre par defaut doit etre assez
        # haute pour l'afficher entierement sans intervention de
        # l'utilisateur.
        self.root.deiconify()
        self.root.update_idletasks()

        notebook = None
        for child in self.root.winfo_children():
            if isinstance(child, __import__("tkinter").ttk.Notebook):
                notebook = child
                break
        self.assertIsNotNone(notebook, "Notebook principal introuvable")
        notebook.select(self.app.invoices_tab)
        self.root.update_idletasks()

        required_height = self.root.winfo_reqheight()
        default_geometry = self.root.geometry()
        default_height = int(default_geometry.split("x")[1].split("+")[0])

        self.assertGreaterEqual(
            default_height, required_height,
            f"La fenetre par defaut ({default_height}px) est plus basse que le "
            f"contenu requis par l'onglet Factures ({required_height}px).",
        )
        min_width, min_height = self.root.wm_minsize()
        self.assertGreaterEqual(min_height, required_height)

    # -- item audit D1 : gestionnaire d'exception Tk global + journalisation
    # -- (aucun echec silencieux dans l'exe empaquete, console=False) -------

    def test_uncaught_exception_in_a_tk_callback_is_logged_and_shown_to_the_user(self):
        # Reproduit directement le point d'entree que Tkinter appelle
        # lui-meme quand un callback (clic de bouton, etc.) leve une
        # exception non geree : root.report_callback_exception. Le
        # comportement par defaut de Tkinter est d'imprimer la trace sur
        # stderr et de CONTINUER silencieusement - invisible dans l'exe
        # empaquete (console=False, voir TempoFacture.spec) puisqu'aucun
        # terminal n'y est attache.
        try:
            raise RuntimeError("panne simulee pour le test")
        except RuntimeError:
            exc_info = sys.exc_info()

        with patch("tkinter.messagebox.showerror") as mock_error:
            self.app.root.report_callback_exception(*exc_info)

        # 1) L'utilisateur voit un message clair : pas d'echec silencieux.
        mock_error.assert_called_once()
        title, message = mock_error.call_args[0][:2]
        self.assertEqual(title, gui.APP_TITLE)
        self.assertIn("erreur inattendue", message.lower())

        # 2) La trace complete a ete journalisee dans un fichier exploitable
        # pour un rapport de bug, dans le dossier de donnees de l'app (ici
        # redirige vers self.tmp par le patch de gui._data_dir en setUp).
        log_path = self.tmp / "logs" / "tempofacture.log"
        self.assertTrue(log_path.exists())
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("RuntimeError", content)
        self.assertIn("panne simulee pour le test", content)
        # Le chemin communique a l'utilisateur pointe bien vers ce fichier.
        self.assertIn(str(log_path), message)

    def test_a_real_button_callback_raising_reaches_the_global_handler(self):
        # Reproduit fidelement le scenario de l'audit (voir D1) : un clic
        # sur un bouton dont le handler leve une exception non prevue ne
        # doit plus se traduire par "je clique et il ne se passe rien" -
        # verifie via un vrai widget Tkinter invoque de bout en bout (pas
        # seulement un appel direct au gestionnaire ci-dessus).
        def boom():
            raise RuntimeError("boom bouton")

        button = tkinter.ttk.Button(self.root, text="x", command=boom)
        with patch("tkinter.messagebox.showerror") as mock_error:
            button.invoke()
            self.root.update()

        self.assertTrue(self.root.winfo_exists())  # pas de plantage de la fenetre
        mock_error.assert_called_once()
        log_path = self.tmp / "logs" / "tempofacture.log"
        self.assertIn("boom bouton", log_path.read_text(encoding="utf-8"))

    def test_configure_logging_replaces_stale_handlers_instead_of_stacking(self):
        # _configure_logging() est appelee a chaque creation de
        # TempoFactureApp (une par test dans cette classe, chacune avec un
        # _data_dir different) : si les handlers s'empilaient au lieu
        # d'etre remplaces, une seule erreur finirait journalisee plusieurs
        # fois, et un handler resterait ouvert sur le dossier temporaire
        # (deja supprime) d'un test precedent.
        gui._configure_logging()
        gui._configure_logging()
        self.assertEqual(len(gui._logger.handlers), 1)

    def test_business_error_messages_never_embed_client_or_financial_data(self):
        # Le contenu journalise inclut le message de l'exception : ce test
        # verrouille le fait que les erreurs "metier" de ce projet ne
        # contiennent jamais de donnee client/financiere (nom, email,
        # adresse, montant) qui se retrouverait alors en clair dans le
        # fichier de log - seulement des identifiants/statuts neutres.
        client_id = self.app.db.add_client(
            "Client Ultra Confidentiel", "secret@example.com", "1 rue Privee", 123.45
        )
        project_id = self.app.db.add_project(client_id, "Projet Secret")
        entry_id = self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        self.app.db.create_invoice(client_id, [entry_id])

        with self.assertRaises(ValueError) as ctx:
            self.app.db.delete_time_entry(entry_id)  # deja facturee

        message = str(ctx.exception)
        self.assertNotIn("Client Ultra Confidentiel", message)
        self.assertNotIn("secret@example.com", message)
        self.assertNotIn("1 rue Privee", message)
        self.assertNotIn("123.45", message)
        self.assertNotIn("Projet Secret", message)


if __name__ == "__main__":
    unittest.main()
