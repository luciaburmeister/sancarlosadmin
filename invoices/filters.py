from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import DropdownFilter, RadioFilter
from django.contrib.contenttypes.models import ContentType

class HasItemsFilter(RadioFilter):
    title = _("has items")
    parameter_name = "has_items"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Yes")),
            ("no", _("No")),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(items__isnull=False).distinct()
        if self.value() == "no":
            return queryset.filter(items__isnull=True)
        return queryset

class ItemsMissingAccountFilter(RadioFilter):
    title = _("items missing account")
    parameter_name = "items_missing_account"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Yes")),
            ("no", _("No")),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            # Invoices that have items where at least one item has account=None
            return queryset.filter(items__account__isnull=True, items__isnull=False).distinct()
        if self.value() == "no":
            # Invoices where all items have an account
            # Exclude invoices that have any item with no account
            return queryset.exclude(items__account__isnull=True, items__isnull=False)
        return queryset

class DueDatePassedFilter(DropdownFilter):
    title = _("due date passed")
    parameter_name = "due_date_passed"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Overdue")),
            ("no", _("Not overdue")),
        )

    def queryset(self, request, queryset):
        today = timezone.localdate()
        if self.value() == "yes":
            return queryset.filter(due_date__lt=today)
        if self.value() == "no":
            return queryset.exclude(due_date__lt=today)
        return queryset


class IsReconciledFilter(DropdownFilter):
    title = _("bank reconciled")
    parameter_name = "is_reconciled"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Reconciled")),
            ("partial", _("Partially reconciled")),
            ("no", _("Not reconciled")),
        )

    def queryset(self, request, queryset):
        from payments.models import PaymentAllocation

        if self.value() is None:
            return queryset

        ct = ContentType.objects.get_for_model(queryset.model)

        # Invoice IDs that have at least one allocation with a confirmed reconciliation
        reconciled_ids = set(
            PaymentAllocation.objects.filter(
                content_type=ct,
                payment__reconciliation__is_confirmed=True,
            ).values_list("object_id", flat=True)
        )

        # Invoice IDs that have at least one allocation WITHOUT a confirmed reconciliation
        unreconciled_ids = set(
            PaymentAllocation.objects.filter(content_type=ct)
            .exclude(payment__reconciliation__is_confirmed=True)
            .values_list("object_id", flat=True)
        )

        if self.value() == "yes":
            # All allocations are reconciled (none left unreconciled)
            return queryset.filter(pk__in=reconciled_ids - unreconciled_ids)
        if self.value() == "partial":
            # Some reconciled, some not
            return queryset.filter(pk__in=reconciled_ids & unreconciled_ids)
        if self.value() == "no":
            # No reconciled allocations at all
            return queryset.exclude(pk__in=reconciled_ids)
        return queryset


class BalanceStatusFilter(RadioFilter):
    title = _("total balanced")
    parameter_name = "is_total_balanced"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Balanced")),
            ("no", _("Unbalanced")),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(is_total_balanced=True)
        if self.value() == "no":
            return queryset.filter(is_total_balanced=False)
        return queryset


class OcrStatusFilter(RadioFilter):
    title = _("OCR status")
    parameter_name = "ocr_status"

    def lookups(self, request, model_admin):
        return (
            ("processing", _("Processing")),
            ("ok", _("OK")),
            ("error", _("Error")),
            ("not_started", _("Not started")),
        )

    def queryset(self, request, queryset):
        if self.value() == "processing":
            return queryset.filter(ocr_processing=True)
        if self.value() == "ok":
            return queryset.filter(ocr_processed=True).filter(
                Q(ocr_error__isnull=True) | Q(ocr_error="")
            )
        if self.value() == "error":
            return queryset.exclude(Q(ocr_error__isnull=True) | Q(ocr_error=""))
        if self.value() == "not_started":
            return queryset.filter(ocr_processing=False, ocr_processed=False).filter(
                Q(ocr_error__isnull=True) | Q(ocr_error="")
            )
        return queryset
