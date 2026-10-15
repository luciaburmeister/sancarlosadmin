"""Mounted under /admin/documents/ (alumniniooerp/urls.py), behind the admin login."""
from django.contrib import admin
from django.urls import path

from core.views.document_status import DocumentDownloadView, DocumentStatusAPIView, DocumentStatusPageView

urlpatterns = [
    path("status/", admin.site.admin_view(DocumentStatusPageView.as_view()), name="core_document_status"),
    path(
        "status/<str:task_id>/api/",
        admin.site.admin_view(DocumentStatusAPIView.as_view()),
        name="core_document_status_api",
    ),
    path(
        "status/<str:task_id>/download/",
        admin.site.admin_view(DocumentDownloadView.as_view()),
        name="core_document_download",
    ),
]
