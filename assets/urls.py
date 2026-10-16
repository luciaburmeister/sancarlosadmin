from django.urls import path

from assets.views.asset_transaction_document import (
    AssetTransactionDocumentDownloadView,
    AssetTransactionDocumentStatusView,
    asset_transaction_document_view,
)

urlpatterns = [
    path(
        "albaran/<str:token>/",
        asset_transaction_document_view,
        name="asset_transaction_document",
    ),
    path(
        "albaran/<str:token>/status/<str:task_id>/",
        AssetTransactionDocumentStatusView.as_view(),
        name="asset_transaction_document_status",
    ),
    path(
        "albaran/<str:token>/download/<str:task_id>/",
        AssetTransactionDocumentDownloadView.as_view(),
        name="asset_transaction_document_download",
    ),
]
