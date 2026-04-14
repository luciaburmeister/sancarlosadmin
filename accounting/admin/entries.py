from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from core.admin.company_admin import CompanyRestrictedAdmin
from accounting.models import JournalEntry, JournalEntryLine, EntryStatus

class JournalEntryLineInline(TabularInline):
    model = JournalEntryLine
    extra = 0
    autocomplete_fields = ["account"]
    fields = ["account", "description", "debit", "credit"]
    
    # We might want to make it readonly if entry is posted?
    # But CompanyRestrictedAdmin or ModelAdmin usually handle this?
    # We implemented validation in clean(), showing error is fine.
    
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
             return [f.name for f in self.model._meta.fields]
        return super().get_readonly_fields(request, obj)
    
    def has_add_permission(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(JournalEntry)
class JournalEntryAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "date", "journal", "company", "status", "total_debit", "total_credit", "is_balanced"]
    list_filter = ["company", "journal", "status", "date"]
    search_fields = ["number", "reference", "description"]
    
    ordering = ["-date", "-number"]
    list_select_related = ["company", "journal"]
    autocomplete_fields = ["company","journal","purchase_invoice","sales_invoice"]
    date_hierarchy = "date"
    
    inlines = [JournalEntryLineInline]
    
    readonly_fields = ["posted_at", "posted_by", "number"]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.status == EntryStatus.POSTED:
            # Lock everything except maybe reference?
            return [f.name for f in self.model._meta.fields]
        return readonly

    def save_model(self, request, obj, form, change):
        # We can handle posting action here perhaps? 
        # For now just standard save.
        if not obj.pk:
             # creation
             pass
        super().save_model(request, obj, form, change)
