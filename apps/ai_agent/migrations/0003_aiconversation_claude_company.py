import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0088_company_claude_api_key_encrypted"),
        ("ai_agent", "0002_aimessage_artifacts"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiconversation",
            name="claude_company",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ai_conversations",
                to="company.company",
                verbose_name="Company",
            ),
        ),
    ]
