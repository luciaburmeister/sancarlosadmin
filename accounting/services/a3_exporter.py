import tablib
import logging
from decimal import Decimal
from django.db.models import Sum
from invoices.models import Invoice, AppliedTax
from accounting.models import JournalEntry, JournalEntryLine

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

    def _get_recibidas_sheet(self):
        """
        Sheet 1: Facturas Recibidas
        A: Factura, B: Fecha, C: Concepto, D: NIF, E: Nombre, F: Base, G: %IVA, H: Cuota, I: %RE
        """
        headers = [
            'Factura', 'Fecha', 'Concepto', 'NIF', 'Nombre', 
            'Base', '% IVA', 'Cuota IVA', '% RE'
        ]
        sheet = tablib.Dataset(title='Facturas Recibidas')
        sheet.headers = headers

        invoices = Invoice.objects.filter(
            company=self.company,
            date_issued__range=(self.start_date, self.end_date)
        ).select_related('provider')

        for inv in invoices:
            # An invoice can have multiple tax lines. We should export one row per tax line or aggregate?
            # Standard A3 import often expects one row per tax rate.
            taxes = inv.get_taxes()
            
            # If no items, base is 0
            base_total = inv.items.aggregate(total=Sum('line_total'))['total']
            base_amount = self._to_float(base_total)

            if not taxes.exists():
                provider_name = inv.provider.name if inv.provider else "PROVEEDOR DESCONOCIDO"
                provider_nif = inv.provider.tax_identifier if inv.provider else ""
                
                sheet.append([
                    inv.invoice_number or f"ID {inv.id}",
                    inv.date_issued.strftime('%d/%m/%Y') if inv.date_issued else '',
                    f"Factura {inv.invoice_number or inv.id} {provider_name}",
                    provider_nif,
                    provider_name,
                    base_amount,
                    0, 0, 0
                ])
            else:
                for tax in taxes:
                    if tax.tax_type.is_debit:
                        provider_name = inv.provider.name if inv.provider else "PROVEEDOR DESCONOCIDO"
                        provider_nif = inv.provider.tax_identifier if inv.provider else ""

                        sheet.append([
                            inv.invoice_number or f"ID {inv.id}",
                            inv.date_issued.strftime('%d/%m/%Y') if inv.date_issued else '',
                            f"Factura {inv.invoice_number or f'ID {inv.id}'} {provider_name}",
                            provider_nif,
                            provider_name,
                            base_amount,
                            self._to_float(tax.selected_percentage),
                            self._to_float(tax.calculated_amount),
                            0 # RE not handled yet or separate line
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
            for line in entry.lines.all():
                sheet.append([
                    entry.date.strftime('%d/%m/%Y'),
                    entry.number,
                    entry.reference, # Using reference as concept
                    entry.reference, # Using reference as document
                    line.account.code,
                    line.account.name,
                    self._to_float(line.debit),
                    self._to_float(line.credit)
                ])

        return sheet
