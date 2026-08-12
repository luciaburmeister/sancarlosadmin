# Generated manually to reconcile parallel 0073 (main) and 0074 (feature) branches.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0073_bankaccount_consent_checked_at_and_more"),
        ("company", "0074_company_asset_plan_draft_notification_emails"),
    ]

    operations = []
