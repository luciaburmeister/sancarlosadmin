import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation

from import_export import resources, fields

from .models import Client,  Quote, QuoteItem
from configurator.models import Configuration, ConfigurationItem, ConfigurationStatus
from django.utils.translation import gettext_lazy as _

# Exact column names expected in the import file (after diacritic-stripping)
COL_NAME    = "nombre comercial"
COL_TAX     = "nif/cif"
COL_ADDRESS = "direccion completa"
COL_PHONE   = "telefono"
COL_EMAIL   = "email"
# Extra address columns that can be combined into a full address
COL_CITY    = "poblacion"
COL_POSTAL  = "codigo postal"
COL_PROVINCE = "provincia"

EMPTY_PLACEHOLDER = "-"

# All known header variants for each Client column.
_NAME_ALIASES = frozenset([
    "nombre comercial",
    "nombre",
    "name",
    "razon social",
    "cliente",
    "client",
])

_TAX_ALIASES = frozenset([
    "nif/cif",
    "nif",
    "cif",
    "nif / cif",
    "tax",
    "tax id",
    "tax_identifier",
    "fiscal",
    "identificador fiscal",
])

_ADDRESS_ALIASES = frozenset([
    "direccion completa",
    "direccion",
    "domicilio",
    "address",
    "dir",
    "dir.",
])

_PHONE_ALIASES = frozenset([
    "telefono",
    "tel",
    "tel.",
    "phone",
])

_EMAIL_ALIASES = frozenset([
    "email",
    "e-mail",
    "correo",
    "correo electronico",
    "mail",
])

_CITY_ALIASES = frozenset([
    "poblacion",
    "ciudad",
    "city",
    "localidad",
])

_POSTAL_ALIASES = frozenset([
    "codigo postal",
    "cp",
    "c.p.",
    "postal code",
    "zip",
])

_PROVINCE_ALIASES = frozenset([
    "provincia",
    "province",
    "estado",
])


class ClientResource(resources.ModelResource):
    """
    Imports Client records from a file whose columns can be:
      - Spanish: "Nombre comercial", "Nif/Cif", "Dirección completa", "Teléfono", "Email"
      - English: "name", "tax_identifier", "address", "phone", "Email"

    - Column matching is case-insensitive, diacritic-tolerant, and alias-aware.
    - If separate city/postal/province columns exist, they are merged into address.
    - Empty cells are stored as "-".
    - The company is set from the form selection (passed as default_company).
    - Existing records are matched by tax_identifier, then name + company.
    """

    balance_owed = fields.Field(column_name=_("Balance owed"), readonly=True)
    balance_overdue = fields.Field(column_name=_("Overdue"), readonly=True)
    balance_not_due = fields.Field(column_name=_("Not overdue"), readonly=True)

    class Meta:
        model = Client
        import_id_fields = ["id"]   # prevents _check_import_id_fields from raising
        fields = (
            "id",
            "name",
            "commercial_name",
            "tax_identifier",
            "address",
            "phone",
            "email",
            "balance_overdue",
            "balance_not_due",
            "balance_owed",
            "company__name"
        )

    def get_queryset(self):
        # Only used when the admin doesn't pass its (already annotated) queryset.
        from invoices.models import SalesInvoice, SalesInvoiceStatus
        from payments.mixins import (
            annotate_outstanding_balance,
            annotate_split_outstanding_balance,
        )

        qs = super().get_queryset()
        # Add total balance first, then split it
        qs = annotate_outstanding_balance(
            qs, SalesInvoice, "client", "balance_owed",
            invoice_filters={"status": SalesInvoiceStatus.FINAL},
        )
        # Add the split (overdue / not due)
        return annotate_split_outstanding_balance(
            qs, SalesInvoice, "client",
            invoice_filters={"status": SalesInvoiceStatus.FINAL},
        )

    def dehydrate_balance_owed(self, client):
        return float(getattr(client, "balance_owed", 0) or 0)

    def dehydrate_balance_overdue(self, client):
        return float(getattr(client, "balance_overdue", 0) or 0)

    def dehydrate_balance_not_due(self, client):
        return float(getattr(client, "balance_not_due", 0) or 0)

    def __init__(self, *args, **kwargs):
        self._default_company = kwargs.pop("default_company", None)
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Normalise headers — strip diacritics + invisible chars, then
    # collapse aliases to canonical column names
    # ------------------------------------------------------------------

    def before_import(self, dataset, **kwargs):
        if dataset.headers:
            def _canonical(h):
                n = _norm_header(h)
                if n in _NAME_ALIASES:
                    return COL_NAME
                if n in _TAX_ALIASES:
                    return COL_TAX
                if n in _ADDRESS_ALIASES:
                    return COL_ADDRESS
                if n in _PHONE_ALIASES:
                    return COL_PHONE
                if n in _EMAIL_ALIASES:
                    return COL_EMAIL
                if n in _CITY_ALIASES:
                    return COL_CITY
                if n in _POSTAL_ALIASES:
                    return COL_POSTAL
                if n in _PROVINCE_ALIASES:
                    return COL_PROVINCE
                return n
            dataset.headers = [_canonical(h) for h in dataset.headers]
        super().before_import(dataset, **kwargs)

    # ------------------------------------------------------------------
    # Convert each row's values to strings; replace blanks with "-"
    # ------------------------------------------------------------------

    def before_import_row(self, row, row_number=None, **kwargs):
        for key in list(row.keys()):
            val = row[key]
            # Excel reads numeric cells as floats (e.g. "612345678" → 612345678.0)
            if isinstance(val, float):
                val = str(int(val)) if val == int(val) else str(val)
            elif val is None:
                val = ""
            else:
                val = str(val).strip()

            row[key] = val if val else EMPTY_PLACEHOLDER

    # ------------------------------------------------------------------
    # Build a composite address from separate columns if the main
    # address column alone doesn't contain the full information.
    # ------------------------------------------------------------------

    @staticmethod
    def _build_address(row):
        """Combine street, city, postal code and province into one address."""
        street   = row.get(COL_ADDRESS, EMPTY_PLACEHOLDER)
        city     = row.get(COL_CITY, EMPTY_PLACEHOLDER)
        postal   = row.get(COL_POSTAL, EMPTY_PLACEHOLDER)
        province = row.get(COL_PROVINCE, EMPTY_PLACEHOLDER)

        parts = []
        if street and street != EMPTY_PLACEHOLDER:
            parts.append(street)
        extras = []
        if postal and postal != EMPTY_PLACEHOLDER:
            extras.append(postal)
        if city and city != EMPTY_PLACEHOLDER:
            extras.append(city)
        if province and province != EMPTY_PLACEHOLDER:
            extras.append(f"({province})")
        if extras:
            parts.append(" ".join(extras))
        return ", ".join(parts) if parts else EMPTY_PLACEHOLDER

    # ------------------------------------------------------------------
    # Map file columns → Client fields, set company
    # ------------------------------------------------------------------

    def import_instance(self, instance, row, **kwargs):
        """
        Read the expected columns.  Everything else is ignored.
        Company is always taken from self._default_company (form selection).
        """
        instance.name           = row.get(COL_NAME,    EMPTY_PLACEHOLDER)
        instance.tax_identifier = row.get(COL_TAX,     EMPTY_PLACEHOLDER)
        instance.address        = self._build_address(row)
        instance.phone          = row.get(COL_PHONE,    EMPTY_PLACEHOLDER)

        email_val = row.get(COL_EMAIL, EMPTY_PLACEHOLDER)
        if email_val and email_val != EMPTY_PLACEHOLDER:
            instance.email = email_val

        if self._default_company:
            instance.company = self._default_company

    # ------------------------------------------------------------------
    # Find an existing record or create a new one
    # ------------------------------------------------------------------

    def get_or_init_instance(self, instance_loader, row):
        tax_id = row.get(COL_TAX, EMPTY_PLACEHOLDER)
        if tax_id and tax_id != EMPTY_PLACEHOLDER:
            try:
                return Client.objects.get(
                    tax_identifier=tax_id,
                    company=self._default_company,
                ) if self._default_company else Client.objects.get(
                    tax_identifier=tax_id,
                ), False
            except Client.DoesNotExist:
                pass
            except Client.MultipleObjectsReturned:
                return Client.objects.filter(
                    tax_identifier=tax_id,
                    company=self._default_company,
                ).first() if self._default_company else Client.objects.filter(
                    tax_identifier=tax_id,
                ).first(), False

        name = row.get(COL_NAME, EMPTY_PLACEHOLDER)
        if name and name != EMPTY_PLACEHOLDER and self._default_company:
            try:
                return Client.objects.get(
                    name__iexact=name,
                    company=self._default_company,
                ), False
            except Client.DoesNotExist:
                pass

        return self.init_instance(row), True

    # ------------------------------------------------------------------
    # Skip rows where every cell is empty / placeholder
    # ------------------------------------------------------------------

    def skip_row(self, instance, original, row, import_validation_errors=None):
        name = row.get(COL_NAME, "")
        return not name or name == EMPTY_PLACEHOLDER


# ─────────────────────────────────────────────────────────────────────────────
# Column names expected in the import file (after diacritic-stripping)
# File columns: Número de documento | Fecha | Nombre de cliente | Total | Estado
# ─────────────────────────────────────────────────────────────────────────────

Q_COL_DOC    = "numero de documento"   # Número de documento
Q_COL_DATE   = "fecha"
Q_COL_CLIENT = "nombre de cliente"
Q_COL_TOTAL  = "total"
Q_COL_STATUS = "estado"
Q_EMPTY      = "-"

# Map incoming Spanish display labels → internal choice keys.
# The model stores keys ("draft", "accepted", …) and the admin displays
# their translated labels ("Borrador", "Aceptado", …).
# Both the ETL output labels and the raw choice display names are covered.
_STATUS_LABEL_TO_KEY = {
    "borrador":    "draft",
    "draft":       "draft",
    "ofertado":    "sent",
    "enviado":     "sent",
    "sent":        "sent",
    "aceptado":    "accepted",
    "accepted":    "accepted",
    "rechazado":   "rejected",
    "no aceptado": "rejected",
    "rejected":    "rejected",
    "perdido":     "rejected",
    "cancelado":   "rejected",
}



# Invisible Unicode characters that Excel sometimes embeds in cell text.
# str.strip() does NOT remove these, so they must be stripped explicitly.
_INVISIBLE = frozenset([
    '\u00a0',  # non-breaking space
    '\u00ad',  # soft hyphen
    '\u200b',  # zero-width space
    '\u200c',  # zero-width non-joiner
    '\u200d',  # zero-width joiner
    '\u2060',  # word joiner
    '\ufeff',  # BOM / zero-width no-break space
])


def _norm_header(h):
    """
    Normalise a header string for robust column matching:
      1. Strip invisible Unicode chars that Excel embeds but str.strip() misses.
      2. Lowercase + strip ASCII/Unicode whitespace.
      3. NFKD decomposition to remove diacritics (ñ→n, á→a, etc.).
    """
    if h is None:
        return ""
    s = "".join(c for c in str(h) if c not in _INVISIBLE)
    s = s.strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


class QuoteImportResource(resources.ModelResource):
    """
    Imports Quote records from a file with exactly these columns:
        "Número de documento", "Fecha", "Nombre de cliente", "Total", "Estado"

    - Headers are normalised (lowercase + diacritic-strip) before matching.
    - Empty cells are stored as "-".
    - Company is taken from the form selection (default_company).
    - Client is looked up by name within that company.
    - Status is mapped from Spanish display values to internal keys.
    - Always creates new records (append-only).
    """

    class Meta:
        model = Quote
        import_id_fields = ["id"]
        fields = ("id", "date", "project_name", "status")
        skip_unchanged = False
        report_skipped = True

    def __init__(self, *args, **kwargs):
        self._default_company = kwargs.pop("default_company", None)
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Normalise headers: lowercase + strip diacritics
    # ------------------------------------------------------------------

    def before_import(self, dataset, **kwargs):
        if dataset.headers:
            dataset.headers = [_norm_header(h) for h in dataset.headers]
        super().before_import(dataset, **kwargs)

    # ------------------------------------------------------------------
    # Convert each row's values to strings; replace blanks with "-"
    # ------------------------------------------------------------------

    def before_import_row(self, row, row_number=None, **kwargs):
        for key in list(row.keys()):
            val = row[key]
            if isinstance(val, float):
                val = str(int(val)) if val == int(val) else str(val)
            elif val is None:
                val = ""
            else:
                val = str(val).strip()
            row[key] = val if val else Q_EMPTY

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(val):
        """Parse DD/MM/YYYY, YYYY-MM-DD, and common variants."""
        if not val or val == Q_EMPTY:
            return None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d"):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_money(val):
        """
        Parse a money string to Decimal, handling Spanish number format.
          "1.234,56" → 1234.56   "1234.56" → 1234.56   "1234,56" → 1234.56
        """
        if not val or val == Q_EMPTY:
            return Decimal("0.00")
        s = val.replace("€", "").replace(" ", "").strip()
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")   # "1.234,56"
            else:
                s = s.replace(",", "")                     # "1,234.56"
        elif "," in s:
            s = s.replace(",", ".")                        # "1234,56"
        try:
            return Decimal(s)
        except InvalidOperation:
            return Decimal("0.00")

    # ------------------------------------------------------------------
    # Map file columns → Quote fields
    # ------------------------------------------------------------------

    def import_instance(self, instance, row, **kwargs):
        from djmoney.money import Money

        instance.project_name = row.get(Q_COL_DOC, Q_EMPTY)
        instance.date = self._parse_date(row.get(Q_COL_DATE, Q_EMPTY))
        instance.total_price = Money(self._parse_money(row.get(Q_COL_TOTAL, Q_EMPTY)), "EUR")

        # Map incoming Spanish display label → internal choice key
        status_raw = row.get(Q_COL_STATUS) or ""
        if status_raw and status_raw != Q_EMPTY:
            instance.status = _STATUS_LABEL_TO_KEY.get(_norm_header(status_raw), "draft")
        else:
            instance.status = "draft"

        # Look up client by name within the selected company
        client_name = row.get(Q_COL_CLIENT, Q_EMPTY)
        if client_name and client_name != Q_EMPTY and self._default_company:
            try:
                instance.client = Client.objects.get(
                    name__iexact=client_name,
                    company=self._default_company,
                )
            except Client.DoesNotExist:
                pass
            except Client.MultipleObjectsReturned:
                instance.client = Client.objects.filter(
                    name__iexact=client_name,
                    company=self._default_company,
                ).first()

        if self._default_company:
            instance.company = self._default_company

    # ------------------------------------------------------------------
    # Always create a new record (append-only)
    # ------------------------------------------------------------------

    def get_or_init_instance(self, instance_loader, row):
        return self.init_instance(row), True

    # ------------------------------------------------------------------
    # Skip rows where document number is empty
    # ------------------------------------------------------------------

    def skip_row(self, instance, original, row, import_validation_errors=None):
        doc = row.get(Q_COL_DOC, "")
        return not doc or doc == Q_EMPTY


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Shared column constants for QuoteItem and ConfigurationItem imports
# ─────────────────────────────────────────────────────────────────────────────

# After _norm_header these are the normalised keys we match against.
# The Excel may use any of the aliases listed in _ARTICLE_CODE_ALIASES;
# before_import rewrites them all to QI_COL_REF so the rest of the code
# only ever sees one key.

QI_COL_DOC      = "numero de documento"   # Número de documento → project name = Quote link
QI_COL_REF      = "codigo de articulo"   # canonical key — all article-code aliases collapse here
QI_COL_DESC     = "descripcion"          # canonical key — all description aliases collapse here
QI_COL_QTY      = "cantidad"
QI_COL_PRICE    = "precio unitario"      # canonical key — all price aliases collapse here
QI_COL_DISCOUNT = "descuento"            # canonical key — all discount aliases collapse here
QI_COL_COST     = "coste unitario"       # optional — updates Material.cost_per_unit when present
QI_COL_FAMILY   = "descripcion familia"  # optional — updates Material.family when present

_DISCOUNT_ALIASES = frozenset([
    "descuento",
    "discount",
    "dto",
    "dto.",
    "desc",
    "rebaja",
])

_COST_ALIASES = frozenset([
    "coste unitario",
    "coste",
    "cost",
    "costo",
    "precio coste",
    "precio de coste",
    "coste/ud",
    "coste ud",
])

_FAMILY_ALIASES = frozenset([
    "descripcion familia",
    "desc familia",
    "familia",
    "family",
    "grupo articulo",
    "grupo",
    "subfamilia",
    "categoria",      # ETL output column name after diacritic-strip
    "category",
])

# All known header variants that mean "article / material code".
_ARTICLE_CODE_ALIASES = frozenset([
    "codigo de articulo",
    "codigo articulo",
    "cod articulo",
    "cod. articulo",
    "referencia",
    "ref",
    "ref.",
    "codigo",
    "code",
    "article code",
    "item code",
])

# All known header variants that mean "description / article name".
_DESC_ALIASES = frozenset([
    "descripcion",
    "nombre articulo",
    "nombre de articulo",
    "nombre",
    "description",
    "detalle",
    "concepto",
    "articulo",
])

# All known header variants that mean "unit price / price".
_PRICE_ALIASES = frozenset([
    "precio unitario",
    "precio",
    "price",
    "importe",
    "pvp",
    "valor",
    "amount",
    "total",
])

# ─────────────────────────────────────────────────────────────────────────────
# Shared helper: quote lookup (used by both item resources)
# ─────────────────────────────────────────────────────────────────────────────

def _find_quote(company, doc_norm, cache):
    """Return the Quote whose project_name normalises to doc_norm, or None."""
    if doc_norm in cache:
        return cache[doc_norm]
    quote = None
    if company:
        for q in Quote.objects.filter(company=company):
            if _norm_header(q.project_name) == doc_norm:
                quote = q
                break
    cache[doc_norm] = quote
    return quote


class QuoteItemImportResource(resources.ModelResource):
    """
    Imports QuoteItem records.

    Expected columns (header names are case-insensitive, diacritic-tolerant):
        Número de documento | Código de artículo | Descripción | Cantidad | Precio unitario

    "Código de artículo" also accepts: Referencia, Ref, Código, Code, …
    (see _ARTICLE_CODE_ALIASES for the full list)

    Workflow per row:
    - Find the Quote by project_name match within the selected company.
    - Find Material by reference_code; auto-create it if missing.
    - Upsert: update existing QuoteItem for (quote, material) or create new.
    """

    class Meta:
        model = QuoteItem
        import_id_fields = ["id"]
        fields = ("id", "quantity")
        skip_unchanged = False
        report_skipped = True

    def __init__(self, *args, **kwargs):
        self._default_company = kwargs.pop("default_company", None)
        self._quote_cache    = {}
        self._material_cache = {}
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Normalise headers — collapse all aliases to canonical keys
    # ------------------------------------------------------------------

    def before_import(self, dataset, **kwargs):
        if dataset.headers:
            def _canonical(h):
                n = _norm_header(h)
                if n in _ARTICLE_CODE_ALIASES:
                    return QI_COL_REF
                if n in _DESC_ALIASES:
                    return QI_COL_DESC
                if n in _PRICE_ALIASES:
                    return QI_COL_PRICE
                if n in _DISCOUNT_ALIASES:
                    return QI_COL_DISCOUNT
                if n in _COST_ALIASES:
                    return QI_COL_COST
                if n in _FAMILY_ALIASES:
                    return QI_COL_FAMILY
                return n
            dataset.headers = [_canonical(h) for h in dataset.headers]

        # Pre-warm quote cache: 1 query for all quotes in the company
        if self._default_company:
            for q in Quote.objects.filter(company=self._default_company):
                key = _norm_header(q.project_name)
                self._quote_cache[key] = q

        # Pre-warm material cache: 1 query for all materials in the company
        from materials.models import Material
        if self._default_company:
            for m in Material.objects.filter(company=self._default_company):
                self._material_cache[m.reference_code] = m

        super().before_import(dataset, **kwargs)

    # ------------------------------------------------------------------
    # Normalise cell values
    # ------------------------------------------------------------------

    def before_import_row(self, row, row_number=None, **kwargs):
        for key in list(row.keys()):
            val = row[key]
            if isinstance(val, float):
                val = str(int(val)) if val == int(val) else str(val)
            elif val is None:
                val = ""
            else:
                val = str(val).strip()
            row[key] = val if val else Q_EMPTY

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_quote(self, row):
        doc_raw = row.get(QI_COL_DOC, Q_EMPTY)
        if not doc_raw or doc_raw == Q_EMPTY:
            return None
        return _find_quote(self._default_company, _norm_header(doc_raw), self._quote_cache)

    def _get_import_category(self):
        """Get or create a fallback 'Importado' category for the default company."""
        if hasattr(self, "_import_category_cache"):
            return self._import_category_cache
        from materials.models import Category
        category, _ = Category.objects.get_or_create(
            name="Importado",
            company=self._default_company,
        )
        self._import_category_cache = category
        return category

    def _get_or_create_material(self, row):
        """Return (or create) the Material identified by the article code."""
        ref = row.get(QI_COL_REF, Q_EMPTY).strip()
        if not ref or ref == Q_EMPTY:
            return None
        if ref in self._material_cache:
            return self._material_cache[ref]

        from materials.models import Material, UnitChoices
        material = Material.objects.filter(reference_code=ref).first()
        if not material:
            desc = row.get(QI_COL_DESC, Q_EMPTY)
            name = desc if desc and desc != Q_EMPTY else ref
            from djmoney.money import Money
            material = Material.objects.create(
                reference_code=ref,
                name=name,
                company=self._default_company,
                unit_type=UnitChoices.UNITS,
                cost_per_unit=Money(0, "EUR"),
                category=self._get_import_category(),
                provider=None,
            )
        self._material_cache[ref] = material
        return material

    # ------------------------------------------------------------------
    # Find existing QuoteItem (quote + material) or init a new one
    # ------------------------------------------------------------------

    def get_or_init_instance(self, instance_loader, row):
        quote    = self._get_quote(row)
        material = self._get_or_create_material(row)
        if quote and material:
            try:
                return QuoteItem.objects.get(quote=quote, material=material), False
            except QuoteItem.DoesNotExist:
                pass
        return self.init_instance(row), True

    # ------------------------------------------------------------------
    # Map file columns → QuoteItem fields
    # ------------------------------------------------------------------

    def import_instance(self, instance, row, **kwargs):
        from djmoney.money import Money

        quote    = self._get_quote(row)
        material = self._get_or_create_material(row)

        if quote:
            instance.quote = quote
        if material:
            instance.material = material

        qty_raw = row.get(QI_COL_QTY, Q_EMPTY)
        try:
            instance.quantity = Decimal(
                qty_raw.replace(",", ".") if qty_raw and qty_raw != Q_EMPTY else "1"
            )
        except InvalidOperation:
            instance.quantity = Decimal("1")

        instance.unit_price = Money(
            QuoteImportResource._parse_money(row.get(QI_COL_PRICE, Q_EMPTY)), "EUR"
        )

        disc_raw = row.get(QI_COL_DISCOUNT, Q_EMPTY)
        try:
            instance.discount = Decimal(
                disc_raw.replace(",", ".").replace("%", "").strip()
                if disc_raw and disc_raw != Q_EMPTY else "0"
            )
        except InvalidOperation:
            instance.discount = Decimal("0")

        # If a cost column is present, update the material's catalogue cost.
        cost_raw = row.get(QI_COL_COST, Q_EMPTY)
        if cost_raw and cost_raw != Q_EMPTY and instance.material_id:
            from djmoney.money import Money
            cost_val = QuoteImportResource._parse_money(cost_raw)
            if cost_val > 0:
                instance.material.cost_per_unit = Money(cost_val, "EUR")
                instance.material.save(update_fields=["cost_per_unit"])

        # If a family column is present, find/create the MaterialFamily and set it.
        family_raw = row.get(QI_COL_FAMILY, Q_EMPTY)
        if family_raw and family_raw != Q_EMPTY and instance.material_id:
            from materials.models import MaterialFamily
            family, _ = MaterialFamily.objects.get_or_create(
                name=family_raw.strip(),
                company=self._default_company,
            )
            instance.material.family = family
            instance.material.save(update_fields=["family"])

    # ------------------------------------------------------------------
    # Skip rows with no document number or no article code
    # ------------------------------------------------------------------

    def skip_row(self, instance, original, row, import_validation_errors=None):
        doc = row.get(QI_COL_DOC, "")
        ref = row.get(QI_COL_REF, "")
        if (not doc or doc == Q_EMPTY) or (not ref or ref == Q_EMPTY):
            return True
        # Skip if the quote doesn't exist in the DB — avoids null quote_id errors
        if self._get_quote(row) is None:
            return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# ConfigurationItem import
# File columns: Número de documento | Código de configuración | Código de artículo
#               | Descripción | Cantidad | Precio unitario
#
# Each unique (document, config_code) pair maps to one Configuration record
# linked to the Quote.  Each row creates/updates a ConfigurationItem inside it.
# ─────────────────────────────────────────────────────────────────────────────

CI_COL_DOC        = "numero de documento"
CI_COL_CONFIG     = "codigo de configuracion"   # Código de configuración
CI_COL_COMP_CODE  = "codigo de articulo"        # unified via _ARTICLE_CODE_ALIASES
CI_COL_DESC       = "descripcion"
CI_COL_QTY        = "cantidad"
CI_COL_PRICE      = "precio unitario"

# Config-code aliases (e.g. "Ref. configuración", "Código config", …)
_CONFIG_CODE_ALIASES = frozenset([
    "codigo de configuracion",
    "codigo configuracion",
    "cod configuracion",
    "cod. configuracion",
    "config code",
    "configuration code",
    "config",
])


class ConfigurationItemImportResource(resources.ModelResource):
    """
    Imports ConfigurationItem records (tailored/configured items per quote).

    Expected columns:
        Número de documento | Código de configuración | Código de artículo
        | Descripción | Cantidad | Precio unitario

    Workflow per row:
    - Find Quote by project_name within selected company.
    - Find or create the Configuration for (quote, config_code).
      The Configuration is created with status=draft and linked to the quote.
    - Find or create the Component by code.
    - Upsert ConfigurationItem for (configuration, component).
    """

    class Meta:
        model = ConfigurationItem
        import_id_fields = ["id"]
        fields = ("id", "quantity")
        skip_unchanged = False
        report_skipped = True

    def __init__(self, *args, **kwargs):
        self._default_company  = kwargs.pop("default_company", None)
        self._quote_cache      = {}   # doc_norm → Quote | None
        self._config_cache     = {}   # (doc_norm, config_code) → Configuration | None
        self._component_cache  = {}   # code → Component
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Normalise headers
    # ------------------------------------------------------------------

    def before_import(self, dataset, **kwargs):
        if dataset.headers:
            def _canonical(h):
                norm = _norm_header(h)
                if norm in _ARTICLE_CODE_ALIASES:
                    return CI_COL_COMP_CODE
                if norm in _CONFIG_CODE_ALIASES:
                    return CI_COL_CONFIG
                if norm in _DESC_ALIASES:
                    return CI_COL_DESC
                if norm in _PRICE_ALIASES:
                    return CI_COL_PRICE
                return norm
            dataset.headers = [_canonical(h) for h in dataset.headers]

        # Pre-warm quote cache: 1 query for all quotes in the company
        if self._default_company:
            for q in Quote.objects.filter(company=self._default_company):
                self._quote_cache[_norm_header(q.project_name)] = q

        # Pre-warm component cache: 1 query for all components in the company
        from configurator.models import Component
        if self._default_company:
            for c in Component.objects.filter(company=self._default_company):
                if c.code:
                    self._component_cache[c.code] = c

        super().before_import(dataset, **kwargs)

    # ------------------------------------------------------------------
    # Normalise cell values
    # ------------------------------------------------------------------

    def before_import_row(self, row, row_number=None, **kwargs):
        for key in list(row.keys()):
            val = row[key]
            if isinstance(val, float):
                val = str(int(val)) if val == int(val) else str(val)
            elif val is None:
                val = ""
            else:
                val = str(val).strip()
            row[key] = val if val else Q_EMPTY

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_quote(self, row):
        doc_raw = row.get(CI_COL_DOC, Q_EMPTY)
        if not doc_raw or doc_raw == Q_EMPTY:
            return None
        return _find_quote(self._default_company, _norm_header(doc_raw), self._quote_cache)

    def _get_or_create_configuration(self, row):
        """Find or create the Configuration for this (quote, config_code) pair."""
        from configurator.models import ProductTemplate

        quote = self._get_quote(row)
        if not quote:
            return None

        config_code = row.get(CI_COL_CONFIG, Q_EMPTY).strip()
        if not config_code or config_code == Q_EMPTY:
            return None

        cache_key = (_norm_header(row.get(CI_COL_DOC, "")), config_code)
        if cache_key in self._config_cache:
            return self._config_cache[cache_key]

        # Try to find an existing Configuration on this quote with this name
        config = Configuration.objects.filter(
            quote=quote,
            name=config_code,
        ).first()

        if not config:
            # Get or create a minimal ProductTemplate to satisfy the NOT NULL FK
            template, _ = ProductTemplate.objects.get_or_create(
                company=self._default_company,
                name=config_code,
                defaults={"show_in_configurator": False},
            )
            config = Configuration.objects.create(
                company=self._default_company,
                client=quote.client,
                quote=quote,
                product=template,
                name=config_code,
                status=ConfigurationStatus.DRAFT,
            )

        self._config_cache[cache_key] = config
        return config

    def _get_or_create_component(self, row):
        """Find or create the Component identified by article code."""
        from materials.models import Component

        code = row.get(CI_COL_COMP_CODE, Q_EMPTY).strip()
        if not code or code == Q_EMPTY:
            return None
        if code in self._component_cache:
            return self._component_cache[code]

        component = Component.objects.filter(
            code=code,
            company=self._default_company,
        ).first()
        if not component:
            desc = row.get(CI_COL_DESC, Q_EMPTY)
            name = desc if desc and desc != Q_EMPTY else code
            component = Component.objects.create(
                code=code,
                name=name,
                company=self._default_company,
            )
        self._component_cache[code] = component
        return component

    # ------------------------------------------------------------------
    # Find existing ConfigurationItem or init a new one
    # ------------------------------------------------------------------

    def get_or_init_instance(self, instance_loader, row):
        config    = self._get_or_create_configuration(row)
        component = self._get_or_create_component(row)
        if config and component:
            try:
                return ConfigurationItem.objects.get(
                    configuration=config, component=component
                ), False
            except ConfigurationItem.DoesNotExist:
                pass
        return self.init_instance(row), True

    # ------------------------------------------------------------------
    # Map file columns → ConfigurationItem fields
    # ------------------------------------------------------------------

    def import_instance(self, instance, row, **kwargs):
        from djmoney.money import Money

        config    = self._get_or_create_configuration(row)
        component = self._get_or_create_component(row)

        if config:
            instance.configuration = config
        if component:
            instance.component = component

        qty_raw = row.get(CI_COL_QTY, Q_EMPTY)
        try:
            instance.quantity = Decimal(
                qty_raw.replace(",", ".") if qty_raw and qty_raw != Q_EMPTY else "1"
            )
        except InvalidOperation:
            instance.quantity = Decimal("1")

        instance.unit_price = Money(
            QuoteImportResource._parse_money(row.get(CI_COL_PRICE, Q_EMPTY)), "EUR"
        )
        instance.is_template_item = False   # user-added, not from a BOM rule

    # ------------------------------------------------------------------
    # Skip rows missing document number, config code, or article code
    # ------------------------------------------------------------------

    def skip_row(self, instance, original, row, import_validation_errors=None):
        doc    = row.get(CI_COL_DOC, "")
        config = row.get(CI_COL_CONFIG, "")
        comp   = row.get(CI_COL_COMP_CODE, "")
        return (
            (not doc    or doc    == Q_EMPTY) or
            (not config or config == Q_EMPTY) or
            (not comp   or comp   == Q_EMPTY)
        )


class QuoteExportResource(resources.ModelResource):
    """Read-only resource used only for exporting Quotes."""

    client_name = fields.Field(column_name=_("Client"))
    client_email = fields.Field(column_name=_("Client Email"))
    client_phone = fields.Field(column_name=_("Client Phone"))
    project_name_field = fields.Field(column_name=_("Project Name"), attribute="project_name")
    status_field = fields.Field(column_name=_("Status"), attribute="status")
    subtotal_amount = fields.Field(column_name=_("Subtotal"))
    subtotal_currency = fields.Field(column_name=_("Subtotal Currency"))
    discount_amount_value = fields.Field(column_name=_("Discount Amount"))
    discounted_subtotal_value = fields.Field(column_name=_("Discounted Subtotal"))
    cost_amount = fields.Field(column_name=_("Total cost"))
    cost_currency = fields.Field(column_name=_("Cost Currency"))
    margin_eur = fields.Field(column_name=_("Margin (€)"))
    margin_pct = fields.Field(column_name=_("Margin (%)"))
    tax_amount_value = fields.Field(column_name=_("Tax Amount"))
    tax_currency = fields.Field(column_name=_("Tax Currency"))
    total_amount = fields.Field(column_name=_("Total"))
    total_currency = fields.Field(column_name=_("Total Currency"))
    date_sent_field = fields.Field(column_name=_("Date Sent"), attribute="date_sent")
    date_accepted_field = fields.Field(column_name=_("Date Accepted"), attribute="date_accepted")
    date_rejected_field = fields.Field(column_name=_("Date Rejected"), attribute="date_rejected")
    expires_at_field = fields.Field(column_name=_("Expires At"), attribute="expires_at")
    created_at_field = fields.Field(column_name=_("Created At"), attribute="created_at")
    company_name = fields.Field(column_name=_("Company"))

    class Meta:
        from .models import Quote
        model = Quote
        fields = (
            "id",
            "client_name",
            "client_email",
            "client_phone",
            "project_name_field",
            "status_field",
            "subtotal_amount",
            "subtotal_currency",
            "discount_amount_value",
            "discounted_subtotal_value",
            "cost_amount",
            "cost_currency",
            "margin_eur",
            "margin_pct",
            "date_sent_field",
            "date_accepted_field",
            "date_rejected_field",
            "tax_amount_value",
            "tax_currency",
            "total_amount",
            "total_currency",
            "expires_at_field",
            "created_at_field",
            "company_name",
        )
        export_order = fields

    # Map field names to Excel number format codes.
    # Used by QuoteAdmin.get_export_data to apply native cell formatting.
    COLUMN_FORMATS = {
        "subtotal_amount": '#,##0.00',
        "discount_amount_value": '#,##0.00',
        "discounted_subtotal_value": '#,##0.00',
        "cost_amount": '#,##0.00',
        "margin_eur": '#,##0.00',
        "margin_pct": '0.0%',
        "tax_amount_value": '#,##0.00',
        "total_amount": '#,##0.00',
    }

    def get_queryset(self):
        return super().get_queryset().select_related("client", "company")

    def dehydrate_client_name(self, quote):
        return quote.client.name if quote.client else ""

    def dehydrate_client_email(self, quote):
        return quote.client.email if quote.client else ""

    def dehydrate_client_phone(self, quote):
        return quote.client.phone if quote.client else ""

    def dehydrate_status_field(self, quote):
        return quote.get_status_display()

    def dehydrate_subtotal_amount(self, quote):
        return float(quote.subtotal_price.amount) if quote.subtotal_price else 0.0

    def dehydrate_subtotal_currency(self, quote):
        return str(quote.subtotal_price.currency) if quote.subtotal_price else "EUR"

    def dehydrate_discount_amount_value(self, quote):
        return float(quote.discount_amount.amount) if quote.discount_amount else 0.0

    def dehydrate_discounted_subtotal_value(self, quote):
        return float(quote.discounted_subtotal.amount) if quote.discounted_subtotal else 0.0

    def dehydrate_cost_amount(self, quote):
        return float(quote.total_cost.amount) if quote.total_cost else 0.0

    def dehydrate_cost_currency(self, quote):
        return str(quote.total_cost.currency) if quote.total_cost else "EUR"

    def dehydrate_margin_eur(self, quote):
        revenue = float(quote.discounted_subtotal.amount) if quote.discounted_subtotal else 0.0
        cost = float(quote.total_cost.amount) if quote.total_cost else 0.0
        return round(revenue - cost, 2)

    def dehydrate_margin_pct(self, quote):
        revenue = float(quote.discounted_subtotal.amount) if quote.discounted_subtotal else 0.0
        cost = float(quote.total_cost.amount) if quote.total_cost else 0.0
        if cost:
            return round((revenue - cost) / cost, 4)  # 0.145 = 14.5%
        return 0.0

    def dehydrate_tax_amount_value(self, quote):
        return float(quote.tax_amount.amount) if quote.tax_amount else 0.0

    def dehydrate_tax_currency(self, quote):
        return str(quote.tax_amount.currency) if quote.tax_amount else "EUR"

    def dehydrate_total_amount(self, quote):
        return float(quote.total_price.amount) if quote.total_price else 0.0

    def dehydrate_total_currency(self, quote):
        return str(quote.total_price.currency) if quote.total_price else "EUR"

    def dehydrate_company_name(self, quote):
        return quote.company.name if quote.company else ""
