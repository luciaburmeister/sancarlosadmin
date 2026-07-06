from django.db import migrations


def copy_comments_forward(apps, schema_editor):
    """Copy legacy plain-text comments into comment_entries.

    Legacy comments have no timestamp, so "at" stays null.
    """
    ProductionItemSession = apps.get_model("production", "ProductionItemSession")
    qs = ProductionItemSession.objects.exclude(comments=[]).filter(comment_entries=[])
    batch = []
    for session in qs.iterator():
        session.comment_entries = [
            {"text": text, "at": None}
            for text in (session.comments or [])
            if (text or "").strip()
        ]
        batch.append(session)
        if len(batch) >= 500:
            ProductionItemSession.objects.bulk_update(batch, ["comment_entries"])
            batch = []
    if batch:
        ProductionItemSession.objects.bulk_update(batch, ["comment_entries"])


def copy_comments_backward(apps, schema_editor):
    # comments field is left untouched by the forward migration; nothing to undo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0039_productionitemsession_comment_entries"),
    ]

    operations = [
        migrations.RunPython(copy_comments_forward, copy_comments_backward),
    ]
