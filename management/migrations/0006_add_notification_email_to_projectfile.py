from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('management', '0005_add_resource_planning'),
    ]

    operations = [
        migrations.AddField(
            model_name='projectfile',
            name='notification_email',
            field=models.EmailField(
                blank=True,
                help_text='Email address for project notifications (e.g., asset transactions). Falls back to the client email if left empty.',
                max_length=254,
                verbose_name='Notification Email',
            ),
        ),
    ]
