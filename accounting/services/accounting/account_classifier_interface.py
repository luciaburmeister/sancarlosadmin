"""
Account Classifier Interface

A "contract" or "rulebook" — NOT a workflow.
Any account classifier (Gemini, OpenAI, etc.) must implement this interface.
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class InvoiceLineItem:
    """A single line item from an invoice, passed to the classifier."""
    description: Optional[str]
    quantity: Optional[float]
    unit_price: Optional[float]
    total: Optional[float]
    tax_rate: Optional[float]


@dataclass
class AccountOption:
    """A single account from the chart of accounts, passed to the classifier."""
    code: str
    name: str


@dataclass
class ClassificationResult:
    """
    The result of classifying a single invoice.

    - invoice_id: the PK of the Invoice being classified
    - account_code: the code Gemini chose (e.g. "621"), or None if unsure
    - confident: True if Gemini is sure, False if unsure
      -> If False: account_code will be None and invoice gets flagged for manual review
    """
    invoice_id: int
    account_code: Optional[str]
    confident: bool


class AccountClassifierInterface(ABC):

    @abstractmethod
    def classify_invoices(
        self,
        invoices: List[dict],
        account_options: List[AccountOption],
        company_activity_description: str = "",
        accounting_framework: str = "",
    ) -> List[ClassificationResult]:
        """
        Classify a batch of invoices into accounting accounts.
        Args:
            invoices: List of dicts, each with:
                - "id": Invoice PK (int)
                - "line_items": list of InvoiceLineItem
            account_options: Postable accounts for this specific company.
            company_activity_description: What the company does.
            accounting_framework: The accounting standard in use, e.g. "PGC (Spain)", "UK GAAP".

        Returns:
            List of ClassificationResult, one per invoice.
            Order is not guaranteed — match by invoice_id.

        Raises:
            ValueError: If the response from the provider is unparseable.
        """
        raise NotImplementedError("Subclasses must implement classify_invoices")