from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from .models import ProductModel, ManufacturingItem, CutListEntry, LaborEntry
from unfold.widgets import UnfoldAdminColorInputWidget

@admin.register(ProductModel)
class ProductModelAdmin(ModelAdmin):
    list_display = ["name", "slug", "logic_handler_path"]
    search_fields = ["name", "description"]
    prepopulated_fields = {"slug": ("name",)}
    fieldsets = (
        (None, {
            "fields": ("name", "slug", "description")
        }),
        ("Logic Configuration", {
            "classes": ("collapse",),
            "fields": ("logic_handler_path",),
            "description": "Path to the Python class that handles the calculation formulas."
        }),
    )

class CutListEntryInline(TabularInline):
    model = CutListEntry
    extra = 0
    tab = True 
    fields = [
        "material", 
        "description", 
        "units_needed", 
        "measure_mm", 
        "cut_type", 
        "cost_snapshot"
    ]
    autocomplete_fields = ["material"] 

class LaborEntryInline(TabularInline):
    model = LaborEntry
    extra = 0
    tab = True
    fields = ["task_name", "hours", "hourly_rate"]
    


@admin.register(ManufacturingItem)
class ManufacturingItemAdmin(ModelAdmin):
    list_display = [
        "product_model", 
        "order", 
        "width", 
        "height", 
        "quantity", 
        "finish_ral"
    ]
    list_filter = ["product_model", "finish_ral"]
    search_fields = ["order__project_name", "observations"]
    
    inlines = [CutListEntryInline, LaborEntryInline]

    fieldsets = (
        ("Order Context", {
            "fields": ("order", "product_model")
        }),
        ("Dimensions & Specs", {
            "fields": (("width", "height"), ("quantity", "finish_ral"))
        }),
        ("Notes", {
            "fields": ("observations",)
        }),
    )
    
    actions = ["calculate_cuts_action"]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields["finish_ral"].widget = UnfoldAdminColorInputWidget()
        return form

    @admin.action(description="Recalculate Cut Lists for selected items")
    def calculate_cuts_action(self, request, queryset):
        count = 0
        for item in queryset:
            item.calculate_cut_list()
            count += 1
        self.message_user(request, f"Recalculated cuts for {count} items.")