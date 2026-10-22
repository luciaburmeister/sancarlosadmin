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
    """Run InvoiceMatcher for a single Invoice, SalesInvoice, or ExpenseClaim.

    model_name: 'invoice', 'salesinvoice', or 'expenseclaim'

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
        "expenseclaim": "expenses.ExpenseClaim",
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


@shared_task(bind=True, max_retries=2, default_retry_delay=300)
def match_fixed_cost_invoices_task(self):
    """Periodic task: for every unmatched FixedCostInstallment of an active
    schedule, (re)run FixedCostInvoiceMatcher to generate pending suggestions.

    Runs every 6 hours via Celery Beat, same cadence as sync_bank_transactions.
    Never confirms automatically - same as the manual "Find Matching Invoice"
    button on FixedCostInstallmentAdmin, just triggered on a schedule instead
    of a click.
    """
    from apps.expenses.models import FixedCostInstallment
    from banking.services.transaction_matching import FixedCostInvoiceMatcher

    try:
        installments = FixedCostInstallment.objects.filter(
            matched_invoice__isnull=True,
            schedule__is_active=True,
        )
        total = installments.count()
        with_new_suggestions = 0
        for installment in installments:
            if FixedCostInvoiceMatcher(installment).run():
                with_new_suggestions += 1

        logger.info(
            "Fixed cost invoice matcher: checked %d installment(s), %d got new suggestion(s).",
            total, with_new_suggestions,
        )
        return {"checked": total, "with_new_suggestions": with_new_suggestions}
    except Exception as exc:
        logger.exception("Fixed cost invoice matcher failed")
        raise self.retry(exc=exc)


@shared_task(name="banking.tasks.advance_fixed_cost_schedules_task")
def advance_fixed_cost_schedules_task():
    """Daily periodic task: generate due FixedCostInstallments for every
    active FixedCostSchedule.

    Without this, installments only get created when someone edits and
    saves the schedule in the admin - this is what makes new installments
    appear on their own as they enter the HORIZON_DAYS window, with no
    manual step required. One failing schedule is logged and skipped,
    never blocking the rest.
    """
    from apps.expenses.models import FixedCostSchedule

    advanced = 0
    errors = []
    for schedule in FixedCostSchedule.objects.filter(is_active=True):
        try:
            schedule.generate_due_installments()
            advanced += 1
        except Exception as exc:
            logger.exception("Failed to advance FixedCostSchedule %s", schedule.pk)
            errors.append(f"schedule {schedule.pk}: {exc}")

    logger.info(
        "Fixed cost schedules advanced: %d succeeded, %d failed.",
        advanced, len(errors),
    )
    return {"advanced": advanced, "errors": errors}


@shared_task(name="banking.tasks.send_bank_accounts_summary")
def send_bank_accounts_summary():
    """Periodic task: emails each company's bank account balances (last
    known balance + when it was last synced) to that company's configured
    recipients (Company.bank_summary_emails).

    Recipients configured on several companies receive ONE consolidated
    email covering all of their companies instead of one email per
    company. Recipients are grouped by the exact set of companies they
    are subscribed to, so recipients sharing the same set still get a
    single batched email.

    Companies with no recipients configured are skipped — this is opt-in
    per company, unlike company.tasks.send_document_reminders which falls
    back to company.email.

    Schedule it via a django-celery-beat PeriodicTask (admin > Periodic
    Tasks) pointing at "banking.tasks.send_bank_accounts_summary".
    """
    from django.template.loader import render_to_string
    from django.utils.formats import date_format, number_format
    from django.utils.timezone import localtime

    from company.models import Company
    from core.services.email_service import EmailService

    companies = (
        Company.objects.exclude(bank_summary_emails=[])
        .prefetch_related("bankaccounts")
    )

    company_summaries = {}
    recipient_company_ids = {}  # normalized email -> {"email": str, "company_ids": [int]}

    for company in companies:
        accounts = list(company.bankaccounts.all())
        if not accounts:
            continue

        account_rows = []
        for account in accounts:
            if account.last_known_balance is not None:
                balance_str = (
                    f"{number_format(account.last_known_balance.amount, 2, force_grouping=True)} €"
                )
            else:
                balance_str = "—"

            if account.last_balance_updated_at is not None:
                synced_str = date_format(
                    localtime(account.last_balance_updated_at),
                    "SHORT_DATETIME_FORMAT",
                )
            else:
                synced_str = "Nunca sincronizado"

            account_rows.append({
                "bank_name": account.bank_name,
                "identifier": account.iban or account.account_number or "—",
                "balance": balance_str,
                "synced_at": synced_str,
            })

        company_summaries[company.pk] = {
            "company": company,
            "accounts": account_rows,
        }

        for address in company.bank_summary_emails:
            normalized = address.strip().lower()
            if not normalized:
                continue
            entry = recipient_company_ids.setdefault(
                normalized, {"email": address.strip(), "company_ids": []}
            )
            if company.pk not in entry["company_ids"]:
                entry["company_ids"].append(company.pk)

    # Recipients subscribed to the same set of companies share one email.
    groups = {}  # tuple of company ids -> [recipient emails]
    for entry in recipient_company_ids.values():
        groups.setdefault(tuple(entry["company_ids"]), []).append(entry["email"])

    emails_sent = 0
    errors = []

    for company_ids, recipients in groups.items():
        summaries = [company_summaries[pk] for pk in company_ids]
        names = ", ".join(s["company"].name for s in summaries)
        single_company = summaries[0]["company"] if len(summaries) == 1 else None

        try:
            html_message = render_to_string(
                "company/email/bank_accounts_summary.html",
                {
                    "companies": [
                        {"name": s["company"].name, "accounts": s["accounts"]}
                        for s in summaries
                    ],
                },
            )
            success = EmailService.send_email(
                to=recipients,
                subject=f"Resumen de cuentas bancarias: {names}",
                body_html=html_message,
                related_object=single_company,
                company=single_company,
                email_document="bank_summary",
            )
            if success:
                emails_sent += 1
            else:
                errors.append(f"Failed to send summary for companies [{names}]")
        except Exception as e:
            errors.append(f"Error processing companies [{names}]: {str(e)}")

    if errors:
        raise Exception(f"Bank accounts summary task completed with errors: {'; '.join(errors)}")

    return {"task": "send_bank_accounts_summary", "status": "ok", "emails_sent": emails_sent}


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
