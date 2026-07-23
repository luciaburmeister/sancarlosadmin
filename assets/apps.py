from django.apps import AppConfig


class AssetsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'assets'

    def ready(self):
        from assets.models import AssetTransaction
        from assets.services.movement_context import build_asset_transaction_context
        from management.services.work_entry import WorkEntryService

        WorkEntryService.register_context_builder(AssetTransaction, build_asset_transaction_context)
