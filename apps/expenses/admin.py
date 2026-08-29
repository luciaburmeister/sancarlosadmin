import os

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.http import HttpResponse
from django.urls import path, reverse
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display

from core.admin.company_access import get_user_companies
from core.admin.company_admin import CompanyRestrictedAdmin
from apps.expenses.forms import ExpenseClaimRejectForm, ExpenseTicketExchangeRateOverrideForm
from invoices.models import InvoicePaymentStatus
from payments.admin_inlines import DocumentPaymentsInline
from apps.expenses.models import (
    ExchangeRate,
    ExchangeRateSource,
    ExpenseClaim,
    ExpenseClaimStatus,
    ExpenseTicket,
)
from apps.expenses.tasks import process_expense_ticket
from apps.expenses.views.expense_ticket_upload import ExpenseTicketUploadAdminView
from apps.expenses.views.expense_claim_add_ticket import ExpenseClaimAddTicketAdminView
from apps.expenses.views.expense_claim_register_payment import ExpenseClaimRegisterPaymentView


class ExpenseTicketInline(TabularInline):
    model = ExpenseTicket
    tab = True
    verbose_name = _("Ticket")
    verbose_name_plural = _("Linked Tickets")
    extra = 0
    can_delete = True
    show_change_link = True
    fields = ("vendor_name", "date_of_purchase", "original_amount", "converted_amount", "needs_manual_review")
    readonly_fields = ("vendor_name", "date_of_purchase", "original_amount", "converted_amount", "needs_manual_review")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        # obj here is the parent ExpenseClaim. Only its own submitter can edit
        # tickets on it, and only while it's still a draft - except superusers,
        # who bypass ownership checks everywhere else in Django and shouldn't
        # be silently locked out here for support/admin purposes.
        if obj is None or request.user.is_superuser:
            return super().has_change_permission(request, obj)
        if obj.submitted_by_id == request.user.id:
            return obj.status == ExpenseClaimStatus.DRAFT and super().has_change_permission(request, obj)
        return False

    def has_delete_permission(self, request, obj=None):
        if obj is None or request.user.is_superuser:
            return super().has_delete_permission(request, obj)
        if obj.submitted_by_id == request.user.id:
            return obj.status == ExpenseClaimStatus.DRAFT and super().has_delete_permission(request, obj)
        return False


@admin.register(ExpenseClaim)
class ExpenseClaimAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "__str__",
        "submitted_by",
        "status_badge",
        "payment_status_badge",
        "total_claimed",
        "total_reimbursed",
        "amount_outstanding",
        "created_at",
    )
    list_filter = ("status", "payment_status", "company")
    search_fields = ("submitted_by__email", "submitted_by__first_name")
    autocomplete_fields = ("company", "submitted_by")
    # status/total_reimbursed/rejection_reason only ever change through the
    # Submit/Approve/Reject actions below, never by direct editing.
    readonly_fields = (
        "status",
        "payment_status",
        "total_claimed",
        "total_reimbursed",
        "amount_outstanding",
        "rejection_reason",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    ("company", "submitted_by"),
                    "status",
                    "rejection_reason",
                )
            },
        ),
        (
            _("Payment Tracking"),
            {
                "fields": (
                    "payment_status",
                    "total_claimed",
                    "total_reimbursed",
                    "amount_outstanding",
                )
            },
        ),
        (
            _("Notes"),
            {
                "fields": ("notes",)
            },
        ),
        (
            _("Timestamps"),
            {
                "fields": (("created_at", "updated_at"),)
            },
        ),
    )
    inlines = [ExpenseTicketInline, DocumentPaymentsInline]
    actions_detail = [
        "add_ticket_detail_action",
        "submit_claim_detail_action",
        "approve_claim_detail_action",
        "reject_claim_detail_action",
        "revise_claim_detail_action",
        "register_payment_detail_action",
        "run_matcher_detail_action",
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.has_perm("expenses.approve_expenseclaim"):
            return qs
        return qs.filter(submitted_by=request.user)

    def has_change_permission(self, request, obj=None):
        if obj is None or request.user.is_superuser:
            return super().has_change_permission(request, obj)
        if obj.submitted_by_id == request.user.id:
            return obj.status == ExpenseClaimStatus.DRAFT
        return False

    def has_delete_permission(self, request, obj=None):
        if obj is None or request.user.is_superuser:
            return super().has_delete_permission(request, obj)
        if obj.submitted_by_id == request.user.id:
            return obj.status == ExpenseClaimStatus.DRAFT and super().has_delete_permission(request, obj)
        return False

    # ── Display helpers ──────────────────────────────────────────────────────

    @display(
        description=_("Status"),
        label={
            ExpenseClaimStatus.DRAFT.label: "default",
            ExpenseClaimStatus.SUBMITTED.label: "warning",
            ExpenseClaimStatus.APPROVED.label: "info",
            ExpenseClaimStatus.REIMBURSED.label: "success",
            ExpenseClaimStatus.REJECTED.label: "danger",
        },
    )
    def status_badge(self, obj):
        return obj.get_status_display()

    @display(
        description=_("Payment"),
        label={
            InvoicePaymentStatus.UNPAID.label: "danger",
            InvoicePaymentStatus.PARTIAL.label: "warning",
            InvoicePaymentStatus.PAID.label: "success",
        },
    )
    def payment_status_badge(self, obj):
        return obj.get_payment_status_display()

    @display(description=_("Outstanding"))
    def amount_outstanding(self, obj):
        # amount_remaining is total_claimed - total_reimbursed; both are always
        # set (they default to 0), so this never needs a null guard.
        return obj.amount_remaining

    def save_formset(self, request, form, formset, change):
        if formset.model is ExpenseTicket:
            instances = formset.save(commit=False)
            for instance in instances:
                instance.save()
            formset.save_m2m()
            for obj in formset.deleted_objects:
                obj.claim = None
                obj.save(update_fields=["claim"])
            form.instance.recalculate_total()
        else:
            super().save_formset(request, form, formset, change)

    def add_view(self, request, form_url="", extra_context=None):
        company = get_user_companies(request.user).first()
        if company is None:
            self.message_user(request, _("You don't have access to any company yet - contact an admin."), level="error")
            return redirect(reverse("admin:expenses_expenseclaim_changelist"))
        claim = ExpenseClaim.objects.create(submitted_by=request.user, company=company)
        return redirect(reverse("admin:expenses_expenseclaim_change", args=[claim.pk]))

    # ── Detail actions ───────────────────────────────────────────────────────

    @action(description=_("Add Ticket"), url_path="add-ticket", icon="add", permissions=["add_ticket_detail_action"])
    def add_ticket_detail_action(self, request, object_id):
        return redirect(reverse("admin:expenses_expenseclaim_add_ticket", args=[object_id]))

    def has_add_ticket_detail_action_permission(self, request, object_id):
        claim = self.get_object(request, object_id)
        if not claim or claim.status != ExpenseClaimStatus.DRAFT:
            return False
        return request.user.is_superuser or claim.submitted_by_id == request.user.id

    @action(description=_("Submit Claim"), url_path="submit", icon="send", permissions=["submit_claim_detail_action"])
    def submit_claim_detail_action(self, request, object_id):
        claim = self.get_object(request, object_id)
        try:
            claim.transition_to(ExpenseClaimStatus.SUBMITTED)
            self.message_user(request, _("Claim submitted for approval."))
        except ValidationError as e:
            self.message_user(request, "; ".join(e.messages), level="error")
        return redirect(reverse("admin:expenses_expenseclaim_change", args=[object_id]))

    def has_submit_claim_detail_action_permission(self, request, object_id):
        claim = self.get_object(request, object_id)
        if not claim or claim.status != ExpenseClaimStatus.DRAFT:
            return False
        return request.user.is_superuser or claim.submitted_by_id == request.user.id

    @action(description=_("Approve"), url_path="approve", icon="check_circle", permissions=["approve_claim_detail_action"])
    def approve_claim_detail_action(self, request, object_id):
        claim = self.get_object(request, object_id)
        try:
            claim.transition_to(ExpenseClaimStatus.APPROVED)
            self.message_user(request, _("Claim approved."))
        except ValidationError as e:
            self.message_user(request, "; ".join(e.messages), level="error")
        return redirect(reverse("admin:expenses_expenseclaim_change", args=[object_id]))

    def has_approve_claim_detail_action_permission(self, request, object_id):
        claim = self.get_object(request, object_id)
        return (
            bool(claim)
            and claim.submitted_by_id != request.user.id
            and request.user.has_perm("expenses.approve_expenseclaim")
            and claim.status == ExpenseClaimStatus.SUBMITTED
        )

    @action(
        description=_("Reject"),
        url_path="reject",
        icon="cancel",
        permissions=["reject_claim_detail_action"],
        dialog={
            "title": _("Reject Claim"),
            "description": _("Please provide a reason for rejecting this claim."),
            "form_class": ExpenseClaimRejectForm,
            "form_submit_text": _("Reject Claim"),
        },
    )
    def reject_claim_detail_action(self, request, form, object_id):
        claim = self.get_object(request, object_id)
        claim.rejection_reason = form.cleaned_data["rejection_reason"]
        claim.save(update_fields=["rejection_reason"])
        try:
            claim.transition_to(ExpenseClaimStatus.REJECTED)
            self.message_user(request, _("Claim rejected."))
        except ValidationError as e:
            self.message_user(request, "; ".join(e.messages), level="error")
        # This action is submitted via htmx from inside the dialog modal, which
        # targets/selects "#dialog-form" in the response. A real 3xx redirect
        # gets auto-followed by the browser's XHR *before* htmx's JS ever sees
        # it, so a plain redirect()+HX-Redirect combo never reaches htmx - it
        # only sees the final page, which has no #dialog-form, and swaps
        # nothing in. HX-Redirect must be set on a plain 200 response instead.
        response = HttpResponse(status=200)
        response["HX-Redirect"] = reverse("admin:expenses_expenseclaim_change", args=[object_id])
        return response

    def has_reject_claim_detail_action_permission(self, request, object_id):
        claim = self.get_object(request, object_id)
        return (
            bool(claim)
            and claim.submitted_by_id != request.user.id
            and request.user.has_perm("expenses.approve_expenseclaim")
            and claim.status == ExpenseClaimStatus.SUBMITTED
        )

    @action(description=_("Revise Claim"), url_path="revise", icon="restart_alt", permissions=["revise_claim_detail_action"])
    def revise_claim_detail_action(self, request, object_id):
        claim = self.get_object(request, object_id)
        try:
            claim.transition_to(ExpenseClaimStatus.DRAFT)
            claim.rejection_reason = ""
            claim.save(update_fields=["rejection_reason"])
            self.message_user(request, _("Claim reopened as a draft. You can now edit it and resubmit."))
        except ValidationError as e:
            self.message_user(request, "; ".join(e.messages), level="error")
        return redirect(reverse("admin:expenses_expenseclaim_change", args=[object_id]))

    def has_revise_claim_detail_action_permission(self, request, object_id):
        claim = self.get_object(request, object_id)
        if not claim or claim.status != ExpenseClaimStatus.REJECTED:
            return False
        return request.user.is_superuser or claim.submitted_by_id == request.user.id

    @action(
        description=_("Register Payment"),
        url_path="register-payment",
        icon="payments",
        permissions=["register_payment_detail_action"],
    )
    def register_payment_detail_action(self, request, object_id):
        return redirect(reverse("admin:expenses_expenseclaim_register_payment", args=[object_id]))

    def has_register_payment_detail_action_permission(self, request, object_id):
        claim = self.get_object(request, object_id)
        if not claim or claim.status != ExpenseClaimStatus.APPROVED:
            return False
        if claim.amount_remaining.amount <= 0:
            return False
        # Reimbursing is a finance job, not something the claimant does for
        # themselves - same bar as approving.
        return request.user.is_superuser or request.user.has_perm("expenses.approve_expenseclaim")

    @action(description=_("Run Matcher"), url_path="run-matcher", icon="compare_arrows", permissions=["run_matcher_detail_action"])
    def run_matcher_detail_action(self, request, object_id):
        from banking.tasks import run_invoice_matcher_task

        claim = self.get_object(request, object_id)
        result = run_invoice_matcher_task.delay("expenseclaim", claim.pk)
        back_url = reverse("admin:expenses_expenseclaim_change", args=[claim.pk])
        status_url = (
            reverse("admin:banking_matcher_status")
            + f"?task_id={result.id}&task_type=expenseclaim&back_url={back_url}"
        )
        return redirect(status_url)

    def has_run_matcher_detail_action_permission(self, request, object_id):
        claim = self.get_object(request, object_id)
        # Only approved claims are reimbursable — mirrors the candidate filter
        # the transaction-side matcher already applies to expense claims.
        return True

    # ── Custom URLs ──────────────────────────────────────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/add-ticket/",
                self.admin_site.admin_view(
                    ExpenseClaimAddTicketAdminView.as_view(model_admin=self)
                ),
                name="expenses_expenseclaim_add_ticket",
            ),
            path(
                "<path:object_id>/register-payment/",
                self.admin_site.admin_view(
                    ExpenseClaimRegisterPaymentView.as_view(model_admin=self)
                ),
                name="expenses_expenseclaim_register_payment",
            ),
        ]
        return custom + urls


@admin.register(ExpenseTicket)
class ExpenseTicketAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "vendor_name",
        "submitted_by",
        "date_of_purchase",
        "original_amount",
        "ticket_number",
        "claim",
        "reimbursed_status",
        "ocr_status",
    )
    list_filter = ("company","date_of_purchase","claim", "needs_manual_review", "ocr_processed")
    search_fields = ("vendor_name", "vendor_tax_id", "ticket_number", "submitted_by__email")
    autocomplete_fields = ("company", "submitted_by", "provider", "invoice")
    readonly_fields = (
        "claim",
        "file_hash",
        "ocr_processed",
        "ocr_processing",
        "ocr_error",
        "ocr_raw_response",
        "created_at",
        "exchange_rate_from_currency",
        "exchange_rate_to_currency",
        "exchange_rate_date",
        # Both are only ever changed through the "Override Exchange Rate"
        # action below, never by direct editing.
        "exchange_rate_value",
        "converted_amount",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("claim")

    def has_change_permission(self, request, obj=None):
        # Mirrors the lock in ExpenseTicket.save(): once a ticket's claim has
        # moved past draft, its fields can no longer be edited here either -
        # this is what the "Change" link on the claim's Linked Tickets tab
        # opens directly, bypassing the inline's own permission checks.
        if obj is not None and obj.claim_id and obj.claim.status != ExpenseClaimStatus.DRAFT:
            return False
        return super().has_change_permission(request, obj)

    @display(description=_("From Currency"))
    def exchange_rate_from_currency(self, obj):
        return obj.exchange_rate.from_currency if obj.exchange_rate_id else "-"

    @display(description=_("To Currency"))
    def exchange_rate_to_currency(self, obj):
        return obj.exchange_rate.to_currency if obj.exchange_rate_id else "-"

    @display(description=_("Rate Date"))
    def exchange_rate_date(self, obj):
        return obj.exchange_rate.rate_date if obj.exchange_rate_id else "-"

    @display(description=_("Rate"))
    def exchange_rate_value(self, obj):
        if not obj.exchange_rate_id:
            return "-"
        rate = obj.exchange_rate
        return f"{rate.rate} ({_('manually overridden')})" if rate.manually_overridden else str(rate.rate)

    fieldsets = (
        (
            _("Receipt"),
            {
                "fields": (
                    "photo",
                    ("submitted_by", "company"),
                    "claim",
                    "invoice",
                )
            },
        ),
        (
            _("Vendor"),
            {
                "fields": (
                    "provider",
                    "vendor_name",
                    "vendor_tax_id",
                    ("vendor_address", "vendor_city", "vendor_country"),
                )
            },
        ),
        (
            _("Amount"),
            {
                "fields": (
                    "original_amount",
                    ("exchange_rate_from_currency", "exchange_rate_to_currency", "exchange_rate_date"),
                    "exchange_rate_value",
                    "converted_amount",
                    "date_of_purchase",
                    "notes"
                )
            },
        ),
        (
            _("OCR"),
            {
                "fields": (
                    "ocr_processed",
                    "ocr_processing",
                    "ocr_error",
                    "ocr_raw_response",
                    "file_hash",
                )
            },
        ),
    )

    actions = ["send_to_process", "create_claim"]
    actions_detail = [
        "send_to_process_detail_action",
        "create_claim_detail_action",
        "create_invoice_from_ticket_detail_action",
        "override_exchange_rate_detail_action",
    ]

    # ── List actions ────────────────────────────────────────────────────────

    @admin.action(description=_("Process selected tickets with AI"))
    def send_to_process(self, request, queryset):
        ids = list(queryset.values_list("id", flat=True))
        for ticket_id in ids:
            process_expense_ticket.delay(ticket_id)
        self.message_user(request, _("Started OCR processing for %(n)d ticket(s).") % {"n": len(ids)})

    @admin.action(description=_("Create claim from selected tickets"))
    def create_claim(self, request, queryset):
        already_assigned = queryset.filter(claim__isnull=False)
        unassigned = queryset.filter(claim__isnull=True)

        if already_assigned.exists():
            self.message_user(
                request,
                _("%(n)d ticket(s) already belong to a claim and were skipped.") % {"n": already_assigned.count()},
                level="warning",
            )

        if not unassigned.exists():
            return

        first = unassigned.first()
        unassigned_count = unassigned.count()
        claim = ExpenseClaim.objects.create(
            submitted_by=first.submitted_by,
            company=first.company,
        )
        unassigned.update(claim=claim)
        claim.recalculate_total()

        self.message_user(request, _("Claim #%(pk)d created with %(n)d ticket(s).") % {"pk": claim.pk, "n": unassigned_count})
        return redirect(reverse("admin:expenses_expenseclaim_change", args=[claim.pk]))

    # ── Detail actions ───────────────────────────────────────────────────────

    @action(description=_("Run OCR"), url_path="run-ocr", icon="psychology")
    def send_to_process_detail_action(self, request, object_id):
        process_expense_ticket.delay(int(object_id))
        self.message_user(request, _("OCR processing queued."))
        return redirect(reverse("admin:expenses_expenseticket_change", args=[object_id]))

    @action(description=_("Create Claim"), url_path="create-claim", icon="receipt_long")
    def create_claim_detail_action(self, request, object_id):
        ticket = ExpenseTicket.objects.select_related("claim").get(pk=object_id)

        if ticket.claim_id:
            self.message_user(
                request,
                _("This ticket already belongs to Claim #%(pk)d.") % {"pk": ticket.claim_id},
                level="warning",
            )
            return redirect(reverse("admin:expenses_expenseticket_change", args=[object_id]))

        claim = ExpenseClaim.objects.create(
            submitted_by=ticket.submitted_by,
            company=ticket.company,
        )
        ExpenseTicket.objects.filter(pk=object_id).update(claim=claim)
        claim.recalculate_total()

        self.message_user(request, _("Claim #%(pk)d created.") % {"pk": claim.pk})
        return redirect(reverse("admin:expenses_expenseclaim_change", args=[claim.pk]))

    @action(
        description=_("Create Invoice"),
        url_path="create-invoice",
        icon="request_quote",
        permissions=["create_invoice_from_ticket_detail_action"],
    )
    def create_invoice_from_ticket_detail_action(self, request, object_id):
        from invoices.models import Invoice

        ticket = ExpenseTicket.objects.select_related("provider", "company").get(pk=object_id)

        # apply_exchange_rate() always converts into the company's base
        # currency (EUR everywhere in this system) — original_amount stays
        # in whatever currency was printed on the receipt.
        if ticket.converted_amount is not None:
            amount, currency = ticket.converted_amount.amount, "EUR"
        else:
            amount, currency = ticket.original_amount.amount, ticket.original_amount_currency

        invoice = Invoice(
            company=ticket.company,
            provider=ticket.provider,
            invoice_number=ticket.ticket_number or None,
            date_issued=ticket.date_of_purchase,
            net_amount=Money(amount, currency),
            total_amount=Money(amount, currency),
            currency=currency,
            uploaded_by=request.user,
            needs_manual_review=True,
        )
        with ticket.photo.open("rb") as f:
            invoice.file.save(os.path.basename(ticket.photo.name), ContentFile(f.read()), save=False)
        invoice.save()

        ticket.invoice = invoice
        ticket.save(update_fields=["invoice"])

        self.message_user(request, _("Invoice created and linked to this ticket."))
        return redirect(reverse("admin:invoices_invoice_change", args=[invoice.pk]))

    def has_create_invoice_from_ticket_detail_action_permission(self, request, object_id):
        ticket = self.get_object(request, object_id)
        if not ticket or ticket.invoice_id or not ticket.photo or ticket.original_amount is None:
            return False
        return self.has_change_permission(request, ticket)

    @action(
        description=_("Override Exchange Rate"),
        url_path="override-exchange-rate",
        icon="edit",
        permissions=["override_exchange_rate_detail_action"],
        dialog={
            "title": _("Override Exchange Rate"),
            "description": _("Set a manual rate for this ticket's conversion. Other tickets using the fetched rate are unaffected."),
            "form_class": ExpenseTicketExchangeRateOverrideForm,
            "form_submit_text": _("Save Rate"),
        },
    )
    def override_exchange_rate_detail_action(self, request, form, object_id):
        ticket = self.get_object(request, object_id)
        from_currency = form.cleaned_data["from_currency"]
        to_currency = form.cleaned_data["to_currency"]
        new_rate = form.cleaned_data["rate"]

        override, _created = ExchangeRate.objects.update_or_create(
            rate_date=ticket.date_of_purchase,
            from_currency=from_currency,
            to_currency=to_currency,
            manually_overridden=True,
            defaults={"rate": new_rate, "source": ExchangeRateSource.MANUAL},
        )
        ticket.exchange_rate = override
        ticket.converted_amount = override.convert(ticket.original_amount.amount)
        ticket.save(update_fields=["exchange_rate", "converted_amount"])
        if ticket.claim_id:
            ticket.claim.recalculate_total()
        self.message_user(request, _("Exchange rate overridden."))

        # See ExpenseClaimAdmin.reject_claim_detail_action for why this must be
        # a plain 200 with HX-Redirect rather than a real redirect.
        response = HttpResponse(status=200)
        response["HX-Redirect"] = reverse("admin:expenses_expenseticket_change", args=[object_id])
        return response

    def has_override_exchange_rate_detail_action_permission(self, request, object_id):
        ticket = self.get_object(request, object_id)
        # Need an amount and a date to base a rate on - this also covers tickets
        # that never got a conversion applied (e.g. OCR misread the currency
        # and skipped conversion entirely), letting the user create one here.
        if not ticket or ticket.original_amount is None or ticket.date_of_purchase is None:
            return False
        return self.has_change_permission(request, ticket)

    # ── Display helpers ──────────────────────────────────────────────────────

    @display(
        description=_("OCR Status"),
        label={
            "Processing": "warning",
            "OK":         "success",
            "Error":      "danger",
            "Pending":    "default",
        },
    )
    def ocr_status(self, obj):
        if obj.ocr_processing:
            return "Processing"
        if obj.ocr_processed and not obj.ocr_error:
            return "OK"
        if obj.ocr_error:
            return "Error"
        return "Pending"

    @display(
        description=_("Reimbursed"),
        label={
            "Reimbursed": "success",
            "Not reimbursed": "default",
        },
    )
    def reimbursed_status(self, obj):
        if obj.claim_id and obj.claim.status == ExpenseClaimStatus.REIMBURSED:
            return "Reimbursed"
        return "Not reimbursed"

    # ── Custom URLs ──────────────────────────────────────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "upload/",
                self.admin_site.admin_view(
                    ExpenseTicketUploadAdminView.as_view(model_admin=self)
                ),
                name="expenses_expenseticket_upload",
            ),
        ]
        return custom + urls

    def add_view(self, request, form_url="", extra_context=None):
        return redirect("admin:expenses_expenseticket_upload")
