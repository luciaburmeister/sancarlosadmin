from datetime import date

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, get_object_or_404, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display, action

from company.models import Employee
from core.admin.company_access import get_user_companies

from vacations.models import (
    VacationPolicy,
    VacationWallet,
    VacationMovement,
    VacationRequest,
    VacationRequestStatus,
    PolicyAbsenceType,
    WorkCalendar,
    PublicHoliday,
)

from vacations.permissions import has_vacations_privileged_access
from decimal import Decimal
from django.db.models import Sum
from company.utils import get_current_employee


class VacationMovementInline(TabularInline):
    model = VacationMovement
    verbose_name = _("Vacation movement")
    verbose_name_plural = _("Vacation movements")
    extra = 0
    tab = True
    can_delete = True

    fields = (
        "date",
        "movement_type",
        "days",
        "reason",
        "created_by",
        "created_at",
    )
    readonly_fields = ("created_at",)
    ordering = ("-date", "-created_at")

class PolicyAbsenceTypeInline(TabularInline):
    model = PolicyAbsenceType
    extra = 0
    tab = True
    fields = ("name", "code", "consumes_vacation", "requires_approval", "is_active")
    verbose_name = _("Policy absence type")
    verbose_name_plural = _("Policy absence types")


@admin.register(VacationPolicy)
class VacationPolicyAdmin(ModelAdmin):
    list_display = (
        "id",
        "code",
        "name",
        "country",
        "yearly_days",
        "carry_over_max",
        "is_active",
    )
    list_filter = ("country", "is_active")
    search_fields = ("code", "name")
    inlines = (PolicyAbsenceTypeInline,)

    fieldsets = (
        (None, {"fields": ("code", "name", "country", "is_active")}),
        (_("Allowance"), {"fields": ("yearly_days",)}),
        (_("Carryover"), {"fields": ("carry_over_max",)}),
        (_("Rules"), {"fields": ("notes",)}),
    )

    def has_module_permission(self, request):
        return has_vacations_privileged_access(request.user)

    def has_view_permission(self, request, obj=None):
        return has_vacations_privileged_access(request.user)


@admin.register(VacationWallet)
class VacationWalletAdmin(ModelAdmin):
    list_display = (
        "id",
        "employee",
        "policy",
        "year",
        "available_display",
        "pending_display",
        "used_ytd",
        "updated_at",
    )
    list_filter = ("policy", "year")
    search_fields = ("employee__full_name", "employee__email", "policy__name")
    autocomplete_fields = ("employee", "policy")

    inlines = (VacationMovementInline,)

    readonly_fields = ("balance", "accrued_ytd", "used_ytd", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("employee", "policy", "year")}),
        (_("Balance"), {"fields": ("accrued_ytd",)}),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )

    @display(description=_("Available"))
    def available_display(self, obj):
        return obj.balance

    @display(description=_("Pending"))
    def pending_display(self, obj):
        total = (
            VacationRequest.objects.filter(
                wallet=obj,
                status=VacationRequestStatus.PENDING,
            ).aggregate(total=Sum("requested_days"))["total"]
        )
        return total if total is not None else Decimal("0.00")

    @action(description=_("Recompute balance from movements"))
    def recompute_balance(self, request, queryset):
        for wallet in queryset:
            wallet.recompute_from_movements()
        self.message_user(request, _("Selected wallets were recomputed."))

    actions = ("recompute_balance",)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("employee", "policy")

        if has_vacations_privileged_access(request.user):
            return qs.filter(employee__company__in=get_user_companies(request.user))

        employee = get_current_employee(request.user)
        if not employee:
            return qs.none()
        return qs.filter(employee=employee)

    def has_module_permission(self, request):
        return request.user.is_authenticated

    def has_view_permission(self, request, obj=None):
        return request.user.is_authenticated

    def has_change_permission(self, request, obj=None):
        if has_vacations_privileged_access(request.user):
            return True
        return False

    change_form_template = "admin/vacations/vacationwallet/change_form.html"

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        wallet = self.get_object(request, object_id)

        if wallet is None:
            return super().change_view(request, object_id, form_url, extra_context=extra_context)

        if not has_vacations_privileged_access(request.user):
            employee = get_current_employee(request.user)
            if not employee or wallet.employee_id != employee.id:
                raise PermissionDenied

        today = date.today()

        requests_qs = (
            VacationRequest.objects
            .select_related("approved_by", "employee")
            .filter(wallet=wallet)
            .order_by("-start_date", "-created_at")
        )

        upcoming_requests = []
        past_requests = []

        for r in requests_qs:
            can_edit = (
                r.status == VacationRequestStatus.PENDING
                and (
                    has_vacations_privileged_access(request.user)
                    or r.employee.user_id == request.user.id
                )
            )
            edit_url = reverse("admin:vacations_vacationrequest_change", args=[r.pk]) if can_edit else None

            row = {
                "obj": r,
                "can_edit": can_edit,
                "edit_url": edit_url,
            }

            if r.start_date > today or r.status == VacationRequestStatus.PENDING:
                upcoming_requests.append(row)
            elif r.end_date < today:
                past_requests.append(row)
            else:
                upcoming_requests.append(row)

        add_url = reverse("admin:vacations_vacationrequest_add") + f"?employee={wallet.employee_id}&wallet={wallet.id}"

        pending_total = (
            VacationRequest.objects.filter(
                wallet=wallet,
                status=VacationRequestStatus.PENDING,
            ).aggregate(total=Sum("requested_days"))["total"]
        )
        if pending_total is None:
            pending_total = Decimal("0.00")

        extra_context["vacation_dashboard"] = {
            "wallet": wallet,
            "available": wallet.balance,
            "pending": pending_total,
            "used": wallet.used_ytd,
            "upcoming_requests": upcoming_requests,
            "past_requests": past_requests,
            "request_absence_url": add_url,
            "today": today,
        }

        return super().change_view(request, object_id, form_url, extra_context=extra_context)


@admin.register(VacationMovement)
class VacationMovementAdmin(ModelAdmin):
    list_display = (
        "id",
        "wallet",
        "date",
        "movement_type",
        "days",
        "created_by",
        "created_at",
    )
    list_filter = ("movement_type", "date", "wallet__policy", "wallet__year")
    search_fields = ("wallet__employee__full_name", "wallet__employee__email", "reason")
    autocomplete_fields = ("wallet",)
    ordering = ("-date", "-created_at")

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("wallet", "wallet__employee", "wallet__policy")

        if has_vacations_privileged_access(request.user):
            return qs.filter(wallet__employee__company__in=get_user_companies(request.user))

        employee = get_current_employee(request.user)
        if not employee:
            return qs.none()
        return qs.filter(wallet__employee=employee)

    def has_module_permission(self, request):
        return has_vacations_privileged_access(request.user)

    def has_view_permission(self, request, obj=None):
        return has_vacations_privileged_access(request.user)
    
    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(VacationRequest)
class VacationRequestAdmin(ModelAdmin):
    change_form_template = "admin/vacations/vacationrequest/change_form.html"

    list_display = (
    "id",
    "employee",
    "policy",
    "start_date",
    "end_date",
    "requested_days",
    "reason",
    "is_paid",
    "status_badge",
    "approved_by",
    "approved_at",
    "created_at",
    )

    list_filter = ("status", "is_paid", "reason", "start_date", "end_date", "wallet__year", "wallet__policy")
    search_fields = ("employee__full_name", "employee__email", "notes")
    autocomplete_fields = ("employee", "wallet", "approved_by") 
    ordering = ("-created_at",)

    readonly_fields = ("requested_days", "approved_by", "approved_at", "is_paid")


    fieldsets = (
        (None, {"fields": ("employee", "wallet", "reason", "notes")}),
        (_("Dates"), {"fields": ("start_date", "end_date")}),
        (_("Status"), {"fields": ("status", "requested_days", "is_paid", "approved_by", "approved_at")}),
    )

    @display(description=_("Policy"))
    def policy(self, obj):
        return obj.wallet.policy if obj.wallet_id else "-"

    @display(
        description=_("Status"),
        label={
            VacationRequestStatus.PENDING: "warning",
            VacationRequestStatus.APPROVED: "success",
            VacationRequestStatus.REJECTED: "danger",
            VacationRequestStatus.CANCELLED: "info",
        },
    )
    def status_badge(self, obj):
        return obj.status

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("employee", "wallet", "wallet__policy")

        if has_vacations_privileged_access(request.user):
            return qs.filter(employee__company__in=get_user_companies(request.user))

        employee = get_current_employee(request.user)
        if not employee:
            return qs.none()
        return qs.filter(employee=employee)

    def get_autocomplete_fields(self, request):
        if has_vacations_privileged_access(request.user):
            return ("employee", "wallet", "approved_by")
        return ("approved_by",)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not has_vacations_privileged_access(request.user):
            employee = get_current_employee(request.user)

            if db_field.name == "employee":
                kwargs["queryset"] = (
                    Employee.objects.filter(pk=employee.pk) if employee else Employee.objects.none()
                )

            if db_field.name == "wallet":
                if employee:
                    kwargs["queryset"] = (
                        VacationWallet.objects.filter(employee=employee)
                        .select_related("policy")
                        .order_by("-year", "-id")
                    )
                else:
                    kwargs["queryset"] = VacationWallet.objects.none()

        if db_field.name == "absence_type":
            obj_id = request.resolver_match.kwargs.get("object_id")

            if obj_id:
                obj = (
                    VacationRequest.objects
                    .select_related("wallet__policy")
                    .filter(pk=obj_id)
                    .first()
                )
                if obj and obj.wallet_id:
                    kwargs["queryset"] = (
                        PolicyAbsenceType.objects
                        .filter(policy=obj.wallet.policy, is_active=True)
                        .order_by("name")
                    )
                else:
                    kwargs["queryset"] = PolicyAbsenceType.objects.none()

            else:
                wallet_id = request.GET.get("wallet")
                if wallet_id:
                    wallet = (
                        VacationWallet.objects
                        .select_related("policy")
                        .filter(pk=wallet_id)
                        .first()
                    )
                    if wallet:
                        kwargs["queryset"] = (
                            PolicyAbsenceType.objects
                            .filter(policy=wallet.policy, is_active=True)
                            .order_by("name")
                        )
                    else:
                        kwargs["queryset"] = PolicyAbsenceType.objects.none()
                else:
                    kwargs["queryset"] = PolicyAbsenceType.objects.none()

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)

        if not has_vacations_privileged_access(request.user):
            employee = get_current_employee(request.user)
            if employee and "employee" in form.base_fields:
                form.base_fields["employee"].initial = employee
                form.base_fields["employee"].disabled = True


            if employee and "wallet" in form.base_fields:
                current_year = timezone.localdate().year
                wallet = (
                    VacationWallet.objects
                    .filter(employee=employee, year=current_year)
                    .order_by("-id")
                    .first()
                )
                if wallet:
                    form.base_fields["wallet"].initial = wallet
                    form.base_fields["wallet"].disabled = True

        return form

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "my-vacations/",
                self.admin_site.admin_view(self.my_vacations_view),
                name="vacations_my_vacations",
            ),
            path(
                "<int:request_id>/approve/",
                self.admin_site.admin_view(self.approve_view),
                name="vacations_vacationrequest_approve",
            ),
        ]
        return custom + urls

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}

        obj = self.get_object(request, object_id)

        can_approve = request.user.is_superuser or request.user.has_perm("vacations.can_approve_vacations")
        show_button = bool(obj) and can_approve and obj.status == VacationRequestStatus.PENDING

        extra_context["approve_url"] = reverse(
            "admin:vacations_vacationrequest_approve",
            args=[object_id],
        )
        extra_context["show_approve_button"] = show_button

        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        if "employee" in request.GET:
            initial["employee"] = request.GET.get("employee")
        if "wallet" in request.GET:
            initial["wallet"] = request.GET.get("wallet")
        return initial

    def approve_view(self, request, request_id: int):
        if not (request.user.is_superuser or request.user.has_perm("vacations.can_approve_vacations")):
            raise PermissionDenied

        req = get_object_or_404(
            VacationRequest.objects.select_related("wallet"),
            pk=request_id,
        )

        if req.status != VacationRequestStatus.PENDING:
            messages.warning(request, _("Only pending requests can be approved."))
            return redirect(reverse("admin:vacations_vacationrequest_change", args=[req.pk]))

        req.approve(request.user)

        messages.success(request, _("Request approved."))
        return redirect(reverse("admin:vacations_vacationrequest_change", args=[req.pk]))

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not (request.user.is_superuser or request.user.has_perm("vacations.can_approve_vacations")):
            actions.pop("approve_requests", None)
            actions.pop("reject_requests", None)
        return actions

    @action(description=_("Approve selected requests (creates consumption movements)"))
    def approve_requests(self, request, queryset):
        queryset = queryset.filter(status=VacationRequestStatus.PENDING)
        approved_count = 0

        for req in queryset.select_related("wallet"):
            req.approve(request.user)
            approved_count += 1

        self.message_user(request, _(f"Approved {approved_count} request(s)."))

    @action(description=_("Reject selected requests"))
    def reject_requests(self, request, queryset):
        queryset = queryset.filter(status=VacationRequestStatus.PENDING)
        rejected_count = 0

        for req in queryset:
            req.status = VacationRequestStatus.REJECTED
            req.save(update_fields=["status", "updated_at"])
            rejected_count += 1

        self.message_user(request, _(f"Rejected {rejected_count} request(s)."))

    @action(description=_("Cancel selected requests"))
    def cancel_requests(self, request, queryset):
        today = date.today()

        if request.user.is_superuser or request.user.has_perm("vacations.can_approve_vacations"):
            qs = queryset
        else:
            qs = queryset.filter(employee__user=request.user)

        qs = qs.filter(end_date__gte=today,
            status__in=[
                VacationRequestStatus.PENDING,
                VacationRequestStatus.APPROVED,
            ],
        )

        cancelled_count = 0

        for req in qs:
            req.cancel(cancelled_by=request.user)
            cancelled_count += 1

        self.message_user(request, _(f"Cancelled {cancelled_count} request(s)."))

    actions = ("approve_requests", "reject_requests", "cancel_requests")

    def has_module_permission(self, request):
        return request.user.is_authenticated

    def has_view_permission(self, request, obj=None):
        return request.user.is_authenticated

    def has_add_permission(self, request):
        return request.user.is_authenticated

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return True

        if has_vacations_privileged_access(request.user):
            return True

        return (
            obj.employee.user == request.user
            and obj.status == VacationRequestStatus.PENDING
        )

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def save_model(self, request, obj, form, change):
        if not has_vacations_privileged_access(request.user):
            employee = get_current_employee(request.user)
            if not employee:
                raise PermissionDenied(_("This account is not linked to an employee."))

            obj.employee = employee

            if obj.wallet_id is None and obj.start_date:
                obj.wallet = (
                    VacationWallet.objects
                    .filter(employee=employee, year=obj.start_date.year)
                    .order_by("-id")
                    .first()
                )

        super().save_model(request, obj, form, change)

    def my_vacations_view(self, request):
        employee = get_current_employee(request.user)
        if not employee:
            raise PermissionDenied(_("This account is not linked to an employee."))

        today = date.today()
        current_year = today.year

        wallet = (
            VacationWallet.objects
            .filter(employee=employee, year=current_year)
            .select_related("policy")
            .order_by("-year", "-id")
            .first()
        )

        requests_qs = (
            VacationRequest.objects
            .select_related("wallet", "wallet__policy", "approved_by", "employee")
            .filter(employee=employee)
            .order_by("-start_date", "-created_at")
        )

        upcoming_requests = []
        past_requests = []

        for r in requests_qs:
            can_edit = (r.status == VacationRequestStatus.PENDING)
            edit_url = reverse("admin:vacations_vacationrequest_change", args=[r.pk]) if can_edit else None

            row = {
                "obj": r,
                "can_edit": can_edit,
                "edit_url": edit_url,
            }

            if r.start_date > today or r.status == VacationRequestStatus.PENDING:
                upcoming_requests.append(row)
            elif r.end_date < today:
                past_requests.append(row)
            else:
                upcoming_requests.append(row)

        if wallet:
            pending_total = (
                VacationRequest.objects
                .filter(wallet=wallet, status=VacationRequestStatus.PENDING)
                .aggregate(total=Sum("requested_days"))["total"]
            )
            if pending_total is None:
                pending_total = Decimal("0.00")

            available = wallet.balance
            used = wallet.used_ytd
        else:
            pending_total = Decimal("0.00")
            available = None
            used = None

        ctx = dict(
            self.admin_site.each_context(request),
            title=_("My vacations"),
            employee=employee,
            wallet=wallet,
            available=available,
            pending=pending_total,
            used=used,
            upcoming_requests=upcoming_requests,
            past_requests=past_requests,
            request_absence_url=reverse("admin:vacations_vacationrequest_add"),
            today=today,
        )
        return render(request, "admin/vacations/my_vacations.html", ctx)
    
class PublicHolidayInline(TabularInline):
    model = PublicHoliday
    extra = 1
    tab = True
    fields = ("date", "name", "is_national")
    ordering = ("date",)


@admin.register(WorkCalendar)
class WorkCalendarAdmin(ModelAdmin):
    list_display = ("name", "city", "country", "is_active")
    list_filter = ("city", "is_active")
    search_fields = ("name",)
    inlines = (PublicHolidayInline,)

    fieldsets = (
        (None, {"fields": ("name", "city", "country", "is_active")}),
    )

