# Remove company scope from backlog labels

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('management', '0024_add_backlog_internal_labels'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='resourceplanningbackloglabel',
            name='unique_backlog_label_per_company',
        ),
        migrations.RemoveField(
            model_name='resourceplanningbackloglabel',
            name='company',
        ),
        migrations.AlterField(
            model_name='resourceplanningbackloglabel',
            name='name',
            field=models.CharField(max_length=100, unique=True, verbose_name='Name'),
        ),
    ]
