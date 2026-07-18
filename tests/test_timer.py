"""Tests pour timer.py : demarrage/arret du chronometre, detection
d'inactivite (mockee - pas de dependance a l'etat reel du poste)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database
from timer import Timer, get_idle_seconds


class TimerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "test.sqlite")
        self.addCleanup(self.db.close)
        client_id = self.db.add_client("Client")
        self.project_id = self.db.add_project(client_id, "Projet")
        self.timer = Timer(self.db)

    def test_start_creates_running_entry(self):
        entry_id = self.timer.start(self.project_id, "Travail")
        self.assertTrue(self.timer.is_running)
        running = self.db.get_running_entry()
        self.assertEqual(running["id"], entry_id)

    def test_cannot_start_twice(self):
        self.timer.start(self.project_id)
        with self.assertRaises(RuntimeError):
            self.timer.start(self.project_id)

    def test_stop_persists_end_time(self):
        entry_id = self.timer.start(self.project_id)
        self.timer.stop()
        self.assertFalse(self.timer.is_running)
        self.assertIsNone(self.db.get_running_entry())
        entries = self.db.list_time_entries(project_id=self.project_id)
        self.assertIsNotNone(entries[0]["end_time"])

    def test_stop_when_not_running_returns_none(self):
        self.assertIsNone(self.timer.stop())

    def test_stop_removing_idle_time_backdates_end_time(self):
        import time
        self.timer.start(self.project_id)
        time.sleep(0.05)
        self.timer.stop_removing_idle_time(idle_seconds=30.0)
        entries = self.db.list_time_entries(project_id=self.project_id)
        entry = entries[0]
        from datetime import datetime
        start = datetime.fromisoformat(entry["start_time"])
        end = datetime.fromisoformat(entry["end_time"])
        # L'heure de fin doit etre anterieure a "maintenant" d'environ 30s,
        # donc tres proche (voire avant) de l'heure de debut.
        self.assertLess((end - start).total_seconds(), 5.0)

    def test_elapsed_seconds_is_zero_when_not_running(self):
        self.assertEqual(self.timer.elapsed_seconds(), 0.0)

    def test_format_duration(self):
        self.assertEqual(Timer.format_duration(0), "00:00:00")
        self.assertEqual(Timer.format_duration(3661), "01:01:01")
        self.assertEqual(Timer.format_duration(-5), "00:00:00")


class GetIdleSecondsTestCase(unittest.TestCase):
    def test_returns_float_without_crashing(self):
        # Test d'integration reel (pas mocke) : verifie juste que l'appel
        # Win32 fonctionne sur cette machine et renvoie un nombre plausible.
        idle = get_idle_seconds()
        self.assertIsInstance(idle, float)
        self.assertGreaterEqual(idle, 0.0)

    def test_returns_zero_when_getlastinputinfo_fails(self):
        with patch("ctypes.windll.user32.GetLastInputInfo", return_value=0):
            self.assertEqual(get_idle_seconds(), 0.0)


if __name__ == "__main__":
    unittest.main()
