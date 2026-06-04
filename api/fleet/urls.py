from django.urls import path

from company.views.views import LocationRecordCreateView
from assets.views import asset_transaction_imagen_view

from .views import assets, attendance, zones

urlpatterns = [
    path("assets/", assets.AssetsListView.as_view(), name="assets"),
    path("assets/<int:asset_id>/", assets.AssetDetailView.as_view(), name="asset_by_id"),
    path("assets/<int:asset_id>/pickup/", assets.AssetPickupView.as_view(), name="pickup"),
    path("assets/<int:asset_id>/deliver/", assets.AssetDeliverView.as_view(), name="deliver"),
    path("assets/<int:asset_id>/assign/", assets.AssetAssignView.as_view(), name="assign"),
    path("assets/<int:asset_id>/qr/", assets.AssetQRView.as_view(), name="asset_qr"),
    path(
        "assets/<int:asset_id>/check-location/",
        assets.AssetCheckDeliveryLocationView.as_view(),
        name="check_delivery_location",
    ),
    path("assets/transaction/<int:transaction_id>/imagen/", asset_transaction_imagen_view, name="transaction_imagen"),
    path("attendance/clock-in/", attendance.AttendanceClockInView.as_view(), name="clock_in"),
    path("attendance/clock-out/", attendance.AttendanceClockOutView.as_view(), name="clock_out"),
    path("attendance/status/", attendance.AttendanceStatusView.as_view(), name="attendance_status"),
    path("location-records/", LocationRecordCreateView.as_view(), name="location-record-create"),
    path("zones/", zones.ZonesListView.as_view(), name="zones"),
    path("clients/", zones.ClientsListView.as_view(), name="clients"),
    path("plannings/", attendance.PlanningsListView.as_view(), name="plannings"),
    path("movements/", attendance.MovementsListView.as_view(), name="movements"),
    path("identifiers/", zones.AssetIdentifiersView.as_view(), name="identifiers"),
    path("config/", zones.ConfigListView.as_view(), name="config"),

]