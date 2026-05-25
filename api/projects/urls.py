from django.urls import path

from .views import configurations, project_files, quote_photos, quotes

urlpatterns = [
    path("project-files/", project_files.ProjectFileListCreateView.as_view(), name="project_file_list_create"),
    path(
        "project-files/<int:project_file_id>/",
        project_files.ProjectFileDetailView.as_view(),
        name="project_file_detail",
    ),
    path(
        "project-files/<int:project_file_id>/configurations/",
        configurations.ConfigurationCreateView.as_view(),
        name="configuration_create",
    ),
    path(
        "project-files/<int:project_file_id>/configurations/<int:config_id>/",
        configurations.ConfigurationManageView.as_view(),
        name="configuration_manage",
    ),
    path(
        "project-files/<int:project_file_id>/quotes/",
        quotes.PresupuestoListCreateView.as_view(),
        name="presupuesto_list_create",
    ),
    path("quotes/<int:quote_id>/", quotes.PresupuestoDetailView.as_view(), name="presupuesto_detail"),
    path(
        "quotes/<int:quote_id>/configurations/",
        quotes.ProductoListCreateView.as_view(),
        name="producto_list_create",
    ),
    path(
        "quotes/<int:quote_id>/configurations/<int:config_id>/",
        quotes.ProductoDetailView.as_view(),
        name="producto_detail",
    ),
    path(
        "quotes/<int:quote_id>/photos/",
        quote_photos.QuotePhotoListCreateView.as_view(),
        name="quote_photo_list_create",
    ),
    path(
        "quotes/<int:quote_id>/photos/<int:photo_id>/",
        quote_photos.QuotePhotoDeleteView.as_view(),
        name="quote_photo_delete",
    ),
    path(
        "photo-categories/",
        quote_photos.PhotoCategoryListCreateView.as_view(),
        name="photo_category_list_create",
    ),
    path(
        "photo-categories/<int:category_id>/",
        quote_photos.PhotoCategoryDetailView.as_view(),
        name="photo_category_detail",
    ),
    path(
        "configurations/preview/",
        configurations.ConfigurationPreviewView.as_view(),
        name="configuration_preview",
    ),
    path(
        "configurations/<int:config_id>/convert/",
        configurations.ConfigurationConvertView.as_view(),
        name="configuration_convert",
    ),
]
