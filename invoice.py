"""Calcul des lignes de facture et generation du PDF.

La logique de calcul (build_line_items, compute_totals) est entierement
pure : aucun acces disque, aucune dependance a fpdf2. Seule
generate_invoice_pdf() touche a la mise en page/au rendu.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

_CENTS = Decimal("0.01")

_UNICODE_FONT_FAMILY = "NotoSansSC"
_UNICODE_FONT_FILE = "NotoSansSC-Regular.otf"


def _round_money(value) -> float:
    """Arrondit un montant a 2 decimales avec un demi-arrondi "au superieur"
    (comme attendu sur une facture), en passant par Decimal : round() natif
    de Python arrondit le float binaire sous-jacent (round-half-to-even sur
    sa valeur binaire, pas sur sa valeur decimale), ce qui peut sous-facturer
    de maniere silencieuse sur certaines combinaisons heures/taux (ex :
    0.05 h a 62.5/h)."""
    return float(Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP))


def _round_hours(value) -> float:
    """Meme piege que _round_money() ci-dessus, mais pour les HEURES
    agregees d'un projet (voir build_line_items) : round() natif de Python
    arrondit la representation binaire sous-jacente (round-half-to-even),
    pas la valeur decimale attendue sur une facture. Une somme d'heures qui
    tombe pres d'une frontiere x,xx5 peut ainsi etre arrondie vers le bas
    au lieu du haut, sous-facturant silencieusement le client sur cette
    ligne (bug trouve a l'audit, voir A1 - ex : deux entrees totalisant
    exactement 2h40m30s = 2.675h : round() natif donne 2.67, l'arrondi
    commercial correct est 2.68)."""
    return float(Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP))


@dataclass
class LineItem:
    project_name: str
    hours: float
    rate: float

    @property
    def amount(self) -> float:
        return _round_money(Decimal(str(self.hours)) * Decimal(str(self.rate)))


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
        try:
            hours = compute_duration_hours(entry["start_time"], entry["end_time"])
        except (ValueError, TypeError):
            # Horodatage corrompu/mal importe : on exclut cette seule entree
            # plutot que de faire echouer la facturation de tout le client.
            continue
        hours_by_project[project_id] = hours_by_project.get(project_id, 0.0) + hours
        names_by_project[project_id] = entry["project_name"]

    line_items = []
    for project_id, hours in hours_by_project.items():
        rate = db.effective_hourly_rate(project_id)
        line_items.append(LineItem(project_name=names_by_project[project_id], hours=_round_hours(hours), rate=rate))
    return line_items


def compute_totals(line_items: list, tax_rate: float) -> tuple:
    """Renvoie (sous-total, montant de taxe, total), arrondis a 2 decimales."""
    subtotal_dec = sum((Decimal(str(li.amount)) for li in line_items), Decimal("0.00"))
    tax_amount = _round_money(subtotal_dec * Decimal(str(tax_rate)) / Decimal(100))
    subtotal = float(subtotal_dec)
    total = _round_money(subtotal_dec + Decimal(str(tax_amount)))
    return subtotal, tax_amount, total


def format_amount(amount: float, currency: str = "EUR") -> str:
    # Le code devise (EUR, USD...) est utilise tel quel plutot qu'un symbole
    # (€, $...) : la police de base "Helvetica" de fpdf2 ne supporte que le
    # jeu de caracteres Latin-1, qui ne contient pas le signe euro. Plus
    # simple et plus robuste qu'embarquer une police Unicode pour ce seul
    # symbole.
    #
    # Separateurs a la convention francaise (espace insecable pour les
    # milliers, virgule pour la decimale) plutot que le point decimal
    # anglo-saxon d'origine : l'integralite de l'interface, le vocabulaire
    # (TVA, SIRET) et le public cible (voir README) sont francophones, mais
    # le point decimal restait affiche tel quel alors que la virgule des
    # milliers avait deja ete convertie en espace - un melange incoherent
    # des deux conventions a la fois (bug trouve a l'audit, voir B7). Une
    # espace INSECABLE (pas une espace normale) est utilisee pour les
    # milliers afin qu'un total ne se retrouve jamais coupe en deux lignes
    # au milieu d'un nombre par un retour a la ligne automatique.
    #
    # La conversion virgule/point ne porte que sur la partie NUMERIQUE,
    # avant d'accoler le code devise : celui-ci est un champ 100% libre
    # (voir Parametres) qui peut lui-meme contenir un point (ex. "U.S.
    # DOLLARS") - un tel point ne doit jamais etre altere par cette
    # conversion.
    formatted_number = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted_number} {currency}"


def _latin1_safe(text: str) -> str:
    """Remplace tout caractere non representable par la police de base
    (Latin-1) par '?' plutot que de laisser fpdf2 lever une exception. Les
    champs libres (nom d'entreprise, notes...) sont souvent copies-colles
    depuis Word/le web et peuvent contenir des tirets longs, guillemets
    courbes ou emoji qui ne font pas partie de ce jeu de caracteres.

    N'est plus utilise qu'en dernier recours, quand la police Unicode
    embarquee (voir _register_unicode_font ci-dessous) est introuvable :
    dans le cas normal, generate_invoice_pdf() utilise _glyph_safe() a la
    place, qui ne degrade que les tres rares caracteres vraiment absents de
    cette police (au lieu d'effacer integralement tout texte hors
    Latin-1 - bug trouve a l'audit, voir B2)."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _glyph_safe(text: str, cmap: dict) -> str:
    """Remplace par '?' les seuls caracteres reellement absents du jeu de
    glyphes de la police Unicode embarquee, au lieu de degrader tout texte
    hors Latin-1 comme le faisait _latin1_safe() (bug trouve a l'audit, voir
    B2 : un nom de client redige en chinois/japonais/coreen/cyrillique/grec
    disparaissait entierement, remplace par une suite de '?', alors que la
    police choisie (Noto Sans SC) sait parfaitement les afficher). cmap est
    le dictionnaire {code_point: glyph_id} expose par fpdf2 pour la police
    active (pdf.current_font.cmap) : ne couvre pas, par exemple, les emoji
    ou certaines ecritures rares (arabe, thai, hebreu...), qui restent donc
    degradees en '?' plutot que de faire planter la generation."""
    return "".join(ch if ord(ch) in cmap else "?" for ch in text)


def _resource_path(relative: str) -> Path:
    """Chemin vers un fichier de ressource embarque (la police Unicode
    ci-dessous), que l'app tourne depuis les sources ou depuis l'executable
    PyInstaller (ou les ressources additionnelles declarees dans
    TempoFacture.spec sont extraites dans un dossier temporaire expose par
    sys._MEIPASS). Duplique gui._resource_path() : invoice.py reste
    volontairement un module autonome, sans dependre de gui.py."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def _format_date_fr(iso_date: str) -> str:
    """Convertit une date de stockage au format ISO 8601 (AAAA-MM-JJ - voir
    les commentaires de db.py qui justifient ce choix cote base : non
    ambigu, trie naturellement) vers le format usuel francais JJ/MM/AAAA,
    uniquement pour l'IMPRESSION sur la facture. L'integralite de
    l'interface, le vocabulaire (TVA, SIRET) et le public cible (voir
    README) sont francophones ; le format ISO brut ("Date d'emission :
    2026-07-22"), bien qu'ideal en interne, est inhabituel sur un document
    remis a un client francais (bug trouve a l'audit, voir B6). Ne touche
    jamais au stockage : seule la chaine imprimee sur le PDF change.

    Si la valeur ne correspond pas au format ISO attendu (donnee
    corrompue, ancien format...), on l'affiche telle quelle plutot que de
    faire echouer toute la generation de la facture pour un simple
    probleme d'affichage de date."""
    try:
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return iso_date


def _register_unicode_font(pdf) -> Optional[dict]:
    """Enregistre aupres de fpdf2 la police TrueType/OpenType Unicode
    embarquee (Noto Sans SC, licence SIL Open Font License 1.1 - voir
    fonts/NOTICE.txt) et renvoie son jeu de glyphes disponibles (cmap), pour
    que generate_invoice_pdf() puisse l'utiliser a la place de la police de
    base "Helvetica" (Latin-1 uniquement) sur tous les champs (entreprise,
    client, projets, notes) - corrige a la racine la perte totale d'un nom
    de client redige dans une ecriture non latine (bug trouve a l'audit,
    voir B2).

    Renvoie None si le fichier de police est introuvable ou invalide,
    auquel cas generate_invoice_pdf() se rabat sur "Helvetica" et l'ancien
    filet de securite _latin1_safe() : la generation d'une facture ne doit
    jamais echouer a cause d'une ressource de police manquante."""
    font_path = _resource_path(f"fonts/{_UNICODE_FONT_FILE}")
    if not font_path.exists():
        return None
    try:
        pdf.add_font(_UNICODE_FONT_FAMILY, style="", fname=str(font_path))
        pdf.add_font(_UNICODE_FONT_FAMILY, style="B", fname=str(font_path))
        pdf.set_font(_UNICODE_FONT_FAMILY, "", 10)
        return dict(pdf.current_font.cmap)
    except Exception:
        return None


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

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # Police Unicode embarquee (voir _register_unicode_font) utilisee pour
    # TOUS les champs, y compris les libelles fixes en francais ci-dessous :
    # Noto Sans SC couvre une sur-ensemble strict de Latin-1 (y compris les
    # caracteres accentues francais), donc l'unifier evite de melanger deux
    # polices visuellement differentes sur le meme document. Se rabat sur
    # "Helvetica" (et le filet de securite _latin1_safe) uniquement si le
    # fichier de police est introuvable (bug trouve a l'audit, voir B2).
    unicode_cmap = _register_unicode_font(pdf)
    if unicode_cmap is not None:
        font_family = _UNICODE_FONT_FAMILY

        def _safe_text(text: str) -> str:
            return _glyph_safe(text, unicode_cmap)
    else:
        font_family = "Helvetica"
        _safe_text = _latin1_safe

    company_name = _safe_text(company_name)
    company_info = _safe_text(company_info)
    client_name = _safe_text(client_name)
    client_address = _safe_text(client_address)
    notes = _safe_text(notes)
    invoice_number = _safe_text(invoice_number)
    issue_date = _safe_text(issue_date)
    due_date = _safe_text(due_date) if due_date else due_date
    currency = _safe_text(currency)
    line_items = [
        LineItem(_safe_text(li.project_name), li.hours, li.rate) for li in line_items
    ]

    subtotal, tax_amount, total = compute_totals(line_items, tax_rate)

    pdf.set_font(font_family, "B", 20)
    pdf.cell(0, 12, "FACTURE", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font(font_family, "", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 5, company_name)
    if company_info:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, company_info)
    pdf.ln(4)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font(font_family, "B", 11)
    pdf.cell(0, 6, f"Facture no {invoice_number}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font_family, "", 10)
    pdf.cell(0, 5, f"Date d'emission : {_format_date_fr(issue_date)}", new_x="LMARGIN", new_y="NEXT")
    if due_date:
        pdf.cell(0, 5, f"Echeance : {_format_date_fr(due_date)}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font(font_family, "B", 10)
    pdf.cell(0, 5, "Facture a :", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font_family, "", 10)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 5, client_name)
    if client_address:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, client_address)
    pdf.ln(6)

    # Tableau des prestations
    col_widths = [90, 25, 30, 30]
    headers = ["Description", "Heures", "Taux horaire", "Montant"]
    row_height = 7

    def _draw_table_header():
        pdf.set_font(font_family, "B", 9)
        pdf.set_fill_color(235, 235, 235)
        for width, header in zip(col_widths, headers):
            pdf.cell(width, row_height, header, border=1, fill=True)
        pdf.ln()
        pdf.set_font(font_family, "", 9)

    _draw_table_header()

    def _truncate_to_width(text: str, width: float) -> str:
        # Meme logique que pour la colonne Description : si le texte
        # formate (montant + devise longue, ex. "DOLLARS AMERICAINS")
        # deborde de la largeur de sa colonne, on le tronque avec une
        # ellipse plutot que de laisser fpdf2 le faire deborder sur la
        # colonne voisine (cell() ne retourne pas a la ligne et n'empeche
        # pas le depassement visuel).
        truncated = text
        while truncated and pdf.get_string_width(truncated + "...") > width - 2:
            truncated = truncated[:-1]
        if truncated != text:
            truncated += "..."
        return truncated

    pdf.set_font(font_family, "", 9)
    for item in line_items:
        # Une facture avec de nombreuses lignes (nombreux projets/entrees)
        # peut deborder sur une page suivante (pagination automatique, voir
        # set_auto_page_break ci-dessus) : sans ce garde-fou, l'en-tete du
        # tableau (Description/Heures/Taux horaire/Montant) n'etait dessine
        # qu'une seule fois avant la boucle, donc absent de la page 2+ - un
        # client relisant une longue facture devait revenir a la page 1
        # pour se rappeler quelle colonne represente quoi (bug trouve a
        # l'audit, voir B4). will_page_break() indique par avance si la
        # prochaine cellule declencherait un saut de page (sans le
        # declencher lui-meme) : on saute alors la page nous-memes et on
        # redessine l'en-tete AVANT la ligne, plutot que de laisser le saut
        # automatique se produire au milieu de la ligne sans en-tete au-
        # dessus.
        if pdf.will_page_break(row_height):
            pdf.add_page()
            _draw_table_header()
        # Un nom de projet trop long deborderait sur les colonnes suivantes
        # (cell() ne retourne pas a la ligne et n'empeche pas le
        # depassement) : on le tronque avec une ellipse pour qu'il tienne
        # dans sa colonne.
        label = item.project_name
        while label and pdf.get_string_width(label + "...") > col_widths[0] - 2:
            label = label[:-1]
        if label != item.project_name:
            label += "..."
        hours_text = _truncate_to_width(f"{item.hours:.2f} h", col_widths[1])
        rate_text = _truncate_to_width(format_amount(item.rate, currency) + "/h", col_widths[2])
        amount_text = _truncate_to_width(format_amount(item.amount, currency), col_widths[3])
        pdf.cell(col_widths[0], row_height, label, border=1)
        pdf.cell(col_widths[1], row_height, hours_text, border=1, align="R")
        pdf.cell(col_widths[2], row_height, rate_text, border=1, align="R")
        pdf.cell(col_widths[3], row_height, amount_text, border=1, align="R")
        pdf.ln()

    pdf.ln(4)
    subtotal_text = format_amount(subtotal, currency)
    tax_label = f"TVA ({tax_rate:g} %)"
    tax_text = format_amount(tax_amount, currency)
    total_text = format_amount(total, currency)

    # Bloc de totaux (Sous-total/TVA/Total) : CONTRAIREMENT aux lignes
    # d'articles ci-dessus, ou tronquer un libelle avec une ellipse est un
    # compromis lisible acceptable (voir _truncate_to_width), tronquer un
    # CHIFFRE du total rendrait le montant a payer ambigu sur le document
    # officiel remis au client - la donnee la plus critique de toute la
    # facture (bug trouve a l'audit, voir B1 : un montant eleve combine a
    # une devise longue, ex. "DOLLARS AMERICAINS", tronquait desormais les
    # chiffres eux-memes, ex. "1 481 481 88..."). Ces 3 lignes ne sont pas
    # contraintes par un alignement de colonnes avec d'autres lignes
    # (contrairement au tableau ci-dessus) : par defaut la mise en page
    # reste identique a avant (colonne de 30mm alignee sous le tableau) ;
    # ce n'est que si un montant formate deborderait de cette colonne
    # standard qu'on retrecit la largeur du libelle (qui n'a besoin que de
    # quelques mots courts) pour agrandir d'autant la colonne du montant,
    # jusqu'a la largeur imprimable totale de la page - jamais de perte de
    # chiffres.
    standard_label_width = sum(col_widths[:3])
    standard_amount_width = col_widths[3]

    def _text_width(text: str, style: str, size: float) -> float:
        pdf.set_font(font_family, style, size)
        return pdf.get_string_width(text)

    fits_standard_width = (
        _text_width(subtotal_text, "", 10) <= standard_amount_width - 2
        and _text_width(tax_text, "", 10) <= standard_amount_width - 2
        and _text_width(total_text, "B", 11) <= standard_amount_width - 2
    )
    if fits_standard_width:
        totals_label_width = standard_label_width
        totals_amount_width = standard_amount_width
    else:
        longest_label_width = max(
            _text_width("Sous-total", "", 10),
            _text_width(tax_label, "", 10),
            _text_width("Total", "B", 11),
        )
        totals_label_width = longest_label_width + 6
        totals_amount_width = pdf.epw - totals_label_width

    pdf.set_font(font_family, "", 10)
    pdf.cell(totals_label_width, 6, "Sous-total", align="R")
    pdf.cell(totals_amount_width, 6, subtotal_text, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(totals_label_width, 6, tax_label, align="R")
    pdf.cell(totals_amount_width, 6, tax_text, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font_family, "B", 11)
    pdf.cell(totals_label_width, 8, "Total", align="R")
    pdf.cell(totals_amount_width, 8, total_text, align="R", new_x="LMARGIN", new_y="NEXT")

    if notes:
        pdf.ln(8)
        pdf.set_font(font_family, "", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, notes)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))


def reexport_invoice_pdf(db, invoice_id: int, output_path: Path, company_name: str, company_info: str) -> None:
    """Regenere le PDF d'une facture deja emise (copie perdue, fichier
    supprime...) exclusivement a partir des champs figes en base a la
    creation : numero, dates, taux de TVA, devise, notes et lignes (voir
    Database.create_invoice). Aucune ecriture en base : reexporter ne
    consomme ni numero ni heures. Seules exceptions au gel : le nom et
    l'adresse du client ne sont pas historises en base, ce sont donc leurs
    valeurs actuelles qui apparaissent sur le PDF reexporte."""
    invoice = db.get_invoice(invoice_id)
    if invoice is None:
        raise ValueError(f"facture introuvable : {invoice_id}")
    client = db.get_client(invoice["client_id"])
    if client is None:
        raise ValueError(f"client introuvable pour la facture {invoice['invoice_number']}")
    line_items = [
        LineItem(row["project_name"], row["hours"], row["rate"])
        for row in db.get_invoice_line_items(invoice_id)
    ]
    generate_invoice_pdf(
        output_path=output_path,
        invoice_number=invoice["invoice_number"],
        issue_date=invoice["issue_date"][:10],
        due_date=invoice["due_date"],
        company_name=company_name,
        company_info=company_info,
        client_name=client["name"],
        client_address=client["address"],
        line_items=line_items,
        tax_rate=invoice["tax_rate"],
        notes=invoice["notes"],
        currency=invoice["currency"],
    )
