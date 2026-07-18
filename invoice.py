"""Calcul des lignes de facture et generation du PDF.

La logique de calcul (build_line_items, compute_totals) est entierement
pure : aucun acces disque, aucune dependance a fpdf2. Seule
generate_invoice_pdf() touche a la mise en page/au rendu.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class LineItem:
    project_name: str
    hours: float
    rate: float

    @property
    def amount(self) -> float:
        return round(self.hours * self.rate, 2)


def compute_duration_hours(start_iso: str, end_iso: str) -> float:
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return max(0.0, (end - start).total_seconds() / 3600.0)


def build_line_items(time_entries: list, db) -> list:
    """Regroupe des entrees de temps (deja terminees) par projet, additionne
    les heures, et calcule le montant avec le taux horaire effectif de
    chaque projet (voir Database.effective_hourly_rate)."""
    hours_by_project: dict = {}
    names_by_project: dict = {}
    for entry in time_entries:
        if not entry["end_time"]:
            continue  # une entree en cours (jamais arretee) n'est pas facturable
        project_id = entry["project_id"]
        hours = compute_duration_hours(entry["start_time"], entry["end_time"])
        hours_by_project[project_id] = hours_by_project.get(project_id, 0.0) + hours
        names_by_project[project_id] = entry["project_name"]

    line_items = []
    for project_id, hours in hours_by_project.items():
        rate = db.effective_hourly_rate(project_id)
        line_items.append(LineItem(project_name=names_by_project[project_id], hours=round(hours, 2), rate=rate))
    return line_items


def compute_totals(line_items: list, tax_rate: float) -> tuple:
    """Renvoie (sous-total, montant de taxe, total), arrondis a 2 decimales."""
    subtotal = round(sum(li.amount for li in line_items), 2)
    tax_amount = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)
    return subtotal, tax_amount, total


def format_amount(amount: float, currency: str = "EUR") -> str:
    # Le code devise (EUR, USD...) est utilise tel quel plutot qu'un symbole
    # (€, $...) : la police de base "Helvetica" de fpdf2 ne supporte que le
    # jeu de caracteres Latin-1, qui ne contient pas le signe euro. Plus
    # simple et plus robuste qu'embarquer une police Unicode pour ce seul
    # symbole.
    return f"{amount:,.2f} {currency}".replace(",", " ")


def _latin1_safe(text: str) -> str:
    """Remplace tout caractere non representable par la police de base
    (Latin-1) par '?' plutot que de laisser fpdf2 lever une exception. Les
    champs libres (nom d'entreprise, notes...) sont souvent copies-colles
    depuis Word/le web et peuvent contenir des tirets longs, guillemets
    courbes ou emoji qui ne font pas partie de ce jeu de caracteres."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


def generate_invoice_pdf(
    *,
    output_path: Path,
    invoice_number: str,
    issue_date: str,
    due_date: Optional[str],
    company_name: str,
    company_info: str,
    client_name: str,
    client_address: str,
    line_items: list,
    tax_rate: float,
    notes: str = "",
    currency: str = "EUR",
) -> None:
    """Genere une facture PDF simple (fpdf2 - pure Python, sans binaire
    externe requis)."""
    from fpdf import FPDF

    company_name = _latin1_safe(company_name)
    company_info = _latin1_safe(company_info)
    client_name = _latin1_safe(client_name)
    client_address = _latin1_safe(client_address)
    notes = _latin1_safe(notes)
    line_items = [
        LineItem(_latin1_safe(li.project_name), li.hours, li.rate) for li in line_items
    ]

    subtotal, tax_amount, total = compute_totals(line_items, tax_rate)

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 12, "FACTURE", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 5, company_name)
    if company_info:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, company_info)
    pdf.ln(4)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, f"Facture no {invoice_number}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, f"Date d'emission : {issue_date}", new_x="LMARGIN", new_y="NEXT")
    if due_date:
        pdf.cell(0, 5, f"Echeance : {due_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, "Facture a :", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 5, client_name)
    if client_address:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, client_address)
    pdf.ln(6)

    # Tableau des prestations
    col_widths = [90, 25, 30, 30]
    headers = ["Description", "Heures", "Taux horaire", "Montant"]
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(235, 235, 235)
    for width, header in zip(col_widths, headers):
        pdf.cell(width, 7, header, border=1, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 9)
    for item in line_items:
        pdf.cell(col_widths[0], 7, item.project_name, border=1)
        pdf.cell(col_widths[1], 7, f"{item.hours:.2f} h", border=1, align="R")
        pdf.cell(col_widths[2], 7, format_amount(item.rate, currency) + "/h", border=1, align="R")
        pdf.cell(col_widths[3], 7, format_amount(item.amount, currency), border=1, align="R")
        pdf.ln()

    pdf.ln(4)
    label_width = sum(col_widths[:3])
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(label_width, 6, "Sous-total", align="R")
    pdf.cell(col_widths[3], 6, format_amount(subtotal, currency), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(label_width, 6, f"TVA ({tax_rate:g} %)", align="R")
    pdf.cell(col_widths[3], 6, format_amount(tax_amount, currency), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(label_width, 8, "Total", align="R")
    pdf.cell(col_widths[3], 8, format_amount(total, currency), align="R", new_x="LMARGIN", new_y="NEXT")

    if notes:
        pdf.ln(8)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, notes)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
