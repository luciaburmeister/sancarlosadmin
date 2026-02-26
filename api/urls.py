from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.login_view, name="api_login"),
    path("assets/", views.assets_view, name="api_assets"), #Use Django rest framework and viewsets for better API design and functionality
    path("assets/<int:asset_id>/pickup/", views.pickup_view, name="api_pickup"),
    path("assets/<int:asset_id>/deliver/", views.deliver_view, name="api_deliver"),
]