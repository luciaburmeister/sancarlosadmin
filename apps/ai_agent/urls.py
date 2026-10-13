from django.urls import path

from apps.ai_agent.views import (
    ArtifactDownloadView,
    ConversationListView,
    SaveReportView,
    SendMessageView,
)

app_name = "ai_agent"

urlpatterns = [
    path("send/", SendMessageView.as_view(), name="send_message"),
    path("reports/", SaveReportView.as_view(), name="save_report"),
    path("conversations/", ConversationListView.as_view(), name="conversation_list"),
    path(
        "artifacts/<int:pk>/download/",
        ArtifactDownloadView.as_view(),
        name="download_artifact",
    ),
]
