"""Celery tasks for the banking app — periodic bank transaction sync."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_matcher_for_transaction(self, bank_transaction_id: int):
    """Run the transaction matcher for a single BankTransaction.

    Dispatched automatically after every new transaction is imported.
    Skipped if a confirmed reconciliation already exists.

    Returns a result dict consumed by the matcher status page:
        {"status": "done",    "reconciliation_id": X, "has_match": bool}
        {"status": "skipped"}
        {"status": "no_match"}
    """
    from banking.models import BankReconciliation, BankTransaction, ReconciliationStatus
    from banking.services.transaction_matching import TransactionMatcher

    try:
        txn = BankTransaction.objects.get(pk=bank_transaction_id)

        if txn.reconciliation_status == ReconciliationStatus.RECONCILED:
            logger.info("Transaction %s already reconciled, skipping matcher.", bank_transaction_id)
            return {"status": "skipped"}

        BankReconciliation.objects.filter(bank_transaction=txn, is_confirmed=False).delete()

        matcher = TransactionMatcher(txn)
        recon = matcher.run()
        selected = TransactionMatcher.get_selected_allocations(
            recon.suggested_allocations,
        )
        logger.info(
            "Matcher ran for transaction %s: %d suggestion(s), unmatched=%s",
            bank_transaction_id,
            len(recon.suggested_allocations),
            recon.unmatched_amount,
        )
        return {
            "status": "done",
            "reconciliation_id": recon.pk,
            "has_match": bool(selected),
        }
    except BankTransaction.DoesNotExist:
        logger.error("BankTransaction %s does not exist, skipping matcher.", bank_transaction_id)
        return {"status": "error", "message": f"BankTransaction {bank_transaction_id} not found."}
    except Exception as exc:
        logger.exception("Matcher failed for transaction %s", bank_transaction_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def run_invoice_matcher_task(self, model_name: str, invoice_id: int):
    """Run InvoiceMatcher for a single Invoice or SalesInvoice.

    model_name: 'invoice' or 'salesinvoice'

    Returns a result dict consumed by the matcher status page:
        {"status": "done",     "suggestion_id": X, "has_match": bool}
        {"status": "no_match"}
    """
    from banking.models import InvoiceReconciliationSuggestion
    from banking.services.transaction_matching import InvoiceMatcher
    from django.contrib.contenttypes.models import ContentType

    model_map = {
        "invoice": "invoices.Invoice",
        "salesinvoice": "invoices.SalesInvoice",
    }
    app_label, model = model_map[model_name].split(".")

    try:
        from django.apps import apps
        InvoiceModel = apps.get_model(app_label, model)
        invoice = InvoiceModel.objects.get(pk=invoice_id)

        ct = ContentType.objects.get_for_model(invoice)
        InvoiceReconciliationSuggestion.objects.filter(
            content_type=ct, object_id=invoice_id, is_confirmed=False
        ).delete()

        matcher = InvoiceMatcher(invoice)
        suggestion = matcher.run()
        has_match = bool(suggestion.suggested_combinations)
        logger.info(
            "InvoiceMatcher ran for %s #%s: %d combination(s)",
            model_name,
            invoice_id,
            len(suggestion.suggested_combinations),
        )
        return {
            "status": "done" if has_match else "no_match",
            "suggestion_id": suggestion.pk,
            "has_match": has_match,
        }
    except Exception as exc:
        logger.exception("InvoiceMatcher failed for %s #%s", model_name, invoice_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def sync_bank_transactions(self):
    """Periodic task: fetch new transactions for all connected bank accounts.

    Runs every 6 hours via Celery Beat.  Syncs all BankAccounts that
    have a nuek_token (Arcopay PSD2 token).
    """
    from banking.services.arcopay_import import ArcopayService

    try:
        result = ArcopayService.sync_all_connected_accounts()
        logger.info(
            "Bank transaction sync complete: %d new transactions across %d accounts",
            result.synced,
            len(result.accounts),
        )
        if result.errors:
            logger.warning("Sync errors: %s", result.errors)
        return result.to_dict()
    except Exception as exc:
        logger.exception("Bank transaction sync failed")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def sync_bank_account_task(self, bank_account_id: int):
    """On-demand task: fetch transactions for a single BankAccount."""
    from company.models import BankAccount
    from banking.services.arcopay_import import ArcopayService

    try:
        account = BankAccount.objects.get(pk=bank_account_id)
        count = ArcopayService.sync_account(account)
        logger.info(
            "Bank account %s synced: %d new transactions",
            bank_account_id, count,
        )
        return {"bank_account_id": bank_account_id, "imported": count}
    except BankAccount.DoesNotExist:
        logger.error("BankAccount %s does not exist", bank_account_id)
        return {"error": f"BankAccount {bank_account_id} not found"}
    except Exception as exc:
        logger.exception("Sync failed for BankAccount %s", bank_account_id)
        raise self.retry(exc=exc)
