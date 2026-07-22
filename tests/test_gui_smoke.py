"""Smoke tests de bout en bout pour gui.py : pilotent la VRAIE interface
Tkinter (vraie fenetre Tk, vrais widgets, vraie base SQLite temporaire) -
seuls tkinter.messagebox, tkinter.filedialog et tkinter.simpledialog sont
mockes, comme dans le reste de la suite, pour ne jamais faire dependre un
test automatise d'un clic humain sur une boite de dialogue modale."""

import re
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

    # -- item audit Phase 4, C1 : le taux effectif pre-calcule en Python ---
    # -- reste correct pour un projet avec taux propre ET un projet qui ----
    # -- herite du taux de son client ---------------------------------------

    def test_refresh_projects_shows_effective_rate_for_project_and_client_level_rates(self):
        client_id = self.app.db.add_client("Client Taux", hourly_rate=40.0)
        project_with_own_rate = self.app.db.add_project(client_id, "Projet Taux Propre", hourly_rate=90.0)
        project_inheriting = self.app.db.add_project(client_id, "Projet Herite")
        self.app._refresh_projects()

        values_own = self.app.projects_tree.item(str(project_with_own_rate), "values")
        values_inherit = self.app.projects_tree.item(str(project_inheriting), "values")
        # Separateur decimal francais (virgule), voir B7 : "90,00 EUR", pas
        # "90.00 EUR".
        self.assertEqual(values_own[3], "90,00 EUR")
        self.assertEqual(values_inherit[3], "40,00 EUR")

    # -- item audit Phase 6, C2 : self._currency() (requete "settings") ----
    # -- ne doit etre interroge qu'une seule fois par rafraichissement de --
    # -- la liste Clients, pas une fois par ligne affichee (meme anti- -----
    # -- pattern, accessoire de C1, deja corrige pour _refresh_projects) ---

    def test_refresh_clients_queries_the_currency_setting_only_once(self):
        self.app.db.add_client("Client A", hourly_rate=10.0)
        self.app.db.add_client("Client B", hourly_rate=20.0)
        self.app.db.add_client("Client C", hourly_rate=30.0)

        with patch.object(self.app, "_currency", wraps=self.app._currency) as spy_currency:
            self.app._refresh_clients()

        spy_currency.assert_called_once()
        self.assertEqual(len(self.app.clients_tree.get_children()), 3)

    # -- item audit Phase 6, E2 : aucun formulaire ne se soumettait au ------
    # -- clavier (touche Entree) avant ce correctif - les tests ci-dessous -
    # -- verifient que le VRAI gestionnaire lie a <Return>/<Escape> est ----
    # -- effectivement appele, sur chaque formulaire principal et chaque ---
    # -- mini-dialogue. Note methodologique : synthetiser un vrai evenement
    # -- clavier (widget.event_generate("<Return>")) a ete essaye en premier
    # -- mais s'est avere non fiable dans ce harnais de tests (fonctionne en
    # -- isolation, mais Windows restreint le vol de focus clavier repete
    # -- par un meme processus des que plusieurs tests s'enchainent dans la
    # -- meme suite - echecs intermittents constates empiriquement selon
    # -- l'ordre d'execution). On invoque donc directement la commande Tcl
    # -- deja enregistree par bind() - ce qui exerce le VRAI callback Python
    # -- cible de maniere deterministe - plutot que de simuler la frappe au
    # -- niveau du systeme d'exploitation. Meme categorie de limitation deja
    # -- documentee pour <Double-1>, voir
    # -- test_clients_and_projects_trees_have_a_double_click_binding_wired.

    def _invoke_bound_key(self, widget, sequence, *, keysym, keycode, keysym_num, char):
        script = widget.bind(sequence)
        match = re.search(r"\[(\S+) %#", script or "")
        if match is None:
            raise AssertionError(f"aucun binding {sequence} enregistre sur {widget}")
        funcid = match.group(1)
        # Args factices correspondant a l'ordre attendu par
        # tkinter.Misc._substitute (%# %b %f %h %k %s %t %w %x %y %A %E %K
        # %N %W %T %X %Y %D) : seuls %K (keysym) et %N (keysym_num)
        # importent reellement ici puisque nos gestionnaires ignorent le
        # contenu de l'evenement, mais tous les champs doivent avoir un
        # type Tcl valide (entier/booleen) pour que la substitution interne
        # de tkinter ne leve pas d'exception.
        args = ["1", "0", "0", "0", keycode, "0", "0", "0", "0", "0", char, "0", keysym, keysym_num, str(widget), "2", "0", "0", "0"]
        widget.tk.call(funcid, *args)

    def _invoke_return(self, widget):
        self._invoke_bound_key(widget, "<Return>", keysym="Return", keycode="36", keysym_num="65293", char="\r")

    def _invoke_escape(self, widget):
        self._invoke_bound_key(widget, "<Escape>", keysym="Escape", keycode="27", keysym_num="65307", char="\x1b")

    def _invoke_key_on_open_dialog(self, invoke_fn):
        """A utiliser via root.after(...) pendant qu'un dialogue modal
        (Toplevel avec grab_set()/wait_window()) est ouvert. Toute erreur
        est deliberement avalee ici plutot que remontee via assertTrue/
        assertIsNotNone : une exception levee dans un callback planifie par
        after() ne fait pas echouer le test (elle part dans
        root.report_callback_exception, deja surcharge par l'application
        pour afficher une VRAIE boite tkinter.messagebox non mockee dans ce
        contexte, ce qui bloquerait indefiniment toute la suite de tests en
        attendant un clic humain). Le succes ou l'echec du correctif E2 est
        verifie par le test appelant sur la valeur RENVOYEE par le dialogue
        une fois wait_window() debloque, jamais depuis ce callback."""
        for widget in self.app.root.winfo_children():
            if isinstance(widget, tkinter.Toplevel):
                try:
                    invoke_fn(widget)
                except Exception:
                    pass
                finally:
                    if widget.winfo_exists():
                        widget.destroy()
                return

    def _invoke_return_on_open_dialog(self):
        self._invoke_key_on_open_dialog(self._invoke_return)

    def _invoke_escape_on_open_dialog(self):
        self._invoke_key_on_open_dialog(self._invoke_escape)

    def test_pressing_return_in_the_client_form_submits_it(self):
        with patch.object(self.app, "_add_client") as mock_add:
            self._invoke_return(self.app.client_name_entry)
        mock_add.assert_called_once()

    def test_pressing_return_in_the_project_form_submits_it(self):
        with patch.object(self.app, "_add_project") as mock_add:
            self._invoke_return(self.app.project_name_entry)
        mock_add.assert_called_once()

    def test_pressing_return_in_the_manual_time_entry_form_submits_it(self):
        with patch.object(self.app, "_add_manual_entry") as mock_add:
            self._invoke_return(self.app.manual_hours_entry)
        mock_add.assert_called_once()

    def test_pressing_return_in_the_invoice_tax_field_generates_the_invoice(self):
        with patch.object(self.app, "_generate_invoice") as mock_generate:
            self._invoke_return(self.app.invoice_tax_entry)
        mock_generate.assert_called_once()

    def test_pressing_return_in_the_settings_form_saves_settings(self):
        with patch.object(self.app, "_save_settings") as mock_save:
            self._invoke_return(self.app.settings_currency_entry)
        mock_save.assert_called_once()

    def test_pressing_return_in_the_client_edit_dialog_saves_it(self):
        self.app.root.after(50, self._invoke_return_on_open_dialog)
        result = self.app._prompt_client_fields("Modifier le client", "Nom Original", "mail@test.fr", "1 rue Test", 50.0)
        self.assertEqual(result, {"name": "Nom Original", "email": "mail@test.fr", "address": "1 rue Test", "hourly_rate": "50.0"})

    def test_pressing_escape_in_the_client_edit_dialog_cancels_it(self):
        self.app.root.after(50, self._invoke_escape_on_open_dialog)
        result = self.app._prompt_client_fields("Modifier le client", "Nom Original", "", "", 50.0)
        self.assertIsNone(result)

    def test_pressing_return_in_the_project_edit_dialog_saves_it(self):
        self.app.root.after(50, self._invoke_return_on_open_dialog)
        result = self.app._prompt_project_fields("Modifier le projet", "Projet X", 75.0)
        self.assertEqual(result, {"name": "Projet X", "hourly_rate": "75.0"})

    def test_pressing_escape_in_the_project_edit_dialog_cancels_it(self):
        self.app.root.after(50, self._invoke_escape_on_open_dialog)
        result = self.app._prompt_project_fields("Modifier le projet", "Projet X", 75.0)
        self.assertIsNone(result)

    def test_pressing_return_in_the_time_entry_edit_dialog_saves_it(self):
        self.app.root.after(50, self._invoke_return_on_open_dialog)
        result = self.app._prompt_time_entry_fields("Modifier l'entree", "2.5", "Description")
        self.assertEqual(result, {"hours": "2.5", "description": "Description"})

    def test_pressing_escape_in_the_time_entry_edit_dialog_cancels_it(self):
        self.app.root.after(50, self._invoke_escape_on_open_dialog)
        result = self.app._prompt_time_entry_fields("Modifier l'entree", "2.5", "Description")
        self.assertIsNone(result)

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

    # -- item audit Phase 4, A5 : suppression definitive de facture vs -----
    # -- annulation (avertissement sur le trou de numerotation) ------------

    def test_delete_invoice_confirmation_warns_about_the_numbering_gap(self):
        # Regression (audit Phase 4, A5) : le message de confirmation
        # d'origine ne mentionnait que la liberation des heures, jamais la
        # consequence sur la numerotation legale (numero definitivement
        # perdu) ni l'alternative "Marquer annulee" qui evite ce trou. Ce
        # test verrouille que le numero de facture et l'alternative
        # "annulee" apparaissent bien dans le message presente a
        # l'utilisateur avant toute suppression definitive.
        client_id = self.app.db.add_client("Client Suppression")
        project_id = self.app.db.add_project(client_id, "Projet")
        entry_id = self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        invoice_id = self.app.db.create_invoice(client_id, [entry_id])
        invoice_number = self.app.db.get_invoice(invoice_id)["invoice_number"]
        self.app._refresh_invoices()
        self.app.invoices_tree.selection_set(str(invoice_id))

        with patch("tkinter.messagebox.askyesno", return_value=False) as askyesno:
            self.app._delete_invoice()

        askyesno.assert_called_once()
        message = askyesno.call_args[0][1]
        self.assertIn(invoice_number, message)
        self.assertIn("annulee", message.lower())
        self.assertIn("numero", message.lower())
        # La confirmation a ete refusee : la facture doit toujours exister,
        # avec son numero intact (aucun trou cree).
        self.assertIsNotNone(self.app.db.get_invoice(invoice_id))

    def test_confirming_invoice_deletion_still_deletes_it(self):
        # La confirmation enrichie (message plus long) ne doit rien casser
        # du comportement existant quand l'utilisateur confirme reellement.
        client_id = self.app.db.add_client("Client Suppression")
        project_id = self.app.db.add_project(client_id, "Projet")
        entry_id = self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        invoice_id = self.app.db.create_invoice(client_id, [entry_id])
        self.app._refresh_invoices()
        self.app.invoices_tree.selection_set(str(invoice_id))

        with patch("tkinter.messagebox.askyesno", return_value=True):
            self.app._delete_invoice()

        self.assertIsNone(self.app.db.get_invoice(invoice_id))
        self.assertIsNone(self.app.db.get_time_entry(entry_id)["invoice_id"])  # heure redevenue facturable

    # -- item audit Phase 4, A6 : rappel contextuel de la mention legale --
    # -- de franchise en base de TVA quand le taux saisi est 0 -------------

    def test_tva_hint_is_shown_by_default_since_the_tax_field_defaults_to_zero(self):
        # self.invoice_tax_var vaut "0" des la construction de l'onglet
        # (cas d'usage typique d'un auto-entrepreneur exonere, voir
        # README/public cible) : le rappel doit donc etre visible sans
        # aucune action de l'utilisateur. winfo_manager() (plutot que
        # winfo_ismapped(), toujours faux ici puisque self.root est
        # withdraw() dans setUp) reflete fidelement l'appel a pack()/
        # pack_forget() independamment de l'affichage reel a l'ecran.
        self.assertNotEqual(self.app.tva_hint_var.get(), "")
        self.assertEqual(self.app.tva_hint_button.winfo_manager(), "pack")

    def test_tva_hint_disappears_when_a_nonzero_tax_rate_is_entered(self):
        self.app.invoice_tax_var.set("20")
        self.assertEqual(self.app.tva_hint_var.get(), "")
        self.assertEqual(self.app.tva_hint_button.winfo_manager(), "")

        self.app.invoice_tax_var.set("0")
        self.assertNotEqual(self.app.tva_hint_var.get(), "")
        self.assertEqual(self.app.tva_hint_button.winfo_manager(), "pack")

    def test_insert_tva_exemption_note_appends_the_legal_mention(self):
        self.app.invoice_notes_var.set("Merci de votre confiance.")
        self.app._insert_tva_exemption_note()
        self.assertEqual(
            self.app.invoice_notes_var.get(),
            "Merci de votre confiance. TVA non applicable, art. 293 B du CGI",
        )

    def test_insert_tva_exemption_note_does_not_duplicate_if_already_present(self):
        self.app.invoice_notes_var.set("Deja present : TVA non applicable, art. 293 B du CGI")
        self.app._insert_tva_exemption_note()
        self.assertEqual(
            self.app.invoice_notes_var.get(),
            "Deja present : TVA non applicable, art. 293 B du CGI",
        )

    # -- item audit Phase 4, E3 : edition d'un client/projet existant ------
    # -- (double-clic sur la ligne, aucun chemin GUI n'existait avant) -----

    def test_double_click_edit_client_updates_name_email_address_and_rate(self):
        client_id = self.app.db.add_client("Ancien Nom", "old@example.com", "Ancienne adresse", 50.0)
        self.app._refresh_clients()

        with patch.object(
            self.app, "_prompt_client_fields",
            return_value={
                "name": "Nouveau Nom", "email": "new@example.com",
                "address": "Nouvelle adresse", "hourly_rate": "90",
            },
        ):
            self.app.clients_tree.selection_set(str(client_id))
            self.app._edit_client()

        client = self.app.db.get_client(client_id)
        self.assertEqual(client["name"], "Nouveau Nom")
        self.assertEqual(client["email"], "new@example.com")
        self.assertEqual(client["address"], "Nouvelle adresse")
        self.assertEqual(client["hourly_rate"], 90.0)

    def test_cancelling_edit_client_dialog_leaves_the_client_unchanged(self):
        client_id = self.app.db.add_client("Nom Original", hourly_rate=50.0)
        self.app._refresh_clients()

        with patch.object(self.app, "_prompt_client_fields", return_value=None):
            self.app.clients_tree.selection_set(str(client_id))
            self.app._edit_client()

        client = self.app.db.get_client(client_id)
        self.assertEqual(client["name"], "Nom Original")
        self.assertEqual(client["hourly_rate"], 50.0)

    def test_clients_and_projects_trees_have_a_double_click_binding_wired(self):
        # Tk ne permet pas de synthetiser un <Double-1> via event_generate()
        # (le modificateur "Double" est detecte en interne a partir de deux
        # vrais clics rapproches, pas reproductible de maniere fiable dans
        # un test automatise) - on verifie donc que le binding est bien
        # enregistre sur chaque widget ; le comportement de _edit_client/
        # _edit_project est deja verrouille par les tests ci-dessus et
        # ci-dessous via un appel direct.
        self.assertTrue(self.app.clients_tree.bind("<Double-1>"))
        self.assertTrue(self.app.projects_tree.bind("<Double-1>"))

    def test_double_click_edit_project_updates_name_and_rate(self):
        client_id = self.app.db.add_client("Client", hourly_rate=40.0)
        project_id = self.app.db.add_project(client_id, "Ancien Projet")
        self.app._refresh_projects()

        with patch.object(
            self.app, "_prompt_project_fields", return_value={"name": "Nouveau Projet", "hourly_rate": "75"}
        ):
            self.app.projects_tree.selection_set(str(project_id))
            self.app._edit_project()

        project = self.app.db.get_project(project_id)
        self.assertEqual(project["name"], "Nouveau Projet")
        self.assertEqual(project["hourly_rate"], 75.0)

    def test_double_click_edit_project_can_clear_the_rate_to_inherit_from_client(self):
        # Vider le champ taux doit remettre le projet en heritage du taux
        # de son client (hourly_rate redevient NULL en base), pas le forcer
        # a 0.
        client_id = self.app.db.add_client("Client", hourly_rate=40.0)
        project_id = self.app.db.add_project(client_id, "Projet", hourly_rate=99.0)
        self.app._refresh_projects()

        with patch.object(self.app, "_prompt_project_fields", return_value={"name": "Projet", "hourly_rate": ""}):
            self.app.projects_tree.selection_set(str(project_id))
            self.app._edit_project()

        project = self.app.db.get_project(project_id)
        self.assertIsNone(project["hourly_rate"])
        self.assertEqual(self.app.db.effective_hourly_rate(project_id), 40.0)

    def test_editing_client_rate_via_the_gui_does_not_retroactively_change_an_issued_invoice(self):
        # Verrouille A3 (gel des lignes de facture a l'emission) sur le
        # NOUVEAU chemin d'edition expose par ce correctif : avant E3, le
        # seul moyen de changer un taux client deja en base etait d'appeler
        # db.update_client() directement dans un test (voir
        # test_invoice_line_items_are_frozen_after_rate_change dans
        # test_invoice.py) - jamais via un vrai chemin GUI, puisqu'aucun
        # n'existait (angle mort signale par l'audit, section L3).
        client_id = self.app.db.add_client("Client Facture", hourly_rate=50.0)
        project_id = self.app.db.add_project(client_id, "Projet")
        self.app.db.add_manual_time_entry(
            project_id, "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00"
        )
        entries = self.app.db.list_time_entries(project_id=project_id)
        line_items = gui.build_line_items(entries, self.app.db)
        invoice_id = self.app.db.create_invoice(client_id, [e["id"] for e in entries], line_items=line_items)
        stored_before = self.app.db.get_invoice_line_items(invoice_id)
        self.assertEqual(stored_before[0]["rate"], 50.0)

        self.app._refresh_clients()
        with patch.object(
            self.app, "_prompt_client_fields",
            return_value={"name": "Client Facture", "email": "", "address": "", "hourly_rate": "120"},
        ):
            self.app.clients_tree.selection_set(str(client_id))
            self.app._edit_client()

        self.assertEqual(self.app.db.get_client(client_id)["hourly_rate"], 120.0)
        stored_after = self.app.db.get_invoice_line_items(invoice_id)
        self.assertEqual(stored_after[0]["rate"], 50.0)  # toujours figee, jamais 120

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

    # -- item audit Phase 5, E1 : chronometre restaure au demarrage --------
    # -- doit remplir les champs Projet/Description visibles, pas juste ----
    # -- l'etat interne -------------------------------------------------------

    def test_restoring_a_running_timer_fills_in_the_project_and_description_fields(self):
        # Bug trouve a l'audit (E1) : _restore_running_timer() restaurait
        # correctement l'etat interne du chronometre (timer.project_id,
        # boutons Demarrer/Arreter, combo Projet verrouille) mais n'appelait
        # jamais timer_project_var.set()/timer_description_var.set() -
        # l'utilisateur voyait le temps defiler ("00:00:01"...) sans aucune
        # indication visible de ce qui etait effectivement chronometre, sauf
        # a aller consulter la ligne "(en cours)" du tableau plus bas.
        client_id = self.app.db.add_client("Client Chrono")
        project_id = self.app.db.add_project(client_id, "Refonte site vitrine")
        self.app.db.start_time_entry(project_id, "Travail en cours (chronometre demarre)")
        self.app._refresh_timer_project_choices()

        # Simule l'etat "vide" observe juste apres l'ouverture de
        # l'application, avant que _restore_running_timer() ne s'execute.
        self.app.timer_project_var.set("")
        self.app.timer_description_var.set("")

        self.app._restore_running_timer()

        self.assertEqual(self.app.timer_project_var.get(), f"{project_id} - Refonte site vitrine")
        self.assertEqual(
            self.app.timer_description_var.get(), "Travail en cours (chronometre demarre)"
        )
        # L'etat interne et les boutons restent corrects (non-regression).
        self.assertEqual(self.app.timer.project_id, project_id)
        self.assertEqual(str(self.app.timer_start_button["state"]), "disabled")
        self.assertEqual(str(self.app.timer_stop_button["state"]), "normal")

    def test_restoring_a_running_timer_with_no_description_leaves_the_field_empty(self):
        # La description est optionnelle (chaine vide en base) : le
        # correctif ne doit ni planter, ni laisser un texte residuel d'un
        # etat precedent dans le champ.
        client_id = self.app.db.add_client("Client Chrono")
        project_id = self.app.db.add_project(client_id, "Projet sans description")
        self.app.db.start_time_entry(project_id, "")
        self.app._refresh_timer_project_choices()
        self.app.timer_description_var.set("ancien texte residuel")

        self.app._restore_running_timer()

        self.assertEqual(self.app.timer_project_var.get(), f"{project_id} - Projet sans description")
        self.assertEqual(self.app.timer_description_var.get(), "")

    def test_no_running_timer_leaves_the_fields_untouched(self):
        # Non-regression : quand aucun chronometre n'est en cours (cas
        # courant), _restore_running_timer() doit continuer a ne rien faire.
        self.app.timer_project_var.set("")
        self.app.timer_description_var.set("valeur avant appel")

        self.app._restore_running_timer()

        self.assertEqual(self.app.timer_description_var.get(), "valeur avant appel")


if __name__ == "__main__":
    unittest.main()
