from django.urls import path

from .views import employees, stages

urlpatterns = [
    path(
        "configurations/<int:config_id>/stages/",
        stages.ConfigurationStagesView.as_view(),
        name="configuration_stages",
    ),
    path(
        "configurations/<int:config_id>/stages/cancel/",
        stages.ConfigurationCancelTicketView.as_view(),
        name="configuration_cancel_ticket",
    ),
    path(
        "configurations/<int:config_id>/items/<int:item_id>/stages/<str:stage_name>/assign/",
        stages.StageAssignItemView.as_view(),
        name="stage_assign_item",
    ),
    path(
        "configurations/<int:config_id>/items/<int:item_id>/stages/<str:stage_name>/start/",
        stages.StageStartItemView.as_view(),
        name="stage_start_item",
    ),
    path(
        "configurations/<int:config_id>/items/<int:item_id>/stages/<str:stage_name>/leave/",
        stages.StageLeaveItemView.as_view(),
        name="stage_leave_item",
    ),
    path(
        "configurations/<int:config_id>/items/<int:item_id>/stages/<str:stage_name>/finish/",
        stages.StageFinishItemView.as_view(),
        name="stage_finish_item",
    ),
    path(
        "configurations/<int:config_id>/items/<int:item_id>/stages/<str:stage_name>/revert/",
        stages.StageRevertItemView.as_view(),
        name="stage_revert_item",
    ),
    path("employees/eligible/", employees.EligibleEmployeesView.as_view(), name="eligible_employees"),
]
