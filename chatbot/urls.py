from django.urls import path

from chatbot.views import ChatView, CompaniesView

app_name = "chatbot"

urlpatterns = [
    path("companies/", CompaniesView.as_view(), name="companies"),
    path("chat/", ChatView.as_view(), name="chat"),
]
