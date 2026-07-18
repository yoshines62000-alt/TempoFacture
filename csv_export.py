"""Export CSV des entrees de temps et de l'historique de factures - pour
transmettre des heures brutes a un comptable ou analyser dans un tableur,
sans passer par la generation d'un PDF."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from invoice import LineItem, compute_duration_hours, compute_totals


def export_time_entries_csv(entries: list, output_path: Path) -> None:
    """entries : lignes issues de Database.list_time_entries() (avec
    project_name deja jointe)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig (BOM) : Excel sous Windows n'affiche correctement les
    # caracteres accentues d'un CSV UTF-8 que si le BOM est present.
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "Projet", "Debut", "Fin", "Heures", "Description", "Facture"])
        for entry in entries:
            hours = ""
            if entry["end_time"]:
                hours = f"{compute_duration_hours(entry['start_time'], entry['end_time']):.2f}"
            writer.writerow([
                entry["id"], entry["project_name"], entry["start_time"],
                entry["end_time"] or "", hours, entry["description"], entry["invoice_id"] or "",
            ])


def export_invoices_csv(invoices: list, db, output_path: Path) -> None:
    """invoices : lignes issues de Database.list_invoices(). Le total de
    chaque facture est recalcule a partir du snapshot fige de ses lignes
    (get_invoice_line_items), jamais des taux horaires actuels - memes
    montants que ceux imprimes sur le PDF d'origine."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Numero", "Client", "Date d'emission", "Echeance", "TVA (%)", "Total", "Statut"])
        for invoice in invoices:
            stored_items = db.get_invoice_line_items(invoice["id"])
            line_items = [LineItem(row["project_name"], row["hours"], row["rate"]) for row in stored_items]
            _, _, total = compute_totals(line_items, invoice["tax_rate"])
            client = db.get_client(invoice["client_id"])
            writer.writerow([
                invoice["invoice_number"], client["name"] if client else "",
                invoice["issue_date"][:10], (invoice["due_date"] or "")[:10],
                invoice["tax_rate"], f"{total:.2f}", invoice["status"],
            ])
