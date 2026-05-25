from django.urls import include, path

from .cortizo_views import cortizo_import_view

urlpatterns = [
    path("auth/", include("api.auth.urls")),
    path("", include("api.fleet.urls")),
    path("", include("api.me.urls")),
    path("sales/", include("api.sales.urls")),
    path("", include("api.catalog.urls")),
    path("", include("api.projects.urls")),
    path("", include("api.production.urls")),
    path("", include("api.billing.urls")),
    path("cortizo/import/", cortizo_import_view, name="cortizo_import"),
]
