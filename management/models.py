from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import models
from django.db.models import Exists, ExpressionWrapper, DurationField, F, OuterRef, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from djmoney.models.fields import MoneyField
from djmoney.money import Money
from company.models import CompanyOwnedModel
from timestamps.models import Timestampable
from sales.models import Client
from management.services import ProjectFileService

@dataclass(frozen=True)
class ProjectFileDistanceSummary:
    """Total driving distance for a ProjectFile's AssetTransactions, and
    which ones couldn't be resolved (missing coordinates, no route found,
    provider error) -- those are excluded from total_km, not counted as 0."""

    total_km: float
    resolved_count: int
    unresolved_transaction_ids: list


@dataclass(frozen=True)
class LaborHoursMarginSummary:
    """Billed labor hours vs. real hours worked on a ProjectFile, and the resulting margin."""

    billed_hours: Decimal
    billed_amount: Decimal
    real_hours: Decimal
    gap_hours: Decimal
    extra_margin: Decimal
    deviation_pct: Decimal
    has_labor_configs: bool
    open_sessions: int


class ProjectFileStatus(models.TextChoices):
    NEW = "new", _("New")
    ACTIVE = "active", _("Active")
    ON_HOLD = "on_hold", _("On Hold")
    COMPLETED = "completed", _("Completed")
    CANCELLED = "cancelled", _("Cancelled")


class ProjectFileDeliveryStatus(models.TextChoices):
    """Derived from the expediente's delivery orders (sent or delivered)."""
    NOT_APPLICABLE = "not_applicable", _("Nothing to deliver")
    NOT_DELIVERED = "not_delivered", _("Not delivered")
    PARTIALLY_DELIVERED = "partially_delivered", _("Partially delivered")
    DELIVERED = "delivered", _("Delivered")


class ProjectFileProductionStatus(models.TextChoices):
    """Derived from the expediente's production tickets."""
    NO_PRODUCTION = "no_production", _("No production")
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In production")
    DONE = "done", _("Production done")


class ProjectFile(Timestampable, CompanyOwnedModel):
    """
    Expediente: The central hub for a project's lifecycle.
    """
    FULLY_INVOICED_PERCENTAGE = Decimal("99.99")
    ADVANCE_PENDING_TOLERANCE = Decimal("0.01")

    reference = models.CharField(
        max_length=50, 
        unique=False,  
        blank=True, 
        null=True,
        verbose_name=_("Reference"),
        help_text=_("Auto-generated internal reference (e.g., EXP-00001)")
    )
    incremental_id = models.PositiveIntegerField(
        db_index=True, 
        blank=True, 
        null=True,
        verbose_name=_("Incremental ID")
    )
    name = models.CharField(
        max_length=255, 
        verbose_name=_("Project Name"),
        help_text=_("Project or site name")
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="project_files",
        verbose_name=_("Client")
    )
    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="project_files",
        verbose_name=_("Asset / Vehicle"),
        help_text=_("Asset (e.g. vehicle) this project file manages."),
    )
    status = models.CharField(
        max_length=20,
        choices=ProjectFileStatus.choices,
        default=ProjectFileStatus.NEW,
        verbose_name=_("Status")
    )
    delivery_status = models.CharField(
        max_length=25,
        choices=ProjectFileDeliveryStatus.choices,
        default=ProjectFileDeliveryStatus.NOT_APPLICABLE,
        db_index=True,
        editable=False,
        verbose_name=_("Delivery"),
        help_text=_("Derived from the sent/delivered delivery orders of this project file."),
    )
    production_status = models.CharField(
        max_length=20,
        choices=ProjectFileProductionStatus.choices,
        default=ProjectFileProductionStatus.NO_PRODUCTION,
        db_index=True,
        editable=False,
        verbose_name=_("Production"),
        help_text=_("Derived from the production tickets of this project file."),
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    start_date = models.DateField(null=True, blank=True, verbose_name=_("Start Date"), default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True, verbose_name=_("End Date"))
    notification_email = models.EmailField(
        blank=True,
        verbose_name=_("Notification Email"),
        help_text=_("Email address for project notifications (e.g., asset transactions). Falls back to the client email if left empty."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
        verbose_name=_("Created by"),
    )

    total_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Amount"),
        help_text=_("Cached total of accepted quotes + direct configurations."),
    )

    total_invoiced = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Invoiced"),
        help_text=_("Cached sum of sales invoices linked to this project file."),
    )

    percentage_invoiced = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("% Invoiced"),
        help_text=_("Cached percentage of total_amount that has been invoiced."),
    )

    total_purchased = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Purchased"),
        help_text=_("Cached committed cost of approved/closed Purchase Orders linked to this project file."),
    )

    total_costs = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Costs"),
        help_text=_("Cached sum of cost entries linked to this project file."),
    )

    advance_pending_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        editable=False,
        verbose_name=_("Advances Pending Deduction"),
        help_text=_("Cached taxable base of non-cancelled advance invoices not yet deducted on a later invoice."),
    )

    class Meta:
        verbose_name = _("Project File")
        verbose_name_plural = _("Project Files")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'reference'], 
                name='unique_reference_per_company'
            )
        ]

    def save(self, *args, **kwargs):
        if not self.reference:
            ref_data = ProjectFileService.generate_next_reference_data(self.company)
            self.reference = ref_data['reference']
            self.incremental_id = ref_data['incremental_id']
        super().save(*args, **kwargs)

    def __str__(self):
        ref = self.reference or f"#{self.pk}"
        return f"[{ref}] {self.name} - {self.client}"

    @property
    def is_fully_invoiced(self) -> bool:
        """True when the expediente is invoiced at (practically) 100% and no
        advance is left to deduct — a 100% advance alone does not count.

        Uses the cached totals so no extra queries are needed.
        """
        total = getattr(self.total_amount, "amount", self.total_amount)
        pct = self.percentage_invoiced or Decimal("0.00")
        advance_pending = (
            getattr(self.advance_pending_amount, "amount", self.advance_pending_amount)
            or Decimal("0.00")
        )
        return bool(
            total and total > 0 and pct >= self.FULLY_INVOICED_PERCENTAGE
            and advance_pending <= self.ADVANCE_PENDING_TOLERANCE
        )

    def has_advance_invoices(self) -> bool:
        """Whether an advance invoice, a cancelled one included, belongs to
        this expediente. An advance always keeps its expediente, so the
        expediente cannot be deleted then."""
        return self.sales_invoices.filter(document_type="advance").exists()

    @property
    def operational_status(self):
        """Where the expediente stands, read off the derived statuses.
        Delivery wins as soon as anything has shipped."""
        if self.delivery_status == ProjectFileDeliveryStatus.DELIVERED:
            return "delivered", _("Delivered")
        if self.delivery_status == ProjectFileDeliveryStatus.PARTIALLY_DELIVERED:
            return "partially_delivered", _("Partially delivered")
        if self.production_status in (
            ProjectFileProductionStatus.PENDING,
            ProjectFileProductionStatus.IN_PROGRESS,
        ):
            return "in_production", _("In production")
        if self.delivery_status == ProjectFileDeliveryStatus.NOT_DELIVERED:
            return "ready_to_deliver", _("Ready to deliver")
        return "nothing_pending", _("Nothing pending")

    def recompute_status(self):
        """Refresh delivery_status / production_status and the lifecycle
        transition they imply. See management.services.project_file_status."""
        from management.services.project_file_status import recompute_project_file_status

        return recompute_project_file_status(self.pk)

    def production_hours_summary(self):
        """Worked hours on this project file: total, per employee, and each employee's sessions."""
        from production.models import ProductionItemSession, ProductionItemSessionStatus

        sessions = list(
            ProductionItemSession.objects.for_project_file(self)
            .completed()
            .select_related("created_by")
            .order_by("created_by__last_name", "created_by__first_name", "start_time")
        )

        employees = {}
        for session in sessions:
            if not session.created_by_id:
                continue
            entry = employees.setdefault(
                session.created_by_id,
                {"employee": session.created_by, "sessions": [], "total_seconds": 0},
            )
            entry["sessions"].append(session)
            entry["total_seconds"] += int(session.duration.total_seconds())

        employee_entries = sorted(employees.values(), key=lambda e: -e["total_seconds"])
        open_sessions = ProductionItemSession.objects.for_project_file(self).filter(
            status=ProductionItemSessionStatus.IN_PROGRESS,
        ).count()

        return {
            "total_seconds": sum(e["total_seconds"] for e in employee_entries),
            "employees": employee_entries,
            "open_sessions": open_sessions,
        }

    def labor_hours_margin_summary(self):
        """Billed labor hours (from labor-flagged Configurations) vs. real hours
        worked (all production sessions on this project file), and the resulting
        extra margin. Real hours are NOT filtered by the labor-hours flag -- real
        labor can happen on tickets not billed as a dedicated labor line.
        """
        from django.db.models.functions import Coalesce
        from configurator.models import Configuration, ConfigurationStatus

        billed = (
            Configuration.objects.filter(
                product__is_labor_hours_template=True,
                status__in=[
                    ConfigurationStatus.SHOP,
                    ConfigurationStatus.COMPLETED,
                    ConfigurationStatus.CONVERTED,
                    ConfigurationStatus.PRODUCTION_DONE,
                ],
            )
            .annotate(resolved_project_file_id=Coalesce("quote__project_file_id", "project_file_id"))
            .filter(resolved_project_file_id=self.pk)
            .aggregate(billed_hours=Sum("units"), billed_amount=Sum("subtotal_price"))
        )
        billed_hours = billed["billed_hours"] or Decimal("0")
        billed_amount = billed["billed_amount"] or Decimal("0")
        if hasattr(billed_amount, "amount"):
            billed_amount = billed_amount.amount

        hours_summary = self.production_hours_summary()
        real_hours = Decimal(hours_summary["total_seconds"]) / Decimal(3600)

        gap_hours = billed_hours - real_hours
        hourly_rate = (billed_amount / billed_hours) if billed_hours else Decimal("0")
        extra_margin = gap_hours * hourly_rate
        deviation_pct = (gap_hours / billed_hours * 100) if billed_hours else Decimal("0")

        return LaborHoursMarginSummary(
            billed_hours=billed_hours,
            billed_amount=billed_amount,
            real_hours=real_hours,
            gap_hours=gap_hours,
            extra_margin=extra_margin,
            deviation_pct=deviation_pct,
            has_labor_configs=billed_hours > 0,
            open_sessions=hours_summary["open_sessions"],
        )

    def distance_summary(self) -> ProjectFileDistanceSummary:
        """Total km driven across this expediente's AssetTransactions that
        have both zones set. Resolves exactly the (from, to) pairs that
        actually occur -- not the full cross product of every zone touched,
        which would blow up fast (e.g. 53 distinct zones is 2756 possible
        ordered pairs, but a real expediente might only ever use 57 of
        them) and could fire one live provider call per unneeded pair.
        Transactions whose pair can't be resolved are listed in
        unresolved_transaction_ids instead of silently counting as 0km."""
        transactions = list(
            self.asset_transactions
            .filter(from_zone__isnull=False, to_zone__isnull=False)
            .select_related("from_zone", "to_zone")
        )
        if not transactions:
            return ProjectFileDistanceSummary(total_km=0.0, resolved_count=0, unresolved_transaction_ids=[])

        try:
            from assets.services.route_optimizer import GoogleRouteOptimizer

            pairs = {(t.from_zone_id, t.to_zone_id): (t.from_zone, t.to_zone) for t in transactions}
            distances = GoogleRouteOptimizer().get_distances_km(list(pairs.values()))
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "distance_summary failed for ProjectFile %s", self.pk, exc_info=True,
            )
            return ProjectFileDistanceSummary(
                total_km=0.0, resolved_count=0,
                unresolved_transaction_ids=[t.pk for t in transactions],
            )

        total_km = 0.0
        resolved_count = 0
        unresolved_ids = []
        for t in transactions:
            km = distances.get((t.from_zone_id, t.to_zone_id))
            if km is None:
                unresolved_ids.append(t.pk)
            else:
                total_km += km
                resolved_count += 1

        return ProjectFileDistanceSummary(
            total_km=round(total_km, 1),
            resolved_count=resolved_count,
            unresolved_transaction_ids=unresolved_ids,
        )

    def production_tickets_summary(self):
        """Every production ticket linked to this project file -- direct FK or,
        for legacy tickets, resolved through the Configuration's quote -- each
        with the hours worked on it (from its own completed sessions).
        Tickets with zero sessions still appear, at 0h.
        """
        from configurator.models import Configuration
        from production.models import ProductionItemSession, ProductionTicket

        config_ct = ContentType.objects.get_for_model(Configuration)
        fallback_config_ids = Configuration.objects.filter(quote__project_file=self).values_list("id", flat=True)
        tickets = list(
            ProductionTicket.objects.filter(
                Q(project_file=self)
                | Q(project_file__isnull=True, content_type=config_ct, object_id__in=fallback_config_ids)
            ).order_by("-created_at")
        )

        seconds_by_ticket = dict(
            ProductionItemSession.objects.completed()
            .filter(production_item__ticket_id__in=[t.id for t in tickets])
            .values("production_item__ticket_id")
            .annotate(total=Sum(ExpressionWrapper(F("end_time") - F("start_time"), output_field=DurationField())))
            .values_list("production_item__ticket_id", "total")
        )

        return [
            {
                "ticket": ticket,
                "worked_hours": (
                    Decimal(seconds_by_ticket[ticket.id].total_seconds()) / Decimal(3600)
                    if ticket.id in seconds_by_ticket else Decimal("0")
                ),
            }
            for ticket in tickets
        ]

    def recalculate_total_amount(self):
        """
        Recompute total_amount, total_invoiced, percentage_invoiced and
        advance_pending_amount.
        All values are subtotals (pre-tax) so the comparison is apples-to-apples.
        Cancelled invoices bill nothing and are left out.
        Call this whenever a related Quote, Configuration, or SalesInvoice changes.
        """
        from django.apps import apps
        Quote = apps.get_model("sales", "Quote")
        Configuration = apps.get_model("configurator", "Configuration")
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")

        # Accepted quotes subtotal (pre-tax, post-discount)
        quote_subtotal = (
            Quote.objects.filter(
                project_file=self,
                status="accepted",
            ).aggregate(total=Sum("discounted_subtotal"))["total"]
            or Decimal("0.00")
        )
        if hasattr(quote_subtotal, "amount"):
            quote_subtotal = quote_subtotal.amount

        # Direct configurations (no quote) subtotal (pre-tax)
        config_subtotal = (
            Configuration.objects.filter(
                project_file=self,
                quote__isnull=True,
                status__in=["shop", "completed", "converted", "production_done"],
            )
            .exclude(product__exclude_from_invoicing=True)
            .aggregate(total=Sum("subtotal_price"))["total"]
            or Decimal("0.00")
        )
        if hasattr(config_subtotal, "amount"):
            config_subtotal = config_subtotal.amount

        # Work entries subtotal
        work_entry_subtotal = (
            self.work_entries.aggregate(total=Sum("total_price"))["total"]
            or Decimal("0.00")
        )
        if hasattr(work_entry_subtotal, "amount"):
            work_entry_subtotal = work_entry_subtotal.amount

        invoiceable = Decimal(str(quote_subtotal)) + Decimal(str(config_subtotal)) + Decimal(str(work_entry_subtotal))

        # Sum of sales invoice taxable bases (pre-tax, post-discount), so the
        # figure is comparable with the quotes' discounted_subtotal above.
        # Source 1: single-PF invoices (project_file FK on invoice)
        from django.db.models import F, Q
        SalesInvoiceItem = apps.get_model("invoices", "SalesInvoiceItem")

        fk_invoiced = (
            SalesInvoice.objects.filter(
                project_file=self,
            ).exclude(
                status="cancelled",
            ).aggregate(total=Sum(F("subtotal_amount") - F("discount_amount")))["total"]
            or Decimal("0.00")
        )
        if hasattr(fk_invoiced, "amount"):
            fk_invoiced = fk_invoiced.amount

        # Source 2: bulk invoices (project_file FK on individual items)
        item_invoiced = (
            SalesInvoiceItem.objects.filter(
                project_file=self,
                invoice__project_file__isnull=True,  # only bulk invoices
            ).exclude(
                invoice__status="cancelled",
            ).aggregate(total=Sum("line_total"))["total"]
            or Decimal("0.00")
        )
        if hasattr(item_invoiced, "amount"):
            item_invoiced = item_invoiced.amount

        invoiced = Decimal(str(fk_invoiced)) + Decimal(str(item_invoiced))

        pct = (
            (invoiced / invoiceable * Decimal("100")).quantize(Decimal("0.01"))
            if invoiceable > 0 else Decimal("0.00")
        )

        # Advances still to deduct: the advance lines' base less the
        # deduction lines (≤ 0) of later invoices, pro formas included.
        advance_lines = SalesInvoiceItem.objects.filter(
            invoice__project_file=self,
            invoice__document_type="advance",
        ).exclude(invoice__status="cancelled")
        advance_base = advance_lines.aggregate(total=Sum("line_total"))["total"]
        advance_deducted = None
        if advance_base is not None:  # no advance, nothing to deduct
            advance_deducted = (
                SalesInvoiceItem.objects.filter(
                    advance_line__in=advance_lines,
                ).exclude(
                    invoice__status="cancelled",
                ).aggregate(total=Sum("line_total"))["total"]
            )
        advance_pending = Decimal("0.00")
        for amount in (advance_base, advance_deducted):
            if amount is not None:
                advance_pending += Decimal(str(getattr(amount, "amount", amount)))

        self.total_amount = invoiceable
        self.total_invoiced = invoiced
        self.percentage_invoiced = pct
        self.advance_pending_amount = advance_pending
        self.save(update_fields=[
            "total_amount", "total_amount_currency",
            "total_invoiced", "total_invoiced_currency",
            "percentage_invoiced",
            "advance_pending_amount", "advance_pending_amount_currency",
        ])

    def recalculate_total_purchased(self):
        """
        Recompute total_purchased: committed cost of Purchase Orders linked to
        this project file. Only APPROVED/CLOSED POs count — draft/pending/
        cancelled POs aren't a real commitment yet, mirroring how only
        "accepted" quotes count on the revenue side.
        """
        from django.apps import apps
        from purchasing.models import PurchaseOrderStatus
        PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")

        purchased = (
            PurchaseOrder.objects.filter(
                project_file=self,
                status__in=[PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.CLOSED],
            ).aggregate(total=Sum("total_amount"))["total"]
            or Decimal("0.00")
        )
        if hasattr(purchased, "amount"):
            purchased = purchased.amount

        self.total_purchased = Decimal(str(purchased))
        self.save(update_fields=["total_purchased", "total_purchased_currency"])

    def recalculate_total_costs(self):
        """
        Recompute total_costs: sum of CostEntry lines on this project file.
        Call this whenever a related CostEntry changes.
        """
        costs = (
            self.cost_entries.aggregate(total=Sum("total_price"))["total"]
            or Decimal("0.00")
        )
        if hasattr(costs, "amount"):
            costs = costs.amount

        self.total_costs = Decimal(str(costs))
        self.save(update_fields=["total_costs", "total_costs_currency"])

    @property
    def estimated_margin(self):
        """total_amount minus committed purchases and cost entries (all pre-tax)."""
        from djmoney.money import Money

        def _amt(value):
            return getattr(value, "amount", None) if value is not None else None

        revenue = _amt(self.total_amount) or Decimal("0.00")
        purchased = _amt(self.total_purchased) or Decimal("0.00")
        costs = _amt(self.total_costs) or Decimal("0.00")
        currency = getattr(self.total_amount, "currency", "EUR")
        return Money(revenue - purchased - costs, currency)


class ResourcePlanningType(CompanyOwnedModel):
    """
    Company-configurable planning categories.
    Examples: "Machine Work", "Maintenance", "Installation", "Transport"
    """
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    color = models.CharField(
        max_length=7,
        default="#3B82F6",
        verbose_name=_("Color"),
        help_text=_("Hex color for calendar display, e.g. #3B82F6"),
    )
    icon = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Icon"),
        help_text=_("Material icon name, e.g. 'build', 'local_shipping'"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        verbose_name = _("Resource Planning Type")
        verbose_name_plural = _("Resource Planning Types")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"],
                name="unique_planning_type_per_company",
            ),
        ]

    def __str__(self):
        return self.name


class ResourcePlanningBacklogLabel(models.Model):
    """
    Global internal labels for backlog items (not company-scoped).
    Examples: "Urgent", "Waiting for material", "Client pending"
    """
    name = models.CharField(max_length=100, unique=True, verbose_name=_("Name"))
    color = models.CharField(
        max_length=7,
        default="#6B7280",
        verbose_name=_("Color"),
        help_text=_("Hex color for display, e.g. #6B7280"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        verbose_name = _("Internal Backlog Label")
        verbose_name_plural = _("Internal Backlog Labels")
        ordering = ["name"]

    def __str__(self):
        return self.name


class ResourcePlanningStatus(models.TextChoices):
    PLANNED = "planned", _("Planned")
    CONFIRMED = "confirmed", _("Confirmed")
    IN_PROGRESS = "in_progress", _("In Progress")
    COMPLETED = "completed", _("Completed")
    CANCELLED = "cancelled", _("Cancelled")


class ResourcePlanningGroup(Timestampable, CompanyOwnedModel):
    """
    Optional grouping for ResourcePlanning records created together.
    Holds no metadata — the group is just an identifier (its PK) that
    lets the admin update or cancel all members in a single action.
    Repetition series (calendar-style repeats) are marked with
    is_repetition: their members have different dates, so they are edited
    through the series editor instead of the regular group form.
    """

    is_repetition = models.BooleanField(default=False, verbose_name=_("Repetition series"))

    class Meta:
        verbose_name = _("Resource Planning Group")
        verbose_name_plural = _("Resource Planning Groups")

    def __str__(self):
        return f"Group #{self.pk}"

    def cancel_all(self) -> None:
        self.plannings.update(status=ResourcePlanningStatus.CANCELLED)


class ResourcePlanning(Timestampable, CompanyOwnedModel):
    """
    Generic resource planning: assign any resource (Employee, Machine, Asset…)
    to a ProjectFile for a specific date/time window.
    """
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        related_name="resource_plannings",
        verbose_name=_("Project File"),
    )

    planning_type = models.ForeignKey(
        ResourcePlanningType,
        on_delete=models.PROTECT,
        related_name="plannings",
        verbose_name=_("Planning Type"),
    )

    group = models.ForeignKey(
        ResourcePlanningGroup,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="plannings",
        verbose_name=_("Group"),
    )

    # Generic FK → the resource being planned
    resource_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        verbose_name=_("Resource Type"),
    )
    resource_object_id = models.PositiveBigIntegerField(
        verbose_name=_("Resource ID"),
    )
    resource = GenericForeignKey("resource_content_type", "resource_object_id")

    # Schedule
    start = models.DateTimeField(verbose_name=_("Start"))
    end = models.DateTimeField(verbose_name=_("End"))
    exclude_weekends = models.BooleanField(default=False, verbose_name=_("Exclude weekends"))

    status = models.CharField(
        max_length=20,
        choices=ResourcePlanningStatus.choices,
        default=ResourcePlanningStatus.PLANNED,
        verbose_name=_("Status"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    show_planning_type = models.BooleanField(
        default=False,
        verbose_name=_("Show planning type"),
        help_text=_("Label this planning with its planning type instead of the project file name in the schedule view."),
    )
    not_done = models.BooleanField(
        default=False,
        verbose_name=_("Not done"),
        help_text=_("Mark this planning as not done as expected; it is highlighted in red in the schedule view."),
    )

    class Meta:
        verbose_name = _("Resource Planning")
        verbose_name_plural = _("Resource Plannings")
        ordering = ["start"]
        indexes = [
            models.Index(
                fields=["resource_content_type", "resource_object_id"],
            ),
            models.Index(fields=["start", "end"]),
            models.Index(fields=["project_file", "start"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(end__gt=models.F("start")),
                name="resource_planning_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.resource} → {self.project_file} ({self.start:%d/%m %H:%M}–{self.end:%H:%M})"

    def cancel(self) -> None:
        self.status = ResourcePlanningStatus.CANCELLED
        self.save(update_fields=["status"])

    def clean(self):
        super().clean()
        if self.start and self.end and self.end <= self.start:
            raise ValidationError({
                "end": _("End must be after start."),
            })

        # Warn about overlaps for the same resource
        if self.resource_content_type_id and self.resource_object_id and self.start and self.end:
            overlaps = ResourcePlanning.objects.filter(
                resource_content_type=self.resource_content_type,
                resource_object_id=self.resource_object_id,
                start__lt=self.end,
                end__gt=self.start,
            ).exclude(
                pk=self.pk,
            ).exclude(
                status=ResourcePlanningStatus.CANCELLED,
            )
            if overlaps.exists():
                raise ValidationError(
                    _("This resource already has a planning that overlaps with the selected time range.")
                )


class ResourcePlanningBacklogStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SCHEDULED = "scheduled", _("Scheduled")
    CANCELLED = "cancelled", _("Cancelled")


class ResourcePlanningBacklogItem(Timestampable, CompanyOwnedModel):
    """
    A planning prepared without dates or resources, waiting to be scheduled.
    Scheduling one creates ResourcePlanning records through the regular bulk
    form and marks the item as scheduled. If those plannings are later
    cancelled the item does NOT revive automatically — use the manual
    "Reopen" admin action instead.
    """

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        related_name="planning_backlog_items",
        verbose_name=_("Project File"),
    )
    planning_type = models.ForeignKey(
        ResourcePlanningType,
        on_delete=models.PROTECT,
        related_name="backlog_items",
        verbose_name=_("Planning Type"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    show_planning_type = models.BooleanField(
        default=False,
        verbose_name=_("Show planning type"),
        help_text=_("Label this planning with its planning type instead of the project file name in the schedule view."),
    )
    estimated_days = models.PositiveSmallIntegerField(
        default=1,
        verbose_name=_("Estimated days"),
        help_text=_("Working days; used to suggest the date range when scheduling."),
    )
    daily_start = models.TimeField(null=True, blank=True, verbose_name=_("Daily start"))
    daily_end = models.TimeField(null=True, blank=True, verbose_name=_("Daily end"))
    exclude_weekends = models.BooleanField(default=True, verbose_name=_("Exclude weekends"))
    priority = models.PositiveIntegerField(
        default=100,
        db_index=True,
        verbose_name=_("Priority"),
        help_text=_("Lower numbers appear first in the backlog."),
    )
    is_reusable = models.BooleanField(
        default=False,
        verbose_name=_("Reusable"),
        help_text=_("Keep this item in the backlog after scheduling so it can be planned again."),
    )
    status = models.CharField(
        max_length=20,
        choices=ResourcePlanningBacklogStatus.choices,
        default=ResourcePlanningBacklogStatus.PENDING,
        verbose_name=_("Status"),
    )
    scheduled_group = models.ForeignKey(
        ResourcePlanningGroup,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="backlog_items",
        verbose_name=_("Scheduled group"),
    )
    scheduled_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Scheduled at"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("Created by"),
    )
    internal_labels = models.ManyToManyField(
        ResourcePlanningBacklogLabel,
        blank=True,
        related_name="backlog_items",
        verbose_name=_("Internal labels"),
    )

    class Meta:
        verbose_name = _("Planning Backlog Item")
        verbose_name_plural = _("Planning Backlog")
        ordering = ["priority", "created_at"]
        indexes = [
            models.Index(fields=["company", "status", "priority"]),
        ]

    def save(self, *args, **kwargs):
        # Inline creation from the ProjectFile admin does not set company;
        # inherit it from the expediente so company-scoped queries see the item.
        if not self.company_id and self.project_file_id:
            self.company_id = self.project_file.company_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.project_file} · {self.planning_type} ({self.get_status_display()})"


# ---------------------------------------------------------------------------
# Work Entries
# ---------------------------------------------------------------------------


class WorkEntryCategory(CompanyOwnedModel):
    """
    Catalog of work entry categories (e.g. Puesta, Recogida, Transporte).
    Used to group WorkEntryTypes so project-file-scoped overrides can
    replace company-wide defaults of the same category.
    """

    name = models.CharField(
        max_length=200,
        verbose_name=_("Name"),
    )
    slug = models.SlugField(
        max_length=100,
        verbose_name=_("Slug"),
        help_text=_("Unique identifier for this category within the company."),
    )
    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
    )

    class Meta:
        verbose_name = _("Work Entry Category")
        verbose_name_plural = _("Work Entry Categories")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "slug"],
                name="unique_work_entry_category_per_company",
            ),
        ]

    def __str__(self):
        return self.name


class WorkEntryDirection(models.TextChoices):
    REVENUE = "revenue", _("Revenue")
    COST = "cost", _("Cost")


class WorkEntryType(CompanyOwnedModel):
    """
    Configurable template defining a category of work (revenue) or cost and
    how to price it. The direction decides which entry model is produced:
    WorkEntry for revenue, CostEntry for cost.
    Can be company-wide (no project file) or scoped to one or more project files.
    When several types share a category and direction, a scoped type takes
    priority over the company-wide default.
    """

    HAS_PROJECT_FILES = "has_project_files"

    direction = models.CharField(
        max_length=10,
        choices=WorkEntryDirection.choices,
        default=WorkEntryDirection.REVENUE,
        verbose_name=_("Direction"),
        help_text=_("Revenue types create Work Entries; cost types create Cost Entries."),
    )
    category = models.ForeignKey(
        WorkEntryCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_entry_types",
        verbose_name=_("Category"),
        help_text=_("Groups this type so project-file overrides replace company-wide defaults of the same category."),
    )
    name = models.CharField(max_length=200, verbose_name=_("Name"))
    unit_label = models.CharField(
        max_length=50,
        default="unit",
        verbose_name=_("Unit Label"),
        help_text=_("e.g. trip, hour, km, unit"),
    )

    applicability_expression = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Applicability Expression"),
        help_text=_(
            "safe_eval condition evaluated against the source object context. "
            "Determines if this type SHOULD be created for a given source. "
            "Leave empty to always apply.\n"
            "Example: transaction_type == 'delivery'"
        ),
    )

    description_template = models.CharField(
        max_length=500,
        blank=True,
        verbose_name=_("Description Template"),
        help_text=_(
            "Template for auto-generating the work entry description. "
            "Use {field_name} placeholders.\n"
            "Example: Delivery: {asset_name} → {to_zone}"
        ),
    )

    # Linked pricing data — only these feed into expression context
    materials = models.ManyToManyField(
        "materials.Material",
        blank=True,
        related_name="work_entry_types",
        verbose_name=_("Materials"),
        help_text=_("Materials whose pricing is available in expression context as materials.<rule_key>."),
    )
    components = models.ManyToManyField(
        "materials.Component",
        blank=True,
        related_name="work_entry_types",
        verbose_name=_("Components"),
        help_text=_("Components whose pricing is available in expression context as components.<slug>."),
    )

    source_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Source Model"),
        help_text=_("Restrict this type to a specific source model (e.g. Asset Transaction)."),
    )

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="custom_work_entry_types",
        verbose_name=_("Project File"),
        help_text=_("Leave empty for company-wide types. Set to restrict to a specific Expediente."),
    )
    project_files = models.ManyToManyField(
        "management.ProjectFile",
        blank=True,
        related_name="scoped_work_entry_types",
        verbose_name=_("Project Files"),
        help_text=_(
            "Leave empty for company-wide types. Select one or more project files "
            "to restrict this type to them."
        ),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        verbose_name = _("Work Entry Type")
        verbose_name_plural = _("Work Entry Types")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "category", "project_file", "direction"],
                name="unique_work_entry_type_per_company_category_project_dir",
            ),
        ]

    def __str__(self):
        suffix = f" [{self.project_file.reference}]" if self.project_file else ""
        return f"{self.name}{suffix}"

    def scoped_project_files(self):
        """Legacy ``project_file`` plus ``project_files``, without duplicates."""
        by_id = {}
        if self.project_file_id:
            by_id[self.project_file_id] = self.project_file
        for project_file in self.project_files.all():
            by_id.setdefault(project_file.pk, project_file)
        return sorted(by_id.values(), key=lambda pf: ((pf.reference or ""), pf.pk))

    def is_project_scoped(self):
        if self.project_file_id:
            return True
        annotated = self.__dict__.get(self.HAS_PROJECT_FILES)
        if annotated is not None:
            return bool(annotated)
        prefetched = getattr(self, "_prefetched_objects_cache", None)
        if prefetched is not None and "project_files" in prefetched:
            return bool(self.project_files.all())
        return self.project_files.exists()

    def apply_legacy_project_file(self, project_files):
        """Keep the single link on one of the given files, or clear it."""
        files = list(project_files)
        ids = {pf.pk for pf in files}
        if not ids:
            self.project_file = None
        elif self.project_file_id not in ids:
            self.project_file = min(files, key=lambda pf: pf.pk)

    def validate_project_scope(self, project_files):
        files = [pf for pf in project_files if pf.pk]
        if self.company_id is not None and any(pf.company_id != self.company_id for pf in files):
            raise ValidationError(
                {
                    "project_files": _(
                        "Project files must belong to the same company as this type."
                    ),
                }
            )
        if not self.category_id or not files:
            return
        ids = [pf.pk for pf in files]
        overlap = (
            type(self)
            .objects.filter(
                company_id=self.company_id,
                category_id=self.category_id,
                direction=self.direction,
            )
            .filter(Q(project_file_id__in=ids) | Q(project_files__in=ids))
            .distinct()
        )
        if self.pk:
            overlap = overlap.exclude(pk=self.pk)
        if overlap.exists():
            raise ValidationError(
                {
                    "project_files": _(
                        "Another type with this category and direction is already "
                        "scoped to one of these project files."
                    ),
                }
            )

    @classmethod
    def applicable_to_source(cls, source):
        """Active types for this source: company-wide ones, plus any scoped to its project file."""
        m2m = cls._meta.get_field("project_files")
        through = m2m.remote_field.through
        link = through._meta.get_field(m2m.m2m_field_name())
        has_project_files = Exists(
            through.objects.filter(**{link.attname: OuterRef("pk")})
        )
        content_type = ContentType.objects.get_for_model(source)
        types = cls.objects.filter(
            Q(source_content_type=content_type) | Q(source_content_type__isnull=True),
            is_active=True,
            company_id=source.company_id,
        ).annotate(**{cls.HAS_PROJECT_FILES: has_project_files}).select_related("category")
        project_file_id = getattr(source, "project_file_id", None)
        if not project_file_id:
            return types
        return types.filter(
            (Q(project_file__isnull=True) & Q(**{cls.HAS_PROJECT_FILES: False}))
            | Q(project_file_id=project_file_id)
            | Q(project_files=project_file_id)
        ).distinct()

    @property
    def entry_model(self):
        """Concrete entry model this type produces."""
        return CostEntry if self.direction == WorkEntryDirection.COST else WorkEntry

    def evaluate_applicability(self, context: dict) -> bool:
        """Check if this type applies to a source given its context."""
        if not self.applicability_expression:
            return True
        from configurator.utils import safe_eval
        try:
            return bool(safe_eval(self.applicability_expression, context))
        except Exception:
            return False

    def compute_unit_price(self, context: dict) -> Decimal:
        """
        Execute pricing steps in order. Each step's output is added to the
        context for subsequent steps. Returns the last step's result rounded
        to 2 decimals (half up), or Decimal('0.00') if no steps exist.
        Intermediate steps keep full precision.

        If `context['components']` is a lazy accessor, it is kept in sync
        with the step_context so component price rules can use variables
        defined by earlier steps.
        """
        from configurator.utils import safe_eval

        steps = self.pricing_steps.all()
        if not steps.exists():
            return Decimal("0.00")

        step_context = dict(context)
        result = Decimal("0.00")

        # Get reference to lazy accessor (if present) to keep it in sync
        lazy_components = step_context.get("components")
        _has_bind = hasattr(lazy_components, "bind_context")

        for step in steps:
            try:
                if _has_bind:
                    lazy_components.bind_context(step_context)
                value = safe_eval(step.expression, step_context)
                step_context[step.variable_name] = value
                result = Decimal(str(value))
            except Exception:
                pass

        return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class WorkEntryPricingStep(models.Model):
    """
    An ordered expression step in a WorkEntryType's pricing chain.
    Each step evaluates an expression and outputs a named variable
    available to subsequent steps. The last step's output becomes
    the unit_price.
    """

    entry_type = models.ForeignKey(
        WorkEntryType,
        on_delete=models.CASCADE,
        related_name="pricing_steps",
        verbose_name=_("Work Entry Type"),
    )

    order = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Order"),
        help_text=_("Execution order (lower = first)."),
        db_index=True
    )

    variable_name = models.SlugField(
        max_length=50,
        verbose_name=_("Variable Name"),
        help_text=_("Output variable name for this step, e.g. 'base_cost'. Available to subsequent steps."),
    )

    expression = models.TextField(
        verbose_name=_("Expression"),
        help_text=_(
            "safe_eval expression. Can use context variables (quantity, default_price, "
            "materials.*, components.*, source fields) plus variables from previous steps.\n"
            "Example: data.distance_km * 3.5"
        ),
    )

    class Meta:
        verbose_name = _("Pricing Step")
        verbose_name_plural = _("Pricing Steps")
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["entry_type", "variable_name"],
                name="unique_pricing_step_variable",
            ),
        ]

    def __str__(self):
        return f"#{self.order} {self.variable_name}"

    def clean(self):
        super().clean()
        from configurator.utils import validate_expression_for_safe_eval
        if self.expression:
            try:
                validate_expression_for_safe_eval(self.expression)
            except ValueError as e:
                raise ValidationError({"expression": str(e)})


class ProjectFileEntryBase(Timestampable, CompanyOwnedModel):
    """
    Shared shape of a priced line on a ProjectFile (Expediente): a quantity,
    a unit price, an optional GenericFK source (e.g. AssetTransaction).
    Concrete subclasses declare `project_file` and `entry_type` so their
    related names stay explicit, and set DIRECTION to the WorkEntryDirection
    their entry_type must have.
    """

    DIRECTION: str = WorkEntryDirection.REVENUE

    description = models.CharField(
        max_length=500,
        verbose_name=_("Description"),
    )

    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("1.00"),
        verbose_name=_("Quantity"),
    )

    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Unit Price"),
    )

    total_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Price"),
        help_text=_("Auto-computed: quantity × unit_price."),
    )

    date = models.DateField(verbose_name=_("Date"))

    # GenericFK to any source object
    source_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Source Type"),
    )
    source_object_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Source ID"),
    )
    source = GenericForeignKey("source_content_type", "source_object_id")

    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        abstract = True
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.description} ({self.total_price})"

    def clean(self):
        super().clean()
        errors = {}
        if self.quantity is not None and self.quantity <= 0:
            errors["quantity"] = _("Quantity must be greater than zero.")
        unit_amount = getattr(self.unit_price, "amount", self.unit_price)
        if unit_amount is not None and unit_amount < 0:
            errors["unit_price"] = _("Unit price cannot be negative.")
        entry_type = getattr(self, "entry_type", None)
        if entry_type is not None and entry_type.direction != self.DIRECTION:
            errors["entry_type"] = _(
                "This type has direction '%(actual)s' but a %(model)s requires '%(expected)s'."
            ) % {
                "actual": entry_type.get_direction_display(),
                "model": self._meta.verbose_name,
                "expected": WorkEntryDirection(self.DIRECTION).label,
            }
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Auto-fill description from template if empty
        if not self.description and self.entry_type and self.entry_type.description_template:
            try:
                from management.services.work_entry import WorkEntryService
                context = WorkEntryService.build_source_context(self.source)
                self.description = self.entry_type.description_template.format(**context)
            except Exception:
                self.description = self.entry_type.name

        # Compute total (quantity may carry 2 decimals, so round the product)
        total = self.unit_price * self.quantity
        amount = getattr(total, "amount", total)
        rounded = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.total_price = Money(rounded, total.currency) if hasattr(total, "currency") else rounded
        super().save(*args, **kwargs)


class WorkEntry(ProjectFileEntryBase):
    """
    A billable unit of work linked to a ProjectFile (Expediente). Revenue side.
    Can optionally reference a source object (e.g. AssetTransaction)
    via GenericFK. Multiple WorkEntries can point to the same source.
    """

    DIRECTION = WorkEntryDirection.REVENUE

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        related_name="work_entries",
        verbose_name=_("Project File"),
    )

    entry_type = models.ForeignKey(
        WorkEntryType,
        on_delete=models.PROTECT,
        related_name="entries",
        verbose_name=_("Work Entry Type"),
    )

    is_locked = models.BooleanField(
        default=False,
        verbose_name=_("Locked"),
        help_text=_("Locked after invoicing to prevent edits."),
    )

    applied_taxes = GenericRelation(
        "invoices.AppliedTax",
        content_type_field="content_type",
        object_id_field="object_id",
    )

    class Meta(ProjectFileEntryBase.Meta):
        verbose_name = _("Work Entry")
        verbose_name_plural = _("Work Entries")
        indexes = [
            models.Index(fields=["source_content_type", "source_object_id"]),
            models.Index(fields=["project_file", "date"]),
        ]

    # Fields frozen once the entry has been invoiced (is_locked=True).
    LOCKED_FIELDS = ("project_file_id", "entry_type_id", "quantity", "unit_price", "date")

    def clean(self):
        super().clean()
        if not self.pk:
            return
        stored = (
            WorkEntry.objects.filter(pk=self.pk)
            .values("is_locked", *self.LOCKED_FIELDS)
            .first()
        )
        if not stored or not stored["is_locked"]:
            return
        for field in self.LOCKED_FIELDS:
            current = getattr(self, field)
            current = getattr(current, "amount", current)
            previous = getattr(stored[field], "amount", stored[field])
            if current != previous:
                raise ValidationError(
                    _("This work entry has been invoiced and is locked; its pricing fields cannot be changed.")
                )

    @property
    def total_with_tax(self):
        """total_price + applied tax amounts, retentions (IRPF) subtracted."""
        from djmoney.money import Money
        tax_sum = Decimal("0")
        for tax in self.applied_taxes.all():
            if tax.calculated_amount is not None:
                amt = tax.calculated_amount.amount if isinstance(tax.calculated_amount, Money) else Decimal(str(tax.calculated_amount))
                if tax.tax_type.behavior == "add":
                    tax_sum += amt
                else:
                    tax_sum -= amt
        base = self.total_price.amount if hasattr(self.total_price, 'amount') else Decimal(str(self.total_price or 0))
        currency = self.total_price.currency if hasattr(self.total_price, 'currency') else 'EUR'
        return Money(base + tax_sum, currency)


class CostEntry(ProjectFileEntryBase):
    """
    A cost incurred on a ProjectFile (Expediente). Cost side, net amounts only
    (input VAT lives on purchase invoices). Produced by cost-direction
    WorkEntryTypes through the same pricing engine as WorkEntry.
    """

    DIRECTION = WorkEntryDirection.COST

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        related_name="cost_entries",
        verbose_name=_("Project File"),
    )

    entry_type = models.ForeignKey(
        WorkEntryType,
        on_delete=models.PROTECT,
        related_name="cost_entries",
        verbose_name=_("Cost Entry Type"),
    )

    class Meta(ProjectFileEntryBase.Meta):
        verbose_name = _("Cost Entry")
        verbose_name_plural = _("Cost Entries")
        indexes = [
            models.Index(fields=["source_content_type", "source_object_id"]),
            models.Index(fields=["project_file", "date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="cost_entry_quantity_positive",
            ),
        ]
