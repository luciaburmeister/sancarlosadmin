from celery import shared_task
from django.utils import timezone

import logging
logger = logging.getLogger(__name__)


@shared_task(bind=True, name="finance.advance_and_compute_periods")
def advance_and_compute_periods(self):
    """
    Runs hourly via Celery Beat:
    1. Generates any missing TaxPeriods up to today for all active rules
    2. Marks overdue periods
    3. Computes ALL non-paid periods in chronological order per rule
       so accumulated_credit is always correct
    """
    from .models import TaxPeriod, TaxPeriodStatus, TaxScheduleRule, advance_all_rules

    # Step 1 & 2: generate missing periods + flag overdue
    advance_all_rules()

    # Step 3: compute all rules in order
    computed = 0
    for rule in TaxScheduleRule.objects.filter(is_active=True):
        periods = TaxPeriod.objects.filter(
            rule=rule,
            status__in=[
                TaxPeriodStatus.OPEN,
                TaxPeriodStatus.PENDING_PAYMENT,
                TaxPeriodStatus.OVERDUE,
            ],
        ).order_by("date_from")

        for period in periods:
            try:
                period.compute(save=True)
                computed += 1
            except Exception as e:
                logger.error(f"finance: failed to compute period {period.pk}: {e}")

    logger.info(f"finance: computed {computed} periods in chronological order")
    return {"advanced": True, "computed": computed}