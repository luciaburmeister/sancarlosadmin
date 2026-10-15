from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlsplit

from django.contrib.auth.models import Permission
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from production.models import PlannedProductionSession, ProductionItem, ProductionItemStatus
from production.tests.factories import (
    STAGE_CUT,
    STAGE_PAINT,
    STAGE_WELD,
    add_schedule,
    make_company,
    make_employee,
    make_machine,
    make_session,
    make_staff_user,
    make_ticket,
    resource_for,
)

SCHEDULE_URL = "admin:production_plannedproductionsession_schedule"
ACTION_URL = "admin:production_plannedproductionsession_schedule_action"

MONDAY = datetime(2026, 9, 14)


class StaffUserMixin:
    username_prefix = "user"

    def staff_user(self, *, perms=(), companies=None):
        return make_staff_user(
            username=f"{self.username_prefix}{Permission.objects.count()}",
            companies=[self.company] if companies is None else companies,
            perms=perms,
        )


class ScheduleViewTest(StaffUserMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()

        cls.operator = resource_for(make_employee(cls.company, first_name="Ana"))
        cls.cnc = resource_for(make_machine(cls.company, name="CNC-01"))
        cls.ticket = make_ticket(cls.company)

    def at(self, hour: int) -> datetime:
        return MONDAY.replace(hour=hour, tzinfo=self.tz)

    def login_with_view_permission(self):
        user = self.staff_user(perms=["view_plannedproductionsession"])
        self.client.force_login(user)
        return user

    # --- page rendering ----------------------------------------------------

    def test_daily_machine_people_view_renders_for_a_permitted_user(self):
        self.login_with_view_permission()

        response = self.client.get(reverse(SCHEDULE_URL))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ps-root")
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'name="day"')
        self.assertContains(response, "calendar_month")
        self.assertTrue(response.context["is_machine_people"])
        # The confirmation dialog uses the dedicated calendar overlay.
        self.assertContains(response, 'class="ps-confirm-overlay"')
        self.assertContains(response, 'aria-modal="true"')

    def test_backlog_payload_is_embedded_for_a_user_who_can_plan(self):
        user = self.staff_user(
            perms=[
                "view_plannedproductionsession",
                "add_plannedproductionsession",
            ]
        )
        self.client.force_login(user)

        response = self.client.get(reverse(SCHEDULE_URL))

        self.assertContains(response, "ps-backlog-data")

    def test_pending_card_renders_the_configuration_and_current_stage(self):
        self.login_with_view_permission()
        configuration = self.ticket.source_object
        configuration.name = "Window A"
        configuration.save(update_fields=["name"])
        ProductionItem.objects.create(
            ticket=self.ticket,
            company=self.company,
            planning={
                STAGE_CUT: {"status": ProductionItemStatus.DONE},
                STAGE_WELD: {"status": ProductionItemStatus.PENDING},
                STAGE_PAINT: {"status": ProductionItemStatus.PENDING},
            },
        )

        with translation.override("es"):
            response = self.client.get(reverse(SCHEDULE_URL))

        self.assertContains(response, f"#{configuration.pk} · Window A")
        self.assertContains(response, "Etapa Actual: soldadura")

    def test_each_grouping_mode_renders(self):
        self.login_with_view_permission()

        for grouping in ("machine_people", "machine", "employee", "ticket"):
            with self.subTest(grouping=grouping):
                response = self.client.get(
                    reverse(SCHEDULE_URL), {"group_by": grouping}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["group_by"], grouping)

    def test_old_ticket_link_is_kept_compatible(self):
        self.login_with_view_permission()

        response = self.client.get(reverse(SCHEDULE_URL), {"view": "ticket"})

        self.assertTrue(response.context["is_ticket"])

    def test_an_unknown_grouping_falls_back_to_machine_people(self):
        self.login_with_view_permission()

        response = self.client.get(reverse(SCHEDULE_URL), {"group_by": "nonsense"})

        self.assertTrue(response.context["is_machine_people"])

    def test_day_navigation_keeps_grouping_and_repeated_params_but_drops_legacy_view(self):
        self.login_with_view_permission()

        response = self.client.get(
            reverse(SCHEDULE_URL),
            {
                "day": MONDAY.date().isoformat(),
                "group_by": "machine",
                "view": "ticket",
                "custom": ["first", "second"],
            },
        )

        for url, day in (
            (response.context["previous_day_url"], "2026-09-13"),
            (response.context["next_day_url"], "2026-09-15"),
        ):
            parsed = urlsplit(url)
            params = QueryDict(parsed.query)
            self.assertEqual(parsed.path, reverse(SCHEDULE_URL))
            self.assertEqual(params["day"], day)
            self.assertEqual(params.getlist("custom"), ["first", "second"])
            self.assertEqual(params["group_by"], "machine")
            self.assertNotIn("view", params)

    def test_week_navigation_and_day_drilldown_preserve_grouping(self):
        self.login_with_view_permission()

        response = self.client.get(
            reverse(SCHEDULE_URL),
            {
                "period": "week",
                "day": "2026-09-16",
                "group_by": "machine",
                "custom": ["first", "second"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_week"])
        self.assertEqual(response.context["week_start"].isoformat(), "2026-09-14")
        self.assertEqual(response.context["week_end"].isoformat(), "2026-09-20")
        self.assertNotContains(response, 'id="ps-root"')
        self.assertNotContains(response, 'src="/static/production/schedule.js"')
        for url, expected_day in (
            (response.context["previous_day_url"], "2026-09-09"),
            (response.context["next_day_url"], "2026-09-23"),
        ):
            params = QueryDict(urlsplit(url).query)
            self.assertEqual(params["period"], "week")
            self.assertEqual(params["day"], expected_day)
            self.assertEqual(params.getlist("custom"), ["first", "second"])

        monday_url = response.context["week"]["days"][0]["url"]
        monday_params = QueryDict(urlsplit(monday_url).query)
        self.assertEqual(monday_params["period"], "day")
        self.assertEqual(monday_params["day"], "2026-09-14")
        self.assertEqual(monday_params["group_by"], "machine")
        self.assertContains(response, monday_url.replace("&", "&amp;"))

        day_response = self.client.get(monday_url)
        self.assertEqual(day_response.status_code, 200)
        self.assertFalse(day_response.context["is_week"])
        self.assertContains(day_response, 'id="ps-root"')
        week_params = QueryDict(urlsplit(day_response.context["week_view_url"]).query)
        self.assertEqual(week_params["period"], "week")
        self.assertEqual(week_params["day"], "2026-09-14")
        self.assertEqual(week_params["group_by"], "machine")

    def test_week_all_entry_opens_its_stage_category_in_the_day_view(self):
        self.login_with_view_permission()
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(9),
            end=self.at(10),
            resources=[self.cnc, self.operator],
        )

        week_response = self.client.get(
            reverse(SCHEDULE_URL), {"period": "week", "day": "2026-09-14"}
        )
        row = next(
            row for row in week_response.context["week"]["rows"]
            if row["label"] == STAGE_CUT
        )
        entry_url = row["cells"][0]["url"]
        self.assertEqual(QueryDict(urlsplit(entry_url).query)["expand_stage"], STAGE_CUT)
        self.assertNotIn(
            "expand_stage",
            QueryDict(urlsplit(week_response.context["week"]["days"][0]["url"]).query),
        )

        day_response = self.client.get(entry_url)
        self.assertEqual(day_response.context["initial_group_key"], STAGE_CUT)
        self.assertContains(day_response, 'data-open-group-key="corte"')
        self.assertContains(day_response, 'data-group-key="corte"')
        self.assertNotIn(
            "expand_stage", QueryDict(urlsplit(day_response.context["week_view_url"]).query)
        )

    def test_stage_drilldown_is_ignored_outside_all_or_for_unknown_stages(self):
        self.login_with_view_permission()
        make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(9),
            end=self.at(10),
            resources=[self.cnc, self.operator],
        )

        machine_week = self.client.get(
            reverse(SCHEDULE_URL),
            {"period": "week", "day": "2026-09-14", "group_by": "machine"},
        )
        self.assertNotIn(
            "expand_stage",
            QueryDict(urlsplit(machine_week.context["week"]["rows"][0]["cells"][0]["url"]).query),
        )
        for grouping, stage in (("machine_people", "unknown"), ("machine", STAGE_CUT)):
            with self.subTest(grouping=grouping):
                day_response = self.client.get(
                    reverse(SCHEDULE_URL),
                    {"day": "2026-09-14", "group_by": grouping, "expand_stage": stage},
                )
                self.assertIsNone(day_response.context["initial_group_key"])

    def test_week_has_clickable_days_even_without_sessions(self):
        self.login_with_view_permission()

        with translation.override("es"):
            response = self.client.get(
                reverse(SCHEDULE_URL), {"period": "week", "day": "2026-09-16"}
            )

        self.assertEqual(len(response.context["week"]["days"]), 5)
        self.assertEqual(response.context["week"]["rows"], [])
        self.assertContains(response, "No hay sesiones planificadas para esta semana")
        self.assertContains(response, "Abrir el plan del día", count=5)
        self.assertContains(response, 'colspan="6"')
        self.assertNotContains(response, "day=2026-09-19")
        self.assertNotContains(response, "day=2026-09-20")

    def test_week_without_a_company_schedule_has_no_empty_day_columns(self):
        self.login_with_view_permission()
        self.company.schedule_slots.all().delete()

        with translation.override("en"):
            response = self.client.get(
                reverse(SCHEDULE_URL), {"period": "week", "day": "2026-09-16"}
            )

        self.assertEqual(response.context["week"]["days"], [])
        self.assertContains(response, "This company has no working schedule configured yet.")
        self.assertNotContains(response, "ps-week-table")

    def test_unknown_period_falls_back_to_day_view(self):
        self.login_with_view_permission()

        response = self.client.get(reverse(SCHEDULE_URL), {"period": "month"})

        self.assertFalse(response.context["is_week"])
        self.assertContains(response, 'id="ps-root"')

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse(SCHEDULE_URL))

        self.assertEqual(response.status_code, 302)

    def test_staff_user_without_the_view_permission_is_refused(self):
        user = self.staff_user(perms=[])
        self.client.force_login(user)

        response = self.client.get(reverse(SCHEDULE_URL))

        self.assertEqual(response.status_code, 403)

    def test_a_multi_resource_session_appears_on_the_rendered_board(self):
        self.login_with_view_permission()
        make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.cnc, self.operator],
        )

        response = self.client.get(
            reverse(SCHEDULE_URL), {"day": MONDAY.date().isoformat()}
        )

        self.assertContains(response, "CNC-01")
        self.assertContains(response, "Ana")

    def test_editable_short_sessions_expose_move_grip_and_change_link(self):
        user = self.staff_user(
            perms=["view_plannedproductionsession", "change_plannedproductionsession"]
        )
        self.client.force_login(user)
        session = make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(9).replace(minute=15),
            resources=[self.cnc, self.operator],
        )

        response = self.client.get(
            reverse(SCHEDULE_URL), {"day": MONDAY.date().isoformat()}
        )

        self.assertContains(response, "ps-block--compact")
        self.assertContains(response, "ps-block__move-grip")
        self.assertContains(response, "ps-block__resize--start")
        self.assertContains(response, "ps-block__resize--end")
        self.assertContains(
            response,
            reverse("admin:production_plannedproductionsession_change", args=[session.pk]),
        )

    def test_production_order_cards_hide_the_redundant_ticket_label(self):
        user = self.staff_user(
            perms=["view_plannedproductionsession", "change_plannedproductionsession"]
        )
        self.client.force_login(user)
        session = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(9),
            end=self.at(11),
            resources=[self.operator],
        )

        response = self.client.get(
            reverse(SCHEDULE_URL),
            {"day": MONDAY.date().isoformat(), "group_by": "ticket"},
        )

        self.assertContains(response, f">#{session.pk}</a>")
        self.assertNotContains(response, "ps-block__session-link--stage")
        self.assertContains(
            response,
            reverse("admin:production_plannedproductionsession_change", args=[session.pk]),
        )

    def test_overlapping_sessions_hide_resize_handles(self):
        user = self.staff_user(
            perms=["view_plannedproductionsession", "change_plannedproductionsession"]
        )
        self.client.force_login(user)
        for stage in (STAGE_CUT, STAGE_PAINT):
            make_session(
                self.ticket,
                stage=stage,
                start=self.at(9),
                end=self.at(11),
                resources=[self.operator],
            )

        response = self.client.get(
            reverse(SCHEDULE_URL),
            {"day": MONDAY.date().isoformat(), "group_by": "employee"},
        )

        # Two concurrent sessions split their row into two half-height lanes.
        self.assertContains(response, "height: calc(50.0000% - 4px)", count=2)
        self.assertNotContains(response, "data-resize-edge")

    def test_the_board_query_cost_does_not_grow_with_the_data_over_http(self):
        """The same guarantee as the context-builder's assertNumQueries test,
        exercised through the full request/response cycle. The exact count
        also includes Unfold's admin chrome (session, sidebar, company
        switcher) on top of the view's own fixed budget, so this pins growth
        rather than an absolute number that would be brittle to an Unfold
        upgrade unrelated to this feature."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        self.login_with_view_permission()

        with CaptureQueriesContext(connection) as few:
            response = self.client.get(
                reverse(SCHEDULE_URL), {"day": MONDAY.date().isoformat()}
            )
        self.assertEqual(response.status_code, 200)

        for _ in range(24):
            ticket = make_ticket(self.company)
            make_session(
                ticket,
                start=self.at(8),
                end=self.at(9),
                resources=[self.cnc, self.operator],
            )

        with CaptureQueriesContext(connection) as many:
            response = self.client.get(
                reverse(SCHEDULE_URL), {"day": MONDAY.date().isoformat()}
            )
        self.assertEqual(response.status_code, 200)

        self.assertLessEqual(len(many.captured_queries), len(few.captured_queries) + 2)


class ScheduleActionViewTest(StaffUserMixin, TestCase):
    username_prefix = "actor"

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()
        cls.operator = resource_for(make_employee(cls.company, first_name="Ana"))
        cls.cnc = resource_for(make_machine(cls.company, name="CNC-01"))
        cls.ticket = make_ticket(cls.company)

    def at(self, hour: int) -> datetime:
        return MONDAY.replace(hour=hour, tzinfo=self.tz)

    def post(self, payload):
        return self.client.post(
            reverse(ACTION_URL),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def window_bounds(self):
        return {
            "window_start": self.at(0).isoformat(),
            "window_end": self.at(23).isoformat(),
        }

    # --- happy path ----------------------------------------------------

    def test_move_session_returns_geometry_and_updates_the_row(self):
        user = self.staff_user(perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)
        session = make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )

        response = self.post(
            {
                "action": "move_session",
                "session_id": session.pk,
                "start": self.at(12).isoformat(),
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["sessions"]), 1)
        self.assertIn("left", body["sessions"][0])
        self.assertIn("width", body["sessions"][0])

    def test_resize_session_returns_updated_geometry(self):
        user = self.staff_user(
            perms=["view_plannedproductionsession", "change_plannedproductionsession"]
        )
        self.client.force_login(user)
        session = make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )

        response = self.post(
            {
                "action": "resize_session",
                "session_id": session.pk,
                "end": self.at(13).isoformat(),
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["sessions"][0]["minutes"], 240)
        self.assertIn("left", body["sessions"][0])
        self.assertIn("width", body["sessions"][0])

    def test_reorder_moves_a_stage_and_reflows_the_whole_ticket(self):
        user = self.staff_user(
            perms=["view_plannedproductionsession", "change_plannedproductionsession"]
        )
        self.client.force_login(user)
        first = make_session(
            self.ticket,
            stage=STAGE_CUT,
            start=self.at(8),
            end=self.at(9),
            resources=[self.operator],
            sequence=0,
        )
        second = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.at(9),
            end=self.at(11),
            resources=[self.cnc],
            sequence=1,
        )

        response = self.post(
            {
                "action": "reorder_ticket",
                "ticket_id": self.ticket.pk,
                "session_id": second.pk,
                "before_session_id": first.pk,
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((second.sequence, first.sequence), (0, 1))
        self.assertEqual(second.planned_end, first.planned_start)

    def test_custom_plan_creates_the_submitted_stages(self):
        user = self.staff_user(
            perms=[
                "view_plannedproductionsession",
                "add_plannedproductionsession",
            ]
        )
        self.client.force_login(user)

        response = self.post(
            {
                "action": "plan_ticket_custom",
                "ticket_id": self.ticket.pk,
                "start": self.at(9).isoformat(),
                "stages": [
                    {
                        "stage": stage,
                        "duration_minutes": 30,
                        "resource_ids": [self.operator.pk],
                    }
                    for stage in [STAGE_CUT, "soldadura", STAGE_PAINT]
                ],
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(
            PlannedProductionSession.objects.filter(
                production_ticket=self.ticket
            ).count(),
            3,
        )

    def test_custom_plan_can_create_one_stage(self):
        user = self.staff_user(
            perms=[
                "view_plannedproductionsession",
                "add_plannedproductionsession",
            ]
        )
        self.client.force_login(user)

        response = self.post(
            {
                "action": "plan_ticket_stage_custom",
                "ticket_id": self.ticket.pk,
                "start": self.at(9).isoformat(),
                "stage": {
                    "stage": STAGE_CUT,
                    "duration_minutes": 30,
                    "resource_ids": [self.operator.pk],
                },
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(
            list(
                PlannedProductionSession.objects.filter(
                    production_ticket=self.ticket
                ).values_list("production_stage", flat=True)
            ),
            [STAGE_CUT],
        )

    # --- permissions -----------------------------------------------------

    def test_move_without_change_permission_is_refused(self):
        user = self.staff_user(perms=["view_plannedproductionsession"])
        self.client.force_login(user)
        session = make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )

        response = self.post(
            {
                "action": "move_session",
                "session_id": session.pk,
                "start": self.at(12).isoformat(),
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 403)
        session.refresh_from_db()
        self.assertEqual(timezone.localtime(session.planned_start, self.tz).hour, 9)

    def test_planning_a_ticket_requires_add_permission(self):
        user = self.staff_user(perms=["view_plannedproductionsession"])
        self.client.force_login(user)

        for action in (
            "plan_ticket",
            "plan_ticket_custom",
            "plan_ticket_stage_custom",
        ):
            with self.subTest(action=action):
                response = self.post(
                    {
                        "action": action,
                        "ticket_id": self.ticket.pk,
                        "start": self.at(9).isoformat(),
                        **self.window_bounds(),
                    }
                )

                self.assertEqual(response.status_code, 403)

    def test_unplan_requires_delete_permission(self):
        user = self.staff_user(perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)
        session = make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )

        response = self.post(
            {"action": "unplan_session", "session_id": session.pk}
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(PlannedProductionSession.objects.filter(pk=session.pk).exists())

    def test_unplan_removes_the_planned_session(self):
        user = self.staff_user(
            perms=["view_plannedproductionsession", "delete_plannedproductionsession"]
        )
        self.client.force_login(user)
        session = make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )
        response = self.post({"action": "unplan_session", "session_id": session.pk})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(PlannedProductionSession.objects.filter(pk=session.pk).exists())

    def test_anonymous_post_is_redirected_not_crashed(self):
        response = self.post({"action": "move_session", "session_id": 1, "start": "x"})

        self.assertEqual(response.status_code, 302)

    # --- payload validation -----------------------------------------------

    def test_malformed_json_body_is_a_400(self):
        user = self.staff_user(perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)

        response = self.client.post(
            reverse(ACTION_URL), data=b"not json", content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)

    def test_unknown_action_is_a_400(self):
        user = self.staff_user(perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)

        response = self.post({"action": "teleport_session"})

        self.assertEqual(response.status_code, 400)

    def test_a_malformed_start_is_a_400_not_a_500(self):
        user = self.staff_user(perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)
        session = make_session(
            self.ticket, start=self.at(9), end=self.at(11), resources=[self.operator]
        )

        response = self.post(
            {
                "action": "move_session",
                "session_id": session.pk,
                "start": "not-a-date",
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_session_id_is_a_400_not_a_500(self):
        user = self.staff_user(perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)

        response = self.post({"action": "move_session", "start": self.at(9).isoformat()})

        self.assertEqual(response.status_code, 400)

    # --- conflicts over HTTP -----------------------------------------------

    def test_a_conflicting_move_returns_200_with_requires_confirmation(self):
        user = self.staff_user(perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)
        make_session(
            self.ticket, start=self.at(10), end=self.at(12), resources=[self.operator]
        )
        mover = make_session(
            self.ticket,
            stage=STAGE_PAINT,
            start=self.at(14),
            end=self.at(15),
            resources=[self.operator],
            sequence=1,
        )

        response = self.post(
            {
                "action": "move_session",
                "session_id": mover.pk,
                "start": self.at(10).isoformat(),
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["requires_confirmation"])

    def test_a_locked_session_move_returns_409(self):
        user = self.staff_user(perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)
        session = make_session(
            self.ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.operator],
            locked=True,
        )

        response = self.post(
            {
                "action": "move_session",
                "session_id": session.pk,
                "start": self.at(12).isoformat(),
                **self.window_bounds(),
            }
        )

        self.assertEqual(response.status_code, 409)


class ActionViewCompanyScopingTest(StaffUserMixin, TestCase):
    """A user must not be able to mutate another company's planned sessions
    or tickets through the action endpoint just by knowing their id."""

    username_prefix = "scoping"

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.tz = timezone.get_current_timezone()
        cls.operator = resource_for(make_employee(cls.company, first_name="Ana"))
        cls.ticket = make_ticket(cls.company)

        cls.other_company = make_company(name="Other Co")
        add_schedule(cls.other_company)
        cls.other_operator = resource_for(
            make_employee(cls.other_company, first_name="Bob")
        )
        cls.other_ticket = make_ticket(cls.other_company)

    def at(self, hour: int) -> datetime:
        return MONDAY.replace(hour=hour, tzinfo=self.tz)

    def test_cannot_move_a_session_belonging_to_another_company(self):
        foreign_session = make_session(
            self.other_ticket,
            start=self.at(9),
            end=self.at(11),
            resources=[self.other_operator],
        )
        user = self.staff_user(
            perms=["view_plannedproductionsession", "change_plannedproductionsession"],
            companies=[self.company],
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse(ACTION_URL),
            data=json.dumps(
                {
                    "action": "move_session",
                    "session_id": foreign_session.pk,
                    "start": self.at(13).isoformat(),
                    "window_start": self.at(0).isoformat(),
                    "window_end": self.at(23).isoformat(),
                }
            ),
            content_type="application/json",
        )

        foreign_session.refresh_from_db()
        self.assertEqual(foreign_session.planned_start, self.at(9))
        self.assertEqual(response.status_code, 404)

    def test_cannot_plan_a_ticket_belonging_to_another_company(self):
        user = self.staff_user(
            perms=["view_plannedproductionsession", "add_plannedproductionsession"],
            companies=[self.company],
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse(ACTION_URL),
            data=json.dumps(
                {
                    "action": "plan_ticket",
                    "ticket_id": self.other_ticket.pk,
                    "start": self.at(9).isoformat(),
                    "anchor_resource_id": self.other_operator.pk,
                    "window_start": self.at(0).isoformat(),
                    "window_end": self.at(23).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            PlannedProductionSession.objects.filter(
                production_ticket=self.other_ticket
            ).exists()
        )

    def test_a_same_company_move_still_succeeds(self):
        """The scoping check must not collateral-damage the ordinary case."""
        session = make_session(
            self.ticket, start=self.at(9), end=self.at(10), resources=[self.operator]
        )
        user = self.staff_user(
            perms=["view_plannedproductionsession", "change_plannedproductionsession"],
            companies=[self.company],
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse(ACTION_URL),
            data=json.dumps(
                {
                    "action": "move_session",
                    "session_id": session.pk,
                    "start": self.at(11).isoformat(),
                    "window_start": self.at(0).isoformat(),
                    "window_end": self.at(23).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)["ok"])
        session.refresh_from_db()
        self.assertEqual(session.planned_start, self.at(11))
