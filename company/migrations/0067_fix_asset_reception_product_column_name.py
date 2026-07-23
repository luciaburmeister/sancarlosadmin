from django.db import migrations


class Migration(migrations.Migration):
    """
    0066 originally added `vehicle_reception_product`; that migration file
    was later edited in place to rename it to `asset_reception_product`
    instead of adding a proper RenameField migration. Any database that had
    already run 0066 before the edit is left with the old column name even
    though Django's migration state says `asset_reception_product`. This
    renames the physical column without touching Django's (already correct)
    model state.
    """

    dependencies = [
        ('company', '0066_company_require_asset_on_project_file_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'company_company'
                            AND column_name = 'vehicle_reception_product_id'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'company_company'
                            AND column_name = 'asset_reception_product_id'
                        ) THEN
                            ALTER TABLE company_company
                            RENAME COLUMN vehicle_reception_product_id TO asset_reception_product_id;
                        END IF;
                    END $$;
                    """,
                    reverse_sql="""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'company_company'
                            AND column_name = 'asset_reception_product_id'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'company_company'
                            AND column_name = 'vehicle_reception_product_id'
                        ) THEN
                            ALTER TABLE company_company
                            RENAME COLUMN asset_reception_product_id TO vehicle_reception_product_id;
                        END IF;
                    END $$;
                    """,
                ),
            ],
            state_operations=[],
        ),
    ]
