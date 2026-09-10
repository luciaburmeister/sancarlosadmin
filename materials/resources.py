import unicodedata

from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget, DecimalWidget, CharWidget
from .models import Material, Category, PaymentMethod, Provider
from company.models import Company
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import translation
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money
from decimal import Decimal, InvalidOperation


class MoneyAmountWidget(DecimalWidget):
    """Widget that handles Money objects for the amount portion of a MoneyField."""

    def render(self, value, obj=None, **kwargs):
        if isinstance(value, Money):
            return super().render(value.amount, obj, **kwargs)
        return super().render(value, obj, **kwargs)

    def clean(self, value, row=None, **kwargs):
        if isinstance(value, Money):
            return value.amount
        if value is None or str(value).strip() == "":
            return None
        return super().clean(value, row, **kwargs)


def _normalize(value):
    """Lowercase, trim and strip diacritics so Excel/CSV text matches reliably."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


class PaymentMethodWidget(CharWidget):
    """Exports the readable label and accepts either that label or the stored code.

    Labels are matched in every configured language, so a file exported in
    Spanish still imports from an English session (and the other way round).
    """

    def render(self, value, obj=None, **kwargs):
        if not value:
            return ""
        try:
            return str(PaymentMethod(value).label)
        except ValueError:
            return str(value)

    def clean(self, value, row=None, **kwargs):
        text = _normalize(value)
        if not text:
            return None
        lookup = self._lookup()
        if text not in lookup:
            raise ValidationError(
                _("“%(value)s” is not a valid payment method. Valid values are: %(valid)s")
                % {
                    "value": value,
                    "valid": ", ".join(str(label) for label in PaymentMethod.labels),
                }
            )
        return lookup[text]

    @staticmethod
    def _lookup():
        lookup = {}
        languages = {code for code, _name in getattr(settings, "LANGUAGES", ())}
        languages.add(translation.get_language() or "en")
        languages.add("en")
        for method in PaymentMethod:
            lookup[_normalize(method.value)] = method.value
            for language in languages:
                with translation.override(language):
                    lookup[_normalize(method.label)] = method.value
        return lookup


class MaterialResource(resources.ModelResource):
    category = fields.Field(
        column_name="category",
        attribute="category",
        widget=ForeignKeyWidget(Category, "name"),
    )
    provider = fields.Field(
        column_name="provider",
        attribute="provider",
        widget=ForeignKeyWidget(Provider, "name"),
    )
    company = fields.Field(
        column_name="company",
        attribute="company",
        widget=ForeignKeyWidget(Company, "name"),
    )
    # Explicit fields for the MoneyField's two DB columns so preview works
    cost_per_unit = fields.Field(
        column_name="cost_per_unit",
        attribute="cost_per_unit",
        widget=MoneyAmountWidget(),
    )
    cost_per_unit_currency = fields.Field(
        column_name="cost_per_unit_currency",
        attribute="cost_per_unit_currency",
        widget=CharWidget(),
    )
    # Virtual export-only fields for the split cost columns
    cost_per_unit_value = fields.Field(column_name="cost_per_unit_value", readonly=True)
    currency = fields.Field(column_name="currency", readonly=True)

    class Meta:
        model = Material
        fields = (
            "reference_code",
            "name",
            "rule_key",
            "category",
            "unit_type",
            "cost_per_unit",
            "cost_per_unit_currency",
            "cost_per_unit_value",
            "currency",
            "provider",
            "company",
        )
        export_order = (
            "reference_code",
            "name",
            "rule_key",
            "category",
            "unit_type",
            "cost_per_unit_value",
            "currency",
            "provider",
            "company",
        )
        import_id_fields = ("reference_code",)

    def before_import_row(self, row, **kwargs):
        """Map the split cost columns back to the MoneyField DB columns."""
        # Prefer cost_per_unit_value (export format), fall back to cost_per_unit
        cost = row.get("cost_per_unit_value") or row.get("cost_per_unit")
        curr = row.get("currency") or row.get("cost_per_unit_currency") or "EUR"

        if cost is not None:
            row["cost_per_unit"] = str(cost)
            row["cost_per_unit_currency"] = str(curr)

    def before_save_instance(self, instance, row, **kwargs):
        """
        Construct a proper Money object for the MoneyField.
        django-import-export sets the raw DB columns but MoneyField needs
        a Money object for the descriptor to work correctly.
        """
        cost = row.get("cost_per_unit")
        curr = row.get("cost_per_unit_currency") or "EUR"

        if cost is not None:
            try:
                amount = Decimal(str(cost))
            except (InvalidOperation, ValueError):
                amount = Decimal("0")
            instance.cost_per_unit = Money(amount, str(curr))

    def dehydrate_cost_per_unit_value(self, material):
        return material.cost_per_unit.amount if material.cost_per_unit else 0

    def dehydrate_currency(self, material):
        return str(material.cost_per_unit.currency) if material.cost_per_unit else "EUR"


class PriceSheetItemResource(resources.ModelResource):
    """Export resource for Price Sheet items — always uses the live price."""

    price_sheet_name = fields.Field(column_name=_("Price Sheet"), readonly=True)
    reference = fields.Field(column_name=_("Reference"), readonly=True)
    name = fields.Field(column_name=_("Name"), readonly=True)
    item_type = fields.Field(column_name=_("Type"), readonly=True)
    unit = fields.Field(column_name=_("Unit"), readonly=True)
    unit_price = fields.Field(column_name=_("Unit Price"), readonly=True)
    currency = fields.Field(column_name=_("Currency"), readonly=True)
    provider = fields.Field(column_name=_("Provider"), readonly=True)
    category = fields.Field(column_name=_("Category"), readonly=True)

    class Meta:
        from .models import PriceSheetItem as _Model
        model = _Model
        fields = (
            "price_sheet_name",
            "reference",
            "name",
            "item_type",
            "unit",
            "unit_price",
            "currency",
            "provider",
            "category",
        )
        export_order = fields

    def dehydrate_price_sheet_name(self, item):
        return item.price_sheet.name

    def dehydrate_reference(self, item):
        return item.resolved_reference

    def dehydrate_name(self, item):
        return item.resolved_name

    def dehydrate_item_type(self, item):
        return str(item.item_type)

    def dehydrate_unit(self, item):
        return item.resolved_unit_type

    def dehydrate_unit_price(self, item):
        price = item.resolved_unit_price
        return price.amount if price else 0

    def dehydrate_currency(self, item):
        price = item.resolved_unit_price
        return str(price.currency) if price else "EUR"

    def dehydrate_provider(self, item):
        return item.resolved_provider

    def dehydrate_category(self, item):
        return item.resolved_category


class ProviderResource(resources.ModelResource):
    """Exports Providers and re-imports the exported file to update payment methods.

    Everything except ``payment_method`` is export-only, so a file that was
    exported, edited and imported back can only ever change payment methods —
    names, tax identifiers and balances are never overwritten by accident.
    Rows are matched on ``id``, and rows whose id doesn't belong to a provider
    the user can see are reported as errors instead of creating new providers.
    """

    # Header aliases accepted for the two columns the import reads.
    _ID_ALIASES = frozenset(["id", "pk"])
    _PAYMENT_METHOD_ALIASES = frozenset([
        "payment method",
        "payment_method",
        "metodo de pago",
        "forma de pago",
    ])

    name = fields.Field(column_name=_("Name"), attribute="name", readonly=True)
    contact = fields.Field(column_name=_("Contact"), attribute="contact", readonly=True)
    phone = fields.Field(column_name=_("Phone"), attribute="phone", readonly=True)
    email = fields.Field(column_name=_("Email"), attribute="email", readonly=True)
    tax_identifier = fields.Field(column_name=_("Tax identifier"), attribute="tax_identifier", readonly=True)
    associated_emails = fields.Field(column_name=_("Associated Emails"), readonly=True)
    account = fields.Field(column_name=_("Accounting Account"), readonly=True)
    default_cost_center = fields.Field(column_name=_("Default Cost Center"), readonly=True)
    payment_method = fields.Field(
        column_name=_("Payment Method"),
        attribute="payment_method",
        widget=PaymentMethodWidget(),
    )
    company = fields.Field(column_name=_("Company"), readonly=True)
    balance_owed = fields.Field(column_name=_("Balance owed"), readonly=True)
    balance_overdue = fields.Field(column_name=_("Overdue"), readonly=True)
    balance_not_due = fields.Field(column_name=_("Not overdue"), readonly=True)

    class Meta:
        model = Provider
        fields = (
            "id",
            "name",
            "contact",
            "phone",
            "email",
            "tax_identifier",
            "associated_emails",
            "account",
            "default_cost_center",
            "payment_method",
            "company",
            "balance_overdue",
            "balance_not_due",
            "balance_owed",
        )
        export_order = fields
        import_id_fields = ("id",)
        skip_unchanged = True
        report_skipped = True

    def __init__(self, *args, **kwargs):
        self._allowed_companies = kwargs.pop("allowed_companies", None)
        super().__init__(*args, **kwargs)

    def dehydrate_associated_emails(self, provider):
        return ", ".join(provider.associated_emails or [])

    def dehydrate_account(self, provider):
        return str(provider.account) if provider.account else ""

    def dehydrate_default_cost_center(self, provider):
        return str(provider.default_cost_center) if provider.default_cost_center else ""

    def dehydrate_company(self, provider):
        return provider.company.name if provider.company else ""

    def get_queryset(self):
        # Used when the admin doesn't pass its (already annotated) queryset, and
        # on import to look up the row's provider — hence the company scoping.
        from invoices.models import Invoice
        from payments.mixins import (
            annotate_outstanding_balance,
            annotate_split_outstanding_balance,
        )

        qs = super().get_queryset().select_related("company", "account")
        if self._allowed_companies is not None:
            qs = qs.filter(company__in=self._allowed_companies)
        # Add total balance first, then split it
        qs = annotate_outstanding_balance(
            qs, Invoice, "provider", "balance_owed",
        )
        # Add the split (overdue / not due)
        return annotate_split_outstanding_balance(
            qs, Invoice, "provider",
        )

    def before_import(self, dataset, **kwargs):
        """Accept the header spellings of any language / a hand-made file."""
        if dataset.headers:
            id_column = str(self.fields["id"].column_name)
            payment_column = str(self.fields["payment_method"].column_name)
            headers = []
            for header in dataset.headers:
                normalized = _normalize(header)
                if normalized in self._ID_ALIASES:
                    headers.append(id_column)
                elif (
                    normalized in self._PAYMENT_METHOD_ALIASES
                    or normalized == _normalize(payment_column)
                ):
                    headers.append(payment_column)
                else:
                    headers.append(header)
            dataset.headers = headers
        super().before_import(dataset, **kwargs)

    def before_import_row(self, row, **kwargs):
        """Drop a blank payment method so the provider keeps its current one."""
        column = self.fields["payment_method"].column_name
        if column in row and not _normalize(row[column]):
            del row[column]

    def get_or_init_instance(self, instance_loader, row):
        instance, new = super().get_or_init_instance(instance_loader, row)
        if new:
            raise ValidationError(
                _(
                    "No provider found with id “%(id)s”. The import only updates "
                    "existing providers, so export them first and edit that file."
                )
                % {"id": row.get(self.fields["id"].column_name, "")}
            )
        return instance, new

    def dehydrate_balance_owed(self, provider):
        return float(getattr(provider, "balance_owed", 0) or 0)

    def dehydrate_balance_overdue(self, provider):
        return float(getattr(provider, "balance_overdue", 0) or 0)

    def dehydrate_balance_not_due(self, provider):
        return float(getattr(provider, "balance_not_due", 0) or 0)
