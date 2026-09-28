from django.urls import path

from assets.views.asset_transaction_document import asset_transaction_document_view

urlpatterns = [
    path(
        "albaran/<str:token>/",
        asset_transaction_document_view,
        name="asset_transaction_document",
    ),
]
