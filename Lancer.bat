@echo off
REM Lance l'interface graphique de TempoFacture.
REM Utile si Lancer.vbs ne fonctionne pas : cette version affiche les erreurs eventuelles.
cd /d "%~dp0"
python gui.py
if errorlevel 1 (
    echo.
    echo Une erreur s'est produite. Verifiez que Python et les dependances
    echo sont bien installes : pip install -r requirements.txt
    pause
)
