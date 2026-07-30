from django.urls import path

from company.views.views import LocationRecordCreateView
from assets.views import asset_transaction_imagen_view
from .views.assets import DeliveryCheckView, MachineHoursListView, MachineCheckView, LaborerZonesView, LaborerHoursView, LaborerHoursStatusView
from .views.attendance import MachineHoursHistorialView
from .views import assets, attendance, zones

urlpatterns = [
    path("assets/", assets.AssetsListView.as_view(), name="assets"),
    path("assets/<int:asset_id>/", assets.AssetDetailView.as_view(), name="asset_by_id"),
    path("assets/<int:asset_id>/transaction/", assets.AssetTransactionView.as_view(), name="asset_transaction"),
    path("assets/<int:asset_id>/assign/", assets.AssetAssignView.as_view(), name="assign"),
    path("assets/<int:asset_id>/qr/", assets.AssetQRView.as_view(), name="asset_qr"),
    path(
        "assets/<int:asset_id>/check-location/",
        assets.AssetCheckDeliveryLocationView.as_view(),
        name="check_delivery_location",
    ),
    path("assets/<int:asset_id>/machine-check/", MachineCheckView.as_view(), name="machine_check"),
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
    path("portes/", attendance.PortesPlanningListView.as_view(), name="portes_planning"),
    path("portes/<int:planning_id>/submit/", attendance.PorteSubmitView.as_view(), name="porte_submit"),
    path("assets/<int:asset_id>/delivery-check/", DeliveryCheckView.as_view(), name="asset-delivery-check"),
    path("machine-hours/", MachineHoursListView.as_view(), name="machine_hours_list"),
    path("machine-hours/history/", MachineHoursHistorialView.as_view(), name="machine_hours_history"),
    path("laborer/zones/", LaborerZonesView.as_view(), name="laborer_zones"),
    path("laborer/transaction/", LaborerHoursView.as_view(), name="laborer_transaction"),
    path("laborer/status/", LaborerHoursStatusView.as_view(), name="laborer_status"),
]