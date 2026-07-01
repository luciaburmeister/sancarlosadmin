import django.contrib.postgres.fields
from django.db import migrations, models


def split_comments_forward(apps, schema_editor):
    ProductionItemSession = apps.get_model("production", "ProductionItemSession")
    sessions = ProductionItemSession.objects.exclude(comments__isnull=True).exclude(comments="")
    for session in sessions.iterator():
        notes = [note.strip() for note in session.comments.split("\n\n") if note.strip()]
        session.comments_array = notes
        session.save(update_fields=["comments_array"])


def join_comments_backward(apps, schema_editor):
    ProductionItemSession = apps.get_model("production", "ProductionItemSession")
    sessions = ProductionItemSession.objects.exclude(comments_array=[])
    for session in sessions.iterator():
        session.comments = "\n\n".join(session.comments_array)
        session.save(update_fields=["comments"])


class Migration(migrations.Migration):

    dependencies = [
        ('production', '0037_alter_productionitemsession_comments'),
    ]

    operations = [
        migrations.AddField(
            model_name='productionitemsession',
            name='comments_array',
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.TextField(),
                blank=True,
                default=list,
                help_text='Ordered list of comments left on this session',
                size=None,
                verbose_name='Comments',
            ),
        ),
        migrations.RunPython(split_comments_forward, join_comments_backward),
        migrations.RemoveField(
            model_name='productionitemsession',
            name='comments',
        ),
        migrations.RenameField(
            model_name='productionitemsession',
            old_name='comments_array',
            new_name='comments',
        ),
    ]
