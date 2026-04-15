"""
Account Classifier Service

Orchestrates the workflow of classifying invoices into accounting accounts:

1. Fetch all invoices that have no account assigned yet
2. Group invoices by company
3. For each company:
   - Fetch that company's postable accounts
   - Send invoices + accounts to Gemini in a single batch call
4. For each result:
   - If confident -> save the account to the invoice, needs_manual_review=False
   - If not confident -> leave account as NULL, needs_manual_review=True
5. Return a summary of what happened
"""

from typing import List
from dataclasses import dataclass

from django.db import transaction

from company.admin import company
from invoices.models import Invoice
from accounting.models import CompanyAccount
from .account_classifier_interface import (
    AccountClassifierInterface,
    AccountOption,
    InvoiceLineItem,
    ClassificationBatchResponse,
)


@dataclass
class ClassificationSummary:
    """Summary returned after running the classification pipeline."""
    total_invoices: int         # How many invoices were processed
    total_items: int            # How many line items were processed
    classified_items: int       # How many line items got an account assigned
    flagged_items: int          # How many line items Gemini was unsure about
    errors: List[str]           # Any problems that occurred

    def __str__(self):
        return (
            f"Processed {self.total_invoices} invoices with {self.total_items} items. "
            f"Classified {self.classified_items}/{self.total_items} items. "
            f"{self.flagged_items} items flagged for manual review. "
            f"{len(self.errors)} errors."
        )


class AccountClassifierService:
    """
    Main service for classifying invoices into accounting accounts.

    Usage:
        classifier = GeminiAccountClassifier(api_key=settings.GEMINI_API_KEY)
        service = AccountClassifierService(classifier=classifier)
        summary = service.classify_unclassified_invoices()
        print(summary)
    """

    def __init__(self, classifier: AccountClassifierInterface):
        self.classifier = classifier

    def classify_unclassified_invoices(self, invoice_ids: list = None) -> ClassificationSummary:
        errors = []
        total_classified_items = 0
        total_flagged_items = 0
        total_processed_items = 0

        qs = Invoice.objects.filter(
            items__account__isnull=True, # Look for invoices that have unclassified items
            ocr_processed=True,
        ).distinct()
        if invoice_ids:
            qs = qs.filter(pk__in=invoice_ids)

        unclassified_invoices = qs.select_related("company").prefetch_related("items")
        
        if not unclassified_invoices.exists():
            return ClassificationSummary(
                total_invoices=0, total_items=0,
                classified_items=0, flagged_items=0,
                errors=[],
            )


        # Step 3: Group invoices by company
        from collections import defaultdict
        invoices_by_company = defaultdict(list)
        for invoice in unclassified_invoices:
            invoices_by_company[invoice.company].append(invoice)

        total_invoices = sum(len(v) for v in invoices_by_company.values())
    

        # Step 4: One classifier call per company
        for company, company_invoices in invoices_by_company.items():
            account_options = self._get_account_options(company)
            if not account_options:
                errors.append(f"No postable accounts for company {company}, skipping.")
                continue

            invoice_payload = self._build_invoice_payload(company_invoices)
            if not invoice_payload:
                continue

            # Count items in this batch
            batch_items = sum(len(inv["line_items"]) for inv in invoice_payload)
            total_processed_items += batch_items

            activity = company.business_activity_description if company else ""

            try:
                batch_response = self.classifier.classify_invoices(
                    invoices=invoice_payload,
                    account_options=account_options,
                    company_activity_description=activity,
                    accounting_framework=self._get_accounting_framework(company),
                )
                classified, flagged, save_errors = self._save_results(
                    batch_response=batch_response,
                    valid_codes={acc.code for acc in account_options},
                    company=company,
                )
                total_classified_items += classified
                total_flagged_items += flagged
                errors.extend(save_errors)
            except Exception as e:
                errors.append(f"Classifier failed for company {company}: {str(e)}")


        return ClassificationSummary(
            total_invoices=total_invoices,
            total_items=total_processed_items,
            classified_items=total_classified_items,
            flagged_items=total_flagged_items,
            errors=errors,
        )

    # -------------------------------------------------------------------------
    # PRIVATE HELPERS
    # -------------------------------------------------------------------------

    def _get_account_options(self, company) -> List[AccountOption]: 
        accounts = CompanyAccount.objects.filter(company=company, is_postable=True).values("code", "name")
        return [AccountOption(code=acc["code"], name=acc["name"]) for acc in accounts]


    def _build_invoice_payload(self, invoices) -> List[dict]:
        payload = []
        for invoice in invoices:
            line_items = [
                InvoiceLineItem(
                    id=item.pk,
                    description=item.description,
                    quantity=float(item.quantity) if item.quantity else None,
                    unit_price=float(item.unit_price.amount) if item.unit_price else None,
                    total=float(item.line_total.amount) if item.line_total else None,
                    tax_rate=None,
                )
                for item in invoice.items.all()
                if item.account is None # Only classify unclassified items
            ]
            if line_items:
                payload.append({
                    "id": invoice.pk,
                    "line_items": line_items,
                })
        return payload
    
    def _get_accounting_framework(self, company) -> str:
        FRAMEWORK_MAP = {
            "ES": "PGC (Plan General Contable, Spain)",
            "GB": "UK GAAP",
            "US": "US GAAP",
            "FR": "PCG (Plan Comptable Général, France)",
            "DE": "HGB (Handelsgesetzbuch, Germany)",
        }
        code = str(company.constituted_country) if company.constituted_country else ""
        return FRAMEWORK_MAP.get(code, "")

    @transaction.atomic
    def _save_results(self, batch_response: ClassificationBatchResponse, valid_codes: set, company):
        """
        Save classification results back to the InvoiceItem table.

        - Confident + valid code -> set item.account
        - Not confident or invalid code -> leave item.account NULL
        
        Also updates Invoice.needs_manual_review if any item is not confident.
        And saves the relevant raw response to Invoice.gemini_account_response.
        """
        from invoices.models import InvoiceItem, Invoice

        classified = 0
        flagged = 0
        errors = []

        # Build a map of code -> CompanyAccount for fast lookup
        code_to_account = {
            ca.code: ca
            for ca in CompanyAccount.objects.filter(
                company=company, code__in=valid_codes, is_postable=True
            )
        }

        # Track which invoices were affected to update their needs_manual_review status
        affected_invoice_ids = set()

        for result in batch_response.results:
            try:
                item = InvoiceItem.objects.get(pk=result.line_item_id)
                affected_invoice_ids.add(result.invoice_id)

                if result.confident and result.account_code in code_to_account:
                    # Gemini is confident and the code is valid — assign the account
                    item.account = code_to_account[result.account_code]
                    item.save(update_fields=["account"])
                    classified += 1
                else:
                    # Gemini is unsure or returned an invalid code — flag for review
                    item.account = None
                    item.save(update_fields=["account"])
                    flagged += 1

            except InvoiceItem.DoesNotExist:
                errors.append(f"Line item {result.line_item_id} not found in DB.")
            except Exception as e:
                errors.append(f"Failed to save result for item {result.line_item_id}: {str(e)}")

        # Update needs_manual_review and gemini_account_response for all affected invoices
        for inv_id in affected_invoice_ids:
            try:
                invoice = Invoice.objects.get(pk=inv_id)
                # An invoice needs review if ANY of its items have no account
                has_unclassified_items = invoice.items.filter(account__isnull=True).exists()
                invoice.needs_manual_review = has_unclassified_items
                
                # Filter raw response for this invoice only
                if isinstance(batch_response.raw_response, list):
                    relevant_raw = [
                        item for item in batch_response.raw_response 
                        if item.get("invoice_id") == inv_id
                    ]
                    invoice.gemini_account_response = relevant_raw
                
                invoice.save(update_fields=["needs_manual_review", "gemini_account_response"])
            except Invoice.DoesNotExist:
                pass

        return classified, flagged, errors