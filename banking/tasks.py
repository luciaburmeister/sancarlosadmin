"""Celery tasks for the banking app — periodic bank transaction sync."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


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
        return result
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
