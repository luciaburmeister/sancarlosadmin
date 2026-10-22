from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import CharField, DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce, NullIf
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from import_export import fields, resources, widgets
from import_export.admin import ExportActionModelAdmin
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter, ChoicesDropdownFilter, RangeDateFilter,
)
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from unfold.decorators import action
from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.xlsx_export import FormattedXlsxExportMixin
from accounting.models import JournalEntry, JournalEntryLine, EntryStatus

class JournalEntryLineInline(TabularInline):
    model = JournalEntryLine
    extra = 0
    autocomplete_fields = ["account"]
    fields = ["account", "description", "debit", "credit"]
    
    # We might want to make it readonly if entry is posted?
    # But CompanyRestrictedAdmin or ModelAdmin usually handle this?
    # We implemented validation in clean(), showing error is fine.
    
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
             return [f.name for f in self.model._meta.fields]
        return super().get_readonly_fields(request, obj)
    
    def has_add_permission(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(JournalEntry)
class JournalEntryAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = [
        "number", "date", "journal", "company", "status",
        "total_debit_display", "total_credit_display", "is_balanced_display",
    ]
    list_filter = [
        ("company", AutocompleteSelectMultipleFilter),
        ("journal", AutocompleteSelectMultipleFilter),
        ("lines__account", AutocompleteSelectMultipleFilter),
        ("sales_invoice", AutocompleteSelectMultipleFilter),
        ("purchase_invoice", AutocompleteSelectMultipleFilter),
        ("payment", AutocompleteSelectMultipleFilter),
        ("status", ChoicesDropdownFilter),
        ("date", RangeDateFilter),
        "is_opening",
    ]
    list_filter_submit = True
    search_fields = ["number", "reference"]
    
    ordering = ["-date", "-number"]
    list_select_related = ["company", "journal"]
    autocomplete_fields = ["company", "journal", "purchase_invoice", "sales_invoice", "payment"]
    date_hierarchy = "date"
    
    inlines = [JournalEntryLineInline]
    
    readonly_fields = ["posted_at", "posted_by", "number"]

    def get_queryset(self, request):
        # The totals in one query for the whole page, not two per entry; a
        # subquery each so a filter on lines__account can't fan them out
        def line_total(field):
            lines = (
                JournalEntryLine.objects.filter(entry=OuterRef("pk"))
                .values("entry").annotate(total=Sum(field)).values("total")
            )
            output = DecimalField(max_digits=19, decimal_places=4)
            return Coalesce(Subquery(lines, output_field=output), Value(0), output_field=output)

        return super().get_queryset(request).annotate(
            debit_total=line_total("debit"), credit_total=line_total("credit"),
        )

    @admin.display(description=_("Total debit"), ordering="debit_total")
    def total_debit_display(self, obj):
        return obj.debit_total

    @admin.display(description=_("Total credit"), ordering="credit_total")
    def total_credit_display(self, obj):
        return obj.credit_total

    @admin.display(description=_("Balanced"), boolean=True)
    def is_balanced_display(self, obj):
        return obj.debit_total == obj.credit_total

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.status == EntryStatus.POSTED:
            # Lock everything except maybe reference?
            return [f.name for f in self.model._meta.fields]
        return readonly

    actions = ["post_selected"]
    actions_detail = ["post_detail_action"]

    def has_post_journalentry_permission(self, request, object_id=None):
        return request.user.has_perm("accounting.change_journalentry")

    @staticmethod
    def _post(request, entry):
        """Post one entry in its own savepoint; the ledger's refusal as a message, or None."""
        from company.models import CompanyUser

        posted_by = CompanyUser.objects.filter(user=request.user, company_id=entry.company_id).first()
        try:
            with transaction.atomic():
                entry.post(user=posted_by)
        except ValidationError as e:
            return " ".join(e.messages)
        return None

    @admin.action(description=_("Post selected entries"), permissions=["post_journalentry"])
    def post_selected(self, request, queryset):
        posted, skipped, failed = [], 0, []
        for entry in queryset.select_related("company", "journal").order_by("date", "pk"):
            if entry.status != EntryStatus.DRAFT:
                skipped += 1
                continue
            error = self._post(request, entry)
            if error:
                failed.append(f"{entry.reference or entry.pk}: {error}")
            else:
                posted.append(entry.number)
        if posted:
            self.message_user(request, _("%(count)s entries posted.") % {"count": len(posted)})
        if skipped:
            self.message_user(
                request, _("%(count)s were not drafts and were left as they are.") % {"count": skipped},
                level=messages.WARNING,
            )
        for message in failed[:10]:
            self.message_user(request, message, level=messages.ERROR)
        if len(failed) > 10:
            self.message_user(
                request, _("…and %(count)s more could not be posted.") % {"count": len(failed) - 10},
                level=messages.ERROR,
            )

    @action(description=_("Post"), icon="task_alt", permissions=["post_journalentry"])
    def post_detail_action(self, request, object_id):
        entry = get_object_or_404(self.get_queryset(request), pk=object_id)
        if entry.status != EntryStatus.DRAFT:
            self.message_user(request, _("Only draft entries can be posted."), level=messages.WARNING)
        else:
            error = self._post(request, entry)
            if error:
                self.message_user(request, error, level=messages.ERROR)
            else:
                self.message_user(request, _("Journal entry %s posted.") % entry.number)
        return redirect(reverse("admin:accounting_journalentry_change", args=[object_id]))

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    def get_deleted_objects(self, objs, request):
        # Lines go with their entry: the line admin is read-only, which
        # mustn't block deleting the entry they belong to
        deleted, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        perms_needed.discard(JournalEntryLine._meta.verbose_name)
        return deleted, model_count, perms_needed, protected

    def delete_model(self, request, obj):
        obj.delete(allow_posted=request.user.is_superuser)

    def delete_queryset(self, request, queryset):
        if not request.user.is_superuser:
            posted = queryset.filter(status=EntryStatus.POSTED)
            if posted.exists():
                self.message_user(
                    request,
                    _("%(count)s posted entries were kept: reverse them instead.") % {"count": posted.count()},
                    level=messages.WARNING,
                )
                queryset = queryset.exclude(status=EntryStatus.POSTED)
        for obj in queryset:
            obj.delete(allow_posted=request.user.is_superuser)

    def save_model(self, request, obj, form, change):
        # We can handle posting action here perhaps? 
        # For now just standard save.
        if not obj.pk:
             # creation
             pass
        super().save_model(request, obj, form, change)


class JournalEntryLineResource(resources.ModelResource):
    """One row per line, with its entry's details: the ledger as a flat table."""

    id = fields.Field(attribute="id", column_name="ID", widget=widgets.IntegerWidget())
    date = fields.Field(attribute="entry__date", column_name=_("Date"), widget=widgets.DateWidget("%d/%m/%Y"))
    entry_number = fields.Field(attribute="entry__number", column_name=_("Entry"))
    journal = fields.Field(attribute="entry__journal__name", column_name=_("Journal"))
    status = fields.Field(column_name=_("Status"))
    reference = fields.Field(attribute="entry__reference", column_name=_("Reference"))
    account_code = fields.Field(attribute="account__code", column_name=_("Account"))
    account_name = fields.Field(attribute="account__name", column_name=_("Account name"))
    tax_identifier = fields.Field(attribute="tax_identifier", column_name=_("Tax identifier"))
    description = fields.Field(attribute="description", column_name=_("Description"))
    debit = fields.Field(column_name=_("Debit"))
    credit = fields.Field(column_name=_("Credit"))
    currency = fields.Field(column_name=_("Currency"))
    document = fields.Field(column_name=_("Document"))

    COLUMN_FORMATS = {"debit": "#,##0.00", "credit": "#,##0.00"}

    class Meta:
        model = JournalEntryLine
        fields = (
            "id", "date", "entry_number", "journal", "status", "reference", "account_code",
            "account_name", "tax_identifier", "description", "debit", "credit", "currency", "document",
        )
        export_order = fields

    def dehydrate_status(self, line):
        return line.entry.get_status_display()

    def dehydrate_debit(self, line):
        return line.debit.amount

    def dehydrate_credit(self, line):
        return line.credit.amount

    def dehydrate_currency(self, line):
        return str(line.debit.currency)

    def dehydrate_document(self, line):
        return entry_document_label(line.entry)


def tax_identifier_annotation():
    """
    The NIF of a line's counterparty, worked out in SQL: the client or
    provider whose own account the line is on (430xxxx, 400xxxx), else the
    third party of the document the entry was booked from, so an invoice's
    expense and VAT lines (and a payment's bank line) carry it too. Blank
    when the entry has no third party.
    """
    from materials.models import Provider
    from sales.models import Client

    def owner_of_account(model):
        return Subquery(
            model.objects.filter(account=OuterRef("account")).exclude(tax_identifier="")
            .values("tax_identifier")[:1]
        )

    candidates = [
        owner_of_account(Client),
        owner_of_account(Provider),
        "entry__sales_invoice__client__tax_identifier",
        "entry__purchase_invoice__provider__tax_identifier",
        "entry__payment__client__tax_identifier",
        "entry__payment__provider__tax_identifier",
    ]
    return Coalesce(
        *(NullIf(F(c) if isinstance(c, str) else c, Value("")) for c in candidates),
        Value(""),
        output_field=CharField(),
    )


def entry_document_label(entry):
    """The document the entry was booked from, as a reader would name it."""
    if entry.sales_invoice_id:
        return _("Sales invoice %(number)s") % {"number": entry.sales_invoice.display_number}
    if entry.purchase_invoice_id:
        number = entry.purchase_invoice.invoice_number or entry.purchase_invoice_id
        return _("Purchase invoice %(number)s") % {"number": number}
    if entry.payment_id:
        return _("Payment %(reference)s") % {"reference": entry.payment.reference or entry.payment_id}
    if entry.is_opening:
        return _("Opening entry")
    return ""


@admin.register(JournalEntryLine)
class JournalEntryLineAdmin(
    CompanyRestrictedAdmin, FormattedXlsxExportMixin, ModelAdmin, ExportActionModelAdmin,
):
    """
    Every line of the ledger with its entry's details, for filtering and
    exporting. Read-only: lines are edited in their entry, where the debits
    and credits are kept balanced.
    """

    resource_classes = [JournalEntryLineResource]
    export_form_class = SelectableFieldsExportForm
    list_display = [
        "entry_date", "entry_link", "journal_display", "account", "description",
        "debit", "credit", "status_display", "reference_display", "document_display",
    ]
    list_filter = [
        ("account", AutocompleteSelectMultipleFilter),
        ("entry__journal", AutocompleteSelectMultipleFilter),
        ("entry", AutocompleteSelectMultipleFilter),
        ("entry__sales_invoice", AutocompleteSelectMultipleFilter),
        ("entry__purchase_invoice", AutocompleteSelectMultipleFilter),
        ("entry__payment", AutocompleteSelectMultipleFilter),
        ("entry__status", ChoicesDropdownFilter),
        ("entry__date", RangeDateFilter),
        "entry__is_opening",
    ]
    list_filter_submit = True
    search_fields = [
        "account__code", "account__name", "description", "entry__number", "entry__reference",
    ]
    ordering = ["-entry__date", "-entry__number", "entry_id", "pk"]
    list_select_related = [
        "account", "entry__journal", "entry__sales_invoice", "entry__purchase_invoice", "entry__payment",
    ]
    fields = [
        ("entry_link", "entry_date", "journal_display", "status_display"),
        ("reference_display", "document_display"),
        "account", "description", ("debit", "credit"),
    ]
    readonly_fields = [
        "entry_link", "entry_date", "journal_display", "status_display",
        "reference_display", "document_display", "account", "description", "debit", "credit",
    ]

    @admin.display(description=_("Date"), ordering="entry__date")
    def entry_date(self, line):
        return line.entry.date

    @admin.display(description=_("Entry"), ordering="entry__number")
    def entry_link(self, line):
        url = reverse("admin:accounting_journalentry_change", args=[line.entry_id])
        return format_html('<a href="{}">{}</a>', url, line.entry.number or f"#{line.entry_id}")

    @admin.display(description=_("Journal"), ordering="entry__journal__name")
    def journal_display(self, line):
        return line.entry.journal

    @admin.display(description=_("Status"), ordering="entry__status")
    def status_display(self, line):
        return line.entry.get_status_display()

    @admin.display(description=_("Reference"), ordering="entry__reference")
    def reference_display(self, line):
        return line.entry.reference

    @admin.display(description=_("Document"))
    def document_display(self, line):
        return entry_document_label(line.entry)

    def get_export_queryset(self, request):
        return (
            super().get_export_queryset(request)
            .annotate(tax_identifier=tax_identifier_annotation())
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
