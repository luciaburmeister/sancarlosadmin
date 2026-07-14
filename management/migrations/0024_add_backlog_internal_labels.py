# Generated manually for backlog internal labels

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0062_company_bulk_emit_copy_quote_observations'),
        ('management', '0023_backlog_item_not_reusable_by_default'),
    ]

    operations = [
        migrations.CreateModel(
            name='ResourcePlanningBacklogLabel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Name')),
                ('color', models.CharField(default='#6B7280', help_text='Hex color for display, e.g. #6B7280', max_length=7, verbose_name='Color')),
                ('is_active', models.BooleanField(default=True, verbose_name='Active')),
                ('company', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='%(class)ss', to='company.company', verbose_name='Company')),
            ],
            options={
                'verbose_name': 'Internal Backlog Label',
                'verbose_name_plural': 'Internal Backlog Labels',
                'ordering': ['name'],
            },
        ),
        migrations.AddConstraint(
            model_name='resourceplanningbackloglabel',
            constraint=models.UniqueConstraint(fields=('company', 'name'), name='unique_backlog_label_per_company'),
        ),
        migrations.AddField(
            model_name='resourceplanningbacklogitem',
            name='internal_labels',
            field=models.ManyToManyField(blank=True, related_name='backlog_items', to='management.resourceplanningbackloglabel', verbose_name='Internal labels'),
        ),
    ]
