"""Shared report-prompt classification result and classifier factory."""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.reports.constants import (
    CLASSIFICATION_CONFIDENCE_THRESHOLD,
    MAX_REPORT_SECTIONS,
    ReportDomain,
    SUPPORTED_DOMAINS,
)

_SUPPORTED_VALUES = {d.value for d in SUPPORTED_DOMAINS}


@dataclass(frozen=True)
class ReportClassification:
    domain: str
    confidence: float
    needs_clarification: bool
    source: str = "openai"
    domains: list[str] = field(default_factory=list)

    @property
    def supported_domains(self) -> list[str]:
        raw = list(self.domains) if self.domains else [self.domain]
        chosen: list[str] = []
        for item in raw:
            if item in _SUPPORTED_VALUES and item not in chosen:
                chosen.append(item)
        return chosen

    @property
    def is_clear(self) -> bool:
        if self.needs_clarification:
            return False
        if not self.supported_domains:
            return False
        return self.confidence >= CLASSIFICATION_CONFIDENCE_THRESHOLD


class ReportClassificationPrompt:
    """The tool schema and system prompt ``ClaudeReportClassifier`` uses."""

    TOOL_NAME = "classify_report_domain"
    TOOL_DESCRIPTION = (
        "Classify which ERP reporting domain(s) this request belongs to."
    )

    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": ["domains", "confidence", "needs_clarification"],
        "properties": {
            "domains": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_REPORT_SECTIONS,
                "items": {
                    "type": "string",
                    "enum": [d.value for d in ReportDomain],
                },
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "needs_clarification": {"type": "boolean"},
        },
    }

    DOMAIN_CRITERIA = {
        ReportDomain.CLIENTS: (
            "Client/customer master data: fiscal or commercial name, tax "
            "identifier, sale zone, commissionist agent, payment method or "
            "terms, credit insurance, credit risk limits — the client record "
            "itself, not their invoices, quotes, or payments (those are "
            "sales_invoices/quotes/payments, filtered by client name)"
        ),
        ReportDomain.SALES_INVOICES: (
            "Sales invoices, billing, revenue invoiced, invoice status, "
            "payment status (unpaid/pending/cobro), invoice payment method "
            "(transfer, cash, card, cheque — how the invoice is expected to "
            "be paid), amounts by client or day. Generic billing language "
            "like 'facturado', 'facturación', 'importe facturado', or "
            "'facturas' with NO explicit mention of a provider/supplier or "
            "a purchase defaults here, never to purchasing."
        ),
        ReportDomain.QUOTES: (
            "Quotes, budgets, presupuestos, accepted/rejected/sent quotes, "
            "quote conversion or quote totals by status or month"
        ),
        ReportDomain.PAYMENTS: (
            "Payments received or made, cash collected, how a recorded payment "
            "was made. Not the payment method stored on an invoice."
        ),
        ReportDomain.PRODUCTION_TIME: (
            "Production sessions, shop-floor hours, stage time, labor on production"
        ),
        ReportDomain.INVENTORY: (
            "Inventory, stock levels, stock on hand, low stock alerts, warehouses, "
            "incoming or reserved quantities, item cost or selling price"
        ),
        ReportDomain.PROJECT_FILES: (
            "Project files, expedientes, their status, delivery or production "
            "progress, or whether an expediente has been invoiced yet "
            "(not an individual quote or invoice)"
        ),
        ReportDomain.PURCHASING: (
            "Purchasing, accounts payable, INCOMING INVOICES already billed "
            "by providers/suppliers, money OWED TO providers (not money "
            "clients owe us — that is sales invoices), provider payment "
            "status, confirmation status, or due dates, cost center on an "
            "incoming invoice. ONLY classify here when the prompt explicitly "
            "names a provider/supplier/proveedor, a purchase/compra, or "
            "clearly asks what the company owes/debe — generic billing/"
            "invoicing language with no such mention (e.g. 'facturado', "
            "'importe facturado') belongs to sales_invoices instead. Users "
            "occasionally mis-word 'money we owe providers' as 'clientes a "
            "los que debemos dinero' or 'clients we owe money to' — that "
            "specific phrasing (debemos/owe + dinero/money) still belongs "
            "here even though it says 'clientes'. This is about INVOICES "
            "already received, NOT about purchase orders/pedidos sent to a "
            "provider before any invoice exists — those belong to "
            "purchase_orders, even when the wording also mentions a "
            "provider."
        ),
        ReportDomain.PURCHASE_ORDERS: (
            "Purchase orders (órdenes de compra, pedidos de compra) sent to "
            "providers: approval status (draft/pending/approved/rejected/"
            "closed/cancelled), whether an order has been invoiced yet, "
            "whether it has been received yet (pendiente de recepción, "
            "recibido parcial o total), quantities ordered/received/"
            "invoiced, order amount, payment terms, cost center, linked "
            "project file, who requested, approved, or rejected it. This "
            "is about the ORDER itself, before or regardless of any "
            "incoming invoice — not about the invoice's own payment status "
            "(that is purchasing) and not about warehouse stock on hand "
            "(that is inventory). 'órdenes'/'pedidos' is the strongest "
            "signal for this domain even if the prompt also says "
            "'proveedor'."
        ),
        ReportDomain.CONFIGURATIONS: (
            "Product configurations created in the configurator (draft, "
            "completed, converted to production, direct to shop), their "
            "price/cost or planning status — not quotes or production time"
        ),
        ReportDomain.MATERIALS: (
            "Raw materials/components catalog: material reference, category, "
            "family, cost per unit — not inventory stock levels (that is "
            "inventory) and not providers (that is purchasing)"
        ),
        ReportDomain.ASSETS: (
            "Company assets/fleet: vehicles or machines, their type, active "
            "status, owner/client, assigned employee — not the trips those "
            "assets made (that is asset transactions) and not purchase orders"
        ),
        ReportDomain.ASSET_TRANSACTIONS: (
            "Field operations on assets: verteo, porte, delivery, movement, "
            "machine or laborer hours, who drove (chofer/performed by), "
            "billing status of the trip. Counts of trips, not the money — "
            "revenue is work entries and cost is cost entries"
        ),
        ReportDomain.WORK_ENTRIES: (
            "Billable revenue lines on a project file (expediente), including "
            "lines generated from an asset transaction. Use for what a driver "
            "or a trip invoiced. Not sales invoices as documents, and not costs"
        ),
        ReportDomain.COST_ENTRIES: (
            "Cost lines on a project file (expediente), including costs "
            "generated from an asset transaction. Use for the cost side of a "
            "driver or a trip. Not purchasing invoices and not payroll"
        ),
        ReportDomain.BANKING: (
            "Raw bank statement transactions/movements, reconciliation status "
            "against payments, bank account activity — not Payment records "
            "themselves (that is payments) and not accounts payable/receivable"
        ),
        ReportDomain.FINANCE: (
            "Tax periods, VAT/IRPF tax collected vs paid, tax owed to or "
            "credit from the tax authority, tax due dates — not individual "
            "invoice tax lines"
        ),
        ReportDomain.ACCOUNTING: (
            "General ledger / journal entries, debit and credit lines by "
            "account or journal (sales, purchase, bank, cash, payroll "
            "journals) — the double-entry bookkeeping ledger itself, not "
            "invoices or payments directly"
        ),
        ReportDomain.PAYROLL: (
            "Employee payroll/nómina: gross or net salary, employer cost, "
            "social security contributions, IRPF withholding, by employee or "
            "pay period — not vacation requests or HR time-off"
        ),
        ReportDomain.LOANS: (
            "Bank loan installments: due dates, principal/total due, amount "
            "paid, payment status per installment — not day-to-day payments "
            "or accounts payable to providers"
        ),
        ReportDomain.VACATIONS: (
            "Employee vacation/time-off requests: status, absence reason, "
            "requested days, paid or unpaid, by employee — not payroll"
        ),
        ReportDomain.FEEDBACK: (
            "Customer/agent feedback survey responses about sales calls, by "
            "client, agent, or questionnaire — not expense tickets or any "
            "other kind of feedback"
        ),
        ReportDomain.EXPENSES: (
            "Employee expense claims/reimbursements: claimed amount, "
            "reimbursed amount, approval status, by employee — not accounts "
            "payable to providers (that is purchasing) and not payroll"
        ),
        ReportDomain.EINVOICING: (
            "AEAT/government e-invoicing submission status for sales "
            "invoices: pending, submitted, accepted, rejected, or errored "
            "submissions — not the invoices' own billing/payment status"
        ),
        ReportDomain.UNSUPPORTED: (
            "Anything outside sales invoices, quotes, payments, production time, "
            "inventory, project files, purchasing, purchase orders, "
            "configurations, materials, assets, asset transactions, work "
            "entries, cost entries, banking, finance, accounting, "
            "payroll, loans, vacations, feedback, expenses, or einvoicing"
        ),
    }

    @classmethod
    def system_prompt(cls) -> str:
        criteria = "\n".join(
            f"- {key.value}: {description}"
            for key, description in cls.DOMAIN_CRITERIA.items()
        )
        return (
            "You classify ERP report requests into one or more domains "
            f"(at most {MAX_REPORT_SECTIONS}). Return a list of domains "
            "when the request clearly asks for several datasets in one "
            "report (e.g. invoices and quotes for the same client). "
            "Otherwise return a single-domain list. "
            "Return a confidence between 0 and 1. "
            "Set needs_clarification true ONLY when the request is so vague "
            "that neither a metric nor a grouping can be inferred "
            "(e.g. 'muéstrame algo de ventas'). "
            "Do NOT ask for clarification when the user names a comparison "
            "(accepted vs rejected), a payment state (pendientes de cobro), "
            "a period (este año, último trimestre), or a company — those "
            "are enough to build a report.\n\n"
            f"Domains:\n{criteria}"
        )


def build_classifier():
    """Classify with Claude."""
    from apps.reports.services.claude_classifier import ClaudeReportClassifier

    return ClaudeReportClassifier()
