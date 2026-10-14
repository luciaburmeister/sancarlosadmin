from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0087_workingzone_cif"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="claude_api_key_encrypted",
            field=models.TextField(
                blank=True,
                help_text="Stored encrypted. Set it from the company form.",
                verbose_name="Claude API key",
            ),
        ),
    ]
