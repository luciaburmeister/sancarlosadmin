from django.contrib import admin
from company.models import LocationRecord

@admin.register(LocationRecord)
class LocationRecordAdmin(admin.ModelAdmin):
    list_display = ['employee', 'latitude', 'longitude', 'recorded_at']
    list_filter = ['employee']
    search_fields = ['employee__first_name', 'employee__last_name']