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
    3. Computes tax amounts for ALL non-paid periods (open, pending, overdue)
       so importes are always up to date without manual intervention
    """
    from .models import TaxPeriod, TaxPeriodStatus
    from .models import advance_all_rules

    today = timezone.now().date()

    advance_all_rules()

    periods_to_compute = TaxPeriod.objects.filter(
        status__in=[
            TaxPeriodStatus.OPEN,
            TaxPeriodStatus.PENDING_PAYMENT,
            TaxPeriodStatus.OVERDUE,
        ],
    ).select_related("rule", "rule__tax_type", "company")

    computed = 0
    for period in periods_to_compute:
        try:
            period.compute(save=True)
            computed += 1
        except Exception as e:
            logger.error(f"finance: failed to compute period {period.pk}: {e}")

    logger.info(f"finance: computed {computed} periods")
    return {"advanced": True, "computed": computed}