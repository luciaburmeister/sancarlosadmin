from django.urls import path

from assets.views.asset_transaction_document import (
    AssetTransactionDocumentStatusView,
    AssetTransactionDocumentView,
)

urlpatterns = [
    path(
        "albaran/<str:token>/",
        AssetTransactionDocumentView.as_view(),
        name="asset_transaction_document",
    ),
    path(
        "albaran/<str:token>/status/",
        AssetTransactionDocumentStatusView.as_view(),
        name="asset_transaction_document_status",
    ),
]
