from django.contrib import admin
from django.urls import path
from company.models import LocationRecord
from unfold.admin import ModelAdmin
from company.views.driver_tracking import DriverTrackingView



@admin.register(LocationRecord)
class LocationRecordAdmin(ModelAdmin):
    list_display = ['employee', 'latitude', 'longitude', 'recorded_at']
    list_filter = ['employee']
    search_fields = ['employee__first_name', 'employee__last_name']

    def get_urls(self):
        from company.views.driver_tracking import DriverTrackingView
        return [
            path(
                "driver-tracking/",
                self.admin_site.admin_view(
                    DriverTrackingView.as_view(model_admin=self)
                ),
                name="company_locationrecord_driver_tracking",
            ),
        ] + super().get_urls()