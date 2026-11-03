import logging
from datetime import date

from django.contrib import admin, messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import OuterRef, Q, Subquery
from django.db.models.fields import BLANK_CHOICE_DASH
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from import_export.admin import ExportActionModelAdmin
from unfold.admin import ModelAdmin, StackedInline, TabularInline
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from unfold.decorators import action, display

from banking.models import BankReconciliation
from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.xlsx_export import FormattedXlsxExportMixin
from payments.mixins import unallocated_payments_q
from payments.models import Payment, PaymentAllocation, PaymentStatus, PromissoryNote, PromissoryNoteStatus
from payments.models import MANUAL_PAYMENT_METHODS, Payment, PaymentAllocation, PaymentStatus
from payments.resources import PaymentResource

logger = logging.getLogger(__name__)


class UnallocatedFilter(admin.SimpleListFilter):
    """
    Payments with money still to allocate, by the same rule a client's credit
    is summed with, so the credit figure links to exactly the payments behind it.
    """

    title = _("Pending allocation")
    parameter_name = "unallocated"

    def lookups(self, request, model_admin):
        return [("yes", _("Yes")), ("no", _("No"))]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(unallocated_payments_q())
        if self.value() == "no":
            return queryset.exclude(unallocated_payments_q())
        return queryset


class PaymentAllocationInline(TabularInline):
    """
    Locked (read-only, no add/delete) once the payment is a promissory
    note's issuance: its amount and allocations are set by
    PromissoryNoteService, which also owns distributing any surplus to
    further invoices, the only valid way to add more here.
    """
    model = PaymentAllocation
    extra = 0
    fields = ("content_type", "object_id", "amount_applied", "account")
    autocomplete_fields = ("account",)

    @staticmethod
    def _is_promissory_note_payment(obj):
        return obj is not None and hasattr(obj, "promissory_note_issued")

    def has_add_permission(self, request, obj=None):
        if self._is_promissory_note_payment(obj):
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if self._is_promissory_note_payment(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if self._is_promissory_note_payment(obj):
            return self.fields
        return super().get_readonly_fields(request, obj)


class PaymentBankReconciliationInline(StackedInline):
    """Read-only inline showing the bank transaction this payment was matched to."""
    model = BankReconciliation
    fk_name = "payment"
    extra = 0
    max_num = 0
    can_delete = False
    tab = True
    fields = (
        "transaction_link",
        "is_confirmed",
        "method",
        "unmatched_amount",
        "confirmed_at",
    )
    readonly_fields = (
        "transaction_link",
        "is_confirmed",
        "method",
        "unmatched_amount",
        "confirmed_at",
    )

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Bank Transaction"))
    def transaction_link(self, obj):
        if not obj.pk or not obj.bank_transaction_id:
            return "—"
        txn = obj.bank_transaction
        url = reverse("admin:banking_banktransaction_change", args=[txn.pk])
        return format_html(
            '<a href="{}" style="color: var(--primary-600, #0369a1); font-weight: 600;">{}</a>',
            url, str(txn),
        )


@admin.register(Payment)
class PaymentAdmin(CompanyRestrictedAdmin, FormattedXlsxExportMixin, ModelAdmin, ExportActionModelAdmin):
    resource_classes = [PaymentResource]
    export_form_class = SelectableFieldsExportForm
    list_display = (
        "id",
        "payment_date",
        "company",
        "direction",
        "get_status_label",
        "method",
        "amount",
        "amount_pending_allocation",
        "is_allocated",
        "client",
        "reference",
        "journal_entry_link",
    )
    list_filter = (
        "company", "direction", "status", "method", UnallocatedFilter, "payment_date",
    )
    search_fields = ("reference", "notes", "client__name", "client__commercial_name")
    list_select_related = ("company", "client")
    autocomplete_fields = ("client", "account")
    readonly_fields = (
        "amount_pending_allocation",
        "amount_pending_allocation_currency",
    )
    # Set by PromissoryNoteService and never meant to be hand-edited:
    # correcting a note means cancelling it and issuing a new one, not
    # editing the payment that settled its invoice(s) behind its back.
    _promissory_note_locked_fields = ("amount", "amount_currency", "bank_account", "method")
    inlines = [PaymentAllocationInline, PaymentBankReconciliationInline]
    ordering = ("-payment_date", "-id")
    actions = ["journalize_selected"]
    actions_detail = ["view_journal_entries_detail_action", "journalize_detail_action"]

    # Outcomes of journalizing one payment, and how they're shown
    _JOURNALIZE_LEVELS = {
        "posted": messages.SUCCESS,
        "draft": messages.WARNING,
        "unchanged": messages.INFO,
        "error": messages.ERROR,
    }

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj is not None and hasattr(obj, "promissory_note_issued"):
            fields = tuple(fields) + self._promissory_note_locked_fields
        return fields

    def get_queryset(self, request):
        from accounting.models import JournalEntry

        live = JournalEntry.objects.filter(
            payment=OuterRef("pk"), reversal_of__isnull=True, reversal__isnull=True,
        ).order_by("-pk")
        return super().get_queryset(request).annotate(
            live_entry_id=Subquery(live.values("pk")[:1]),
            live_entry_number=Subquery(live.values("number")[:1]),
        )

    def get_export_queryset(self, request):
        return (
            super().get_export_queryset(request)
            .select_related("bank_account", "provider")
            .prefetch_related("allocations__related_object")
        )

    @display(description=_("Journal entry"))
    def journal_entry_link(self, obj):
        if not getattr(obj, "live_entry_id", None):
            return "—"
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:accounting_journalentry_change", args=[obj.live_entry_id]),
            obj.live_entry_number or _("Draft"),
        )

    @staticmethod
    def _journalize_one(payment):
        """
        Bring the payment's entry in line with it (reversing and rebooking a
        posted one that no longer matches). Returns (outcome, message):
        outcome is a _JOURNALIZE_LEVELS key.
        """
        from accounting.models import EntryStatus
        from accounting.services.journal_generator import JournalizeError
        from accounting.services.payment_journal.engine import PaymentJournalService

        try:
            entry = PaymentJournalService(payment.company).sync(payment)
        except JournalizeError as e:
            return "error", str(e)
        except Exception:
            logger.exception("Could not journalize payment %s", payment.pk)
            return "error", _("The journal entry could not be created; the error was logged.")
        if entry is None:
            return "unchanged", _("The ledger already matches this payment.")
        if entry.status == EntryStatus.POSTED:
            return "posted", _("Journal entry %s posted.") % entry.number
        return "draft", _("Journal entry saved as draft: %s") % entry.post_error

    @action(description=_("Generate Journal Entry"), icon="post_add", permissions=["journalize"])
    def journalize_detail_action(self, request, object_id):
        payment = get_object_or_404(self.get_queryset(request), pk=object_id)
        outcome, message = self._journalize_one(payment)
        self.message_user(request, message, level=self._JOURNALIZE_LEVELS[outcome])
        return redirect(reverse("admin:payments_payment_change", args=[object_id]))

    @action(description=_("View Journal Entries"), icon="menu_book")
    def view_journal_entries_detail_action(self, request, object_id):
        return redirect(reverse("admin:accounting_journalentry_changelist") + f"?payment__id={object_id}")

    @admin.action(description=_("Generate journal entries"), permissions=["journalize"])
    def journalize_selected(self, request, queryset):
        results = {outcome: [] for outcome in self._JOURNALIZE_LEVELS}
        # By date, so the entries' numbers follow the payments' dates
        for payment in queryset.select_related("company").order_by("payment_date", "pk"):
            outcome, message = self._journalize_one(payment)
            results[outcome].append(f"{payment.reference or payment.pk}: {message}")

        if results["posted"]:
            self.message_user(request, _("%(count)s journal entries posted.") % {"count": len(results["posted"])})
        if results["unchanged"]:
            self.message_user(
                request, _("%(count)s already matched the ledger.") % {"count": len(results["unchanged"])},
                level=messages.INFO,
            )
        for outcome in ("draft", "error"):
            for message in results[outcome][:10]:
                self.message_user(request, message, level=self._JOURNALIZE_LEVELS[outcome])

    @staticmethod
    def _intercompany_payment_of(obj):
        """
        The intercompany payment that created ``obj``, or None. Unfold asks
        detail actions' permissions with the object id instead of the object.
        """
        from invoices.models import IntercompanyPayment

        payment_id = getattr(obj, "pk", obj)
        if not payment_id:
            return None
        return IntercompanyPayment.objects.filter(
            Q(payer_payment_id=payment_id) | Q(settlement_payment_id=payment_id)
        ).first()

    def has_journalize_permission(self, request, object_id=None):
        # Booking it again changes nothing in the payment: allowed on intercompany ones too
        return super().has_change_permission(request)

    # An intercompany payment's payments change only with it: cancel it instead
    def has_change_permission(self, request, obj=None):
        if self._intercompany_payment_of(obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if self._intercompany_payment_of(obj):
            return False
        return super().has_delete_permission(request, obj)

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        # Paid by another company is only ever set by its intercompany payment
        if db_field.name == "method":
            kwargs["choices"] = BLANK_CHOICE_DASH + MANUAL_PAYMENT_METHODS
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Editing the amount alone (no allocation touched) would otherwise
        # leave the stored pending figure, and the client's credit, stale.
        form.instance.recalculate_pending_allocation()

    @display(description=_("Allocated"), boolean=True)
    def is_allocated(self, obj):
        return obj.amount_pending_allocation.amount == 0

    @display(
        description=_("Status"),
        ordering="status",
        label={
            PaymentStatus.DRAFT: "warning",
            PaymentStatus.CONFIRMED: "info",
            PaymentStatus.RECONCILED: "success",
            PaymentStatus.CANCELLED: "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status


class InCirculationFilter(admin.SimpleListFilter):
    """
    Notes still outstanding (not yet matured/cancelled) as of a cutoff date,
    for closing a year: which issued/received notes were in circulation on
    that day. Reads today's status, not a historical reconstruction - good
    for a cutoff shortly after close, not for an arbitrary date long past.
    """

    title = _("In circulation as of")
    parameter_name = "in_circulation_as_of"

    def lookups(self, request, model_admin):
        return [
            ("today", _("Today")),
            ("last_year_end", _("Last December 31")),
        ]

    def queryset(self, request, queryset):
        today = timezone.localdate()
        if self.value() == "today":
            as_of = today
        elif self.value() == "last_year_end":
            as_of = date(today.year - 1, 12, 31)
        else:
            return queryset
        return queryset.filter(
            issue_date__lte=as_of,
            status__in=[PromissoryNoteStatus.ISSUED, PromissoryNoteStatus.IN_PORTFOLIO],
        )


@admin.register(PromissoryNote)
class PromissoryNoteAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "code",
        "number",
        "counterparty",
        "amount",
        "issue_date",
        "due_date",
        "bank_account",
        "get_status_label",
    )
    list_filter = ("company", "status", "bank_account", InCirculationFilter, "due_date")
    search_fields = ("number", "code", "provider__name", "client__name", "client__commercial_name")
    list_select_related = ("company", "provider", "client", "bank_account")
    autocomplete_fields = ("provider", "client", "bank_account", "payment")
    readonly_fields = ("code",)
    ordering = ("-issue_date", "-id")

    @display(description=_("Counterparty"))
    def counterparty(self, obj):
        return obj.provider.name if obj.provider_id else obj.client.name

    @display(
        description=_("Status"),
        ordering="status",
        label={
            PromissoryNoteStatus.ISSUED: "info",
            PromissoryNoteStatus.IN_PORTFOLIO: "info",
            PromissoryNoteStatus.PAID: "success",
            PromissoryNoteStatus.COLLECTED: "success",
            PromissoryNoteStatus.CANCELLED: "danger",
            PromissoryNoteStatus.RETURNED: "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status