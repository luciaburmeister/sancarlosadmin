from django.contrib import admin
from company.models import LocationRecord

@admin.register(LocationRecord)
class LocationRecordAdmin(admin.ModelAdmin):
    list_display = ['employee', 'latitude', 'longitude', 'recorded_at']