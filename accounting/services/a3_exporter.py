import tablib
import logging
from decimal import Decimal
from django.db.models import Sum
from invoices.models import Invoice, AppliedTax, A3TaxGroup, InvoiceDocumentType
from accounting.models import JournalEntry, JournalEntryLine, CompanyAccount

logger = logging.getLogger(__name__)

class A3PurchaseExportService:
    """
    Exports Purchase Invoices and Journal Entries to A3-compatible Excel format.
    """

    def __init__(self, company, start_date=None, end_date=None):
        self.company = company
        self.start_date = start_date
        self.end_date = end_date

    def generate_excel_report(self):
        """
        Generates a Databook with two sheets: Facturas Recibidas and Diario.
        """
        book = tablib.Databook()
        
        recibidas_sheet = self._get_recibidas_sheet()
        diario_sheet = self._get_diario_sheet()
        
        book.add_sheet(recibidas_sheet)
        book.add_sheet(diario_sheet)
        
        return book.export('xlsx')

    def _to_float(self, value):
        """
        Safely converts a Money object, Decimal, or None to a float.
        """
        if value is None:
            return 0.0
        if hasattr(value, 'amount'): # djmoney.money.Money
            return float(value.amount)
        return float(value)

    def _format_account(self, account: CompanyAccount):
        """
        Formats account code to 10 digits as required by A3.
        """
        if account.a3_code_override:
            return account.a3_code_override
        # Standard padding to 10 digits
        return account.code.ljust(10, '0')

    def _format_doc(self, doc_number):
        """
        Truncates document number to 10 characters as required by A3.
        """
        if not doc_number:
            return ""
        return str(doc_number)[:10]

    def _get_tax_base(self, tax, fallback_base):
        """
        Returns the base over which this specific tax was applied.
        Falls back to deriving it from the quota and percentage, and
        finally to the invoice total base.
        """
        if tax.base_amount is not None:
            return self._to_float(tax.base_amount)
        pct = self._to_float(tax.selected_percentage)
        quota = self._to_float(tax.calculated_amount)
        if pct and quota:
            return round(quota / (pct / 100.0), 2)
        return fallback_base

    def _get_concept(self, invoice):
        """
        Generates a standardized concept for the invoice.
        """
        provider_name = invoice.provider.name if invoice.provider else "PROVEEDOR DESCONOCIDO"
        return f"Factura {invoice.invoice_number or invoice.id} {provider_name}"

    def _get_recibidas_sheet(self):
        """
        Sheet 1: Facturas Recibidas
        A: Factura, B: Fecha, C: Concepto, D: NIF, E: Nombre, F: Base, G: %IVA, H: Cuota, I: %RE, J: %IRPF, K: Cuota IRPF, L: Total Fact, M: Código cuenta
        """
        sheet = tablib.Dataset(title='Facturas Recibidas')
        sheet.headers = [
            'Factura', 'Fecha', 'Concepto', 'NIF', 'Nombre', 
            'Base', '% IVA', 'Cuota IVA', '% RE', 
            '% IRPF', 'Cuota IRPF', 'Total Fact', 'Código cuenta',
            'Código cuenta Compra/Gasto'
        ]

        # A debt to a group company for what it paid for us is no purchase: its invoice is the one paid
        invoices = Invoice.objects.filter(
            company=self.company,
            date_issued__range=(self.start_date, self.end_date)
        ).exclude(
            document_type=InvoiceDocumentType.INTERCOMPANY
        ).select_related('provider').prefetch_related('items__account')

        for inv in invoices:
            taxes = list(inv.get_taxes())
            
            # Find IRPF if it exists
            irpf_tax = next((t for t in taxes if t.tax_type.a3_tax_group == A3TaxGroup.IRPF), None)
            irpf_pct = self._to_float(irpf_tax.selected_percentage) if irpf_tax else 0.0
            irpf_amt = self._to_float(irpf_tax.calculated_amount) if irpf_tax else 0.0

            # Find RE if it exists
            re_tax = next((t for t in taxes if t.tax_type.a3_tax_group == A3TaxGroup.RE), None)
            re_pct = self._to_float(re_tax.selected_percentage) if re_tax else 0.0
            
            # Base total
            base_total = inv.items.aggregate(total=Sum('line_total'))['total']
            base_amount = self._to_float(base_total)
            total_fact = self._to_float(inv.total_amount)
            
            # Provider account formatted to 10 digits
            provider_account = None
            if inv.provider and hasattr(inv.provider, 'account'):
                 provider_account = inv.provider.account
            
            account_code = self._format_account(provider_account) if provider_account else ""

            # Find the expense account with the highest amount (Group 6)
            expense_account_code = ""
            max_amount = -1
            for item in inv.items.all():
                if item.account and item.account.code.startswith('6'):
                    item_amount = self._to_float(item.line_total)
                    if item_amount > max_amount:
                        max_amount = item_amount
                        expense_account_code = self._format_account(item.account)

            concept = self._get_concept(inv)
            doc_num = self._format_doc(inv.invoice_number or f"ID {inv.id}")
            
            vat_taxes = [t for t in taxes if t.tax_type.a3_tax_group == A3TaxGroup.IVA]

            if not vat_taxes:
                # One row for zero VAT but maybe with IRPF/Base
                sheet.append([
                    doc_num,
                    inv.date_issued.strftime('%d/%m/%Y') if inv.date_issued else '',
                    concept,
                    inv.provider.tax_identifier if inv.provider else "",
                    inv.provider.name if inv.provider else "PROVEEDOR DESCONOCIDO",
                    base_amount,
                    0, 0, 0,
                    irpf_pct, irpf_amt,
                    total_fact,
                    account_code,
                    expense_account_code
                ])
            else:
                for tax in vat_taxes:
                    sheet.append([
                        doc_num,
                        inv.date_issued.strftime('%d/%m/%Y') if inv.date_issued else '',
                        concept,
                        inv.provider.tax_identifier if inv.provider else "",
                        inv.provider.name if inv.provider else "PROVEEDOR DESCONOCIDO",
                        self._get_tax_base(tax, base_amount),
                        self._to_float(tax.selected_percentage),
                        self._to_float(tax.calculated_amount),
                        re_pct,
                        irpf_pct, irpf_amt,
                        total_fact,
                        account_code,
                        expense_account_code
                    ])

        return sheet

    def _get_diario_sheet(self):
        """
        Sheet 2: Diario
        A: Fecha, B: Número, C: Concepto, D: Documento, E: Código cuenta, F: Descripción cuenta, G: Debe, H: Haber
        """
        headers = [
            'Fecha', 'Número', 'Concepto', 'Documento', 
            'Código cuenta', 'Descripción cuenta', 'Debe', 'Haber'
        ]
        sheet = tablib.Dataset(title='Diario')
        sheet.headers = headers

        entries = JournalEntry.objects.filter(
            company=self.company,
            date__range=(self.start_date, self.end_date)
        ).prefetch_related('lines__account')

        for entry in entries:
            # Consistent concept and document with Sheet 1
            # If linked to a purchase invoice, we try to use its concept logic
            concept = entry.reference
            doc_num = self._format_doc(entry.reference.replace("INV ", "") if entry.reference.startswith("INV ") else entry.reference)
            
            if entry.purchase_invoice:
                concept = self._get_concept(entry.purchase_invoice)
                doc_num = self._format_doc(entry.purchase_invoice.invoice_number or entry.purchase_invoice.id)

            for line in entry.lines.all():
                sheet.append([
                    entry.date.strftime('%d/%m/%Y'),
                    entry.number,
                    concept,
                    doc_num,
                    self._format_account(line.account),
                    line.account.name,
                    self._to_float(line.debit),
                    self._to_float(line.credit)
                ])

        return sheet
