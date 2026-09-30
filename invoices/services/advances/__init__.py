"""Advance invoices (facturas de anticipo) and their deduction from the
invoices that later bill an expediente's goods.

An advance invoice (``document_type=advance``) bills a share of an
expediente's taxable base before its goods are invoiced. Later goods
invoices take it off with negative deduction lines pointing at the advance
line (``SalesInvoiceItem.advance_line``). What is left to deduct is derived
from those lines — see ``ProjectFileInvoiceService.get_invoiceable_summary``.
"""
from .creation import AdvanceInvoiceService
from .deductions import AdvanceDeductionService

__all__ = ["AdvanceDeductionService", "AdvanceInvoiceService"]
