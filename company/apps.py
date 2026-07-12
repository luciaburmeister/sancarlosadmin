from django.apps import AppConfig


class CompanyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'company'

    def ready(self):
        from django.contrib import admin
        from unfold.admin import ModelAdmin

        # Globally enable warn_unsaved_form on all admin classes
        admin.ModelAdmin.warn_unsaved_form = True
        ModelAdmin.warn_unsaved_form = True

