import unicodedata

from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from timestamps.models import Timestampable
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.fields import ArrayField
from company.models import Employee,Machine,CompanyOwnedModel
from django.utils import timezone
from django.db.models import Q
from django.core.exceptions import ValidationError
from materials.models import Component
from django.conf import settings


class ProductionTicketStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In Progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")

class ProductionTicket(Timestampable, CompanyOwnedModel):
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="production_tickets",
    )
    object_id = models.PositiveBigIntegerField()
    source_object = GenericForeignKey("content_type", "object_id")

    status = models.CharField(
        max_length=20,
        choices=ProductionTicketStatus.choices,
        default=ProductionTicketStatus.PENDING,
        db_index=True,
        verbose_name=_("Status")
    )

    context = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Company-specific context"),
        verbose_name=_("Contexto")
    )

    order_html = models.TextField(blank=True, null=True, verbose_name=_("Order HTML"))
    ticket_html = models.TextField(blank=True, null=True, verbose_name=_("Ticket HTML"))
    work_order_pdf = models.FileField(
        upload_to="production/work_orders/",
        null=True,
        blank=True,
        verbose_name=_("Work Order PDF")
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_tickets",
        verbose_name=_("Project File")
    )

    last_stage_label = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Last Stage")
    )

    stage_order = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_(
            "Ticket-specific stage order. Empty uses the product's default order."
        ),
        verbose_name=_("Production Stage Order"),
    )


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Lets the ProjectFile status signal re-derive the expediente this
        # ticket was moved away from. Deferred (.only()) loads must not be
        # touched here: reading one would re-enter __init__ forever.
        self._original_project_file_id = (
            None if "project_file_id" in self.get_deferred_fields() else self.project_file_id
        )

    def __str__(self):
        return str(self.pk)

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["company", "last_stage_label"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                name="unique_production_ticket_per_source"
            )
        ]
        permissions = [
            ("production_hours_dashboard", "Can view Production Hours Dashboard"),
        ]

    @property
    def resolved_project_file(self):
        """The ticket's expediente, falling back to the source configuration's
        quote when the FK was never set (legacy tickets)."""
        if self.project_file is not None:
            return self.project_file
        try:
            return self.source_object.quote.project_file
        except Exception:
            return None

    def get_stage_order(self):
        if self.stage_order:
            return list(self.stage_order)
        source = self.source_object
        product = getattr(source, "product", None) if source else None
        return list(getattr(product, "production_stages", []) or [])

    @property
    def display_label(self) -> str:
        """A readable name for this ticket. __str__ stays the bare pk since
        admin search/autocomplete depend on it."""
        project_file = self.resolved_project_file
        return str(project_file) if project_file else f"OP-{self.pk}"


    def get_first_incomplete_stage(self):
        items = list(self.items.all())
        if not items:
            return None

        stage_order = self.get_stage_order()
        if not stage_order:
            return None

        for stage_name in stage_order:
            stage_statuses = []

            for item in items:
                planning = item.planning or {}
                if stage_name not in planning:
                    continue

                info = planning.get(stage_name) or {}
                status = info.get("status") or ProductionItemStatus.PENDING
                stage_statuses.append(status)

            if not stage_statuses:
                continue

            if not all(status == ProductionItemStatus.DONE for status in stage_statuses):
                return stage_name

        return None


    def has_incomplete_stage(self, stage_name):
        items = list(self.items.all())
        if not items:
            return False

        stage_statuses = []

        for item in items:
            planning = item.planning or {}
            if stage_name not in planning:
                continue

            info = planning.get(stage_name) or {}
            status = info.get("status") or ProductionItemStatus.PENDING
            stage_statuses.append(status)

        if not stage_statuses:
            return False

        return not all(status == ProductionItemStatus.DONE for status in stage_statuses)

    def is_stage_unstarted(self, stage_name):
        """True only if every relevant item's stage is still pending (nobody has touched it yet)."""
        items = list(self.items.all())
        if not items:
            return False

        stage_statuses = []

        for item in items:
            planning = item.planning or {}
            if stage_name not in planning:
                continue

            info = planning.get(stage_name) or {}
            status = info.get("status") or ProductionItemStatus.PENDING
            stage_statuses.append(status)

        if not stage_statuses:
            return False

        return all(status == ProductionItemStatus.PENDING for status in stage_statuses)

    def item_ids_for_stage(self, stage_name):
        """IDs of items that go through *stage_name*, regardless of their current status."""
        return [
            item.id for item in self.items.all()
            if stage_name in (item.planning or {})
        ]

    def _fresh_items(self):
        """Return ticket items from DB, bypassing any prefetched cache."""
        return list(ProductionItem.objects.filter(ticket_id=self.pk))

    def compute_status_from_items(self):
        items = self._fresh_items()
        if not items:
            return ProductionTicketStatus.PENDING

        stage_statuses = [
            ((info or {}).get("status") or ProductionItemStatus.PENDING)
            for item in items
            for info in (item.planning or {}).values()
        ]

        if not stage_statuses:
            return ProductionTicketStatus.PENDING

        if all(s == ProductionItemStatus.DONE for s in stage_statuses):
            return ProductionTicketStatus.DONE
        if any(s == ProductionItemStatus.IN_PROGRESS for s in stage_statuses):
            return ProductionTicketStatus.IN_PROGRESS
        if any(s == ProductionItemStatus.DONE for s in stage_statuses):
            return ProductionTicketStatus.IN_PROGRESS

        return ProductionTicketStatus.PENDING

    def recompute_status(self):
        if self.status == ProductionTicketStatus.CANCELLED:
            return

        new_status = self.compute_status_from_items()
        new_label = self.compute_last_stage_label()

        if new_status != self.status or new_label != self.last_stage_label:
            ProductionTicket.objects.filter(pk=self.pk).update(
                status=new_status,
                last_stage_label=new_label
            )
            self.status = new_status
            self.last_stage_label = new_label

        # Keep the linked Configuration in lock-step with the ticket status so
        # the UI badge ("En Producción" ↔ "Producción Completada") reflects
        # reality even after reverts or out-of-order saves. This runs on every
        # recompute (not only inside the change branch) so any drift heals.
        from configurator.models import Configuration, ConfigurationStatus
        source = self.source_object
        if isinstance(source, Configuration):
            if new_status == ProductionTicketStatus.DONE:
                target_cfg_status = ConfigurationStatus.PRODUCTION_DONE
            elif source.status == ConfigurationStatus.SHOP:
                target_cfg_status = ConfigurationStatus.SHOP
            else:
                target_cfg_status = ConfigurationStatus.CONVERTED
            if source.status != target_cfg_status:
                Configuration.objects.filter(pk=source.pk).update(
                    status=target_cfg_status
                )
                source.status = target_cfg_status

        # Status is written with update(), so no post_save fires: roll the
        # ticket status up to the expediente explicitly.
        project_file = self.resolved_project_file
        if project_file is not None:
            project_file.recompute_status()

    def compute_last_stage_label(self):
        items = self._fresh_items()
        if not items:
            return None

        # The ticket may deliberately override the product's normal workflow.
        product_stages = self.get_stage_order()

        candidates = [] # (stage_name, weight)
        
        for item in items:
            ordered = item.get_ordered_planning()
            if not ordered:
                continue
            
            # Find the FIRST non-done stage for this item
            for i, (sname, info) in enumerate(ordered):
                status = (info or {}).get("status") or ProductionItemStatus.PENDING
                if status != ProductionItemStatus.DONE:
                    # Use global index as weight to compare across items
                    try:
                        weight = product_stages.index(sname)
                    except ValueError:
                        weight = 1000 + i # Extras at the end
                        
                    candidates.append((sname, weight))
                    break
        
        if not candidates:
            if any(item.planning for item in items):
                from django.utils.translation import gettext as _
                return _("Done")
            return None

        # Aggregate: Pick the stage that is most advanced (highest index in planning)
        # across all items.
        best_stage, _ = min(candidates, key=lambda x: x[1])
        
        return best_stage


class ProductionItemStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In Progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")

class ProductionItem(Timestampable, CompanyOwnedModel):
    ticket = models.ForeignKey(
        ProductionTicket,
        on_delete=models.CASCADE,
        related_name="items",
    )

    data = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Snapshot data (component, dimensions, notes)")
    )

    planning = models.JSONField(
        default=dict,
        help_text=_("Per-item production stages and progress"),
        verbose_name=_("Process Planning")
    )

    status = models.CharField(
        max_length=20,
        default=ProductionItemStatus.PENDING,
        choices=ProductionItemStatus.choices,
        verbose_name=_("Status")
    )

    component = models.ForeignKey(
        Component,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="production_items",
        verbose_name=_("Component"),
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Quantity"),
    )

    length = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Length"),
    )
    
    assigned_employees = models.ManyToManyField(
        Employee,
        blank=True,
        related_name="assigned_production_items",
        verbose_name=_("Assigned Employees")
    )

    ticket_html = models.TextField(blank=True, null=True, verbose_name=_("Ticket HTML"))

    production_stages_order = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Stable stage order derived from BOM"),
        verbose_name=_("Production Stages Order"),
    )

    priority = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("Manual ordering set by supervisors in the terminal; lower is more urgent. Items without priority sort last."),
        verbose_name=_("Priority"),
    )

    class Meta:
        indexes = [
            GinIndex(fields=["planning"]),
        ]
        permissions = [
            ("can_reorder_items", _("Can reorder production items")),
        ]

    def can_finish_pending(self) -> bool:
        """A never-started stage may be closed directly only when the item's
        expediente is fully invoiced (leftover task cleanup)."""
        project_file = self.ticket.resolved_project_file if self.ticket_id else None
        return project_file is not None and project_file.is_fully_invoiced

    def get_ordered_planning(self):
        """
        Returns the planning dictionary items (stage_name, info) sorted by
        the production_stages_order if present, otherwise fallback to product template order.
        """
        planning = self.planning or {}

        # A ticket-level reorder applies to every item; BOM order is only a
        # fallback for stages the ticket order doesn't mention.
        ticket_order = self.ticket.get_stage_order() if self.ticket_id else []
        stage_order = list(ticket_order or self.production_stages_order or [])
        if ticket_order:
            stage_order.extend(
                stage
                for stage in (self.production_stages_order or [])
                if stage not in ticket_order
            )

        if not stage_order and self.ticket and self.ticket.source_object:
            product = getattr(self.ticket.source_object, "product", None)
            if product:
                stage_order = list(product.production_stages or [])
        
        if stage_order is None:
            stage_order = []

        def sort_key(item):
            stage_name = item[0]
            try:
                return (0, stage_order.index(stage_name))
            except ValueError:
                return (1, stage_name)

        return sorted(planning.items(), key=sort_key)

    def get_stage_planning(self, stage_name_nfc: str) -> dict:
        """This item's planning dict for one stage, matched by NFC-normalized stage name."""
        for k, v in (self.planning or {}).items():
            if unicodedata.normalize("NFC", k) == stage_name_nfc:
                return v if isinstance(v, dict) else {}
        return {}

    def save(self, *args, **kwargs):
        if not self.company_id and self.ticket_id:
            self.company = self.ticket.company
        super().save(*args, **kwargs)
        self.ticket.recompute_status()


class ProductionItemSessionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In Progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")


class ProductionItemSessionKind(models.TextChoices):
    ACTIVITY_STARTED = "activity_started", _("Activity started")
    ACTIVITY_FINISHED = "activity_finished", _("Activity finished")
    SESSION_STARTED = "session_started", _("Session started")
    SESSION_FINISHED = "session_finished", _("Session finished")


class HoursGranularity(models.TextChoices):
    DAY = "day", _("Day")
    WEEK = "week", _("Week")
    MONTH = "month", _("Month")
    YEAR = "year", _("Year")


class ProductionItemSessionQuerySet(models.QuerySet):
    def completed(self):
        """Sessions that represent finished, measurable work."""
        return self.filter(
            status=ProductionItemSessionStatus.DONE,
            start_time__isnull=False,
            end_time__isnull=False,
        )

    def for_product(self, product):
        """Sessions belonging to tickets of the given ProductTemplate."""
        from configurator.models import Configuration

        config_ct = ContentType.objects.get_for_model(Configuration)
        config_ids = Configuration.objects.filter(product=product).values_list(
            "id", flat=True
        )
        ticket_ids = ProductionTicket.objects.filter(
            content_type=config_ct,
            object_id__in=config_ids,
        ).values_list("id", flat=True)
        return self.filter(production_item__ticket_id__in=ticket_ids)

    def for_project_file(self, project_file):
        """Sessions belonging to tickets of the given ProjectFile, including
        legacy tickets without a direct FK (matched through their Configuration's quote)."""
        from configurator.models import Configuration

        config_ct = ContentType.objects.get_for_model(Configuration)
        fallback_config_ids = Configuration.objects.filter(
            quote__project_file=project_file,
        ).values_list("id", flat=True)
        ticket_ids = ProductionTicket.objects.filter(
            Q(project_file=project_file)
            | Q(project_file__isnull=True, content_type=config_ct, object_id__in=fallback_config_ids)
        ).values_list("id", flat=True)
        return self.filter(production_item__ticket_id__in=ticket_ids)


class ProductionItemSession(Timestampable, CompanyOwnedModel):
    production_item = models.ForeignKey(
        ProductionItem,
        on_delete=models.CASCADE,
        related_name="sessions",
        verbose_name=_("Production Item"),
    )
    kind = models.CharField(
        max_length=32,
        choices=ProductionItemSessionKind.choices,
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Type"),
    )
    start_time = models.DateTimeField(verbose_name=("Start Time"),null=True,blank=True)
    end_time = models.DateTimeField(verbose_name=_("End Time"),null=True,blank=True)
    comments = ArrayField(
        base_field=models.TextField(),
        default=list,
        blank=True,
        verbose_name=_("Comments"),
        help_text=_("Ordered list of comments left on this session"),
    )
    comment_entries = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Comment entries"),
        help_text=_('Ordered list of {"text", "at"} comment entries; "at" is null for legacy comments'),
    )
    production_stage = models.CharField(max_length=50,db_index=True,verbose_name=_("Production Stage"))
    status = models.CharField(
        max_length=20,
        choices=ProductionItemSessionStatus.choices,
        default=ProductionItemSessionStatus.PENDING,
        db_index=True,
        verbose_name=_("Status")
    )
    created_by = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="created_sessions",
        null=True,
        blank=True,
        verbose_name=_("Created by")
    )
    completed_by = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="completed_sessions",
        null=True,
        blank=True,
        verbose_name=_("Completed by")
    )

    objects = ProductionItemSessionQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["production_item", "production_stage"]),
        ]
        constraints = [models.UniqueConstraint(
                fields=["production_item", "production_stage", "created_by"],
                condition=Q(end_time__isnull=True),
                name="unique_open_session_per_employee_item_stage",
            )]
        
    def clean(self):
        if self.end_time and self.start_time and self.end_time < self.start_time:
            raise ValidationError(_("End time cannot be before start time"))

        if self.status == ProductionItemSessionStatus.IN_PROGRESS and not self.start_time:
            raise ValidationError(_("In-progress sessions must have a start time"))

        if self.status == ProductionItemSessionStatus.DONE:
            if not self.start_time or not self.end_time:
                raise ValidationError(_("Finished sessions must have start and end times"))
            
    def save(self, *args, **kwargs):
        if not self.company_id and self.production_item_id:
            self.company = self.production_item.company
        self.full_clean()
        super().save(*args, **kwargs)

        ticket = self.production_item.ticket
        ticket.recompute_status()

        
    @property
    def duration(self):
        if not self.end_time:
            return None
        return self.end_time - self.start_time

    @property
    def formatted_duration(self):
        """Duration as `Xh Ym`, or an em dash when the session isn't closed."""
        if not self.duration:
            return "—"
        total_seconds = int(self.duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m"

    def start(self, started_by: Employee | None = None):
        if self.status != ProductionItemSessionStatus.PENDING:
            raise ValidationError(_("Only pending sessions can be started"))
        if self.start_time:
            raise ValidationError(_("Session already started"))

        self.start_time = timezone.now()
        self.status = ProductionItemSessionStatus.IN_PROGRESS
        if started_by:
            self.created_by = started_by
        self.save()
    
    def finish(self, completed_by: Employee):
        if self.status not in {
            ProductionItemSessionStatus.IN_PROGRESS,
            ProductionItemSessionStatus.PENDING,
        }:
            raise ValidationError(_("Only active sessions can be finished"))

        self.end_time = timezone.now()
        self.status = ProductionItemSessionStatus.DONE
        self.completed_by = completed_by
        self.save()

    def finish_with_duration(self, completed_by: Employee, worked_seconds: int):
        """Close an open session using an explicit worked duration (supervisor close)."""
        from datetime import timedelta

        if self.status not in {
            ProductionItemSessionStatus.IN_PROGRESS,
            ProductionItemSessionStatus.PENDING,
        }:
            raise ValidationError(_("Only active sessions can be finished"))

        if worked_seconds < 60:
            raise ValidationError(_("Worked time must be at least 1 minute."))

        now = timezone.now()
        if self.start_time:
            self.end_time = self.start_time + timedelta(seconds=worked_seconds)
        else:
            self.end_time = now
            self.start_time = now - timedelta(seconds=worked_seconds)
        self.status = ProductionItemSessionStatus.DONE
        self.completed_by = completed_by
        self.save()

class ProductionResource(Timestampable,CompanyOwnedModel):
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="production_resources",
    )
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    daily_capacity_minutes = models.PositiveIntegerField(default=1440)
    parallel_capacity = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                name="unique_production_resource_per_target",
            )
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def clean(self):
        # Optional: whitelist allowed models
        if not isinstance(self.target, (Employee, Machine)):
            raise ValidationError(_("Unsupported resource type"))

    def __str__(self):
        return str(self.target)
    
class OperationRequirement(models.Model):
    production_item = models.ForeignKey(
        ProductionItem,
        on_delete=models.CASCADE,
        related_name="operation_requirements",
    )

    production_stage = models.CharField(max_length=50, db_index=True)

    required_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        help_text=_("Type of resource required (Employee, Machine, etc.)"),
    )

    quantity = models.PositiveSmallIntegerField(default=1)
    mandatory = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["production_item", "production_stage"]),
            models.Index(fields=["required_content_type"]),
        ]


class PlannedProductionSessionSource(models.TextChoices):
    AUTOMATIC= "auto",_("Automatic")
    MANUAL = "manual", _("Manual")

class PlannedProductionSession(Timestampable, CompanyOwnedModel):
    """One stage of a ticket, planned into a time slot on one or more resources.

    A machine *and* an operator on the same stage is one session with two
    ``PlannedSessionResource`` rows, never two sessions -- ``resources`` is the
    source of truth. ``primary_resource`` is a denormalised anchor (first
    machine, else first employee) kept only for the optimizer and admin;
    ``sync_session_resources`` is the single helper that maintains both.
    """

    production_ticket = models.ForeignKey(
        ProductionTicket,
        on_delete=models.CASCADE,
        related_name="planned_sessions",
        null=True,
        verbose_name=_("Production Ticket")
    )

    production_stage = models.CharField(
        max_length=50,
        db_index=True,
        verbose_name=_("Production Stage"),
    )

    sequence = models.PositiveSmallIntegerField(
        default=0,
        verbose_name=_("Sequence"),
        help_text=_(
            "Position of this stage within its ticket. Stored rather than read "
            "from the product template, so editing the template does not "
            "silently reorder work that is already planned."
        ),
    )

    planned_start = models.DateTimeField(db_index=True,verbose_name=_("Planned Start"))
    planned_end = models.DateTimeField(db_index=True,verbose_name=_("Planned End"))

    primary_resource = models.ForeignKey(
        "ProductionResource",
        on_delete=models.PROTECT,
        related_name="planned_primary_sessions",
        null=True,
        blank=True,
        verbose_name=_("Primary Resource")
    )

    locked = models.BooleanField(default=False,verbose_name=_("Locked"))
    source = models.CharField(
        max_length=20,
        choices=PlannedProductionSessionSource.choices,
        default="auto",
        verbose_name=_("Source")
    )
    notes = models.TextField(blank=True,verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Planned Production Session")
        verbose_name_plural = _("Planned Production Sessions")
        indexes = [
            models.Index(fields=["planned_start", "planned_end"]),
            models.Index(fields=["primary_resource"]),
            models.Index(fields=["company", "planned_start"]),
            models.Index(fields=["production_ticket", "sequence"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "production_ticket",
                    "production_stage",
                    "planned_start",
                    "planned_end",
                ],
                name="unique_ticket_stage_time_slot",
            ),
        ]
        permissions = [
            (
                "can_reschedule_planned_sessions",
                _("Can reschedule planned production sessions"),
            ),
        ]

    def __str__(self):
        ticket = f"OP-{self.production_ticket_id}" if self.production_ticket_id else "OP-?"
        return f"{ticket} · {self.production_stage}"

    @property
    def duration_minutes(self) -> int:
        """Wall-clock length. Use WorkingCalendar for *working* minutes."""
        return int((self.planned_end - self.planned_start).total_seconds() // 60)

    @classmethod
    def conflicts_for(cls, *, primary_resource_id, start, end, exclude_pk=None):
        """Sessions using *primary_resource_id* that overlap [start, end)."""
        qs = cls.objects.filter(
            Q(primary_resource_id=primary_resource_id)
            | Q(resources__resource_id=primary_resource_id),
            planned_start__lt=end,
            planned_end__gt=start,
        ).distinct()
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs

    @classmethod
    def assert_no_conflict(cls, *, primary_resource_id, start, end, exclude_pk=None):
        """Raise PlannedSessionConflictError if [start, end) overlaps another session on *primary_resource_id*."""
        from production.exceptions import PlannedSessionConflictError

        blocker = cls.conflicts_for(
            primary_resource_id=primary_resource_id, start=start, end=end, exclude_pk=exclude_pk,
        ).first()
        if blocker is not None:
            raise PlannedSessionConflictError(blocker.id)

    def unassign(self):
        """Delete this plan — its ticket+stage becomes backlog again."""
        if self.locked:
            raise ValueError(_("This session is locked and can't be removed."))
        self.delete()

class PlannedSessionResource(models.Model):
    planned_session = models.ForeignKey(
        PlannedProductionSession,
        on_delete=models.CASCADE,
        related_name="resources",
    )

    resource = models.ForeignKey(
        ProductionResource,
        on_delete=models.PROTECT,
        related_name="planned_assignments",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["planned_session", "resource"],
                name="unique_resource_per_planned_session",
            )
        ]
        indexes = [
            models.Index(fields=["resource"]),
        ]


class SchedulingRun(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("solved", "Solved"),
        ("applied", "Applied"),
        ("discarded", "Discarded"),
        ("error", "Error"),
    ]

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="scheduling_runs",
    )

    strategy = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    slot_minutes = models.PositiveIntegerField(default=10)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    input_payload = models.JSONField(default=dict, blank=True)
    result_payload = models.JSONField(default=dict, blank=True)

    solver_status = models.CharField(max_length=50, blank=True, default="")
    objective_value = models.FloatField(null=True, blank=True)
    total_margin = models.FloatField(null=True, blank=True)

    applied_at = models.DateTimeField(null=True, blank=True)
    discarded_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(blank=True, default="")

    def __str__(self):
        return f"SchedulingRun #{self.pk} - {self.strategy} - {self.created_at:%Y-%m-%d %H:%M}"


class AssemblyLineStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    READY = "ready", _("Ready")
    IN_PROGRESS = "in_progress", _("In Progress")
    PAUSED = "paused", _("Paused")
    DONE = "done", _("Done")


class AssemblyLine(Timestampable, CompanyOwnedModel):
    stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        verbose_name=_("Production Stages"),
        help_text=_("The stages this assembly line operates on"),
    )

    tickets = models.ManyToManyField(
        ProductionTicket,
        blank=True,
        related_name="assembly_lines",
        verbose_name=_("Production Tickets"),
    )

    parallel_stages = models.BooleanField(
        default=False,
        verbose_name=_("Parallel Stages"),
        help_text=_("When enabled, all incomplete stages can be started simultaneously instead of sequentially."),
    )

    status = models.CharField(
        max_length=20,
        choices=AssemblyLineStatus.choices,
        default=AssemblyLineStatus.DRAFT,
        db_index=True,
        verbose_name=_("Status"),
    )

    started_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="started_assembly_lines",
        verbose_name=_("Started by"),
    )

    started_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Started at"),
    )
    finished_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Finished at"),
    )

    class Meta:
        verbose_name = _("Assembly Line")
        verbose_name_plural = _("Assembly Lines")
        indexes = [
            models.Index(fields=["company", "status"]),
        ]

    def __str__(self):
        stages_str = ", ".join(self.stages) if self.stages else "—"
        return f"Assembly Line #{self.pk} — {stages_str}"
