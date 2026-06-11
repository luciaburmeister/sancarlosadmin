from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import TaxScheduleRule, TaxPeriod, TaxPeriodStatus


REGEN_FIELDS = {"start_date", "period_length", "period_unit", "payment_day", "meses_hasta_vencimiento"}


@receiver(pre_save, sender=TaxScheduleRule)
def detect_rule_changes(sender, instance, **kwargs):
    if not instance.pk:
        instance._changed_fields = set()
        return
    try:
        old = TaxScheduleRule.objects.get(pk=instance.pk)
        changed = {f for f in REGEN_FIELDS if getattr(old, f) != getattr(instance, f)}
        instance._changed_fields = changed
    except TaxScheduleRule.DoesNotExist:
        instance._changed_fields = set()


@receiver(post_save, sender=TaxScheduleRule)
def auto_generate_periods(sender, instance, created, **kwargs):
    if not instance.is_active:
        return

    changed = getattr(instance, "_changed_fields", set())

    if created or (changed & REGEN_FIELDS):
        # Wipe all OPEN periods — stale ones from previous config
        TaxPeriod.objects.filter(
            rule=instance,
            status=TaxPeriodStatus.OPEN,
        ).delete()

    instance.generate_up_to_today()


def advance_all_rules():
    today = timezone.now().date()

    for rule in TaxScheduleRule.objects.filter(is_active=True):
        _clean_misaligned_periods(rule)
        rule.generate_up_to_today()

    TaxPeriod.objects.filter(
        due_date__lt=today,
        status__in=[TaxPeriodStatus.OPEN, TaxPeriodStatus.PENDING_PAYMENT],
    ).update(status=TaxPeriodStatus.OVERDUE)


def _clean_misaligned_periods(rule):
    """
    Delete OPEN and OVERDUE periods that don't align with the rule's
    current start_date and frequency. PAID and PENDING_PAYMENT are never touched.
    """
    from dateutil.relativedelta import relativedelta

    candidates = list(
        TaxPeriod.objects.filter(
            rule=rule,
            status__in=[TaxPeriodStatus.OPEN, TaxPeriodStatus.OVERDUE],
        )
    )
    if not candidates:
        return

    # Build the set of valid date_from values based on current rule config
    today = timezone.now().date()
    valid_starts = set()
    start = rule.start_date
    while start <= today:
        end = start + relativedelta(**{rule.period_unit: rule.period_length}) - relativedelta(days=1)
        valid_starts.add(start)
        start = end + relativedelta(days=1)

    stale_ids = [p.pk for p in candidates if p.date_from not in valid_starts]
    if stale_ids:
        TaxPeriod.objects.filter(pk__in=stale_ids).delete()