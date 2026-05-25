import base64

from django.core.files.base import ContentFile
from django.db import migrations, models


def migrate_client_terms(apps, schema_editor):
    Client = apps.get_model("sales", "Client")
    Company = apps.get_model("company", "Company")

    for client in Client.objects.all().iterator():
        old_signature = (getattr(client, "terms_signature", None) or "").strip()
        company = None
        if client.company_id:
            company = Company.objects.filter(pk=client.company_id).first()

        if old_signature:
            try:
                raw = (
                    old_signature.split(";base64,", 1)[-1]
                    if ";base64," in old_signature
                    else old_signature
                )
                image_bytes = base64.b64decode(raw)
                client.terms_signature_file.save(
                    f"client_{client.pk}_terms.png",
                    ContentFile(image_bytes),
                    save=False,
                )
            except (ValueError, TypeError):
                pass

            if company:
                client.accepted_terms_company_id = company.pk
                client.accepted_terms_text = (company.terms_text or "").strip()

        client.save(
            update_fields=[
                "terms_signature_file",
                "accepted_terms_company",
                "accepted_terms_text",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0035_company_terms_text"),
        ("sales", "0020_client_terms_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="accepted_terms_company",
            field=models.ForeignKey(
                blank=True,
                help_text="Company whose terms were accepted; immutable once set.",
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="client_terms_acceptances",
                to="company.company",
                verbose_name="T&C company reference",
            ),
        ),
        migrations.AddField(
            model_name="client",
            name="accepted_terms_text",
            field=models.TextField(
                blank=True,
                editable=False,
                help_text="Copy of the company terms at acceptance time.",
                verbose_name="T&C text snapshot",
            ),
        ),
        migrations.AddField(
            model_name="client",
            name="terms_signature_file",
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to="client_terms_signatures/",
                verbose_name="Firma T&C",
            ),
        ),
        migrations.RunPython(migrate_client_terms, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="client",
            name="terms_signature",
        ),
        migrations.RemoveField(
            model_name="client",
            name="terms_version",
        ),
        migrations.RenameField(
            model_name="client",
            old_name="terms_signature_file",
            new_name="terms_signature",
        ),
    ]
