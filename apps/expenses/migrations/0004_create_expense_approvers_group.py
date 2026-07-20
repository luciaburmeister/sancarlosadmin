from django.apps import apps as global_apps
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUP_NAME = "Expense Approvers"
PERMISSION_CODENAMES = [
    ("expenses", "expenseclaim", "approve_expenseclaim"),
    ("expenses", "expenseclaim", "view_expenseclaim"),
    ("expenses", "expenseticket", "view_expenseticket"),
]


def create_group(apps, schema_editor):
    # Permission rows for Meta.permissions are normally inserted by a post_migrate
    # signal that fires once after the *entire* `migrate` run completes - too late
    # for this data migration to rely on them existing yet if it runs in the same
    # invocation as the migration that declared them. Force-create them now.
    for app_config in global_apps.get_app_configs():
        create_permissions(app_config, apps=global_apps, verbosity=0)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    group, _ = Group.objects.get_or_create(name=GROUP_NAME)

    for app_label, model_name, codename in PERMISSION_CODENAMES:
        content_type = ContentType.objects.get(app_label=app_label, model=model_name)
        try:
            permission = Permission.objects.get(content_type=content_type, codename=codename)
        except Permission.DoesNotExist:
            continue
        group.permissions.add(permission)


def remove_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0003_add_approve_expenseclaim_permission"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(create_group, remove_group),
    ]
