"""Moteur de suivi du temps + detection d'inactivite (Windows, via ctypes).

La detection d'inactivite ne fait QUE mesurer le temps ecoule depuis la
derniere activite clavier/souris (un compteur), elle n'enregistre jamais ce
qui a ete tape ou clique - meme principe de confidentialite que dans les
autres outils de cette suite : aucune capture de frappe.
"""

from __future__ import annotations

import ctypes
import time
from typing import Optional


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> float:
    """Secondes ecoulees depuis la derniere activite clavier/souris,
    n'importe ou sur le systeme (pas seulement dans cette application).
    Renvoie 0.0 si l'information est indisponible (plateforme non-Windows,
    appel echoue)."""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # GetTickCount64 renvoie un compteur 64 bits (millisecondes depuis le
        # demarrage), qui ne boucle jamais en pratique (~584 millions
        # d'annees) - contrairement a GetTickCount (32 bits), qui boucle
        # au bout de ~49.7 jours et dont ctypes lirait meme le retour comme
        # un entier signe (negatif) au bout de ~24.8 jours si son restype
        # n'etait pas explicitement fixe a c_uint.
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        millis_since_boot = ctypes.windll.kernel32.GetTickCount64()
        # dwTime lui-meme reste un DWORD 32 bits (limite de l'API Windows,
        # pas de ctypes) : il boucle a zero au bout de ~49.7 jours de temps
        # de fonctionnement. En ne comparant que les 32 bits de poids faible
        # des deux compteurs, la soustraction reste correcte meme apres un
        # tel bouclage (les idles reels sont toujours tres inferieurs a
        # 49.7 jours, donc aucune ambiguite possible).
        idle_millis = (millis_since_boot - info.dwTime) & 0xFFFFFFFF
        return max(0.0, idle_millis / 1000.0)
    except (AttributeError, OSError):
        return 0.0


def get_uptime_seconds() -> Optional[float]:
    """Secondes ecoulees depuis le dernier demarrage (boot) de la machine,
    via GetTickCount64 - utilise a la restauration de session pour detecter
    qu'une extinction complete du PC (pas juste une fermeture propre de
    l'application) a eu lieu pendant qu'un chronometre etait en cours (voir
    _restore_running_timer dans gui.py) : si l'uptime actuel est inferieur au
    temps ecoule depuis le demarrage du chronometre, la machine a
    necessairement redemarre entre-temps.
    Renvoie None si l'information est indisponible (plateforme non-Windows,
    appel echoue) plutot que 0.0, pour distinguer "la machine vient de
    demarrer" (uptime reellement proche de zero) de "impossible a savoir"
    (qui ne doit jamais declencher de faux positif de detection hors-ligne)."""
    try:
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except (AttributeError, OSError):
        return None


class Timer:
    """Chronometre relie a la base de donnees : demarre/arrete une entree de
    temps reelle (persistee), garde en memoire uniquement l'instant de
    demarrage pour calculer le temps ecoule affiche a l'ecran."""

    def __init__(self, db):
        self.db = db
        self.active_entry_id: Optional[int] = None
        self.project_id: Optional[int] = None
        self._start_monotonic: Optional[float] = None
        self._start_wall: Optional[float] = None

    @property
    def is_running(self) -> bool:
        return self.active_entry_id is not None

    def start(self, project_id: int, description: str = "") -> int:
        if self.is_running:
            raise RuntimeError("Un chronometre est deja en cours ; arretez-le avant d'en demarrer un autre.")
        self.active_entry_id = self.db.start_time_entry(project_id, description)
        self.project_id = project_id
        self._start_monotonic = time.monotonic()
        self._start_wall = time.time()
        return self.active_entry_id

    def stop(self) -> Optional[int]:
        if not self.is_running:
            return None
        entry_id = self.active_entry_id
        self.db.stop_time_entry(entry_id)
        self.active_entry_id = None
        self.project_id = None
        self._start_monotonic = None
        self._start_wall = None
        return entry_id

    def sleep_gap_seconds(self) -> float:
        """Ecart entre le temps horloge murale ecoule et le temps compteur
        moniteur ecoule depuis le demarrage. Les compteurs "monotonic" (et
        GetTickCount64, utilise par get_idle_seconds) se figent pendant une
        veille/hibernation Windows, contrairement a l'horloge murale - un
        ecart important signale donc une periode de veille qu'aucun des deux
        n'a pu detecter comme de l'inactivite active."""
        if not self.is_running or self._start_wall is None:
            return 0.0
        wall_elapsed = time.time() - self._start_wall
        monotonic_elapsed = time.monotonic() - self._start_monotonic
        return max(0.0, wall_elapsed - monotonic_elapsed)

    def stop_removing_idle_time(self, idle_seconds: float) -> Optional[int]:
        """Arrete le chronometre en cours, mais en reculant l'heure de fin du
        temps d'inactivite detecte - pour ne pas facturer un client pendant
        que l'utilisateur etait absent de son poste."""
        if not self.is_running:
            return None
        entry_id = self.active_entry_id
        from datetime import datetime, timezone
        end_time = time.time() - idle_seconds
        end_iso = datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()
        running = self.db.get_running_entry()
        if running and end_iso < running["start_time"]:
            # L'inactivite detectee depasse la duree ecoulee depuis le debut
            # (ex : l'utilisateur s'est absente des le demarrage) : on ne
            # recule jamais avant l'heure de debut elle-meme, sinon l'entree
            # deviendrait invalide (fin avant debut).
            end_iso = running["start_time"]
        self.db.stop_time_entry(entry_id, end_time_iso=end_iso)
        self.active_entry_id = None
        self.project_id = None
        self._start_monotonic = None
        # Bug trouve a l'audit (2026-07-28) : contrairement a stop() ci-dessus,
        # _start_wall n'etait jamais remis a None ici - sleep_gap_seconds()
        # restait donc a calculer un ecart horloge murale / compteur moniteur
        # a partir d'un ancien _start_wall perime des le prochain demarrage du
        # chronometre (avant que start() ne le reaffecte), et is_running vaut
        # deja False juste apres cet appel donc sleep_gap_seconds() renvoie
        # 0.0 entre-temps - mais l'etat interne restait incoherent avec stop().
        self._start_wall = None
        return entry_id

    def elapsed_seconds(self) -> float:
        if self._start_monotonic is None:
            return 0.0
        return time.monotonic() - self._start_monotonic

    @staticmethod
    def format_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
