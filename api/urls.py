from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import (
    client_views,
    configuration_views,
    employee_views,
    me_views,
    product_template_views,
    production_stage_views,
    project_file_views,
    quote_views,
    views,
)
from company.views.views import LocationRecordCreateView


urlpatterns = [
    # ── Auth & legacy fleet/attendance ────────────────────────────────────────
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

    # ── Generic API (reusable across companies / mobile apps) ─────────────────
    path("me/", me_views.me_view, name="me"),
    path("me/tasks/", me_views.my_tasks_view, name="my_tasks"),

    path("sales/clients/", client_views.client_list_create_view, name="client_list_create"),

    path("product-templates/", product_template_views.product_template_list_view, name="product_template_list"),

    path("project-files/", project_file_views.project_file_list_create_view, name="project_file_list_create"),
    path("project-files/<int:project_file_id>/", project_file_views.project_file_detail_view, name="project_file_detail"),
    path("project-files/<int:project_file_id>/configurations/", configuration_views.configuration_create_view, name="configuration_create"),
    path("project-files/<int:project_file_id>/configurations/<int:config_id>/", configuration_views.configuration_manage_view, name="configuration_manage"),

    path("project-files/<int:project_file_id>/quotes/", quote_views.presupuesto_list_create_view, name="presupuesto_list_create"),
    path("quotes/<int:quote_id>/", quote_views.presupuesto_detail_view, name="presupuesto_detail"),
    path("quotes/<int:quote_id>/configurations/", quote_views.producto_list_create_view, name="producto_list_create"),
    path("quotes/<int:quote_id>/configurations/<int:config_id>/", quote_views.producto_detail_view, name="producto_detail"),

    path("configurations/preview/", configuration_views.configuration_preview_view, name="configuration_preview"),
    path("configurations/<int:config_id>/convert/", configuration_views.configuration_convert_view, name="configuration_convert"),
    path("configurations/<int:config_id>/stages/", production_stage_views.configuration_stages_view, name="configuration_stages"),
    path("configurations/<int:config_id>/stages/<str:stage_name>/assign/", production_stage_views.stage_assign_view, name="stage_assign"),
    path("configurations/<int:config_id>/stages/<str:stage_name>/start/", production_stage_views.stage_start_view, name="stage_start"),
    path("configurations/<int:config_id>/stages/<str:stage_name>/finish/", production_stage_views.stage_finish_view, name="stage_finish"),
    path("configurations/<int:config_id>/stages/<str:stage_name>/revert/", production_stage_views.stage_revert_view, name="stage_revert"),

    path("employees/eligible/", employee_views.eligible_employees_view, name="eligible_employees"),
]
