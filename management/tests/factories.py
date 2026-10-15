"""Small keyword-only fixture builders shared by the management test modules."""
import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from company.models import Company, Employee
from configurator.models import Configuration, ConfigurationStatus
from management.models import ProjectFile, ResourcePlanningStatus, ResourcePlanningType
from management.services.resource_planning import create_plannings
from sales.models import Client, DeliveryOrder, DeliveryOrderItem, DeliveryOrderStatus, Quote, QuoteStatus


# ── Sales documents feeding an expediente ─────────────────────────────────────

def make_quote(*, company, client, project_file, status=QuoteStatus.ACCEPTED):
    """A quote on `project_file` (accepted by default)."""
    return Quote.objects.create(
        project_name="Quote", company=company, client=client,
        project_file=project_file, status=status,
    )


def make_configuration(*, company, product, client, quote, project_file=None,
                       units="2", name="Config", status=ConfigurationStatus.COMPLETED):
    """A completed configuration of `product`, optionally on a quote."""
    return Configuration.objects.create(
        name=name, company=company, product=product, client=client, quote=quote,
        project_file=project_file, status=status, units=Decimal(units),
    )


def make_delivery_order(*, company, client, project_file, status=DeliveryOrderStatus.DISPATCHED):
    """A delivery order on `project_file` (dispatched by default)."""
    return DeliveryOrder.objects.create(
        company=company, client=client, project_file=project_file, status=status,
    )


def make_delivery_line(*, delivery_order, source, quantity, description=None, unit_price="10.00"):
    """A delivery order line shipping `quantity` of `source` (linked via generic FK)."""
    return DeliveryOrderItem.objects.create(
        delivery_order=delivery_order,
        description=str(source) if description is None else description,
        quantity=Decimal(quantity), unit_price=Decimal(unit_price),
        source_content_type=ContentType.objects.get_for_model(source),
        source_object_id=source.pk,
    )


# ── Resource planning ─────────────────────────────────────────────────────────

class ResourcePlanningFixturesMixin:
    """Company, two employees, client, expediente and planning type for planning tests."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.company = Company.objects.create(name="Test Co")
        cls.employee_ct = ContentType.objects.get(app_label="company", model="employee")
        hired = datetime.date(2020, 1, 1)
        cls.employee = Employee.objects.create(
            company=cls.company, first_name="Ana", last_name="García", hired_at=hired, is_active=True,
        )
        cls.employee2 = Employee.objects.create(
            company=cls.company, first_name="Luis", last_name="Pérez", hired_at=hired, is_active=True,
        )
        cls.client_obj = Client.objects.create(company=cls.company, name="Test Client")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, reference="EXP-00001", name="Test project",
            status="open", client=cls.client_obj,
        )
        cls.planning_type = ResourcePlanningType.objects.create(
            company=cls.company, name="Installation", color="#3B82F6",
        )
        cls.resources = [(cls.employee_ct, cls.employee.pk)]
        cls.two_resources = [
            (cls.employee_ct, cls.employee.pk),
            (cls.employee_ct, cls.employee2.pk),
        ]
        cls.date_from = datetime.date(2026, 7, 7)   # Tuesday
        cls.date_to = datetime.date(2026, 7, 9)     # Thursday
        cls.t_start = datetime.time(8, 0)
        cls.t_end = datetime.time(17, 0)

    def _create(self, **overrides):
        """create_plannings() for the first employee, Tue–Thu 08–17, ungrouped, weekends kept."""
        params = dict(
            company=self.company,
            project_file=self.project_file,
            planning_type=self.planning_type,
            status=ResourcePlanningStatus.PLANNED,
            notes="",
            resources=self.resources,
            date_from=self.date_from,
            date_to=self.date_to,
            daily_start=self.t_start,
            daily_end=self.t_end,
            exclude_weekends=False,
            is_group=False,
        )
        params.update(overrides)
        return create_plannings(**params)
