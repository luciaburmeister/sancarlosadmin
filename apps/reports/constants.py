from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


class ReportVisibility(models.TextChoices):
    PRIVATE = "private", _("Private")
    COMPANY = "company", _("Company")


class ReportGenerationStatus(models.TextChoices):
    PROCESSING = "processing", _("Processing")
    CONFIRM = "confirm", _("Confirm")
    READY = "ready", _("Ready")
    ERROR = "error", _("Error")
    CLARIFY = "clarify", _("Needs detail")


class ReportDomain(models.TextChoices):
    CLIENTS = "clients", _("Clients")
    SALES_INVOICES = "sales_invoices", _("Sales invoices")
    QUOTES = "quotes", _("Quotes")
    PAYMENTS = "payments", _("Payments")
    PRODUCTION_TIME = "production_time", _("Production time")
    INVENTORY = "inventory", _("Inventory")
    PROJECT_FILES = "project_files", _("Project files")
    PURCHASING = "purchasing", _("Purchasing (accounts payable)")
    PURCHASE_ORDERS = "purchase_orders", _("Purchase orders")
    CONFIGURATIONS = "configurations", _("Configurations")
    MATERIALS = "materials", _("Materials")
    ASSETS = "assets", _("Assets")
    ASSET_TRANSACTIONS = "asset_transactions", _("Asset transactions")
    WORK_ENTRIES = "work_entries", _("Work entries")
    COST_ENTRIES = "cost_entries", _("Cost entries")
    BANKING = "banking", _("Bank transactions")
    FINANCE = "finance", _("Tax periods")
    ACCOUNTING = "accounting", _("Journal entry lines")
    PAYROLL = "payroll", _("Payroll")
    LOANS = "loans", _("Loan installments")
    VACATIONS = "vacations", _("Vacation requests")
    FEEDBACK = "feedback", _("Feedback responses")
    EXPENSES = "expenses", _("Expense claims")
    EINVOICING = "einvoicing", _("E-invoicing submissions")
    UNSUPPORTED = "unsupported", _("Unsupported")


class AggregationFunction(models.TextChoices):
    COUNT = "count", _("Count")
    COUNT_DISTINCT = "count_distinct", _("Count distinct")
    SUM = "sum", _("Sum")
    AVG = "avg", _("Average")


class ChartType(models.TextChoices):
    BAR = "bar", pgettext_lazy("chart type", "Bar")
    LINE = "line", pgettext_lazy("chart type", "Line")
    PIE = "pie", pgettext_lazy("chart type", "Pie")
    NONE = "none", _("None")


class FilterOperator(models.TextChoices):
    EQ = "eq", _("Equals")
    GT = "gt", _("Greater than")
    GTE = "gte", _("Greater or equal")
    LT = "lt", _("Less than")
    LTE = "lte", _("Less or equal")
    IN = "in", _("In")
    ICONTAINS = "icontains", _("Contains")


# A classifier result below this confidence is treated as unclear.
CLASSIFICATION_CONFIDENCE_THRESHOLD = 0.7

# Hard cap on grouped rows returned to the UI.
REPORT_ROW_CAP = 500

# A bar/line chart past this many categories is unreadable (overlapping axis
# labels); fall back to table-only regardless of what the spec requested —
# the model can't know a dimension's real cardinality ahead of time.
MAX_CHART_CATEGORIES = 15

# Domains the spec generator may generate a spec for (excludes unsupported).
SUPPORTED_DOMAINS = frozenset(
    {
        ReportDomain.CLIENTS,
        ReportDomain.SALES_INVOICES,
        ReportDomain.QUOTES,
        ReportDomain.PAYMENTS,
        ReportDomain.PRODUCTION_TIME,
        ReportDomain.INVENTORY,
        ReportDomain.PROJECT_FILES,
        ReportDomain.PURCHASING,
        ReportDomain.PURCHASE_ORDERS,
        ReportDomain.CONFIGURATIONS,
        ReportDomain.MATERIALS,
        ReportDomain.ASSETS,
        ReportDomain.ASSET_TRANSACTIONS,
        ReportDomain.WORK_ENTRIES,
        ReportDomain.COST_ENTRIES,
        ReportDomain.BANKING,
        ReportDomain.FINANCE,
        ReportDomain.ACCOUNTING,
        ReportDomain.PAYROLL,
        ReportDomain.LOANS,
        ReportDomain.VACATIONS,
        ReportDomain.FEEDBACK,
        ReportDomain.EXPENSES,
        ReportDomain.EINVOICING,
    }
)
