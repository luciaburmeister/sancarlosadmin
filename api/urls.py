from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from . import views

urlpatterns = [
    path("auth/login/", TokenObtainPairView.as_view(), name="jwt_login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="jwt_refresh"),
    path("assets/", views.assets_view, name="assets"),
    path("assets/<int:asset_id>/", views.asset_by_id_view, name="asset_by_id"),
    path("assets/<int:asset_id>/pickup/", views.pickup_view, name="pickup"),
    path("assets/<int:asset_id>/deliver/", views.deliver_view, name="deliver"),
    path("assets/<int:asset_id>/assign/", views.assign_view, name="assign"),
    path("assets/<int:asset_id>/qr/", views.asset_qr_view, name="asset_qr"),
    path("attendance/clock-in/", views.clock_in_view, name="clock_in"),
    path("attendance/clock-out/", views.clock_out_view, name="clock_out"),
    path("attendance/status/", views.attendance_status_view, name="attendance_status"),
]