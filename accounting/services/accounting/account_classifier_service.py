"""
Account Classifier Service

Orchestrates the workflow of classifying invoices into accounting accounts:

1. Fetch all invoices that have no account assigned yet
2. Fetch all postable accounts from the chart of accounts
3. Send both to Gemini in a single batch call
4. For each result:
   - If confident -> save the account to the invoice
   - If not confident -> leave account as NULL, flag needs_manual_review=True
5. Return a summary of what happened
"""

from typing import List
from dataclasses import dataclass

from django.db import transaction

from company.admin import company
from invoices.models import Invoice
from accounting.models import TemplateAccount
from .account_classifier_interface import (
    AccountClassifierInterface,
    AccountOption,
    InvoiceLineItem,
)


@dataclass
class ClassificationSummary:
    """Summary returned after running the classification pipeline."""
    total_invoices: int         # How many invoices were processed
    classified: int             # How many got an account assigned
    flagged_for_review: int     # How many Gemini was unsure about
    errors: List[str]           # Any problems that occurred

    def __str__(self):
        return (
            f"Classified {self.classified}/{self.total_invoices} invoices. "
            f"{self.flagged_for_review} flagged for manual review. "
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
        total_classified = 0
        total_flagged = 0

        qs = Invoice.objects.filter(
            account__isnull=True,
            ocr_processed=True,
            items__isnull=False,
        )
        if invoice_ids:
            qs = qs.filter(pk__in=invoice_ids)

        unclassified_invoices = qs.select_related("company").distinct()
        
        if not unclassified_invoices.exists():
            return ClassificationSummary(
                total_invoices=0, classified=0,
                flagged_for_review=0, errors=[],
            )

        # Step 2: Fetch account options (same for all companies)
        account_options = self._get_account_options()
        if not account_options:
            return ClassificationSummary(
                total_invoices=0, classified=0,
                flagged_for_review=0,
                errors=["No postable accounts found."],
            )

        # Step 3: Group invoices by company
        from collections import defaultdict
        invoices_by_company = defaultdict(list)
        for invoice in unclassified_invoices:
            invoices_by_company[invoice.company].append(invoice)

        total_invoices = sum(len(v) for v in invoices_by_company.values())
        all_results = []

        # Step 4: One classifier call per company
        for company, company_invoices in invoices_by_company.items():
            invoice_payload = self._build_invoice_payload(company_invoices)
            activity = company.business_activity_description if company else ""

            try:
                results = self.classifier.classify_invoices(
                    invoices=invoice_payload,
                    account_options=account_options,
                    company_activity_description=activity,
                )
                all_results.extend(results)
            except Exception as e:
                errors.append(f"Classifier failed for company {company}: {str(e)}")

        # Step 5: Save all results
        classified, flagged, save_errors = self._save_results(
            results=all_results,
            valid_codes={acc.code for acc in account_options},
        )
        errors.extend(save_errors)

        return ClassificationSummary(
            total_invoices=total_invoices,
            classified=classified,
            flagged_for_review=flagged,
            errors=errors,
        )

    # -------------------------------------------------------------------------
    # PRIVATE HELPERS
    # -------------------------------------------------------------------------

    def _get_account_options(self) -> List[AccountOption]:
        """
        Fetch all postable TemplateAccount rows and convert them
        to AccountOption dataclasses for the classifier.
        """
        accounts = TemplateAccount.objects.filter(is_postable=True).values("code", "name")
        return [AccountOption(code=acc["code"], name=acc["name"]) for acc in accounts]

    def _build_invoice_payload(self, invoices) -> List[dict]:
        payload = []
        for invoice in invoices:
            line_items = [
                InvoiceLineItem(
                    description=item.description,
                    quantity=float(item.quantity) if item.quantity else None,
                    unit_price=float(item.unit_price.amount) if item.unit_price else None,
                    total=float(item.line_total.amount) if item.line_total else None,
                    tax_rate=None,
                )
                for item in invoice.items.all()  # ✅ reads from InvoiceItem rows
            ]
            payload.append({
                "id": invoice.pk,
                "line_items": line_items,
            })
        return payload

    @transaction.atomic
    def _save_results(self, results, valid_codes: set):
        """
        Save classification results back to the Invoice table.

        - Confident + valid code -> set invoice.account, needs_manual_review=False
        - Not confident or invalid code -> leave account NULL, needs_manual_review=True
        """
        classified = 0
        flagged = 0
        errors = []

        # Build a map of code -> TemplateAccount for fast lookup
        code_to_account = {
            ta.code: ta
            for ta in TemplateAccount.objects.filter(
                code__in=valid_codes, is_postable=True
            )
        }

        for result in results:
            try:
                invoice = Invoice.objects.get(pk=result.invoice_id)

                if result.confident and result.account_code in code_to_account:
                    # Gemini is confident and the code is valid — assign the account
                    invoice.account = code_to_account[result.account_code]
                    invoice.needs_manual_review = False
                    invoice.save(update_fields=["account", "needs_manual_review"])
                    classified += 1
                else:
                    # Gemini is unsure or returned an invalid code — flag for review
                    invoice.account = None
                    invoice.needs_manual_review = True
                    invoice.save(update_fields=["account", "needs_manual_review"])
                    flagged += 1

            except Invoice.DoesNotExist:
                errors.append(f"Invoice {result.invoice_id} not found in DB.")
            except Exception as e:
                errors.append(f"Failed to save result for invoice {result.invoice_id}: {str(e)}")

        return classified, flagged, errors