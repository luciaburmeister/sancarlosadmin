from django.urls import path

from .views import MeView, MyTasksView

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("me/tasks/", MyTasksView.as_view(), name="my_tasks"),
]
