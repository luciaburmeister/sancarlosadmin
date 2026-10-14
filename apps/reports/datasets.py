"""Dataset catalog: dimensions, measures, and filters the compiler may use."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.models import (
    BooleanField,
    Case,
    CharField,
    DecimalField,
    F,
    FloatField,
    IntegerField,
    Max,
    OuterRef,
    QuerySet,
    Subquery,
    Sum,
    Value,
    When,
    Window,
)
from django.db.models.expressions import RawSQL, RowRange
from django.db.models.functions import (
    Coalesce,
    Concat,
    Extract,
    Greatest,
    TruncMonth,
)

from accounting.models import EntryStatus, JournalEntryLine, JournalType
from apps.reports.constants import AggregationFunction, ReportDomain
from assets.models import (
    Asset,
    AssetOperationalStatus,
    AssetTransaction,
    AssetTypeCategory,
    BillingStatus,
    TransactionTypeChoices,
)
from banking.models import BankTransaction, ReconciliationStatus
from configurator.models import (
    Configuration,
    ConfigurationPlanningStatus,
    ConfigurationStatus,
)
from apps.einvoicing.models import (
    EInvoicingProviderChoices,
    EInvoicingSubmission,
    SubmissionAction,
    SubmissionStatus,
)
from apps.expenses.models import ExpenseClaim, ExpenseClaimStatus
from feedback.models import FeedbackResponse
from finance.models import TaxPeriod, TaxPeriodStatus
from inventory.choices import ItemType
from loans.models import LoanInstallment, LoanInstallmentPaymentStatus
from inventory.models import InventoryItem, InventoryItemQuerySet
from invoices.models import (
    ConfirmationStatus,
    Invoice,
    InvoiceDocumentType,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from management.models import (
    CostEntry,
    ProjectFile,
    ProjectFileDeliveryStatus,
    ProjectFileProductionStatus,
    ProjectFileStatus,
    WorkEntry,
)
from materials.models import Material, UnitChoices
from payments.mixins import annotate_amount_remaining
from payments.models import Payment, PaymentDirection, PaymentMethod, PaymentStatus
from payroll.models import EmployeePayroll, PayrollPeriod
from production.models import ProductionItemSession
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseOrderType,
)
from sales.models import Client, Quote, QuoteStatus
from vacations.models import AbsenceReason, VacationRequest, VacationRequestStatus


@dataclass(frozen=True)
class CatalogField:
    key: str
    orm_path: str
    label: str
    select_related: tuple[str, ...] = ()
    # When True, eq filters use icontains (names, free text).
    text_match: bool = False
    allowed_values: tuple[str, ...] = ()
    # When True, string filter values are coerced to Decimal.
    numeric: bool = False
    # When True, eq true means stock on hand is at or below min_stock.
    low_stock: bool = False
    # When True, eq filters coerce "true"/"false" (any case) onto a
    # BooleanField annotation or column instead of the raw string value.
    boolean: bool = False


@dataclass(frozen=True)
class CatalogMeasure:
    key: str
    orm_path: str
    label: str
    requires_annotation: bool = False
    unit: str = ""


@dataclass(frozen=True)
class DatasetRelation:
    """A navigable link from this dataset to another one.

    ``orm_accessor`` is always the FK *field name* on whichever model
    actually holds the foreign key — never a reverse related_name. Which
    side holds it is given by ``kind``:

    * ``to_one``: THIS dataset's model holds the FK (``orm_accessor`` is a
      field on it). Traversal reads that FK's id off the source row, then
      looks the target up by pk.
    * ``to_many``: the TARGET dataset's model holds the FK (named
      ``orm_accessor`` on it, pointing back at this dataset). Traversal
      filters the target dataset's queryset by ``{orm_accessor: source_pk}``.

    This keeps traversal a single uniform rule in both directions, using
    only each dataset's own ``base_queryset()`` — never a hardcoded Django
    relation outside the catalog.
    """

    key: str
    target_dataset: str
    orm_accessor: str
    kind: str  # "to_one" | "to_many"
    label: str


class BaseDataset(ABC):
    key: str
    label: str
    permission: str
    company_field: str = "company"
    dimensions: tuple[CatalogField, ...] = ()
    measures: tuple[CatalogMeasure, ...] = ()
    filter_fields: tuple[CatalogField, ...] = ()
    select_related: tuple[str, ...] = ()
    relations: tuple[DatasetRelation, ...] = ()
    # Admin change-view URL name a row links to when its dimensions
    # identify exactly one record; None disables row links.
    detail_url_name: str | None = None

    @abstractmethod
    def base_queryset(self) -> QuerySet:
        raise NotImplementedError

    def prepare_queryset(self, qs: QuerySet, spec) -> QuerySet:
        return qs

    def dimension_map(self) -> dict[str, CatalogField]:
        return {d.key: d for d in self.dimensions}

    def measure_map(self) -> dict[str, CatalogMeasure]:
        return {m.key: m for m in self.measures}

    def filter_map(self) -> dict[str, CatalogField]:
        return {f.key: f for f in self.filter_fields}

    def relation_map(self) -> dict[str, DatasetRelation]:
        return {r.key: r for r in self.relations}

    def catalog_for_prompt(self) -> dict:
        def _field_payload(field: CatalogField) -> dict:
            payload = {"key": field.key, "label": field.label}
            if field.text_match:
                payload["text_match"] = True
            if field.numeric:
                payload["numeric"] = True
            if field.allowed_values:
                payload["allowed_values"] = list(field.allowed_values)
            return payload

        return {
            "dataset": self.key,
            "label": self.label,
            "dimensions": [_field_payload(d) for d in self.dimensions],
            "measures": [
                {"key": m.key, "label": m.label} for m in self.measures
            ],
            "filters": [_field_payload(f) for f in self.filter_fields],
            "aggregations": ["count", "count_distinct", "sum", "avg"],
            "filter_operators": ["eq", "gt", "gte", "lt", "lte", "in", "icontains"],
            "charts": ["bar", "line", "pie", "none"],
            "notes": (
                "For client/employee filters use the name as a string "
                "(e.g. 'San Carlos'), never a numeric id. Prefer icontains. "
                "Every entry in parameters must also appear as a filter's "
                "parameter. Do not list date_from/date_to unless matching "
                "date filters bind them. "
                "For amount thresholds (e.g. invoices over 10000) add a "
                "numeric filter on total_amount/total_price/amount with "
                "op gt/gte/lt/lte and the threshold as a string number. "
                "count_distinct counts distinct values of a dimension: pass "
                "that dimension key as measure (e.g. start_date). For an "
                "average of hours worked per day, group only by the person "
                "and request sum of labor_hours plus count_distinct of the "
                "date — never tally aggregate rows yourself. A worked day "
                "is labor_hours gt 0. "
                "For low stock use filter is_low_stock op eq value true "
                "(stock on hand at or below the item minimum). "
                "For 'outstanding debt' / 'deuda pendiente' / 'cuánto nos "
                "debe cada cliente' on sales invoices, aggregate sum on "
                "amount_remaining grouped by client — do NOT use "
                "total_amount, which ignores payments already applied. "
                "To list individual invoices use dimensions like "
                "invoice_number + client + date_issued (not only client). "
                "Quotes have no separate quote number; to list individual "
                "quotes use internal_name + client + date instead. "
                "For 'money we owe' / 'cuánto debemos' / 'deuda con "
                "proveedores' use the purchasing dataset (accounts "
                "payable), aggregating sum on amount_remaining grouped by "
                "provider — never the sales_invoices or quotes dataset, "
                "which only cover money clients owe us. The purchasing "
                "dataset's only counterparty field is provider; if the "
                "prompt says 'cliente'/'client' while asking who we owe "
                "money to, that means provider — group by provider, this "
                "is not a missing field. "
                "On assets, 'vehículos'/'vehicles'/'camiones'/'trucks' "
                "means asset_category eq truck; 'máquinas'/'machines' "
                "means asset_category eq machine. "
                "On bank transactions, 'ingresos'/income means sum of "
                "amount_in, 'gastos'/'salidas'/expenses means sum of "
                "amount_out — never sum the signed amount measure for "
                "either, since it nets both directions together. "
                "On accounting journal lines, 'saldo'/balance per "
                "account means sum the balance measure grouped by "
                "account_code/account_name — never debit or credit "
                "alone. "
                "On production time, hours a person worked are labor_hours: "
                "overlapping sessions of the same employee count once, "
                "because starting a batch opens one session per item. "
                "duration_hours sums every session and repeats that overlap; "
                "use it for how long items spent in a stage, not for a "
                "person's worked time. Prefer labor_hours or duration_hours "
                "over duration_seconds. "
                "On purchasing, 'centro de coste'/'cost center' is a "
                "dimension/filter — group by it to break down spend by "
                "cost center, e.g. sum amount_remaining or total_amount "
                "grouped by cost_center. "
                "On purchase_orders, 'sin facturar'/'not invoiced' means "
                "filter invoicing_status eq not_invoiced; 'facturado "
                "parcialmente' means partially_invoiced; 'facturado "
                "del todo' means fully_invoiced. 'sin recibir'/'pendiente "
                "de recepción'/'not received' means receiving_status eq "
                "not_received; 'recibido parcialmente' means "
                "partially_received; 'recibido del todo' means "
                "fully_received. 'unidades pedidas'/'cantidad pedida' is "
                "sum of ordered_quantity; 'unidades recibidas' is "
                "received_quantity; 'unidades facturadas' is "
                "invoiced_quantity — never total_amount for a quantity. "
                "Combine with status eq approved for 'órdenes aprobadas "
                "sin facturar' or 'aprobadas pendientes de recibir'."
            ),
        }


class ClientsDataset(BaseDataset):
    key = ReportDomain.CLIENTS
    label = "Clients"
    permission = "sales.view_client"
    select_related = ("sale_zone", "agent")
    detail_url_name = "admin:sales_client_change"
    dimensions = (
        CatalogField("name", "name", "Client fiscal/legal name", text_match=True),
        CatalogField(
            "commercial_name",
            "commercial_name",
            "Client commercial/trading name",
            text_match=True,
        ),
        CatalogField("tax_identifier", "tax_identifier", "Fiscal identifier (NIF/CIF)"),
        CatalogField(
            "sale_zone", "sale_zone__name", "Sale zone", ("sale_zone",)
        ),
        CatalogField(
            "agent", "agent__name", "Commissionist agent", ("agent",)
        ),
        CatalogField(
            "payment_method",
            "payment_method",
            "Default payment method",
            allowed_values=tuple(PaymentMethod.values),
        ),
        CatalogField(
            "is_insured",
            "is_insured",
            "Whether the client has credit insurance. Use eq true/false.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
    )
    measures = (
        CatalogMeasure("max_risk", "max_risk", "Maximum credit risk"),
        CatalogMeasure("current_risk", "current_risk", "Current outstanding risk"),
        CatalogMeasure(
            "payment_terms_days",
            "payment_terms_days",
            "Payment terms (days)",
            unit="días",
        ),
    )
    filter_fields = (
        CatalogField("name", "name", "Client fiscal/legal name", text_match=True),
        CatalogField(
            "commercial_name",
            "commercial_name",
            "Client commercial/trading name",
            text_match=True,
        ),
        CatalogField(
            "tax_identifier", "tax_identifier", "Fiscal identifier (NIF/CIF)",
            text_match=True,
        ),
        CatalogField(
            "sale_zone", "sale_zone__name", "Sale zone name", text_match=True
        ),
        CatalogField(
            "agent", "agent__name", "Commissionist agent name", text_match=True
        ),
        CatalogField(
            "payment_method",
            "payment_method",
            "Default payment method",
            allowed_values=tuple(PaymentMethod.values),
        ),
        CatalogField(
            "is_insured",
            "is_insured",
            "Whether the client has credit insurance. Use eq true/false.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
        CatalogField(
            "max_risk", "max_risk", "Maximum credit risk (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "current_risk",
            "current_risk",
            "Current outstanding risk (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )
    relations = (
        DatasetRelation(
            "project_files",
            ReportDomain.PROJECT_FILES,
            "client",
            "to_many",
            "Project files (expedientes)",
        ),
        DatasetRelation(
            "quotes", ReportDomain.QUOTES, "client", "to_many", "Quotes"
        ),
        DatasetRelation(
            "sales_invoices",
            ReportDomain.SALES_INVOICES,
            "client",
            "to_many",
            "Sales invoices",
        ),
        DatasetRelation(
            "payments", ReportDomain.PAYMENTS, "client", "to_many", "Payments"
        ),
    )

    def base_queryset(self) -> QuerySet:
        return Client.objects.all()


class SalesInvoicesDataset(BaseDataset):
    key = ReportDomain.SALES_INVOICES
    label = "Sales invoices"
    permission = "invoices.view_salesinvoice"
    select_related = ("client",)
    detail_url_name = "admin:invoices_salesinvoice_change"
    dimensions = (
        CatalogField(
            "status",
            "status",
            "Invoice document status",
            allowed_values=tuple(SalesInvoiceStatus.values),
        ),
        CatalogField(
            "payment_status",
            "payment_status",
            "Payment status",
            allowed_values=tuple(InvoicePaymentStatus.values),
        ),
        CatalogField(
            "payment_method",
            "payment_method",
            "Payment method",
            allowed_values=tuple(PaymentMethod.values),
        ),
        CatalogField("invoice_number", "invoice_number", "Invoice number"),
        CatalogField("client", "client__name", "Client", ("client",)),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("date_issued", "date_issued", "Date issued"),
        CatalogField(
            "date_issued_month",
            "date_issued_month",
            "Issue month (first day of month). Use this, not date_issued, "
            "when grouping a trend by month.",
        ),
    )
    measures = (
        CatalogMeasure("total_amount", "total_amount", "Total amount"),
        CatalogMeasure("subtotal_amount", "subtotal_amount", "Subtotal"),
        CatalogMeasure(
            "amount_remaining", "db_amount_remaining", "Outstanding balance"
        ),
        CatalogMeasure("amount_paid", "db_amount_paid", "Amount paid"),
    )
    filter_fields = (
        CatalogField(
            "status",
            "status",
            "Invoice document status",
            allowed_values=tuple(SalesInvoiceStatus.values),
        ),
        CatalogField(
            "payment_status",
            "payment_status",
            "Payment status (unpaid/partial/paid). "
            "Use unpaid or in [unpaid,partial] for outstanding invoices.",
            allowed_values=tuple(InvoicePaymentStatus.values),
        ),
        CatalogField(
            "payment_method",
            "payment_method",
            "Expected payment method (transfer, cash, card, cheque, "
            "sepa_direct_debit, confirming).",
            allowed_values=tuple(PaymentMethod.values),
        ),
        CatalogField("date_issued", "date_issued", "Date issued"),
        CatalogField(
            "total_amount",
            "total_amount",
            "Invoice total amount (numeric threshold, e.g. gt 10000)",
            numeric=True,
        ),
        CatalogField(
            "amount_remaining",
            "db_amount_remaining",
            "Outstanding balance still owed (numeric threshold, e.g. gt 0 "
            "for anything with unpaid balance)",
            numeric=True,
        ),
        CatalogField(
            "client", "client__name", "Client name", text_match=True
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    relations = (
        DatasetRelation(
            "client", ReportDomain.CLIENTS, "client", "to_one", "Client"
        ),
        DatasetRelation(
            "project_file",
            ReportDomain.PROJECT_FILES,
            "project_file",
            "to_one",
            "Project file (expediente)",
        ),
    )

    def base_queryset(self) -> QuerySet:
        qs = SalesInvoice.objects.annotate(
            date_issued_month=TruncMonth("date_issued")
        )
        return annotate_amount_remaining(qs, SalesInvoice)


class QuotesDataset(BaseDataset):
    key = ReportDomain.QUOTES
    label = "Quotes"
    permission = "sales.view_quote"
    select_related = ("client",)
    detail_url_name = "admin:sales_quote_change"
    dimensions = (
        CatalogField(
            "internal_name", "internal_name", "Quote identifier/name", text_match=True
        ),
        CatalogField(
            "status",
            "status",
            "Quote status",
            allowed_values=tuple(QuoteStatus.values),
        ),
        CatalogField("client", "client__name", "Client", ("client",)),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("date", "date", "Date"),
        CatalogField(
            "date_month",
            "date_month",
            "Quote month (first day of month). Use this, not date, when "
            "grouping a trend by month.",
        ),
        CatalogField(
            "is_invoiced",
            "is_invoiced",
            "Whether this quote's project file (expediente) has any "
            "invoiced amount yet. Use eq true/false — true means at "
            "least one sales invoice has been issued against it, false "
            "means it has not been invoiced at all (or has no project "
            "file).",
            allowed_values=("true", "false"),
            boolean=True,
        ),
    )
    measures = (
        CatalogMeasure("total_price", "total_price", "Total price"),
        CatalogMeasure("subtotal_price", "subtotal_price", "Subtotal"),
    )
    filter_fields = (
        CatalogField(
            "internal_name", "internal_name", "Quote identifier/name", text_match=True
        ),
        CatalogField(
            "status",
            "status",
            "Quote status",
            allowed_values=tuple(QuoteStatus.values),
        ),
        CatalogField("date", "date", "Date"),
        CatalogField(
            "total_price",
            "total_price",
            "Quote total price (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "client", "client__name", "Client name", text_match=True
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
        CatalogField(
            "is_invoiced",
            "is_invoiced",
            "Whether this quote's project file (expediente) has any "
            "invoiced amount yet. Use eq true/false.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
    )

    relations = (
        DatasetRelation(
            "client", ReportDomain.CLIENTS, "client", "to_one", "Client"
        ),
        DatasetRelation(
            "project_file",
            ReportDomain.PROJECT_FILES,
            "project_file",
            "to_one",
            "Project file (expediente)",
        ),
    )

    def base_queryset(self) -> QuerySet:
        return Quote.objects.annotate(
            date_month=TruncMonth("date"),
            is_invoiced=Case(
                When(project_file__total_invoiced__gt=0, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )


class PaymentsDataset(BaseDataset):
    key = ReportDomain.PAYMENTS
    label = "Payments"
    permission = "payments.view_payment"
    select_related = ("client",)
    detail_url_name = "admin:payments_payment_change"
    dimensions = (
        CatalogField(
            "status", "status", "Status", allowed_values=tuple(PaymentStatus.values)
        ),
        CatalogField(
            "direction",
            "direction",
            "Direction",
            allowed_values=tuple(PaymentDirection.values),
        ),
        CatalogField(
            "method", "method", "Method", allowed_values=tuple(PaymentMethod.values)
        ),
        CatalogField("client", "client__name", "Client", ("client",)),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("payment_date", "payment_date", "Payment date"),
        CatalogField(
            "payment_month",
            "payment_month",
            "Payment month (first day of month). Use this, not payment_date, "
            "when grouping a trend by month.",
        ),
    )
    measures = (
        CatalogMeasure("amount", "amount", "Amount"),
    )
    filter_fields = (
        CatalogField(
            "status", "status", "Status", allowed_values=tuple(PaymentStatus.values)
        ),
        CatalogField(
            "direction",
            "direction",
            "Direction",
            allowed_values=tuple(PaymentDirection.values),
        ),
        CatalogField("payment_date", "payment_date", "Payment date"),
        CatalogField(
            "amount",
            "amount",
            "Payment amount (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "client", "client__name", "Client name", text_match=True
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    relations = (
        DatasetRelation(
            "client", ReportDomain.CLIENTS, "client", "to_one", "Client"
        ),
    )

    def base_queryset(self) -> QuerySet:
        return Payment.objects.annotate(payment_month=TruncMonth("payment_date"))


_LABOR_TABLE = "_report_session_labor"


def annotate_session_labor_hours(qs: QuerySet) -> QuerySet:
    """Hours of each session that no earlier session of the same employee covers.

    Overlapping sessions count once. A window cannot sit inside SUM/GROUP BY,
    so the value is materialized in a temporary table and joined back per row.
    """
    partition = Case(
        When(created_by_id__isnull=True, then=-F("id")),
        default=F("created_by_id"),
        output_field=IntegerField(),
    )
    exclusive = (
        qs.annotate(
            _prev_end=Window(
                expression=Max("end_time"),
                partition_by=[partition],
                order_by=[F("start_time").asc(), F("id").asc()],
                frame=RowRange(start=None, end=-1),
            )
        )
        .annotate(
            labor_hours=Greatest(
                Value(0.0),
                Extract(F("end_time"), "epoch")
                - Greatest(
                    Extract(F("start_time"), "epoch"),
                    Extract(
                        Coalesce(F("_prev_end"), F("start_time")),
                        "epoch",
                    ),
                ),
                output_field=FloatField(),
            )
            / Value(3600.0)
        )
        .values("id", "labor_hours")
    )
    sql, params = exclusive.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {_LABOR_TABLE}")
        cursor.execute(
            f"CREATE TEMP TABLE {_LABOR_TABLE} AS {sql}",
            params,
        )
        cursor.execute(f"CREATE INDEX ON {_LABOR_TABLE} (id)")
    alias = qs.query.base_table or qs.model._meta.db_table
    return qs.annotate(
        labor_hours=RawSQL(
            f"(SELECT labor_hours FROM {_LABOR_TABLE} "
            f'WHERE {_LABOR_TABLE}.id = "{alias}"."id")',
            [],
            output_field=FloatField(),
        )
    )


class ProductionTimeDataset(BaseDataset):
    key = ReportDomain.PRODUCTION_TIME
    label = "Production time"
    permission = "production.view_productionitemsession"
    select_related = ("created_by",)
    detail_url_name = "admin:production_productionitemsession_change"
    dimensions = (
        CatalogField("production_stage", "production_stage", "Stage"),
        CatalogField("status", "status", "Status"),
        CatalogField("employee", "employee_name", "Employee", ("created_by",)),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("start_date", "start_time__date", "Start date"),
        CatalogField(
            "start_month",
            "start_month",
            "Start month (first day of month). Use this, not start_date, "
            "when grouping a trend by month.",
        ),
    )
    measures = (
        CatalogMeasure(
            "duration_seconds",
            "duration_seconds",
            "Duration (seconds)",
            requires_annotation=True,
        ),
        CatalogMeasure(
            "duration_hours",
            "duration_hours",
            "Duration (hours)",
            requires_annotation=True,
            unit="h",
        ),
        CatalogMeasure(
            "labor_hours",
            "labor_hours",
            "Hours worked",
            requires_annotation=True,
            unit="h",
        ),
    )
    filter_fields = (
        CatalogField("production_stage", "production_stage", "Stage"),
        CatalogField("status", "status", "Status"),
        CatalogField("start_time", "start_time", "Start time"),
        CatalogField("start_date", "start_time__date", "Start date"),
        CatalogField(
            "employee", "employee_name", "Employee name", text_match=True
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
        CatalogField(
            "duration_hours", "duration_hours", "Duration (hours)", numeric=True
        ),
        CatalogField(
            "labor_hours", "labor_hours", "Hours worked", numeric=True
        ),
    )

    def prepare_queryset(self, qs: QuerySet, spec) -> QuerySet:
        needs_labor = any(
            agg.function in (AggregationFunction.SUM, AggregationFunction.AVG)
            and agg.measure == "labor_hours"
            for agg in spec.aggregations
        ) or any(filt.field == "labor_hours" for filt in spec.filters)
        if needs_labor:
            return annotate_session_labor_hours(qs)
        return qs

    def base_queryset(self) -> QuerySet:
        duration_seconds_expr = Extract(
            F("end_time"), "epoch", output_field=FloatField()
        ) - Extract(F("start_time"), "epoch", output_field=FloatField())
        return ProductionItemSession.objects.filter(
            start_time__isnull=False,
            end_time__isnull=False,
        ).annotate(
            employee_name=Concat(
                "created_by__first_name",
                Value(" "),
                "created_by__last_name",
                output_field=CharField(),
            ),
            start_month=TruncMonth("start_time"),
            duration_seconds=duration_seconds_expr,
            duration_hours=duration_seconds_expr / Value(3600.0),
        )


class InventoryDataset(BaseDataset):
    key = ReportDomain.INVENTORY
    label = "Inventory"
    permission = "inventory.view_inventoryitem"
    select_related = ("default_warehouse",)
    detail_url_name = "admin:inventory_inventoryitem_change"
    dimensions = (
        CatalogField("code", "code", "Item code"),
        CatalogField("name", "name", "Item name", text_match=True),
        CatalogField(
            "item_type",
            "item_type",
            "Item type",
            allowed_values=tuple(ItemType.values),
        ),
        CatalogField(
            "warehouse",
            "default_warehouse__name",
            "Warehouse",
            ("default_warehouse",),
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
    )
    measures = (
        CatalogMeasure("stock_on_hand", "stock_on_hand", "Stock on hand"),
        CatalogMeasure("min_stock", "min_stock", "Minimum stock threshold"),
        CatalogMeasure("incoming_quantity", "incoming_quantity", "Incoming quantity"),
        CatalogMeasure("reserved_quantity", "reserved_quantity", "Reserved quantity"),
        CatalogMeasure("selling_price", "selling_price", "Selling price"),
        CatalogMeasure("cost_price", "cost_price", "Cost price"),
        CatalogMeasure(
            "stock_value", "stock_value", "Stock value (stock on hand × cost price)"
        ),
    )
    filter_fields = (
        CatalogField("code", "code", "Item code", text_match=True),
        CatalogField("name", "name", "Item name", text_match=True),
        CatalogField(
            "item_type",
            "item_type",
            "Item type",
            allowed_values=tuple(ItemType.values),
        ),
        CatalogField(
            "warehouse", "default_warehouse__name", "Warehouse name", text_match=True
        ),
        CatalogField(
            "stock_on_hand",
            "stock_on_hand",
            "Stock on hand (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "is_low_stock",
            "min_stock",
            "Low stock: on hand at or below this item's minimum. "
            "Use op eq and value true. Excludes composite items.",
            allowed_values=("true", "false"),
            low_stock=True,
        ),
        CatalogField(
            "min_stock",
            "min_stock",
            "Minimum stock threshold (numeric)",
            numeric=True,
        ),
        CatalogField(
            "selling_price",
            "selling_price",
            "Selling price (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "cost_price", "cost_price", "Cost price (numeric threshold)", numeric=True
        ),
        CatalogField(
            "stock_value",
            "stock_value",
            "Stock value: stock on hand × cost price (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return InventoryItem.objects.annotate(
            stock_on_hand=InventoryItemQuerySet.stock_on_hand_expression(),
            stock_value=InventoryItemQuerySet.stock_value_expression(),
        )


class ProjectFilesDataset(BaseDataset):
    key = ReportDomain.PROJECT_FILES
    label = "Project files"
    permission = "management.view_projectfile"
    select_related = ("client",)
    detail_url_name = "admin:management_projectfile_change"
    dimensions = (
        CatalogField(
            "reference",
            "reference",
            "Project file reference (expediente), e.g. EXP-00001",
        ),
        CatalogField("name", "name", "Project name", text_match=True),
        CatalogField(
            "status",
            "status",
            "Project file status",
            allowed_values=tuple(ProjectFileStatus.values),
        ),
        CatalogField(
            "delivery_status",
            "delivery_status",
            "Delivery status",
            allowed_values=tuple(ProjectFileDeliveryStatus.values),
        ),
        CatalogField(
            "production_status",
            "production_status",
            "Production status",
            allowed_values=tuple(ProjectFileProductionStatus.values),
        ),
        CatalogField("client", "client__name", "Client", ("client",)),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("start_date", "start_date", "Start date"),
        CatalogField(
            "start_month",
            "start_month",
            "Start month (first day of month). Use this, not start_date, "
            "when grouping a trend by month.",
        ),
        CatalogField("created_at", "created_at__date", "Created date"),
        CatalogField(
            "is_invoiced",
            "is_invoiced",
            "Whether this project file has any invoiced amount yet. Use "
            "eq true/false — true means at least one sales invoice has "
            "been issued against it, false means it has not been "
            "invoiced at all.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
    )
    measures = (
        CatalogMeasure(
            "total_amount",
            "total_amount",
            "Total amount (accepted quotes + direct configurations)",
        ),
        CatalogMeasure("total_invoiced", "total_invoiced", "Total invoiced"),
        CatalogMeasure("total_purchased", "total_purchased", "Total purchased"),
        CatalogMeasure("total_costs", "total_costs", "Total costs"),
        CatalogMeasure(
            "percentage_invoiced",
            "percentage_invoiced",
            "Percentage invoiced",
            unit="%",
        ),
    )
    filter_fields = (
        CatalogField(
            "reference", "reference", "Project file reference", text_match=True
        ),
        CatalogField("name", "name", "Project name", text_match=True),
        CatalogField(
            "status",
            "status",
            "Project file status",
            allowed_values=tuple(ProjectFileStatus.values),
        ),
        CatalogField(
            "delivery_status",
            "delivery_status",
            "Delivery status",
            allowed_values=tuple(ProjectFileDeliveryStatus.values),
        ),
        CatalogField(
            "production_status",
            "production_status",
            "Production status",
            allowed_values=tuple(ProjectFileProductionStatus.values),
        ),
        CatalogField("start_date", "start_date", "Start date"),
        CatalogField("created_at", "created_at__date", "Created date"),
        CatalogField(
            "is_invoiced",
            "is_invoiced",
            "Whether this project file has any invoiced amount yet. Use "
            "eq true/false.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
        CatalogField(
            "total_amount",
            "total_amount",
            "Project file total amount (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "percentage_invoiced",
            "percentage_invoiced",
            "Percentage invoiced (numeric threshold, 0-100)",
            numeric=True,
        ),
        CatalogField("client", "client__name", "Client name", text_match=True),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    relations = (
        DatasetRelation(
            "client", ReportDomain.CLIENTS, "client", "to_one", "Client"
        ),
        DatasetRelation(
            "quotes", ReportDomain.QUOTES, "project_file", "to_many", "Quotes"
        ),
        DatasetRelation(
            "sales_invoices",
            ReportDomain.SALES_INVOICES,
            "project_file",
            "to_many",
            "Sales invoices",
        ),
    )

    def base_queryset(self) -> QuerySet:
        return ProjectFile.objects.annotate(
            start_month=TruncMonth("start_date"),
            is_invoiced=Case(
                When(total_invoiced__gt=0, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )


class PurchasingDataset(BaseDataset):
    """Accounts payable: incoming invoices from providers (money we owe)."""

    key = ReportDomain.PURCHASING
    label = "Purchasing (accounts payable)"
    permission = "invoices.view_invoice"
    select_related = ("provider", "cost_center")
    detail_url_name = "admin:invoices_invoice_change"
    dimensions = (
        CatalogField("invoice_number", "invoice_number", "Provider invoice number"),
        CatalogField("provider", "provider__name", "Provider/supplier", ("provider",)),
        CatalogField(
            "cost_center",
            "cost_center__name",
            "Cost center",
            ("cost_center",),
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField(
            "payment_status",
            "payment_status",
            "Payment status",
            allowed_values=tuple(InvoicePaymentStatus.values),
        ),
        CatalogField(
            "document_type",
            "document_type",
            "Document type (invoice or credit note)",
            allowed_values=tuple(InvoiceDocumentType.values),
        ),
        CatalogField(
            "confirmation_status",
            "confirmation_status",
            "Review status (has this incoming invoice been confirmed by a "
            "person yet)",
            allowed_values=tuple(ConfirmationStatus.values),
        ),
        CatalogField("date_issued", "date_issued", "Date issued"),
        CatalogField("due_date", "due_date", "Due date"),
        CatalogField(
            "date_issued_month",
            "date_issued_month",
            "Issue month (first day of month). Use this, not date_issued, "
            "when grouping a trend by month.",
        ),
    )
    measures = (
        CatalogMeasure("total_amount", "total_amount", "Total amount"),
        CatalogMeasure("net_amount", "net_amount", "Net amount (before taxes)"),
        CatalogMeasure(
            "amount_remaining", "db_amount_remaining", "Outstanding balance"
        ),
        CatalogMeasure("amount_paid", "db_amount_paid", "Amount paid"),
    )
    filter_fields = (
        CatalogField(
            "provider", "provider__name", "Provider/supplier name", text_match=True
        ),
        CatalogField(
            "cost_center", "cost_center__name", "Cost center name", text_match=True
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
        CatalogField(
            "payment_status",
            "payment_status",
            "Payment status (unpaid/partial/paid). Use unpaid or in "
            "[unpaid,partial] for what we still owe.",
            allowed_values=tuple(InvoicePaymentStatus.values),
        ),
        CatalogField(
            "document_type",
            "document_type",
            "Document type",
            allowed_values=tuple(InvoiceDocumentType.values),
        ),
        CatalogField(
            "confirmation_status",
            "confirmation_status",
            "Review status (has this incoming invoice been confirmed by "
            "a person yet)",
            allowed_values=tuple(ConfirmationStatus.values),
        ),
        CatalogField("date_issued", "date_issued", "Date issued"),
        CatalogField("due_date", "due_date", "Due date"),
        CatalogField(
            "total_amount",
            "total_amount",
            "Invoice total amount (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "amount_remaining",
            "db_amount_remaining",
            "Outstanding balance still owed to the provider (numeric "
            "threshold, e.g. gt 0 for anything still unpaid)",
            numeric=True,
        ),
    )

    def base_queryset(self) -> QuerySet:
        qs = Invoice.objects.annotate(date_issued_month=TruncMonth("date_issued"))
        return annotate_amount_remaining(qs, Invoice)


_PO_QTY = DecimalField(max_digits=14, decimal_places=3)


def _po_line_sum(field: str):
    """Per-order sum of a line quantity, without joining lines into the row."""

    return Coalesce(
        Subquery(
            PurchaseOrderItem.objects.filter(purchase_order=OuterRef("pk"))
            .values("purchase_order")
            .annotate(_total=Sum(field))
            .values("_total")[:1],
            output_field=_PO_QTY,
        ),
        Value(Decimal("0")),
        output_field=_PO_QTY,
    )


def _po_progress(qty_field: str, none: str, partial: str, full: str):
    return Case(
        When(_ordered_qty__lte=0, then=Value(none)),
        When(**{f"{qty_field}__lte": 0}, then=Value(none)),
        When(**{f"{qty_field}__gte": F("_ordered_qty")}, then=Value(full)),
        default=Value(partial),
        output_field=CharField(max_length=24),
    )


class PurchaseOrdersDataset(BaseDataset):
    """Purchase orders sent to providers — before/regardless of any
    incoming invoice. Distinct from PurchasingDataset (the invoices
    themselves, i.e. accounts payable)."""

    key = ReportDomain.PURCHASE_ORDERS
    label = "Purchase orders"
    permission = "purchasing.view_purchaseorder"
    select_related = (
        "provider",
        "project_file",
        "cost_center",
        "requested_by",
        "approved_by",
        "rejected_by",
    )
    detail_url_name = "admin:purchasing_purchaseorder_change"
    dimensions = (
        CatalogField("number", "number", "Purchase order number"),
        CatalogField("provider", "provider__name", "Provider/supplier", ("provider",)),
        CatalogField(
            "po_type",
            "po_type",
            "Purchase order type",
            allowed_values=tuple(PurchaseOrderType.values),
        ),
        CatalogField(
            "status",
            "status",
            "Approval status",
            allowed_values=tuple(PurchaseOrderStatus.values),
        ),
        CatalogField(
            "invoicing_status",
            "invoicing_status",
            "Whether the order has been invoiced yet: not_invoiced, "
            "partially_invoiced, or fully_invoiced",
            allowed_values=("not_invoiced", "partially_invoiced", "fully_invoiced"),
        ),
        CatalogField(
            "receiving_status",
            "receiving_status",
            "Whether the order has been received yet: not_received, "
            "partially_received, or fully_received",
            allowed_values=("not_received", "partially_received", "fully_received"),
        ),
        CatalogField(
            "external_reference",
            "external_reference",
            "Provider reference (albarán / delivery note number)",
        ),
        CatalogField(
            "project_file",
            "project_file__reference",
            "Linked project file (expediente)",
            ("project_file",),
        ),
        CatalogField(
            "cost_center", "cost_center__name", "Cost center", ("cost_center",)
        ),
        CatalogField(
            "requested_by",
            "requested_by__username",
            "Requested by",
            ("requested_by",),
        ),
        CatalogField(
            "approved_by", "approved_by__username", "Approved by", ("approved_by",)
        ),
        CatalogField(
            "rejected_by", "rejected_by__username", "Rejected by", ("rejected_by",)
        ),
        CatalogField(
            "payment_method",
            "payment_method",
            "Expected payment method",
            allowed_values=tuple(PaymentMethod.values),
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("date_ordered", "date_ordered", "Date ordered"),
        CatalogField(
            "date_ordered_month",
            "date_ordered_month",
            "Order month (first day of month). Use this, not "
            "date_ordered, when grouping a trend by month.",
        ),
    )
    measures = (
        CatalogMeasure("total_amount", "total_amount", "Total amount"),
        CatalogMeasure(
            "ordered_quantity", "_ordered_qty", "Quantity ordered (units)"
        ),
        CatalogMeasure(
            "received_quantity", "_received_qty", "Quantity received (units)"
        ),
        CatalogMeasure(
            "invoiced_quantity", "_invoiced_qty", "Quantity invoiced (units)"
        ),
        CatalogMeasure(
            "payment_terms_days",
            "payment_terms_days",
            "Payment terms in days",
            unit="días",
        ),
    )
    filter_fields = (
        CatalogField(
            "provider", "provider__name", "Provider/supplier name", text_match=True
        ),
        CatalogField(
            "po_type",
            "po_type",
            "Purchase order type",
            allowed_values=tuple(PurchaseOrderType.values),
        ),
        CatalogField(
            "status",
            "status",
            "Approval status",
            allowed_values=tuple(PurchaseOrderStatus.values),
        ),
        CatalogField(
            "invoicing_status",
            "invoicing_status",
            "Whether the order has been invoiced yet: not_invoiced, "
            "partially_invoiced, or fully_invoiced",
            allowed_values=("not_invoiced", "partially_invoiced", "fully_invoiced"),
        ),
        CatalogField(
            "receiving_status",
            "receiving_status",
            "Whether the order has been received yet: not_received, "
            "partially_received, or fully_received",
            allowed_values=("not_received", "partially_received", "fully_received"),
        ),
        CatalogField(
            "external_reference",
            "external_reference",
            "Provider reference (albarán number)",
            text_match=True,
        ),
        CatalogField(
            "project_file",
            "project_file__reference",
            "Linked project file reference",
            text_match=True,
        ),
        CatalogField(
            "cost_center", "cost_center__name", "Cost center name", text_match=True
        ),
        CatalogField(
            "requested_by",
            "requested_by__username",
            "Requested by (username)",
            text_match=True,
        ),
        CatalogField(
            "approved_by",
            "approved_by__username",
            "Approved by (username)",
            text_match=True,
        ),
        CatalogField(
            "rejected_by",
            "rejected_by__username",
            "Rejected by (username)",
            text_match=True,
        ),
        CatalogField(
            "payment_method",
            "payment_method",
            "Expected payment method",
            allowed_values=tuple(PaymentMethod.values),
        ),
        CatalogField("date_ordered", "date_ordered", "Date ordered"),
        CatalogField(
            "total_amount",
            "total_amount",
            "Purchase order total amount (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "payment_terms_days",
            "payment_terms_days",
            "Payment terms in days (numeric threshold, e.g. gt 30)",
            numeric=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return PurchaseOrder.objects.annotate(
            date_ordered_month=TruncMonth("date_ordered"),
            _ordered_qty=_po_line_sum("quantity"),
            _invoiced_qty=_po_line_sum("invoiced_quantity"),
            _received_qty=_po_line_sum("received_quantity"),
        ).annotate(
            invoicing_status=_po_progress(
                "_invoiced_qty",
                "not_invoiced",
                "partially_invoiced",
                "fully_invoiced",
            ),
            receiving_status=_po_progress(
                "_received_qty",
                "not_received",
                "partially_received",
                "fully_received",
            ),
        )


class ConfigurationsDataset(BaseDataset):
    key = ReportDomain.CONFIGURATIONS
    label = "Configurations"
    permission = "configurator.view_configuration"
    select_related = ("client", "product")
    detail_url_name = "admin:configurator_configuration_change"
    dimensions = (
        CatalogField("name", "name", "Configuration name", text_match=True),
        CatalogField(
            "status",
            "status",
            "Configuration status",
            allowed_values=tuple(ConfigurationStatus.values),
        ),
        CatalogField(
            "planning_status",
            "planning_status",
            "Planning status",
            allowed_values=tuple(ConfigurationPlanningStatus.values),
        ),
        CatalogField("client", "client__name", "Client", ("client",)),
        CatalogField("product", "product__name", "Product template", ("product",)),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("planned_at", "planned_at", "Planned at"),
        CatalogField("created_at", "created_at__date", "Created date"),
    )
    measures = (
        CatalogMeasure("total_price", "total_price", "Total price"),
        CatalogMeasure("subtotal_price", "subtotal_price", "Subtotal"),
        CatalogMeasure("total_cost", "total_cost", "Total cost"),
        CatalogMeasure("units", "units", "Units"),
    )
    filter_fields = (
        CatalogField("name", "name", "Configuration name", text_match=True),
        CatalogField(
            "status",
            "status",
            "Configuration status",
            allowed_values=tuple(ConfigurationStatus.values),
        ),
        CatalogField(
            "planning_status",
            "planning_status",
            "Planning status",
            allowed_values=tuple(ConfigurationPlanningStatus.values),
        ),
        CatalogField(
            "client", "client__name", "Client name", text_match=True
        ),
        CatalogField(
            "product", "product__name", "Product template name", text_match=True
        ),
        CatalogField(
            "total_price",
            "total_price",
            "Configuration total price (numeric threshold)",
            numeric=True,
        ),
        CatalogField("planned_at", "planned_at", "Planned at"),
        CatalogField("created_at", "created_at__date", "Created date"),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return Configuration.objects.all()


class MaterialsDataset(BaseDataset):
    key = ReportDomain.MATERIALS
    label = "Materials"
    permission = "materials.view_material"
    select_related = ("category", "family", "provider")
    detail_url_name = "admin:materials_material_change"
    dimensions = (
        CatalogField(
            "reference_code", "reference_code", "Material reference code"
        ),
        CatalogField("name", "name", "Material name", text_match=True),
        CatalogField(
            "unit_type",
            "unit_type",
            "Unit of measure",
            allowed_values=tuple(UnitChoices.values),
        ),
        CatalogField("category", "category__name", "Category", ("category",)),
        CatalogField("family", "family__name", "Family", ("family",)),
        CatalogField("provider", "provider__name", "Provider", ("provider",)),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
    )
    measures = (
        CatalogMeasure("cost_per_unit", "cost_per_unit", "Cost per unit"),
    )
    filter_fields = (
        CatalogField(
            "reference_code",
            "reference_code",
            "Material reference code",
            text_match=True,
        ),
        CatalogField("name", "name", "Material name", text_match=True),
        CatalogField(
            "unit_type",
            "unit_type",
            "Unit of measure",
            allowed_values=tuple(UnitChoices.values),
        ),
        CatalogField(
            "category", "category__name", "Category name", text_match=True
        ),
        CatalogField("family", "family__name", "Family name", text_match=True),
        CatalogField(
            "provider", "provider__name", "Provider name", text_match=True
        ),
        CatalogField(
            "cost_per_unit",
            "cost_per_unit",
            "Cost per unit (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return Material.objects.all()


class AssetsDataset(BaseDataset):
    key = ReportDomain.ASSETS
    label = "Assets"
    permission = "assets.view_asset"
    select_related = ("asset_type", "client", "provider", "assigned_to")
    detail_url_name = "admin:assets_asset_change"
    dimensions = (
        CatalogField("name", "name", "Asset name/label", text_match=True),
        CatalogField(
            "asset_type", "asset_type__name", "Asset type", ("asset_type",)
        ),
        CatalogField(
            "asset_category",
            "asset_type__category",
            "Asset type category: machine or truck/vehicle",
            ("asset_type",),
            allowed_values=tuple(AssetTypeCategory.values),
        ),
        CatalogField(
            "operational_status",
            "operational_status",
            "Operational status",
            allowed_values=tuple(AssetOperationalStatus.values),
        ),
        CatalogField(
            "is_active",
            "is_active",
            "Whether the asset is active. Use eq true/false.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
        CatalogField("client", "client__name", "Owner/client", ("client",)),
        CatalogField("provider", "provider__name", "Provider", ("provider",)),
        CatalogField(
            "assigned_to",
            "assigned_to_name",
            "Assigned to (employee)",
            ("assigned_to",),
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
    )
    measures = ()
    filter_fields = (
        CatalogField("name", "name", "Asset name/label", text_match=True),
        CatalogField(
            "asset_category",
            "asset_type__category",
            "Asset type category: machine or truck/vehicle",
            allowed_values=tuple(AssetTypeCategory.values),
        ),
        CatalogField(
            "operational_status",
            "operational_status",
            "Operational status",
            allowed_values=tuple(AssetOperationalStatus.values),
        ),
        CatalogField(
            "is_active",
            "is_active",
            "Whether the asset is active. Use eq true/false.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
        CatalogField(
            "client", "client__name", "Owner/client name", text_match=True
        ),
        CatalogField(
            "provider", "provider__name", "Provider name", text_match=True
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return Asset.objects.annotate(
            assigned_to_name=Concat(
                "assigned_to__first_name",
                Value(" "),
                "assigned_to__last_name",
                output_field=CharField(),
            ),
        )


def _employee_name(*prefix: str) -> Concat:
    path = "__".join(prefix)
    return Concat(
        f"{path}__first_name",
        Value(" "),
        f"{path}__last_name",
        output_field=CharField(),
    )


def _annotate_source_driver(qs: QuerySet) -> QuerySet:
    """Name of the driver on the asset transaction this line was priced from."""

    driver = (
        AssetTransaction.objects.filter(pk=OuterRef("source_object_id"))
        .annotate(_name=_employee_name("performed_by"))
        .values("_name")[:1]
    )
    tx_type = ContentType.objects.get_for_model(AssetTransaction)
    return qs.annotate(
        driver_name=Case(
            When(
                source_content_type=tx_type,
                then=Subquery(driver, output_field=CharField()),
            ),
            default=Value(""),
            output_field=CharField(),
        ),
        entry_month=TruncMonth("date"),
    )


class AssetTransactionsDataset(BaseDataset):
    key = ReportDomain.ASSET_TRANSACTIONS
    label = "Asset transactions"
    permission = "assets.view_assettransaction"
    select_related = ("asset", "performed_by", "project_file", "from_zone", "to_zone")
    detail_url_name = "admin:assets_assettransaction_change"
    dimensions = (
        CatalogField(
            "transaction_type",
            "transaction_type",
            "Trip kind: delivery, movement, receipt (verteo), porte, "
            "machine hours, or laborer hours",
            allowed_values=tuple(TransactionTypeChoices.values),
        ),
        CatalogField(
            "billing_status",
            "billing_status",
            "Whether this trip has been turned into a billable line",
            allowed_values=tuple(BillingStatus.values),
        ),
        CatalogField(
            "driver",
            "driver_name",
            "Driver who performed it (chofer), first and last name. "
            "This dataset has no money: count trips here, then sum "
            "work_entries for revenue and cost_entries for cost.",
            ("performed_by",),
        ),
        CatalogField("asset", "asset__name", "Asset moved", ("asset",)),
        CatalogField(
            "project_file",
            "project_file__reference",
            "Project file reference (expediente)",
            ("project_file",),
        ),
        CatalogField(
            "from_zone", "from_zone__name", "Origin zone", ("from_zone",)
        ),
        CatalogField(
            "to_zone", "to_zone__name", "Destination zone", ("to_zone",)
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("date", "date", "Transaction date"),
        CatalogField(
            "transaction_month",
            "transaction_month",
            "Transaction month. Use this, not date, when grouping by month.",
        ),
    )
    measures = ()
    filter_fields = (
        CatalogField(
            "transaction_type",
            "transaction_type",
            "Trip kind",
            allowed_values=tuple(TransactionTypeChoices.values),
        ),
        CatalogField(
            "billing_status",
            "billing_status",
            "Billing status",
            allowed_values=tuple(BillingStatus.values),
        ),
        CatalogField(
            "driver",
            "driver_name",
            "Driver name (chofer)",
            text_match=True,
        ),
        CatalogField("asset", "asset__name", "Asset name", text_match=True),
        CatalogField(
            "project_file",
            "project_file__reference",
            "Project file reference",
            text_match=True,
        ),
        CatalogField("date", "date", "Transaction date"),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )
    relations = (
        DatasetRelation(
            "asset", ReportDomain.ASSETS, "asset", "to_one", "Asset"
        ),
        DatasetRelation(
            "project_file",
            ReportDomain.PROJECT_FILES,
            "project_file",
            "to_one",
            "Project file",
        ),
    )

    def base_queryset(self) -> QuerySet:
        return AssetTransaction.objects.annotate(
            driver_name=_employee_name("performed_by"),
            transaction_month=TruncMonth("date"),
        )


class _PricedEntryDataset(BaseDataset):
    """Revenue or cost lines on an expediente, optionally from a trip."""

    entry_model = None
    select_related = ("entry_type", "project_file", "project_file__client")
    dimensions = (
        CatalogField(
            "driver",
            "driver_name",
            "Driver (chofer) of the asset transaction this line was "
            "priced from. Empty when the line has another source. "
            "Filter by the driver's name for rentabilidad.",
        ),
        CatalogField(
            "entry_type", "entry_type__name", "Entry type", ("entry_type",)
        ),
        CatalogField(
            "project_file",
            "project_file__reference",
            "Project file reference (expediente)",
            ("project_file",),
        ),
        CatalogField(
            "client",
            "project_file__client__name",
            "Client of the project file",
            ("project_file__client",),
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("date", "date", "Entry date"),
        CatalogField(
            "entry_month",
            "entry_month",
            "Entry month. Use this, not date, when grouping by month.",
        ),
    )
    measures = (
        CatalogMeasure(
            "total_price",
            "total_price",
            "Line total: quantity × unit price",
        ),
        CatalogMeasure("quantity", "quantity", "Quantity"),
    )
    filter_fields = (
        CatalogField(
            "driver", "driver_name", "Driver name (chofer)", text_match=True
        ),
        CatalogField(
            "entry_type", "entry_type__name", "Entry type name", text_match=True
        ),
        CatalogField(
            "project_file",
            "project_file__reference",
            "Project file reference",
            text_match=True,
        ),
        CatalogField(
            "client",
            "project_file__client__name",
            "Client name",
            text_match=True,
        ),
        CatalogField("date", "date", "Entry date"),
        CatalogField(
            "total_price",
            "total_price",
            "Line total (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )
    relations = (
        DatasetRelation(
            "project_file",
            ReportDomain.PROJECT_FILES,
            "project_file",
            "to_one",
            "Project file",
        ),
    )

    def base_queryset(self) -> QuerySet:
        return _annotate_source_driver(self.entry_model.objects.all())


class WorkEntriesDataset(_PricedEntryDataset):
    key = ReportDomain.WORK_ENTRIES
    label = "Work entries (revenue)"
    permission = "management.view_workentry"
    entry_model = WorkEntry


class CostEntriesDataset(_PricedEntryDataset):
    key = ReportDomain.COST_ENTRIES
    label = "Cost entries"
    permission = "management.view_costentry"
    entry_model = CostEntry


class BankingDataset(BaseDataset):
    key = ReportDomain.BANKING
    label = "Bank transactions"
    permission = "banking.view_banktransaction"
    select_related = ("bank_account", "category")
    detail_url_name = "admin:banking_banktransaction_change"
    dimensions = (
        CatalogField("external_id", "external_id", "Bank transaction external id"),
        CatalogField(
            "bank_account", "bank_account__bank_name", "Bank account", ("bank_account",)
        ),
        CatalogField("category", "category__name", "Category", ("category",)),
        CatalogField(
            "reconciliation_status",
            "reconciliation_status",
            "Reconciliation status",
            allowed_values=tuple(ReconciliationStatus.values),
        ),
        CatalogField(
            "counterparty_name",
            "counterparty_name",
            "Counterparty name",
            text_match=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("transaction_date", "transaction_date", "Transaction date"),
        CatalogField(
            "transaction_month",
            "transaction_month",
            "Transaction month (first day of month). Use this, not "
            "transaction_date, when grouping a trend by month.",
        ),
    )
    measures = (
        CatalogMeasure("amount", "amount", "Amount (positive = in, negative = out)"),
        CatalogMeasure(
            "amount_in", "amount_in", "Money in (incoming movements only)"
        ),
        CatalogMeasure(
            "amount_out", "amount_out", "Money out (outgoing movements only)"
        ),
    )
    filter_fields = (
        CatalogField(
            "bank_account",
            "bank_account__bank_name",
            "Bank account name",
            text_match=True,
        ),
        CatalogField(
            "category", "category__name", "Category name", text_match=True
        ),
        CatalogField(
            "reconciliation_status",
            "reconciliation_status",
            "Reconciliation status",
            allowed_values=tuple(ReconciliationStatus.values),
        ),
        CatalogField(
            "counterparty_name",
            "counterparty_name",
            "Counterparty name",
            text_match=True,
        ),
        CatalogField("transaction_date", "transaction_date", "Transaction date"),
        CatalogField(
            "amount",
            "amount",
            "Transaction amount (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        money_field = DecimalField(max_digits=12, decimal_places=2)
        return BankTransaction.objects.annotate(
            transaction_month=TruncMonth("transaction_date"),
            amount_in=Case(
                When(amount__gt=0, then=F("amount")),
                default=Value(Decimal("0.00")),
                output_field=money_field,
            ),
            amount_out=Case(
                When(amount__lt=0, then=-F("amount")),
                default=Value(Decimal("0.00")),
                output_field=money_field,
            ),
        )


class FinanceDataset(BaseDataset):
    key = ReportDomain.FINANCE
    label = "Tax periods"
    permission = "finance.view_taxperiod"
    select_related = ("rule",)
    detail_url_name = "admin:finance_taxperiod_change"
    dimensions = (
        CatalogField(
            "status",
            "status",
            "Tax period status",
            allowed_values=tuple(TaxPeriodStatus.values),
        ),
        CatalogField("rule", "rule__name", "Tax schedule rule", ("rule",)),
        CatalogField(
            "tax_type", "rule__tax_type__tax_name", "Tax type", ("rule",)
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("date_from", "date_from", "Period start"),
        CatalogField("due_date", "due_date", "Due date"),
    )
    measures = (
        CatalogMeasure("tax_collected", "tax_collected", "Tax collected"),
        CatalogMeasure("tax_paid", "tax_paid", "Tax paid"),
        CatalogMeasure("tax_net", "tax_net", "Tax net (owed to tax authority)"),
        CatalogMeasure(
            "accumulated_credit", "accumulated_credit", "Accumulated credit"
        ),
        CatalogMeasure("net_after_credit", "net_after_credit", "Net to pay"),
    )
    filter_fields = (
        CatalogField(
            "status",
            "status",
            "Tax period status",
            allowed_values=tuple(TaxPeriodStatus.values),
        ),
        CatalogField("rule", "rule__name", "Tax schedule rule name", text_match=True),
        CatalogField(
            "tax_type", "rule__tax_type__tax_name", "Tax type name", text_match=True
        ),
        CatalogField("date_from", "date_from", "Period start"),
        CatalogField("due_date", "due_date", "Due date"),
        CatalogField(
            "net_after_credit",
            "net_after_credit",
            "Net to pay (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return TaxPeriod.objects.all()


class AccountingDataset(BaseDataset):
    key = ReportDomain.ACCOUNTING
    label = "Journal entry lines"
    permission = "accounting.view_journalentryline"
    select_related = ("account", "entry")
    # JournalEntryLine has no standalone admin page (only inline under
    # JournalEntry), so no row link is possible.
    detail_url_name = None
    dimensions = (
        CatalogField(
            "account_code", "account__code", "Account code", ("account",)
        ),
        CatalogField(
            "account_name", "account__name", "Account name", ("account",)
        ),
        CatalogField(
            "entry_number", "entry__number", "Journal entry number", ("entry",)
        ),
        CatalogField(
            "entry_status",
            "entry__status",
            "Journal entry status",
            allowed_values=tuple(EntryStatus.values),
        ),
        CatalogField(
            "journal_type",
            "entry__journal__type",
            "Journal type",
            allowed_values=tuple(JournalType.values),
        ),
        CatalogField(
            "journal_name", "entry__journal__name", "Journal name", ("entry",)
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("entry_date", "entry__date", "Entry date"),
    )
    measures = (
        CatalogMeasure("debit", "debit", "Debit"),
        CatalogMeasure("credit", "credit", "Credit"),
        CatalogMeasure(
            "balance",
            "balance",
            "Balance (debit minus credit). Use this for 'saldo' "
            "requests, not debit/credit separately.",
        ),
    )
    filter_fields = (
        CatalogField(
            "account_code", "account__code", "Account code", text_match=True
        ),
        CatalogField(
            "account_name", "account__name", "Account name", text_match=True
        ),
        CatalogField(
            "entry_status",
            "entry__status",
            "Journal entry status",
            allowed_values=tuple(EntryStatus.values),
        ),
        CatalogField(
            "journal_type",
            "entry__journal__type",
            "Journal type",
            allowed_values=tuple(JournalType.values),
        ),
        CatalogField("entry_date", "entry__date", "Entry date"),
        CatalogField(
            "debit", "debit", "Debit amount (numeric threshold)", numeric=True
        ),
        CatalogField(
            "credit", "credit", "Credit amount (numeric threshold)", numeric=True
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return JournalEntryLine.objects.annotate(balance=F("debit") - F("credit"))


class PayrollDataset(BaseDataset):
    key = ReportDomain.PAYROLL
    label = "Payroll"
    permission = "payroll.view_employeepayroll"
    select_related = ("employee", "period")
    detail_url_name = "admin:payroll_employeepayroll_change"
    dimensions = (
        CatalogField(
            "employee", "employee_name", "Employee", ("employee",)
        ),
        CatalogField(
            "period_status",
            "period__status",
            "Payroll period status",
            allowed_values=tuple(PayrollPeriod.Status.values),
        ),
        CatalogField("period_year", "period__year", "Payroll period year"),
        CatalogField("period_month", "period__month", "Payroll period month"),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
    )
    measures = (
        CatalogMeasure("gross_salary", "gross_salary", "Gross salary"),
        CatalogMeasure("net_salary", "net_salary", "Net salary"),
        CatalogMeasure("employer_cost", "employer_cost", "Employer cost"),
        CatalogMeasure(
            "employer_social_security",
            "employer_social_security",
            "Employer social security",
        ),
        CatalogMeasure(
            "employee_social_security",
            "employee_social_security",
            "Employee social security",
        ),
        CatalogMeasure(
            "irpf_withholding_amount",
            "irpf_withholding_amount",
            "IRPF withholding",
        ),
        CatalogMeasure("total_accrued", "total_accrued", "Total accrued"),
        CatalogMeasure("total_deducted", "total_deducted", "Total deducted"),
    )
    filter_fields = (
        CatalogField(
            "employee", "employee_name", "Employee name", text_match=True
        ),
        CatalogField(
            "period_status",
            "period__status",
            "Payroll period status",
            allowed_values=tuple(PayrollPeriod.Status.values),
        ),
        CatalogField(
            "period_year", "period__year", "Payroll period year", numeric=True
        ),
        CatalogField(
            "period_month", "period__month", "Payroll period month", numeric=True
        ),
        CatalogField(
            "employer_cost",
            "employer_cost",
            "Employer cost (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return EmployeePayroll.objects.annotate(
            employee_name=Concat(
                "employee__first_name",
                Value(" "),
                "employee__last_name",
                output_field=CharField(),
            ),
        )


class LoansDataset(BaseDataset):
    key = ReportDomain.LOANS
    label = "Loan installments"
    permission = "loans.view_loaninstallment"
    company_field = "loan__company"
    select_related = ("loan", "loan__bank")
    # LoanInstallment is only an inline under LoanAdmin, no standalone page.
    detail_url_name = None
    dimensions = (
        CatalogField(
            "loan", "loan__name", "Loan", ("loan",)
        ),
        CatalogField("bank", "loan__bank__name", "Bank", ("loan",)),
        CatalogField(
            "payment_status",
            "payment_status",
            "Installment payment status",
            allowed_values=tuple(LoanInstallmentPaymentStatus.values),
        ),
        CatalogField("due_date", "due_date", "Due date"),
        CatalogField(
            "installment_number", "installment_number", "Installment number"
        ),
        CatalogField(
            "company", "loan__company__name", "Active company name", ("loan",)
        ),
    )
    measures = (
        CatalogMeasure("total_due", "total_due", "Total due"),
        CatalogMeasure("amount_paid", "amount_paid", "Amount paid"),
    )
    filter_fields = (
        CatalogField("loan", "loan__name", "Loan name", text_match=True),
        CatalogField("bank", "loan__bank__name", "Bank name", text_match=True),
        CatalogField(
            "payment_status",
            "payment_status",
            "Installment payment status (unpaid/partial/paid). Use "
            "unpaid or in [unpaid,partial] for what is still owed.",
            allowed_values=tuple(LoanInstallmentPaymentStatus.values),
        ),
        CatalogField("due_date", "due_date", "Due date"),
        CatalogField(
            "total_due",
            "total_due",
            "Total due on the installment (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company", "loan__company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return LoanInstallment.objects.all()


class VacationsDataset(BaseDataset):
    key = ReportDomain.VACATIONS
    label = "Vacation requests"
    permission = "vacations.view_vacationrequest"
    company_field = "employee__company"
    select_related = ("employee",)
    detail_url_name = "admin:vacations_vacationrequest_change"
    dimensions = (
        CatalogField(
            "employee", "employee_name", "Employee", ("employee",)
        ),
        CatalogField(
            "status",
            "status",
            "Vacation request status",
            allowed_values=tuple(VacationRequestStatus.values),
        ),
        CatalogField(
            "reason",
            "reason",
            "Absence reason",
            allowed_values=tuple(AbsenceReason.values),
        ),
        CatalogField(
            "is_paid",
            "is_paid",
            "Whether the absence is paid. Use eq true/false.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
        CatalogField("start_date", "start_date", "Start date"),
        CatalogField(
            "company", "employee__company__name", "Active company name", ("employee",)
        ),
    )
    measures = (
        CatalogMeasure("requested_days", "requested_days", "Requested days"),
    )
    filter_fields = (
        CatalogField(
            "employee", "employee_name", "Employee name", text_match=True
        ),
        CatalogField(
            "status",
            "status",
            "Vacation request status",
            allowed_values=tuple(VacationRequestStatus.values),
        ),
        CatalogField(
            "reason",
            "reason",
            "Absence reason",
            allowed_values=tuple(AbsenceReason.values),
        ),
        CatalogField(
            "is_paid",
            "is_paid",
            "Whether the absence is paid. Use eq true/false.",
            allowed_values=("true", "false"),
            boolean=True,
        ),
        CatalogField("start_date", "start_date", "Start date"),
        CatalogField(
            "requested_days",
            "requested_days",
            "Requested days (numeric threshold)",
            numeric=True,
        ),
        CatalogField(
            "company",
            "employee__company__name",
            "Active company name",
            text_match=True,
        ),
    )

    def base_queryset(self) -> QuerySet:
        return VacationRequest.objects.annotate(
            employee_name=Concat(
                "employee__first_name",
                Value(" "),
                "employee__last_name",
                output_field=CharField(),
            ),
        )


class FeedbackDataset(BaseDataset):
    key = ReportDomain.FEEDBACK
    label = "Feedback responses"
    permission = "feedback.view_feedbackresponse"
    select_related = ("questionnaire", "client", "agent")
    detail_url_name = "admin:feedback_feedbackresponse_change"
    dimensions = (
        CatalogField(
            "questionnaire",
            "questionnaire__name",
            "Feedback questionnaire",
            ("questionnaire",),
        ),
        CatalogField("client", "client__name", "Client", ("client",)),
        CatalogField(
            "agent", "agent_name", "Agent", ("agent",)
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("call_date", "call_date", "Call date"),
        CatalogField(
            "call_month",
            "call_month",
            "Call month (first day of month). Use this, not call_date, "
            "when grouping a trend by month.",
        ),
    )
    measures = ()
    filter_fields = (
        CatalogField(
            "client", "client__name", "Client name", text_match=True
        ),
        CatalogField("agent", "agent_name", "Agent name", text_match=True),
        CatalogField("call_date", "call_date", "Call date"),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return FeedbackResponse.objects.annotate(
            call_month=TruncMonth("call_date"),
            agent_name=Concat(
                "agent__first_name",
                Value(" "),
                "agent__last_name",
                output_field=CharField(),
            ),
        )


class ExpensesDataset(BaseDataset):
    key = ReportDomain.EXPENSES
    label = "Expense claims"
    permission = "expenses.view_expenseclaim"
    select_related = ("submitted_by",)
    detail_url_name = "admin:expenses_expenseclaim_change"
    dimensions = (
        CatalogField(
            "submitted_by",
            "submitted_by__username",
            "Submitted by",
            ("submitted_by",),
        ),
        CatalogField(
            "status",
            "status",
            "Expense claim status",
            allowed_values=tuple(ExpenseClaimStatus.values),
        ),
        CatalogField(
            "payment_status",
            "payment_status",
            "Reimbursement payment status",
            allowed_values=tuple(InvoicePaymentStatus.values),
        ),
        CatalogField(
            "company", "company__name", "Active company name", ("company",)
        ),
        CatalogField("created_at", "created_at__date", "Created date"),
    )
    measures = (
        CatalogMeasure("total_claimed", "total_claimed", "Total claimed"),
        CatalogMeasure(
            "total_reimbursed", "total_reimbursed", "Total reimbursed"
        ),
    )
    filter_fields = (
        CatalogField(
            "submitted_by",
            "submitted_by__username",
            "Submitted by (username)",
            text_match=True,
        ),
        CatalogField(
            "status",
            "status",
            "Expense claim status",
            allowed_values=tuple(ExpenseClaimStatus.values),
        ),
        CatalogField(
            "payment_status",
            "payment_status",
            "Reimbursement payment status (unpaid/partial/paid). Use "
            "unpaid or in [unpaid,partial] for what is still owed to the "
            "employee.",
            allowed_values=tuple(InvoicePaymentStatus.values),
        ),
        CatalogField(
            "total_claimed",
            "total_claimed",
            "Total claimed (numeric threshold)",
            numeric=True,
        ),
        CatalogField("created_at", "created_at__date", "Created date"),
        CatalogField(
            "company", "company__name", "Active company name", text_match=True
        ),
    )

    def base_queryset(self) -> QuerySet:
        return ExpenseClaim.objects.all()


class EInvoicingDataset(BaseDataset):
    key = ReportDomain.EINVOICING
    label = "E-invoicing submissions"
    permission = "einvoicing.view_einvoicingsubmission"
    company_field = "account__company"
    select_related = ("account", "sales_invoice")
    detail_url_name = "admin:einvoicing_einvoicingsubmission_change"
    dimensions = (
        CatalogField(
            "provider_record_id", "provider_record_id", "Provider record id"
        ),
        CatalogField(
            "invoice_number",
            "sales_invoice__invoice_number",
            "Sales invoice number",
            ("sales_invoice",),
        ),
        CatalogField(
            "status",
            "status",
            "Submission status",
            allowed_values=tuple(SubmissionStatus.values),
        ),
        CatalogField(
            "action",
            "action",
            "Submission action",
            allowed_values=tuple(SubmissionAction.values),
        ),
        CatalogField(
            "provider",
            "account__provider",
            "E-invoicing provider (the intermediary/gateway used to "
            "reach the tax authority, e.g. Verifacti)",
            ("account",),
            allowed_values=tuple(EInvoicingProviderChoices.values),
        ),
        CatalogField(
            "company",
            "account__company__name",
            "Active company name",
            ("account",),
        ),
        CatalogField("created_at", "created_at__date", "Created date"),
    )
    measures = ()
    filter_fields = (
        CatalogField(
            "status",
            "status",
            "Submission status (pending/submitted/accepted/rejected/"
            "error). Use error for failed submissions.",
            allowed_values=tuple(SubmissionStatus.values),
        ),
        CatalogField(
            "action",
            "action",
            "Submission action",
            allowed_values=tuple(SubmissionAction.values),
        ),
        CatalogField(
            "provider",
            "account__provider",
            "E-invoicing provider",
            allowed_values=tuple(EInvoicingProviderChoices.values),
        ),
        CatalogField("created_at", "created_at__date", "Created date"),
        CatalogField(
            "company",
            "account__company__name",
            "Active company name",
            text_match=True,
        ),
    )

    def base_queryset(self) -> QuerySet:
        return EInvoicingSubmission.objects.all()


_DATASETS: dict[str, BaseDataset] = {
    ReportDomain.CLIENTS: ClientsDataset(),
    ReportDomain.SALES_INVOICES: SalesInvoicesDataset(),
    ReportDomain.QUOTES: QuotesDataset(),
    ReportDomain.PAYMENTS: PaymentsDataset(),
    ReportDomain.PRODUCTION_TIME: ProductionTimeDataset(),
    ReportDomain.INVENTORY: InventoryDataset(),
    ReportDomain.PROJECT_FILES: ProjectFilesDataset(),
    ReportDomain.PURCHASING: PurchasingDataset(),
    ReportDomain.PURCHASE_ORDERS: PurchaseOrdersDataset(),
    ReportDomain.CONFIGURATIONS: ConfigurationsDataset(),
    ReportDomain.MATERIALS: MaterialsDataset(),
    ReportDomain.ASSETS: AssetsDataset(),
    ReportDomain.ASSET_TRANSACTIONS: AssetTransactionsDataset(),
    ReportDomain.WORK_ENTRIES: WorkEntriesDataset(),
    ReportDomain.COST_ENTRIES: CostEntriesDataset(),
    ReportDomain.BANKING: BankingDataset(),
    ReportDomain.FINANCE: FinanceDataset(),
    ReportDomain.ACCOUNTING: AccountingDataset(),
    ReportDomain.PAYROLL: PayrollDataset(),
    ReportDomain.LOANS: LoansDataset(),
    ReportDomain.VACATIONS: VacationsDataset(),
    ReportDomain.FEEDBACK: FeedbackDataset(),
    ReportDomain.EXPENSES: ExpensesDataset(),
    ReportDomain.EINVOICING: EInvoicingDataset(),
}


def get_dataset(key: str) -> BaseDataset:
    dataset = _DATASETS.get(key)
    if dataset is None:
        raise KeyError(f"Unknown dataset: {key!r}")
    return dataset


def all_datasets() -> list[BaseDataset]:
    return list(_DATASETS.values())


@dataclass(frozen=True)
class DatasetAccess:
    key: str
    label: str
    permission: str
    available: bool


def datasets_access_for_user(user) -> list[DatasetAccess]:
    """Which report datasets the user may query, given their permissions."""

    return [
        DatasetAccess(
            key=dataset.key,
            label=ReportDomain(dataset.key).label,
            permission=dataset.permission,
            available=user.has_perm(dataset.permission),
        )
        for dataset in all_datasets()
    ]
