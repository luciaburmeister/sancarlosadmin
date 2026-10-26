from django.urls import path

from assets.views.asset_transaction_document import (
    AssetTransactionDocumentStatusView,
    AssetTransactionDocumentView,
    AssetTransactionPauseDocumentStatusView,
    AssetTransactionPauseDocumentView,
    AssetTransactionReceiptDocumentStatusView,
    AssetTransactionReceiptDocumentView,
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
    path(
        "albaran-di/<str:token>/",
        AssetTransactionReceiptDocumentView.as_view(),
        name="asset_transaction_receipt_document",
    ),
    path(
        "albaran-di/<str:token>/status/",
        AssetTransactionReceiptDocumentStatusView.as_view(),
        name="asset_transaction_receipt_document_status",
    ),
    path(
        "pausa-di/<str:token>/",
        AssetTransactionPauseDocumentView.as_view(),
        name="asset_transaction_pause_document",
    ),
    path(
        "pausa-di/<str:token>/status/",
        AssetTransactionPauseDocumentStatusView.as_view(),
        name="asset_transaction_pause_document_status",
    ),
]
