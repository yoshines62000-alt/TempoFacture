"""Tests pour la configuration de packaging (PyInstaller).

Aucun code applicatif ici, mais des regressions silencieuses sont faciles
sur ce genre de fichier de configuration statique : personne ne le
remarque tant que l'executable n'est pas reconstruit puis inspecte
manuellement via les proprietes Windows/l'Explorateur.

Correctif audit I4 : l'executable distribue n'embarquait aucune metadonnee
de version Windows (FileVersion/ProductVersion/CompanyName/FileDescription/
LegalCopyright... tous vides ou a 0.0.0.0 dans l'onglet "Details" des
proprietes du fichier), un detail de professionnalisme et un facteur mineur
pouvant influencer certaines heuristiques antivirus (lie a l'ecosysteme
PyInstaller). `version_info.txt` et sa reference dans TempoFacture.spec
corrigent ca - meme demarche que pour "Coffre" (autre outil de la meme
suite, voir Coffre/version_info.txt). Ces tests verrouillent la coherence
entre APP_VERSION (gui.py), version_info.txt et TempoFacture.spec, sans
necessiter de reconstruire l'executable lui-meme (qui reste un exercice
manuel, hors CI/tests).

Correctif audit E6 : icon.ico ne contenait qu'une seule image integree
(16x16, 217 octets) - partout ou Windows a besoin de l'afficher plus grande
(barre des taches, Alt-Tab, icones moyennes/grandes de l'Explorateur), il
devait upscaler cette petite image, resultant en un rendu flou/pixellise.
icon.ico embarque desormais un jeu complet de resolutions standard (16/32/
48/256px), chacune generee explicitement via Pillow (pas laissee a un
upscale automatique de dernier recours par Windows a l'affichage)."""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import APP_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent


class VersionResourceFileTestCase(unittest.TestCase):
    def setUp(self):
        self.version_info_path = REPO_ROOT / "version_info.txt"
        self.spec_path = REPO_ROOT / "TempoFacture.spec"

    def test_version_info_file_exists(self):
        self.assertTrue(
            self.version_info_path.exists(),
            "version_info.txt doit exister a la racine du depot (constat d'audit I4)",
        )

    def test_tempofacture_spec_references_the_version_resource_file(self):
        spec_text = self.spec_path.read_text(encoding="utf-8").replace('"', "'")
        self.assertIn(
            "version='version_info.txt'", spec_text,
            "TempoFacture.spec doit passer version='version_info.txt' a EXE(...) pour que "
            "le prochain build embarque les metadonnees de version",
        )

    def test_version_info_strings_match_app_version(self):
        text = self.version_info_path.read_text(encoding="utf-8")
        file_versions = re.findall(r"StringStruct\(u?'FileVersion',\s*u?'([^']+)'\)", text)
        product_versions = re.findall(r"StringStruct\(u?'ProductVersion',\s*u?'([^']+)'\)", text)
        self.assertEqual(
            file_versions, [APP_VERSION],
            "FileVersion (version_info.txt) doit rester synchronise avec APP_VERSION (gui.py) a chaque publication",
        )
        self.assertEqual(
            product_versions, [APP_VERSION],
            "ProductVersion (version_info.txt) doit rester synchronise avec APP_VERSION (gui.py) a chaque publication",
        )

    def test_version_info_filevers_and_prodvers_tuples_match_app_version(self):
        text = self.version_info_path.read_text(encoding="utf-8")
        expected = [int(part) for part in APP_VERSION.split(".")] + [0]
        for field in ("filevers", "prodvers"):
            match = re.search(rf"{field}=\(([^)]+)\)", text)
            self.assertIsNotNone(match, f"{field} introuvable dans version_info.txt")
            parts = [int(p.strip()) for p in match.group(1).split(",")]
            self.assertEqual(parts, expected, f"{field} doit correspondre a APP_VERSION={APP_VERSION!r}")

    def test_version_info_declares_the_expected_product_identity(self):
        text = self.version_info_path.read_text(encoding="utf-8")
        self.assertIn("StringStruct(u'ProductName', u'TempoFacture')", text)
        self.assertIn("StringStruct(u'OriginalFilename', u'TempoFacture.exe')", text)

    def test_version_info_file_parses_with_pyinstaller_when_available(self):
        # Validation la plus forte possible sans reconstruire l'executable :
        # fait rejouer a PyInstaller lui-meme son propre mini-parseur sur ce
        # fichier, plutot que de se fier uniquement aux regex ci-dessus.
        try:
            from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file
        except ImportError:
            self.skipTest("PyInstaller n'est pas installe dans cet environnement de test")
        version_info = load_version_info_from_text_file(str(self.version_info_path))
        self.assertIsNotNone(version_info)


class IconMultiResolutionTestCase(unittest.TestCase):
    EXPECTED_SIZES = {(16, 16), (32, 32), (48, 48), (256, 256)}

    def setUp(self):
        self.icon_path = REPO_ROOT / "icon.ico"

    def test_icon_file_exists(self):
        self.assertTrue(self.icon_path.exists())

    def test_icon_embeds_every_expected_resolution(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow n'est pas installe dans cet environnement de test")
        with Image.open(self.icon_path) as im:
            embedded_sizes = set(im.info.get("sizes", set()))
        missing = self.EXPECTED_SIZES - embedded_sizes
        self.assertFalse(
            missing,
            f"icon.ico n'embarque pas les resolutions attendues : manquantes = {missing}",
        )

    def test_each_embedded_frame_actually_matches_its_declared_size(self):
        # Verification plus forte que la seule metadonnee "sizes" ci-dessus :
        # charge reellement chaque frame declaree et confirme ses dimensions
        # effectives, pour attraper une eventuelle regression ou une entree
        # de taille declaree mais mal formee.
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow n'est pas installe dans cet environnement de test")
        for size in sorted(self.EXPECTED_SIZES):
            with Image.open(self.icon_path) as im:
                im.size = size
                im.load()
                self.assertEqual(im.size, size)

    def test_tempofacture_spec_still_references_the_single_icon_file(self):
        # Un seul fichier .ico multi-resolution, pas un fichier separe par
        # taille - TempoFacture.spec ne doit pas avoir change sur ce point.
        spec_text = (REPO_ROOT / "TempoFacture.spec").read_text(encoding="utf-8")
        self.assertIn("icon=['icon.ico']", spec_text.replace('"', "'"))


if __name__ == "__main__":
    unittest.main()
