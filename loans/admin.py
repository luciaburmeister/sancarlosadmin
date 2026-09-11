from datetime import date
from decimal import Decimal

from django.contrib import admin, messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
    ChoicesDropdownFilter,
)
from unfold.decorators import action, display

from core.admin.company_admin import CompanyRestrictedAdmin
from loans.models import Bank, Loan, LoanEvent, LoanEventType, LoanInstallment, LoanStatus, ParentLoan
from loans.services.amortization import generate_schedule
from loans.services.bank_catalog import sync_banks_from_arcopay
from loans.views import EarlyRepaymentView
from payments.models import PaymentAllocation


@admin.register(Bank)
class BankAdmin(ModelAdmin):
    list_display = ("name", "swift_bic", "country")
    search_fields = ("name", "arcopay_service", "swift_bic")
    actions_list = ["sync_banks_action"]

    @action(description=_("Sync banks from Arcopay"), icon="sync")
    def sync_banks_action(self, request):
        try:
            count = sync_banks_from_arcopay()
            messages.success(request, _("%(count)d banks synced.") % {"count": count})
        except Exception as e:
            messages.error(request, str(e))
        return redirect(reverse("admin:loans_bank_changelist"))


# ---------------------------------------------------------------------------
# ParentLoan — Tranches inline
# ---------------------------------------------------------------------------

class LoanTrancheInline(TabularInline):
    model = Loan
    extra = 0
    tab = True
    can_delete = False
    show_change_link = False
    fields = (
        "tranche_number",
        "start_date",
        "principal_amount",
        "outstanding_principal_display",
        "term_months",
        "status",
        "reconciliation_status_display",
        "view_schedule_link",
    )
    readonly_fields = fields
    verbose_name = _("Tranche")
    verbose_name_plural = _("Tranches")

    def has_add_permission(self, request, obj=None):
        return False

    def _get_early_repayment_event(self, obj):
        """Returns the LoanEvent created when this tranche was settled."""
        if obj.status != "settled":
            return None
        return (
            obj.parent_loan.events
            .filter(
                event_type="early_repayment",
                date__gte=obj.start_date,
            )
            .order_by("date")
            .first()
        )

    @admin.display(description=_("#"))
    def tranche_number(self, obj):
        tranches = list(
            obj.parent_loan.tranches.order_by("created_at").values_list("pk", flat=True)
        )
        try:
            return tranches.index(obj.pk) + 1
        except ValueError:
            return "—"

    @admin.display(description=_("Outstanding"))
    def outstanding_principal_display(self, obj):
        return obj.outstanding_principal

    @admin.display(description=_("Early Repayment"))
    def reconciliation_status_display(self, obj):
        if obj.status != "settled":
            return "—"
        event = self._get_early_repayment_event(obj)
        if not event:
            return "—"
        from django.contrib.contenttypes.models import ContentType
        from payments.models import PaymentAllocation
        ct = ContentType.objects.get_for_model(LoanEvent)
        alloc = PaymentAllocation.objects.filter(
            content_type=ct, object_id=event.pk
        ).select_related("payment__reconciliation__bank_transaction").first()
        if alloc:
            try:
                txn = alloc.payment.reconciliation.bank_transaction
                url = reverse("admin:banking_banktransaction_change", args=[txn.pk])
                return format_html(
                    '<a href="{}" class="font-medium text-green-600 dark:text-green-400 hover:underline">'
                    '<span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle;">check_circle</span>'
                    ' #{}</a>',
                    url, txn.pk,
                )
            except Exception:
                return "—"
        return format_html(
            '<span class="font-medium text-yellow-600 dark:text-yellow-400">'
            '<span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle;">pending</span>'
            ' Pending</span>'
        )

    @admin.display(description=_("Schedule"))
    def view_schedule_link(self, obj):
        url = reverse("admin:loans_loan_change", args=[obj.pk]) + "#installments"
        return format_html(
            '<a href="{}" class="font-medium text-primary-600 dark:text-primary-400 hover:underline">'
            '<span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle;">table_view</span>'
            ' View</a>',
            url,
        )


# ---------------------------------------------------------------------------
# ParentLoan — Repayment History inline
# ---------------------------------------------------------------------------

class LoanRepaymentHistoryInline(TabularInline):
    """
    Repayment History — only shows reconciled early repayments.
    Reads from LoanEvent filtered to those with a PaymentAllocation.
    Columns: Event | Date | Capital paid | Principal before | Principal after | Bank Transaction
    """
    model = LoanEvent
    extra = 0
    tab = True
    can_delete = False
    show_change_link = False
    fields = (
        "rh_event",
        "rh_date",
        "rh_amount_paid",
        "rh_transaction_link",
    )
    readonly_fields = fields
    verbose_name = _("Early Repayment")
    verbose_name_plural = _("Repayment History")

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        """Only show events that have been reconciled (have a PaymentAllocation)."""
        ct = ContentType.objects.get_for_model(LoanEvent)
        reconciled_ids = PaymentAllocation.objects.filter(
            content_type=ct
        ).values_list("object_id", flat=True)
        return (
            super().get_queryset(request)
            .filter(pk__in=reconciled_ids, event_type="early_repayment")
            .order_by("date")
        )

    def _get_allocation(self, obj):
        ct = ContentType.objects.get_for_model(LoanEvent)
        return PaymentAllocation.objects.filter(
            content_type=ct, object_id=obj.pk
        ).select_related("payment__reconciliation__bank_transaction").first()

    @admin.display(description=_("Event"))
    def rh_event(self, obj):
        return _("Early Repayment")

    @admin.display(description=_("Date"))
    def rh_date(self, obj):
        alloc = self._get_allocation(obj)
        return alloc.payment.payment_date if alloc else obj.date

    @admin.display(description=_("Amount paid"))
    def rh_amount_paid(self, obj):
        alloc = self._get_allocation(obj)
        return alloc.amount_applied if alloc else "—"

    @admin.display(description=_("Bank Transaction"))
    def rh_transaction_link(self, obj):
        alloc = self._get_allocation(obj)
        if not alloc:
            return "—"
        try:
            txn = alloc.payment.reconciliation.bank_transaction
            url = reverse("admin:banking_banktransaction_change", args=[txn.pk])
            label = _("Bank Transaction #%(pk)s") % {"pk": txn.pk}
            return format_html(
                '<a href="{}" target="_blank" class="font-medium text-primary-600 dark:text-primary-400" '
                'style="display:inline-flex;align-items:center;gap:4px;padding:2px 10px;'
                'border-radius:9999px;border:1px solid currentColor;'
                'background-color:color-mix(in srgb, currentColor 12%, transparent);'
                'text-decoration:none;font-size:12px;white-space:nowrap;">'
                '<span class="material-symbols-outlined" style="font-size:14px;">receipt</span>'
                '{}</a>',
                url, label,
            )
        except Exception:
            return "—"


# ---------------------------------------------------------------------------
# Loan — Payment History inline
# ---------------------------------------------------------------------------

class LoanPaymentHistoryInline(TabularInline):
    model = LoanInstallment
    extra = 0
    tab = True
    can_delete = False
    show_change_link = False
    fields = (
        "ph_event",
        "ph_date",
        "ph_term",
        "ph_total_installment",
        "ph_amount_paid",
        "ph_outstanding",
        "ph_transaction",
    )
    readonly_fields = fields
    verbose_name = _("Payment")
    verbose_name_plural = _("Payment History")

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        ct = ContentType.objects.get_for_model(LoanInstallment)

        payment_date_sq = Subquery(
            PaymentAllocation.objects.filter(
                content_type=ct, object_id=OuterRef("pk"),
            ).order_by("payment__payment_date").values("payment__payment_date")[:1]
        )
        amount_applied_sq = Subquery(
            PaymentAllocation.objects.filter(
                content_type=ct, object_id=OuterRef("pk"),
            ).order_by("payment__payment_date").values("amount_applied")[:1]
        )
        txn_id_sq = Subquery(
            PaymentAllocation.objects.filter(
                content_type=ct, object_id=OuterRef("pk"),
            ).order_by("payment__payment_date")
            .values("payment__reconciliation__bank_transaction_id")[:1]
        )
        return (
            super().get_queryset(request)
            .filter(payment_status__in=["paid", "partial"])
            .annotate(
                actual_payment_date=payment_date_sq,
                actual_amount_applied=amount_applied_sq,
                bank_transaction_id=txn_id_sq,
            )
            .order_by("actual_payment_date")
        )

    @admin.display(description=_("Event"))
    def ph_event(self, obj):
        if obj.payment_status == "partial":
            return _("Partial payment")
        return _("Payment")

    @admin.display(description=_("Date"))
    def ph_date(self, obj):
        # Use the annotated real payment date from PaymentAllocation
        return getattr(obj, "actual_payment_date", None) or obj.due_date

    @admin.display(description=_("Term"))
    def ph_term(self, obj):
        total = obj.loan.installments.count()
        return f"{obj.installment_number}/{total}"

    @admin.display(description=_("Total installment"))
    def ph_total_installment(self, obj):
        return obj.total_due

    @admin.display(description=_("Amount paid"))
    def ph_amount_paid(self, obj):
        # Use actual amount from PaymentAllocation, not the field on LoanInstallment
        actual = getattr(obj, "actual_amount_applied", None)
        if actual is not None:
            from djmoney.money import Money
            return Money(actual, obj.total_due_currency)
        return obj.amount_paid

    @admin.display(description=_("Outstanding"))
    def ph_outstanding(self, obj):
        return obj.outstanding_principal_after

    @admin.display(description=_("Bank Transaction"))
    def ph_transaction(self, obj):
        txn_id = getattr(obj, "bank_transaction_id", None)
        if not txn_id:
            return "—"
        url = reverse("admin:banking_banktransaction_change", args=[txn_id])
        label = _("Bank Transaction #%(pk)s") % {"pk": txn_id}
        return format_html(
            '<a href="{}" target="_blank" class="font-medium text-primary-600 dark:text-primary-400" '
            'style="display:inline-flex;align-items:center;gap:4px;padding:2px 10px;'
            'border-radius:9999px;border:1px solid currentColor;'
            'background-color:color-mix(in srgb, currentColor 12%, transparent);'
            'text-decoration:none;font-size:12px;white-space:nowrap;">'
            '<span class="material-symbols-outlined" style="font-size:14px;">receipt</span>'
            '{}</a>',
            url, label,
        )

# ---------------------------------------------------------------------------
# ParentLoan admin
# ---------------------------------------------------------------------------

@admin.register(ParentLoan)
class ParentLoanAdmin(ModelAdmin, CompanyRestrictedAdmin):
    list_display = (
        "display_name",
        "bank",
        "bank_account_short",
        "total_outstanding_display",
        "tranche_count",
        "active_status",
        "contract_pdf_link",
        "company",
    )
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("bank", AutocompleteSelectMultipleFilter),
    )
    list_filter_submit = True
    search_fields = ("name", "reference", "bank__name")
    autocomplete_fields = ("bank", "bank_account")
    inlines = [LoanTrancheInline, LoanRepaymentHistoryInline]
    actions_detail = ["early_repayment_action", "add_tranche_action"]

    fieldsets = (
        (_("Bank and reference"), {
            "fields": (("company", "bank"), ("name", "reference")),
        }),
        (_("Account"), {
            "fields": ("bank_account", "contract_pdf", "notes"),
        }),
    )

    def get_urls(self):
        custom_urls = [
            path(
                "<int:pk>/early-repayment/",
                self.admin_site.admin_view(
                    EarlyRepaymentView.as_view(model_admin=self)
                ),
                name="loans_parentloan_early_repayment",
            ),
        ]
        return custom_urls + super().get_urls()

    def response_add(self, request, obj, post_url_continue=None):
        messages.info(request, _("Parent loan created. Now add the first tranche."))
        return redirect(
            reverse("admin:loans_loan_add") + f"?parent_loan={obj.pk}"
        )

    @admin.display(description=_("Loan"), ordering="name")
    def display_name(self, obj):
        return obj.name or obj.reference or f"#{obj.pk}"

    @admin.display(description=_("Account"))
    def bank_account_short(self, obj):
        if not obj.bank_account_id:
            return "—"
        iban = obj.bank_account.iban or ""
        return f"•••• {iban[-4:]}" if len(iban) >= 4 else iban or "—"

    @admin.display(description=_("Contract"))
    def contract_pdf_link(self, obj):
        if not obj.contract_pdf:
            return "—"
        return format_html('<a href="{}" target="_blank">📄 PDF</a>', obj.contract_pdf.url)

    @admin.display(description=_("Outstanding"))
    def total_outstanding_display(self, obj):
        return obj.total_outstanding or "—"

    @admin.display(description=_("Tranches"))
    def tranche_count(self, obj):
        total = obj.tranches.count()
        active = obj.tranches.filter(status="active").count()
        return f"{active} active / {total} total"

    @display(
        description=_("Status"),
        label={
            "active": "success",
            "settled": "info",
            "defaulted": "danger",
            "cancelled": "warning",
            "draft": "warning",
            "none": "warning",
        },
    )
    def active_status(self, obj):
        tranche = obj.active_tranche
        if tranche:
            return tranche.status
        last = obj.tranches.order_by("-created_at").first()
        return last.status if last else "none"

    @action(description=_("Add tranche"), icon="add_circle")
    def add_tranche_action(self, request, object_id):
        parent = get_object_or_404(ParentLoan, pk=object_id)
        if parent.active_tranche:
            messages.error(
                request,
                _("This loan already has an active tranche. Settle it first or register an early repayment."),
            )
            return redirect(reverse("admin:loans_parentloan_change", args=[object_id]))
        return redirect(
            reverse("admin:loans_loan_add") + f"?parent_loan={object_id}"
        )

    @action(description=_("Register early repayment"), icon="payments")
    def early_repayment_action(self, request, object_id):
        parent = get_object_or_404(ParentLoan, pk=object_id)
        if not parent.active_tranche:
            messages.error(request, _("No active tranche found for this loan."))
            return redirect(reverse("admin:loans_parentloan_change", args=[object_id]))
        return redirect(
            reverse("admin:loans_parentloan_early_repayment", args=[object_id])
        )


# ---------------------------------------------------------------------------
# Loan (tranche) admin
# ---------------------------------------------------------------------------

class LoanInstallmentInline(TabularInline):
    model = LoanInstallment
    extra = 0
    tab = True
    can_delete = False
    fields = (
        "installment_number",
        "due_date",
        "total_due",
        "principal_due",
        "interest_due",
        "payment_status",
        "amount_paid",
        "outstanding_principal_after",
        "mark_paid_button",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Actions"))
    def mark_paid_button(self, obj):
        if not obj.pk or obj.payment_status == "paid":
            return "—"
        url = reverse("admin:loans_loan_mark_installment_paid", args=[obj.pk])
        return format_html(
            '<a href="{}" class="font-medium text-primary-600 dark:text-primary-400 hover:underline" '
            'onclick="return confirm(\'{}\');">'
            '<span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle;">price_check</span>'
            ' {}</a>',
            url,
            _("Mark this installment as paid without a bank transaction?"),
            _("Mark as paid"),
        )


@admin.register(Loan)
class LoanAdmin(ModelAdmin, CompanyRestrictedAdmin):
    list_display = (
        "display_name",
        "parent_loan_link",
        "bank",
        "start_date",
        "principal_amount",
        "colored_status",
        "bank_account_short",
        "company",
    )
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("bank", AutocompleteSelectMultipleFilter),
        ("parent_loan", AutocompleteSelectMultipleFilter),
        ("status", ChoicesDropdownFilter),
        ("amortization_method", ChoicesDropdownFilter),
        ("interest_day_count", ChoicesDropdownFilter),
        ("payment_frequency", ChoicesDropdownFilter),
    )
    list_filter_submit = True
    search_fields = ("name", "reference", "bank__name", "parent_loan__name", "parent_loan__reference")
    autocomplete_fields = ("bank", "bank_account", "parent_loan")
    inlines = [LoanInstallmentInline, LoanPaymentHistoryInline]
    actions_detail = ["generate_schedule_action", "run_matcher_action"]

    fieldsets = (
        (_("Bank and reference"), {
            "fields": (("company", "bank"), ("name", "reference"), "parent_loan"),
        }),
        (_("Financial terms"), {
            "fields": (
                ("principal_amount", "interest_rate"),
                ("interest_rate_type", "amortization_method"),
                "interest_day_count",
            ),
        }),
        (_("Schedule"), {
            "fields": (
                ("start_date", "first_payment_date"),
                ("term_months", "payment_frequency"),
            ),
        }),
        (_("Status and account"), {
            "fields": (("status", "bank_account"), "notes"),
        }),
    )

    def get_urls(self):
        custom_urls = [
            path(
                "installment/<int:installment_id>/mark-paid/",
                self.admin_site.admin_view(self.mark_installment_paid),
                name="loans_loan_mark_installment_paid",
            ),
        ]
        return custom_urls + super().get_urls()

    def mark_installment_paid(self, request, installment_id):
        """
        Mark an installment as paid without a bank transaction, for old
        installments that predate the available bank data and can never be
        reconciled. Creates a manual Payment fully allocated to the
        installment so amount_paid/payment_status (which are recomputed from
        PaymentAllocation) stay consistent.
        """
        installment = get_object_or_404(
            LoanInstallment.objects.select_related("loan__company", "loan__bank_account"),
            pk=installment_id,
        )
        loan = installment.loan
        redirect_url = reverse("admin:loans_loan_change", args=[loan.pk]) + "#installments"

        if not self.has_change_permission(request, loan):
            messages.error(request, _("You do not have permission to modify this loan."))
            return redirect(redirect_url)

        remaining = installment.amount_remaining
        if installment.payment_status == "paid" or remaining.amount <= 0:
            messages.info(request, _("Installment #%(num)s is already paid.") % {
                "num": installment.installment_number,
            })
            return redirect(redirect_url)

        from django.db import transaction as db_transaction
        from payments.models import (
            Payment,
            PaymentCreatedBy,
            PaymentDirection,
            PaymentMethod,
            PaymentStatus,
        )

        with db_transaction.atomic():
            payment = Payment.objects.create(
                company=loan.company,
                payment_date=installment.due_date,
                amount=remaining.amount,
                amount_currency=str(remaining.currency),
                direction=PaymentDirection.OUTBOUND,
                method=PaymentMethod.TRANSFER,
                status=PaymentStatus.CONFIRMED,
                bank_account=loan.bank_account,
                created_by=PaymentCreatedBy.MANUAL,
                notes=_("Marked as paid manually from admin (no bank transaction). %(loan)s") % {
                    "loan": str(installment),
                },
            )
            PaymentAllocation.objects.create(
                payment=payment,
                amount_applied=remaining.amount,
                amount_applied_currency=str(remaining.currency),
                content_type=ContentType.objects.get_for_model(LoanInstallment),
                object_id=installment.pk,
            )

        messages.success(
            request,
            _("Installment #%(num)s marked as paid (%(amount)s).") % {
                "num": installment.installment_number,
                "amount": remaining,
            },
        )
        return redirect(redirect_url)

    def has_add_permission(self, request):
        return bool(request.GET.get("parent_loan"))

    def add_view(self, request, form_url="", extra_context=None):
        if not request.GET.get("parent_loan"):
            messages.warning(
                request,
                _("Loans must be created from a Parent Loan. Please create or select a Parent Loan first."),
            )
            return redirect(reverse("admin:loans_parentloan_changelist"))
        return super().add_view(request, form_url=form_url, extra_context=extra_context)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        parent_loan_id = request.GET.get("parent_loan")
        if not parent_loan_id:
            return initial
        try:
            parent = ParentLoan.objects.select_related(
                "bank", "bank_account", "company"
            ).get(pk=parent_loan_id)
            initial.update({
                "parent_loan": parent.pk,
                "company": parent.company_id,
                "bank": parent.bank_id,
                "bank_account": parent.bank_account_id,
                "name": parent.name,
                "reference": parent.reference,
            })
        except ParentLoan.DoesNotExist:
            pass
        return initial

    def get_form(self, request, obj=None, **kwargs):
        base_form = super().get_form(request, obj, **kwargs)
        parent_loan_id = request.GET.get("parent_loan") or request.POST.get("_parent_loan_id")
        if obj or not parent_loan_id:
            return base_form

        from django import forms as django_forms

        disabled_fields = ["company", "bank", "bank_account", "parent_loan"]

        class TrancheForm(base_form):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                for field_name in disabled_fields:
                    if field_name in self.fields:
                        self.fields[field_name].disabled = True
                try:
                    parent = ParentLoan.objects.select_related(
                        "bank", "bank_account", "company"
                    ).get(pk=parent_loan_id)
                except ParentLoan.DoesNotExist:
                    return
                self.initial["company"] = parent.company_id
                self.initial["bank"] = parent.bank_id
                self.initial["bank_account"] = parent.bank_account_id
                self.initial["parent_loan"] = parent.pk

        TrancheForm.base_fields["_parent_loan_id"] = django_forms.CharField(
            widget=django_forms.HiddenInput(),
            initial=parent_loan_id,
            required=False,
        )
        return TrancheForm

    def save_model(self, request, obj, form, change):
        if not change:
            parent_loan_id = request.POST.get("_parent_loan_id")
            if parent_loan_id:
                try:
                    parent = ParentLoan.objects.select_related(
                        "bank", "bank_account", "company"
                    ).get(pk=parent_loan_id)
                    obj.parent_loan = parent
                    obj.company = parent.company
                    obj.bank = parent.bank
                    obj.bank_account = parent.bank_account
                except ParentLoan.DoesNotExist:
                    pass
        super().save_model(request, obj, form, change)
        if getattr(obj, "_pending_stale_schedule_warning", False):
            messages.warning(
                request,
                _(
                    "You changed financial terms on a loan with already-paid installments. "
                    "The schedule was NOT regenerated automatically — review it manually if needed."
                ),
            )

    @admin.display(description=_("Loan"), ordering="name")
    def display_name(self, obj):
        return obj.name or obj.reference or f"#{obj.pk}"

    @admin.display(description=_("Parent Loan"))
    def parent_loan_link(self, obj):
        if not obj.parent_loan_id:
            return "—"
        url = reverse("admin:loans_parentloan_change", args=[obj.parent_loan_id])
        label = (
            obj.parent_loan.name
            or obj.parent_loan.reference
            or f"#{obj.parent_loan_id}"
        )
        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description=_("Account"))
    def bank_account_short(self, obj):
        if not obj.bank_account_id:
            return "—"
        iban = obj.bank_account.iban or ""
        return f"•••• {iban[-4:]}" if len(iban) >= 4 else iban or "—"

    @display(
        description=_("Status"),
        ordering="status",
        label={
            "active": "success",
            "settled": "info",
            "draft": "warning",
            "defaulted": "danger",
            "cancelled": "warning",
        },
    )
    def colored_status(self, obj):
        return obj.status

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if getattr(obj, "_pending_stale_schedule_warning", False):
            messages.warning(
                request,
                _(
                    "You changed financial terms on a loan with already-paid installments. "
                    "The schedule was NOT regenerated automatically — review it manually if needed."
                ),
            )

    @action(description=_("Generate payment schedule"), icon="calendar_month")
    def generate_schedule_action(self, request, object_id):
        loan = get_object_or_404(Loan, pk=object_id)
        try:
            generate_schedule(loan, force=True)
            messages.success(request, _("Payment schedule generated."))
        except ValueError as e:
            messages.error(request, str(e))
        return redirect(reverse("admin:loans_loan_change", args=[loan.pk]))

    @action(description=_("Check bank transactions"), icon="account_balance")
    def run_matcher_action(self, request, object_id):
        return redirect(reverse("admin:banking_banktransaction_changelist"))