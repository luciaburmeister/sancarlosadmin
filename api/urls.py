from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from . import views, wash_views
from company.views.views import LocationRecordCreateView


urlpatterns = [
    path("auth/login/", views.login_view.as_view(), name="jwt_login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="jwt_refresh"),
    path("assets/", views.assets_view, name="assets"),
    path("assets/<int:asset_id>/", views.asset_by_id_view, name="asset_by_id"),
    path("assets/<int:asset_id>/pickup/", views.pickup_view, name="pickup"),
    path("assets/<int:asset_id>/deliver/", views.deliver_view, name="deliver"),
    path("assets/<int:asset_id>/assign/", views.assign_view, name="assign"),
    path("assets/<int:asset_id>/qr/", views.asset_qr_view, name="asset_qr"),
    path("assets/<int:asset_id>/check-location/", views.check_delivery_location_view, name="check_delivery_location"),
    path("attendance/clock-in/", views.clock_in_view, name="clock_in"),
    path("attendance/clock-out/", views.clock_out_view, name="clock_out"),
    path("attendance/status/", views.attendance_status_view, name="attendance_status"),
    path("location-records/", LocationRecordCreateView.as_view(), name="location-record-create"),
    path("zones/", views.zones_view.as_view(), name="zones"),
    path("clients/", views.clients_view.as_view(), name="clients"),
    path("plannings/", views.plannings_view, name="plannings"),
    path("movements/", views.movements_view, name="movements"),

    # Wash
    path("wash/clients/", wash_views.wash_clients_view, name="wash_clients"),
    path("wash/templates/", wash_views.wash_templates_view, name="wash_templates"),
    path("wash/expedientes/", wash_views.wash_expedientes_view, name="wash_expedientes"),
    path("wash/expedientes/<int:expediente_id>/", wash_views.wash_expediente_detail_view, name="wash_expediente_detail"),
    path("wash/expedientes/<int:expediente_id>/configure/", wash_views.wash_expediente_configure_view, name="wash_expediente_configure"),
    path("wash/expedientes/<int:expediente_id>/configurations/<int:config_id>/", wash_views.wash_configuration_manage_view, name="wash_configuration_manage"),
]