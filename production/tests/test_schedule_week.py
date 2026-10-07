from datetime import date, datetime

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from company.models import WeekdayChoices
from production.services.schedule_week import WeeklyScheduleContext
from production.tests.factories import (
    STAGE_CUT,
    STAGE_WELD,
    add_schedule,
    make_company,
    make_employee,
    make_machine,
    make_session,
    make_ticket,
    resource_for,
)


class WeeklyScheduleContextTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.ticket = make_ticket(cls.company)
        cls.machine = resource_for(make_machine(cls.company, name="CNC-01"))
        cls.operator = resource_for(make_employee(cls.company, first_name="Ana"))

    def at(self, day, hour, minute=0):
        return timezone.make_aware(datetime(2026, 9, day, hour, minute))

    def build(self, grouping="machine_people", company=None):
        return WeeklyScheduleContext(
            company=company or self.company,
            week_start=date(2026, 9, 14),
            group_by=grouping,
        ).build()

    def test_all_view_shows_only_configured_company_stages(self):
        make_session(
            self.ticket, stage=STAGE_CUT, start=self.at(14, 9),
            end=self.at(14, 10), resources=[self.machine, self.operator],
        )
        make_session(
            self.ticket, stage="legacy", start=self.at(14, 11),
            end=self.at(14, 12), resources=[self.operator],
        )

        week = self.build()

        self.assertEqual([row["label"] for row in week["rows"]], [STAGE_CUT])
        self.assertEqual(week["rows"][0]["cells"][0]["entries"][0]["detail"], "CNC-01, Ana Ruiz")
        self.assertEqual(
            [day["date"].isoformat() for day in week["days"]],
            [f"2026-09-{day}" for day in range(14, 19)],
        )

    def test_groupings_show_the_same_session_with_its_resources(self):
        make_session(
            self.ticket, stage=STAGE_WELD, start=self.at(14, 9),
            end=self.at(14, 10), resources=[self.machine, self.operator],
        )

        machine = self.build("machine")["rows"][0]
        employee = self.build("employee")["rows"][0]
        ticket = self.build("ticket")["rows"][0]

        self.assertEqual(machine["label"], "CNC-01")
        self.assertEqual(machine["cells"][0]["entries"][0]["detail"], "Ana Ruiz")
        self.assertEqual(employee["label"], "Ana Ruiz")
        self.assertEqual(employee["cells"][0]["entries"][0]["detail"], "CNC-01")
        self.assertEqual(ticket["label"], f"OP-{self.ticket.pk}")
        self.assertEqual(ticket["cells"][0]["entries"][0]["label"], STAGE_WELD)

    def test_resource_views_only_show_sessions_on_rows_present_in_the_day_view(self):
        make_session(
            self.ticket, start=self.at(14, 9), end=self.at(14, 10),
            resources=[self.operator],
        )

        self.assertEqual(self.build("machine")["rows"], [])
        self.assertEqual(len(self.build("employee")["rows"]), 1)
        self.assertEqual(len(self.build()["rows"]), 1)
        self.assertEqual(len(self.build("ticket")["rows"]), 1)

    def test_overnight_session_appears_on_both_days_without_extra_hours(self):
        make_session(
            self.ticket, start=self.at(14, 23), end=self.at(15, 1),
            resources=[self.operator],
        )

        cells = self.build()["rows"][0]["cells"]

        self.assertEqual(cells[0]["entries"][0]["time"], "23:00–24:00")
        self.assertEqual(cells[1]["entries"][0]["time"], "00:00–01:00")
        self.assertTrue(all(not cell["entries"] for cell in cells[2:]))

    def test_non_working_days_and_their_sessions_are_hidden(self):
        make_session(
            self.ticket, start=self.at(20, 9), end=self.at(20, 10),
            resources=[self.operator],
        )

        self.assertEqual(self.build()["rows"], [])
        self.assertEqual(self.build("ticket")["rows"], [])
        self.assertEqual(len(self.build()["days"]), 5)

    def test_company_with_unusual_workdays_only_shows_those_days(self):
        company = make_company(name="Tuesday and Saturday")
        add_schedule(
            company,
            weekdays=[WeekdayChoices.TUESDAY, WeekdayChoices.SATURDAY],
        )
        ticket = make_ticket(company)
        make_session(ticket, start=self.at(15, 9), end=self.at(15, 10))
        make_session(ticket, start=self.at(19, 9), end=self.at(19, 10))

        week = self.build(company=company)

        self.assertEqual(
            [day["date"].isoformat() for day in week["days"]],
            ["2026-09-15", "2026-09-19"],
        )
        self.assertEqual(len(week["rows"][0]["cells"]), 2)
        self.assertTrue(all(cell["entries"] for cell in week["rows"][0]["cells"]))

    def test_company_without_schedule_has_no_day_columns(self):
        company = make_company(name="No schedule")

        self.assertEqual(
            self.build(company=company),
            {"days": [], "rows": [], "has_schedule": False},
        )

    def test_week_excludes_other_companies_and_sessions_outside_its_dates(self):
        other = make_company(name="Other company")
        other_ticket = make_ticket(other)
        make_session(
            other_ticket, start=self.at(14, 9), end=self.at(14, 10),
        )
        make_session(
            self.ticket, start=self.at(21, 9), end=self.at(21, 10),
        )

        self.assertEqual(self.build()["rows"], [])

    def test_query_count_does_not_grow_with_session_count(self):
        make_session(
            self.ticket, start=self.at(14, 9), end=self.at(14, 10),
            resources=[self.machine, self.operator],
        )
        with CaptureQueriesContext(connection) as first_queries:
            self.build()
        for hour in (10, 11, 12, 13):
            make_session(
                self.ticket, start=self.at(14, hour), end=self.at(14, hour + 1),
                resources=[self.machine, self.operator],
            )
        with CaptureQueriesContext(connection) as more_queries:
            self.build()

        self.assertEqual(len(more_queries), len(first_queries))
